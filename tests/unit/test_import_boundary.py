"""Static AST import boundary (spec D1 / W2-12).

The product tree is scanned directly, not the wheel: this test fails during
development, while ``test_package_install`` proves the same boundary survives
packaging.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "hermes_kanban_lanes"

BANNED_ANYWHERE = frozenset(
    {
        "asyncio",
        "ftplib",
        "http",
        "multiprocessing",
        "probe_host",
        "probe_p0b",
        "requests",
        "socket",
        "telnetlib",
        "tools",
        "urllib",
    }
)
HOST_ROOTS = frozenset({"hermes", "hermes_agent", "hermes_cli", "sqlite3"})
SUBPROCESS_PACKAGES = ("runtime", "verification", "executors")
# Raw process-spawn primitives are held to the same allowlist as
# ``subprocess``: they are a second executor path when used anywhere else.
SPAWN_PACKAGES = SUBPROCESS_PACKAGES
SPAWN_ATTRIBUTES = frozenset({"posix_spawn", "fork"})
# ``hermes_adapter`` may import only W1 contract modules from the product.
ADAPTER_ALLOWED_PRODUCT_ROOTS = frozenset({"contracts"})
# These packages sit below ``runtime`` and must not import upward.
LEAF_PACKAGES = frozenset({"executors", "verification", "workspace"})


def _sources() -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        trees[relative] = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    return trees


def _package_of(relative: str) -> str | None:
    return relative.split("/", 1)[0] if "/" in relative else None


def _iter_imports(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.level if isinstance(node, ast.ImportFrom) else 0, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.level, node.module or ""


def _is_spawn_name(name: str) -> bool:
    return (
        name in SPAWN_ATTRIBUTES
        or name.startswith("spawn")
        or name.startswith("exec")
    )


def _process_spawn_violations(tree: ast.Module) -> list[str]:
    """Direct ``os`` spawn/exec/fork calls and their import forms.

    Catches ``os.posix_spawn(..)``, ``os.spawnv(..)``, ``os.execve(..)``,
    ``os.fork()``, ``from os import posix_spawn`` and ``import posix`` — the
    variants that would bypass a scan keyed only on the ``subprocess`` import.
    """

    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and _is_spawn_name(node.attr)
        ):
            found.append(f"os.{node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module in ("os", "posix"):
                for alias in node.names:
                    if _is_spawn_name(alias.name):
                        found.append(f"from {node.module} import {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("posix", "nt"):
                    found.append(f"import {alias.name}")
    return found


def test_import_boundary_is_static_and_complete():
    trees = _sources()
    assert trees, "no product sources found"

    for relative, tree in trees.items():
        package = _package_of(relative)
        for level, module in _iter_imports(tree):
            root = module.split(".")[0]
            if level == 0:
                assert root not in BANNED_ANYWHERE, f"{relative}: import {module}"
                if root in HOST_ROOTS:
                    assert package == "hermes_adapter", (
                        f"{relative}: host import {module} outside hermes_adapter"
                    )
                if root == "subprocess":
                    assert package in SUBPROCESS_PACKAGES, (
                        f"{relative}: subprocess outside runtime/verification/executors"
                    )
                continue

            # Relative import: resolve the product-level target.
            if level == 1:
                target_package = package
                target = module
            else:
                target_package = package
                target = module
            if package == "hermes_adapter" and level >= 2:
                assert root in ADAPTER_ALLOWED_PRODUCT_ROOTS, (
                    f"{relative}: hermes_adapter imports product module {module}"
                )
            if package in LEAF_PACKAGES:
                assert root not in ("runtime", "hermes_adapter"), (
                    f"{relative}: {package} imports upward module {module}"
                )
            assert root not in BANNED_ANYWHERE, f"{relative}: import {module}"


def test_process_spawn_primitives_only_inside_allowed_packages():
    for relative, tree in _sources().items():
        package = _package_of(relative)
        for violation in _process_spawn_violations(tree):
            assert package in SPAWN_PACKAGES, (
                f"{relative}: raw process spawn {violation} outside "
                "runtime/verification/executors"
            )


def test_no_hermes_cli_reference_outside_adapter():
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        if relative.startswith("hermes_adapter/"):
            continue
        text = path.read_text(encoding="utf-8")
        assert "hermes_cli" not in text, relative
        assert "probe_p0b" not in text, relative
        assert "probe_host" not in text, relative


def test_adapter_does_not_import_runtime_or_other_subpackages():
    tree = _sources()["hermes_adapter/board.py"]
    for level, module in _iter_imports(tree):
        if level >= 2:
            assert module.split(".")[0] in ADAPTER_ALLOWED_PRODUCT_ROOTS, module
