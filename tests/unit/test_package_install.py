"""Offline wheel build + out-of-repository installation test (spec 07 section 5).

This test deliberately does not import the product package.  It copies only
``pyproject.toml`` and ``src/`` into a temporary build directory, builds a
wheel with the already-installed setuptools backend, installs that wheel into a
brand-new venv with ``--no-index --no-deps``, then runs the installed entry
point from a working directory outside the repository with ``PYTHONPATH``
removed.  A source checkout can therefore never masquerade as a successful
installation.
"""

from __future__ import annotations

import ast
import email.parser
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "hermes_kanban_lanes"
DIST_NAME = "hermes-kanban-lanes"
# Single source of truth: the release version lives in pyproject.toml, so a
# version bump never needs a second edit in this file.
WHEEL_VERSION = tomllib.loads(
    (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]
PUBLIC_EXPORT = REPO_ROOT / "tools" / "public_export.py"
BUILD_WHEEL = REPO_ROOT / "tools" / "build_wheel.py"
EXPORT_MANIFEST = "PUBLIC-EXPORT-MANIFEST.json"
# Banned in every product module (D1).
FORBIDDEN_ANYWHERE = frozenset(
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
# Allowed only inside ``hermes_adapter/`` (compatibility boundary, D1).
HOST_IMPORTS = frozenset({"hermes", "hermes_agent", "hermes_cli", "sqlite3"})
# Allowed only inside these subpackages.
SUBPROCESS_IMPORTS = frozenset({"subprocess"})
SUBPROCESS_PACKAGES = ("runtime/", "verification/", "executors/")
# Raw process-spawn primitives share the same allowlist as ``subprocess``;
# anywhere else they would be a second executor path.
SPAWN_ATTRIBUTES = frozenset({"posix_spawn", "fork"})

# Paths relative to ``hermes_kanban_lanes/``; every source file must be listed.
EXPECTED_MODULES = {
    "__init__.py",
    "card_contract.py",
    "cli.py",
    "config.py",
    "contracts.py",
    "executors/__init__.py",
    "executors/base.py",
    "executors/claude.py",
    "executors/codex.py",
    "executors/dsh.py",
    "hermes_adapter/__init__.py",
    "hermes_adapter/board.py",
    "hermes_adapter/intents.py",
    "plugin.py",
    "runtime/__init__.py",
    "runtime/daemon.py",
    "runtime/host_probe.py",
    "runtime/process.py",
    "runtime/release.py",
    "runtime/retention.py",
    "runtime/service.py",
    "runtime/state.py",
    "runtime/supervisor.py",
    "release/__init__.py",
    "release/authorization.py",
    "release/binding.py",
    "release/gate.py",
    "release/merge.py",
    "release/state.py",
    "verification/__init__.py",
    "verification/commands.py",
    "verification/evidence.py",
    "verification/gate.py",
    "workspace/__init__.py",
    "workspace/git.py",
    "workspace/landing.py",
}


def _is_spawn_name(name: str) -> bool:
    return (
        name in SPAWN_ATTRIBUTES
        or name.startswith("spawn")
        or name.startswith("exec")
    )


def _process_spawn_violations(tree: ast.Module) -> list[str]:
    """``os.posix_spawn``/``os.spawn*``/``os.exec*``/``os.fork`` (and imports)."""

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


def _forbidden_roots(relative_module: str) -> frozenset[str]:
    forbidden = set(FORBIDDEN_ANYWHERE)
    if not relative_module.startswith("hermes_adapter/"):
        forbidden |= HOST_IMPORTS
    if not relative_module.startswith(SUBPROCESS_PACKAGES):
        forbidden |= SUBPROCESS_IMPORTS
    return frozenset(forbidden)
VALID_CONFIG = {
    "schema_version": 1,
    "hermes_source": "/srv/hermes/source",
    "hermes_commit": "a" * 40,
    "boards": {"engineering": "/srv/hermes/kanban/engineering.db"},
    "repositories": {
        "primary": {"path": "/srv/hermes/repos/primary", "trusted": True}
    },
    "executors": {"claude": {"argv": ["claude"]}},
    "work_root": "/srv/hermes/work",
    "max_concurrent": 2,
}
VALID_CARD = {
    "schema_version": 1,
    "card_kind": "implementation",
    "lineage": "lineage-1",
    "closure_for": None,
    "executor": "claude",
    "repository": "primary",
    "allow": ["src/"],
    "commands": [{"argv": ["pytest", "-q"], "cwd": "."}],
    "risk": "normal",
}


def _clean_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    return env


def _run(command, *, cwd, env=None):
    return subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd),
        env=env or _clean_env(),
        capture_output=True,
        text=True,
    )


