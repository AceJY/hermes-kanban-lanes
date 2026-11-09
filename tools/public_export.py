#!/usr/bin/env python3
"""Build a clean, public export of this repository.

The export is an explicit allow list: only product sources, public tests,
examples, public documentation and the export tool itself are copied.  Every
file that ends up in the output tree is then scanned; a hit fails the command
closed and prints ``file:line: rule``.  A machine readable manifest records the
relative path, size and SHA-256 of every exported file so a reviewer can
recompute it independently.

The scanner targets *real* leakage shapes: a complete local home path, a
non-reserved e-mail address, a credential-shaped API token, a private key
block, an internal card id, or an ``ANTHROPIC_``/``OPENAI_`` variable assigned
a long credential value.  Reserved (RFC 2606)
e-mail domains such as ``example.invalid`` and human-readable test fixtures are
deliberately not treated as leaks; they cannot be real credentials.

Usage::

    python tools/public_export.py --out /tmp/hkl-public
    python tools/public_export.py --out /tmp/hkl-public --build-wheel
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

MANIFEST_NAME = "PUBLIC-EXPORT-MANIFEST.json"
MANIFEST_SCHEMA_VERSION = 1

#: Files copied verbatim from the repository root.
PUBLIC_FILES = (
    "pyproject.toml",
    "README.md",
    "README.zh-CN.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    ".gitignore",
    "tools/public_export.py",
    "tools/build_wheel.py",
)

#: Directories copied recursively.
PUBLIC_DIRS = (
    "src",
    "tests",
    "examples",
    ".github",
)

#: Never copied, even when they appear below a public directory.
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
        ".worktrees",
    }
)
EXCLUDED_DIR_SUFFIXES = (".egg-info",)
EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo", ".whl")
EXCLUDED_FILE_NAMES = frozenset({".DS_Store"})

#: Individual files under a public directory that are still not published.
#: The host/P0-B probe tools are internal acceptance tooling for a pinned
#: Hermes snapshot, so their regression tests stay out of the public tree too;
#: publishing only the tests would leave them failing on missing probes.
EXCLUDED_RELATIVE_FILES = frozenset(
    {
        "tests/compat/test_host_probe.py",
        "tests/compat/test_p0b_probe.py",
    }
)

# Patterns are assembled from pieces on purpose: this module is itself part of
# the exported tree, so it must not contain a literal hit for its own rules.
_HOME_PATH_FULL = re.compile(r"(?<![\w.])/" + "Users" + r"/[A-Za-z0-9._-]+/")
_HOME_PATH_BARE = re.compile(r"(?<![\w.])/" + "Users" + r"/[A-Za-z0-9._-]+")
_HERMES_HOME = re.compile(r"~/\.hermes\b")
_MACHINE_NAME = re.compile(r"\bAce" + "deMac\b")
_CARD_ID = re.compile(r"\bt_[0-9a-f]{8}\b")
_PRIVATE_KEY = re.compile(r"BEGIN [A-Z ]*PRIVATE" + " KEY")
_GITHUB_TOKEN = re.compile(r"\bgh[op]_")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")
_ANTHROPIC_KEY = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{40,}\b")
_CREDENTIAL_ENV = re.compile(
    r"\b(?:ANTHROPIC|OPENAI)_[A-Z0-9_]*(?:KEY|TOKEN|SECRET)\b"
    r"\s*[:=]\s*[\"'][A-Za-z0-9_-]{40,}[\"']"
)
_EMAIL = re.compile(
    r"(?<![\w.+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})"
)
_RESERVED_EMAIL_DOMAINS = (".invalid", ".test", ".example")
_EMAIL_TOKEN_BOUNDARIES = " \t\"'<>()[`,"

_BASE_RULES = (
    ("hermes-home", _HERMES_HOME),
    ("machine-name", _MACHINE_NAME),
    ("internal-card-id", _CARD_ID),
    ("private-key", _PRIVATE_KEY),
    ("github-token", _GITHUB_TOKEN),
    ("openai-key", _OPENAI_KEY),
    ("anthropic-key", _ANTHROPIC_KEY),
    ("credential-env", _CREDENTIAL_ENV),
)


@dataclass(frozen=True, slots=True)
class Violation:
    path: str
    line: int
    rule: str
    text: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.text}"


def _is_excluded_dir(name: str) -> bool:
    return name in EXCLUDED_DIR_NAMES or name.endswith(EXCLUDED_DIR_SUFFIXES)


def iter_public_files(repo_root: str | Path) -> list[str]:
    """Return the sorted repository-relative files that may be exported."""

    root = Path(repo_root)
    selected: set[str] = set()
    for relative in PUBLIC_FILES:
        if (root / relative).is_file():
            selected.add(relative)
    for directory in PUBLIC_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            parts = path.relative_to(root).parts
            if any(_is_excluded_dir(part) for part in parts[:-1]):
                continue
            if path.name in EXCLUDED_FILE_NAMES:
                continue
            if path.suffix in EXCLUDED_FILE_SUFFIXES:
                continue
            relative = path.relative_to(root).as_posix()
            if relative in EXCLUDED_RELATIVE_FILES:
                continue
            selected.add(relative)
    return sorted(selected)


def export_tree(repo_root: str | Path, out_dir: str | Path) -> list[str]:
    """Copy every public file into ``out_dir``; leave unrelated files alone."""

    root = Path(repo_root)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    exported: list[str] = []
    for relative in iter_public_files(root):
        source = root / relative
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        exported.append(relative)
    return exported


def _rules_for(relative: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Test files may keep a bare ``/Users/<name>`` negative-assertion fixture.

    Everything else (product code, docs, examples, seeded files) is held to the
    stricter bare-home rule, so an accidental full path in a test still fails.
    """

    if relative.startswith("tests/"):
        return (("local-home-path", _HOME_PATH_FULL), *_BASE_RULES)
    return (("local-home-path", _HOME_PATH_BARE), *_BASE_RULES)


