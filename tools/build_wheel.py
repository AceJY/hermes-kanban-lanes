#!/usr/bin/env python3
"""Build the wheel reproducibly and prove that two builds are byte-identical.

The build always runs from a private copy of the project with a fixed
``SOURCE_DATE_EPOCH``, ``PYTHONHASHSEED`` and ``TZ``.  A clean checkout is
never modified: the temporary copy, its ``build/`` directory and its
``*.egg-info`` are removed with the temporary directory.

Usage::

    python tools/build_wheel.py --out dist --epoch 0
    python tools/build_wheel.py --out dist --runs 2

Exit status is 0 only when every build produced the same bytes (same length and
same SHA-256).  The first wheel is copied into ``--out``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

PROJECT_FILES = (
    "pyproject.toml",
    "README.md",
    "README.zh-CN.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
)
PROJECT_DIRS = ("src",)
IGNORED = ("__pycache__", "*.pyc", "*.pyo", "*.egg-info", "build", "dist")


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_epoch(repo_root: Path, environ: dict[str, str] | None = None) -> int:
    """``SOURCE_DATE_EPOCH`` when set, else the HEAD commit time, else 0."""

    source = os.environ if environ is None else environ
    configured = source.get("SOURCE_DATE_EPOCH")
    if configured:
        return int(configured)
    result = subprocess.run(
        ["git", "-C", str(repo_root), "log", "-1", "--format=%ct"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return int(result.stdout.strip())
    return 0


def _stage_project(repo_root: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    for name in PROJECT_FILES:
        source = repo_root / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    for name in PROJECT_DIRS:
        source = repo_root / name
        if source.is_dir():
            shutil.copytree(
                source, destination / name, ignore=shutil.ignore_patterns(*IGNORED)
            )
    return destination


def _build_once(source: Path, out_dir: Path, epoch: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "SOURCE_DATE_EPOCH": str(epoch),
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(source),
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--wheel-dir",
            str(out_dir),
        ],
        cwd=str(source),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "wheel build failed:\n" + (result.stdout or "") + (result.stderr or "")
        )
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one wheel in {out_dir}, got {wheels}")
    return wheels[0]


def build_reproducible(
    *,
    repo_root: str | Path | None = None,
    out_dir: str | Path | None = None,
    epoch: int | None = None,
    runs: int = 2,
) -> dict:
    root = Path(repo_root) if repo_root is not None else default_repo_root()
    destination = Path(out_dir) if out_dir is not None else root / "dist"
    if runs < 2:
        raise ValueError("--runs must be at least 2")
    chosen_epoch = default_epoch(root) if epoch is None else int(epoch)

    with tempfile.TemporaryDirectory(prefix="hkl-wheel-") as work:
        source = _stage_project(root, Path(work) / "source")
        records: list[dict] = []
        data: list[bytes] = []
        for index in range(runs):
            wheel = _build_once(source, Path(work) / f"run-{index}", chosen_epoch)
            payload = wheel.read_bytes()
            data.append(payload)
            records.append(
                {
                    "name": wheel.name,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )

    reproducible = len({record["sha256"] for record in records}) == 1 and len(
        {record["bytes"] for record in records}
    ) == 1
    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.glob(records[0]["name"]):
        stale.unlink()
    wheel_path = destination / records[0]["name"]
    wheel_path.write_bytes(data[0])
    return {
        "ok": reproducible,
        "epoch": chosen_epoch,
        "runs": records,
        "reproducible": reproducible,
        "wheel": str(wheel_path.resolve()),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the wheel twice and compare the bytes."
    )
    parser.add_argument("--out", default=None, help="directory for the wheel")
    parser.add_argument("--repo", default=None, help="repository root")
    parser.add_argument("--epoch", type=int, default=None, help="SOURCE_DATE_EPOCH")
    parser.add_argument("--runs", type=int, default=2, help="number of builds")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        report = build_reproducible(
            repo_root=args.repo,
            out_dir=args.out,
            epoch=args.epoch,
            runs=args.runs,
        )
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["reproducible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