def _scripts_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


@pytest.fixture(scope="module")
def built_install(tmp_path_factory):
    pytest.importorskip(
        "setuptools",
        reason="building a wheel needs the setuptools build backend",
    )
    work = tmp_path_factory.mktemp("hkl-w1-install")
    source = work / "source"
    source.mkdir()
    shutil.copy2(REPO_ROOT / "pyproject.toml", source / "pyproject.toml")
    for name in (
        "README.md",
        "README.zh-CN.md",
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
    ):
        document = REPO_ROOT / name
        if document.is_file():
            shutil.copy2(document, source / name)
    shutil.copytree(
        REPO_ROOT / "src",
        source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )

    wheel_dir = work / "wheel"
    wheel_dir.mkdir()
    build = _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            ".",
            "-w",
            wheel_dir,
        ],
        cwd=source,
    )
    assert build.returncode == 0, f"wheel build failed:\n{build.stdout}\n{build.stderr}"

    wheels = sorted(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    wheel = wheels[0]
    assert wheel.name == f"{PACKAGE_NAME}-{WHEEL_VERSION}-py3-none-any.whl"

    outside = work / "outside"
    outside.mkdir()

    venv_dir = work / "venv"
    created = _run([sys.executable, "-m", "venv", venv_dir], cwd=work)
    assert created.returncode == 0, f"venv creation failed:\n{created.stderr}"

    venv_python = _scripts_dir(venv_dir) / "python"
    installed = _run(
        [
            venv_python,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            wheel,
        ],
        cwd=outside,
    )
    assert installed.returncode == 0, (
        f"offline wheel install failed:\n{installed.stdout}\n{installed.stderr}"
    )

    return {
        "work": work,
        "wheel": wheel,
        "venv_dir": venv_dir,
        "venv_python": venv_python,
        "entry_point": _scripts_dir(venv_dir) / "hermes-kanban-lanes",
        "outside": outside,
    }


def _wheel_members(wheel: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        return {name: archive.read(name) for name in names}


def test_wheel_contains_only_product_package_and_metadata(built_install):
    members = _wheel_members(built_install["wheel"])
    dist_info = f"{PACKAGE_NAME}-{WHEEL_VERSION}.dist-info/"
    allowed = (f"{PACKAGE_NAME}/", dist_info)

    for name in members:
        assert not name.startswith("/"), name
        assert "/../" not in name, name
        assert any(name.startswith(prefix) for prefix in allowed), name
        for forbidden in ("docs/", "tests/", "tools/", "artifacts/", "examples/"):
            assert not name.startswith(forbidden), name
        assert "__pycache__" not in name, name
        assert not name.endswith((".pyc", ".pyo")), name

    package_modules = {
        name[len(PACKAGE_NAME) + 1 :]
        for name in members
        if name.startswith(f"{PACKAGE_NAME}/") and name.endswith(".py")
    }
    assert package_modules == EXPECTED_MODULES
    assert f"{dist_info}entry_points.txt" in members
    entry_points = members[f"{dist_info}entry_points.txt"].decode("utf-8")
    assert "hermes-kanban-lanes = hermes_kanban_lanes.cli:main" in entry_points


def test_wheel_members_match_source_tree(built_install):
    """W3-11: every wheel member is byte-identical to its source blob."""

    members = _wheel_members(built_install["wheel"])
    source_root = REPO_ROOT / "src" / PACKAGE_NAME
    for relative in sorted(EXPECTED_MODULES):
        name = f"{PACKAGE_NAME}/{relative}"
        assert name in members, name
        assert members[name] == (source_root / relative).read_bytes(), name


def _is_git_checkout() -> bool:
    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(REPO_ROOT),
        capture_output=True,
    )
    return probe.returncode == 0


def _git_output(*args: str, env: dict[str, str] | None = None) -> bytes:
    environment = {**os.environ, **env} if env is not None else None
    result = subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
    )
    assert result.returncode == 0, (args, result.stderr.decode("utf-8", "replace"))
    return result.stdout