def _email_findings(line: str) -> Iterator[tuple[str, str]]:
    for match in _EMAIL.finditer(line):
        _local, domain = match.group(1), match.group(2).lower()
        if domain == "localhost" or domain.endswith(_RESERVED_EMAIL_DOMAINS):
            continue
        start = max(line.rfind(char, 0, match.start()) for char in _EMAIL_TOKEN_BOUNDARIES)
        token = line[start + 1 : match.end()]
        # ``git@host`` (scp) and ``scheme://user@host`` are URLs, not mail.
        if token.startswith("git@"):
            continue
        prefix = line[: match.start() + 1]
        if "://" in prefix.split()[-1]:
            continue
        yield "email-address", match.group(0)


def scan_text(relative: str, text: str) -> list[Violation]:
    found: list[Violation] = []
    rules = _rules_for(relative)
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in rules:
            for match in pattern.finditer(line):
                found.append(Violation(relative, number, rule, match.group(0)))
        for rule, value in _email_findings(line):
            found.append(Violation(relative, number, rule, value))
    return found


def scan_tree(out_dir: str | Path) -> list[Violation]:
    """Scan every regular file below ``out_dir`` (including leftovers)."""

    root = Path(out_dir)
    violations: list[Violation] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            data = path.read_bytes()
        except OSError as exc:
            violations.append(Violation(relative, 1, "unreadable-file", str(exc)))
            continue
        # Decode with replacement rather than skipping: an undecodable file is
        # still exported and still gets a manifest entry, so its bytes must be
        # scanned too.  A leak split by invalid UTF-8 must not slip through.
        violations.extend(scan_text(relative, data.decode("utf-8", "replace")))
    return violations


def unexpected_files(out_dir: str | Path, exported: Iterable[str]) -> list[Violation]:
    """Files in ``out_dir`` that the allow list did not produce.

    A reused ``--out`` must not launder stale content into the manifest: the
    exported set and the manifest set have to be exactly equal.  The manifest
    itself is exempt because it is rewritten after a clean scan.
    """

    root = Path(out_dir)
    expected = set(exported) | {MANIFEST_NAME}
    found: list[Violation] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative not in expected:
            found.append(
                Violation(
                    relative,
                    1,
                    "unexpected-file",
                    "not produced by the public allow list",
                )
            )
    return found


def build_manifest(out_dir: str | Path) -> dict:
    root = Path(out_dir)
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == MANIFEST_NAME:
            continue
        data = path.read_bytes()
        entries.append(
            {
                "path": relative,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generator": "tools/public_export.py",
        "files": entries,
    }


def write_manifest(out_dir: str | Path) -> Path:
    root = Path(out_dir)
    target = root / MANIFEST_NAME
    document = build_manifest(root)
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def run_export(repo_root: str | Path, out_dir: str | Path) -> list[Violation]:
    exported = export_tree(repo_root, out_dir)
    violations = scan_tree(out_dir)
    violations.extend(unexpected_files(out_dir, exported))
    if violations:
        return violations
    write_manifest(out_dir)
    return []


def _build_wheel(out_dir: Path, epoch: int | None) -> int:
    builder = Path(__file__).with_name("build_wheel.py")
    command = [sys.executable, str(builder), "--out", str(out_dir / "wheelhouse")]
    if epoch is not None:
        command.extend(["--epoch", str(epoch)])
    return subprocess.run(command, check=False).returncode


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the public allow-listed tree and fail on leaked identifiers."
    )
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parents[1]),
        help="repository root (default: this checkout)",
    )
    parser.add_argument(
        "--build-wheel",
        action="store_true",
        help="after a clean export, build the reproducible wheel into <out>/wheelhouse",
    )
    parser.add_argument("--epoch", type=int, default=None, help="SOURCE_DATE_EPOCH")
    args = parser.parse_args(list(argv) if argv is not None else None)

    out = Path(args.out)
    violations = run_export(Path(args.repo), out)
    if violations:
        for violation in violations:
            print(violation.render(), file=sys.stderr)
        print(
            f"public-export: {len(violations)} forbidden pattern(s); refusing export",
            file=sys.stderr,
        )
        return 1

    if args.build_wheel:
        code = _build_wheel(out, args.epoch)
        if code != 0:
            return code
    print(str((out / MANIFEST_NAME).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