def test_wheel_members_match_landing_head_blob(built_install, tmp_path):
    """W4-13: every wheel member equals the blob the landing commit carries.

    The lane delivers a worktree diff that the landing step commits before the
    gate runs.  When the worktree is already clean this compares against
    ``HEAD`` directly.  Otherwise it stages exactly that delivery into a
    throw-away index and compares against the resulting tree, which is the
    blob content ``HEAD`` will have after the landing commit; the gate then
    re-runs this test against the real ``HEAD``.
    """

    if not _is_git_checkout():
        pytest.skip("not a git checkout")

    members = _wheel_members(built_install["wheel"])
    status = _git_output(
        "status", "--porcelain", "--untracked-files=all", "--", "src",
        "pyproject.toml",
    )
    if status.strip():
        env = {"GIT_INDEX_FILE": str(tmp_path / "landing-index")}
        _git_output("add", "-A", "--", "src", "pyproject.toml", env=env)
        revision = _git_output("write-tree", env=env).decode("ascii").strip()
    else:
        revision = "HEAD"

    for relative in sorted(EXPECTED_MODULES):
        name = f"{PACKAGE_NAME}/{relative}"
        blob = _git_output(
            "cat-file", "blob", f"{revision}:src/{PACKAGE_NAME}/{relative}"
        )
        assert members[name] == blob, name


def test_wheel_sources_have_no_forbidden_imports_or_workspace_paths(built_install):
    members = _wheel_members(built_install["wheel"])
    repo_bytes = str(REPO_ROOT).encode("utf-8")

    for name, data in members.items():
        assert repo_bytes not in data, name
        assert b"/Users" + b"/" not in data, name

    sources = {
        name: data.decode("utf-8")
        for name, data in members.items()
        if name.startswith(f"{PACKAGE_NAME}/") and name.endswith(".py")
    }
    assert sources, "no package sources found in the wheel"

    for name, text in sources.items():
        relative = name[len(PACKAGE_NAME) + 1 :]
        forbidden = _forbidden_roots(relative)
        tree = ast.parse(text, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in forbidden, f"{name}: import {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                root = (node.module or "").split(".")[0]
                assert root not in forbidden, f"{name}: from {node.module}"
        for violation in _process_spawn_violations(tree):
            assert relative.startswith(SUBPROCESS_PACKAGES), (
                f"{name}: raw process spawn {violation} outside "
                "runtime/verification/executors"
            )


def test_installed_package_resolves_outside_the_source_tree(built_install):
    venv_dir = built_install["venv_dir"]
    code = (
        "import json, sys\n"
        "import hermes_kanban_lanes as package\n"
        "print(json.dumps({'file': package.__file__, 'version': package.__version__, "
        "'prefix': sys.prefix}))\n"
    )
    probe = _run(
        [built_install["venv_python"], "-c", code],
        cwd=built_install["outside"],
    )
    assert probe.returncode == 0, probe.stderr
    payload = json.loads(probe.stdout)
    package_file = Path(payload["file"]).resolve()
    assert payload["version"] == WHEEL_VERSION
    assert Path(payload["prefix"]).resolve() == venv_dir.resolve()
    assert package_file.is_relative_to(venv_dir.resolve())
    assert not package_file.is_relative_to(REPO_ROOT.resolve())
    assert REPO_ROOT.resolve() / "src" not in package_file.parents


def test_daemon_module_help_runs_from_outside(built_install):
    result = _run(
        [
            built_install["venv_python"],
            "-m",
            "hermes_kanban_lanes.runtime.daemon",
            "--help",
        ],
        cwd=built_install["outside"],
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "--config" in result.stdout


def test_entry_point_version_runs_from_outside(built_install):
    entry = built_install["entry_point"]
    assert entry.is_file()
    result = _run([entry, "--version"], cwd=built_install["outside"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == WHEEL_VERSION
    assert result.stderr == ""


def test_entry_point_validates_legal_inputs_outside(built_install):
    outside = built_install["outside"]
    inputs = outside / "inputs"
    inputs.mkdir()
    missing = outside / "missing-tree"
    config_document = {
        **VALID_CONFIG,
        "hermes_source": str(missing / "source"),
        "boards": {"engineering": str(missing / "kanban" / "engineering.db")},
        "repositories": {
            "primary": {"path": str(missing / "repo"), "trusted": True}
        },
        "work_root": str(missing / "work"),
    }
    config_path = inputs / "config.json"
    card_path = inputs / "card.json"
    config_path.write_text(json.dumps(config_document), encoding="utf-8")
    card_path.write_text(json.dumps(VALID_CARD), encoding="utf-8")

    entry = built_install["entry_point"]

    version = _run([entry, "--version"], cwd=outside)
    assert version.returncode == 0

    config = _run([entry, "validate-config", config_path], cwd=outside)
    assert config.returncode == 0, config.stderr
    assert json.loads(config.stdout) == {
        "valid": True,
        "schema_version": 1,
        "kind": "config",
    }
    assert config.stderr == ""

    card = _run(
        [entry, "validate-card", card_path, "--config", config_path], cwd=outside
    )
    assert card.returncode == 0, card.stderr
    assert json.loads(card.stdout) == {
        "valid": True,
        "schema_version": 1,
        "kind": "card",
    }
    assert card.stderr == ""

    # Validation must not create the configured (non-existent) paths.
    assert not missing.exists()
    before = sorted(str(path.relative_to(outside)) for path in outside.rglob("*"))
    rerun = _run([entry, "validate-config", config_path], cwd=outside)
    after = sorted(str(path.relative_to(outside)) for path in outside.rglob("*"))
    assert rerun.returncode == 0
    assert before == after


def test_entry_point_rejects_illegal_inputs_outside(built_install):
    outside = built_install["outside"]
    inputs = outside / "rejects"
    inputs.mkdir()
    config_path = inputs / "config.json"
    config_path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")

    bad_config = dict(VALID_CONFIG)
    bad_config["max_concurrent"] = True
    bad_config_path = inputs / "bad-config.json"
    bad_config_path.write_text(json.dumps(bad_config), encoding="utf-8")

    bad_card = dict(VALID_CARD)
    bad_card["repository"] = "secondary"
    bad_card_path = inputs / "bad-card.json"
    bad_card_path.write_text(json.dumps(bad_card), encoding="utf-8")

    entry = built_install["entry_point"]

    no_command = _run([entry], cwd=outside)
    assert no_command.returncode == 2

    config = _run([entry, "validate-config", bad_config_path], cwd=outside)
    assert config.returncode == 2
    payload = json.loads(config.stderr)
    assert payload["valid"] is False
    assert "max_concurrent" in payload["error"]
    assert "Traceback" not in config.stderr

    card = _run(
        [entry, "validate-card", bad_card_path, "--config", config_path],
        cwd=outside,
    )
    assert card.returncode == 2
    payload = json.loads(card.stderr)
    assert payload["valid"] is False
    assert "repository" in payload["error"]
    assert "Traceback" not in card.stderr


# ---------------------------------------------------------------------------
# Public export and reproducible release artifacts
# ---------------------------------------------------------------------------


def _run_export(out_dir: Path):
    return _run(
        [
            sys.executable,
            str(PUBLIC_EXPORT),
            "--out",
            str(out_dir),
            "--repo",
            str(REPO_ROOT),
        ],
        cwd=REPO_ROOT,
    )


def test_public_export_manifest(tmp_path):
    """Only allow-listed files are exported and every digest recomputes."""

    out = tmp_path / "public"
    result = _run_export(out)
    assert result.returncode == 0, result.stdout + result.stderr

    manifest_path = out / EXPORT_MANIFEST
    assert manifest_path.is_file(), "manifest missing"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = {entry["path"]: entry for entry in document["files"]}
    files = sorted(
        path.relative_to(out).as_posix()
        for path in out.rglob("*")
        if path.is_file() and path.relative_to(out).as_posix() != EXPORT_MANIFEST
    )
    assert sorted(entries) == files
    for relative in files:
        data = (out / relative).read_bytes()
        assert entries[relative]["size"] == len(data), relative
        assert entries[relative]["sha256"] == hashlib.sha256(data).hexdigest(), relative

    for prefix in ("docs/", "artifacts/", ".worktrees/", "build/", "dist/"):
        assert not any(relative.startswith(prefix) for relative in files), prefix
    assert not any(
        ".egg-info" in relative or "__pycache__" in relative for relative in files
    )
    # The allow list, not the repository listing, decides what is published.
    assert "pyproject.toml" in files
    assert "tests/conftest.py" in files
    assert "tools/public_export.py" in files
    # The export lives alone: consumers it references have to ship with it.
    assert "tools/build_wheel.py" in files
    assert "tools/probe_host.py" not in files
    assert "tools/probe_p0b.py" not in files
    # ...and tests that import those internal-only probes stay out as well.
    assert "tests/compat/test_host_probe.py" not in files
    assert "tests/compat/test_p0b_probe.py" not in files
    assert "AGENTS.md" not in files


@pytest.mark.parametrize(
    "leaked",
    [
        "/" + "Users" + "/example/secret",
        "gh" + "o_" + "a" * 32,
    ],
)
def test_public_export_rejects_forbidden_patterns(tmp_path, leaked):
    """A seeded leak fails the export closed and names file:line."""

    out = tmp_path / "public"
    initial = _run_export(out)
    assert initial.returncode == 0, initial.stdout + initial.stderr

    seeded = out / "SEEDED-LEAK.txt"
    seeded.write_text("public line\n" + leaked + "\n", encoding="utf-8")
    rejected = _run_export(out)
    assert rejected.returncode != 0
    combined = rejected.stdout + rejected.stderr
    assert "SEEDED-LEAK.txt:2" in combined, combined

    seeded.unlink()
    restored = _run_export(out)
    assert restored.returncode == 0, restored.stdout + restored.stderr


def test_public_export_rejects_leftover_files(tmp_path):
    """A reused --out must not carry stale files into the manifest."""

    out = tmp_path / "public"
    initial = _run_export(out)
    assert initial.returncode == 0, initial.stdout + initial.stderr

    (out / "docs").mkdir()
    (out / "docs" / "old.md").write_text("stale documentation\n", encoding="utf-8")
    (out / "REMOVED-FROM-ALLOWLIST.txt").write_text("stale\n", encoding="utf-8")

    rejected = _run_export(out)
    assert rejected.returncode != 0
    combined = rejected.stdout + rejected.stderr
    assert "docs/old.md:1" in combined, combined
    assert "REMOVED-FROM-ALLOWLIST.txt:1" in combined, combined
    assert "unexpected-file" in combined, combined


def test_public_export_scans_non_utf8_files(tmp_path):
    """An undecodable file is still exported, so its leak must still fail."""

    out = tmp_path / "public"
    initial = _run_export(out)
    assert initial.returncode == 0, initial.stdout + initial.stderr

    (out / "BINARY-LEAK.bin").write_bytes(
        b"bin\xff\xfe\n/" + b"Users" + b"/example/secret\n"
    )

    rejected = _run_export(out)
    assert rejected.returncode != 0
    combined = rejected.stdout + rejected.stderr
    assert "BINARY-LEAK.bin:2" in combined, combined
    assert "local-home-path" in combined, combined


def test_wheel_is_byte_reproducible(tmp_path):
    """Two builds with a fixed SOURCE_DATE_EPOCH are byte-identical."""

    pytest.importorskip(
        "setuptools",
        reason="building a wheel needs the setuptools build backend",
    )
    out = tmp_path / "dist"
    result = _run(
        [
            sys.executable,
            str(BUILD_WHEEL),
            "--out",
            str(out),
            "--repo",
            str(REPO_ROOT),
            "--epoch",
            "0",
            "--runs",
            "2",
        ],
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["reproducible"] is True
    assert report["epoch"] == 0
    assert len(report["runs"]) == 2
    first, second = report["runs"]
    assert first["sha256"] == second["sha256"]
    assert first["bytes"] == second["bytes"]

    wheel = Path(report["wheel"])
    assert wheel.is_file()
    data = wheel.read_bytes()
    assert len(data) == first["bytes"]
    assert hashlib.sha256(data).hexdigest() == first["sha256"]


def test_metadata_public_fields(built_install):
    """The built METADATA carries the public distribution metadata."""

    members = _wheel_members(built_install["wheel"])
    dist_info = f"{PACKAGE_NAME}-{WHEEL_VERSION}.dist-info/"
    metadata_bytes = members[f"{dist_info}METADATA"]
    message = email.parser.Parser().parsestr(metadata_bytes.decode("utf-8"))

    assert message["Version"] == WHEEL_VERSION
    assert message["License"] == "MIT"
    assert message["Author"] == "Acelogic"
    assert (message["Description-Content-Type"] or "").startswith("text/markdown")

    requires = message.get_all("Requires-Dist") or []
    assert any(
        item.lower().startswith("pyyaml") and ">=6" in item for item in requires
    ), requires

    urls = "\n".join(message.get_all("Project-URL") or [])
    assert "Homepage" in urls
    assert "Repository" in urls
    assert "https://github.com/AceJY/hermes-kanban-lanes" in urls

    classifiers = message.get_all("Classifier") or []
    assert "Operating System :: OS Independent" in classifiers
    assert "Programming Language :: Python :: 3.13" in classifiers
    assert "License :: OSI Approved :: MIT License" in classifiers

    description = message.get_payload()
    assert "Hermes Kanban Lanes" in description
    for internal in ("W2", "W6", "W8"):
        assert internal not in description
