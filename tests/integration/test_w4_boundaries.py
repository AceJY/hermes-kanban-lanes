"""W4-13: the W3 live archive still verifies with default parameters.

The archived live run is authoritative evidence for W3.  W4 must not weaken it,
so this test reconstructs the exact W3 repository history (base and code commit
with the identities and timestamps recorded by the archive), extracts the W3
tool source from the W3 landing commit, and calls ``verify_evidence`` with its
default arguments.  The archive itself is hashed before and after to prove the
verification never writes to it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.verification.evidence import verify_evidence

REPO_ROOT = Path(__file__).resolve().parents[2]
W3_LIVE = REPO_ROOT / "artifacts" / "w3" / "raw" / "live"
# The merge commit that landed the W3 lane on ``main``; its ``src`` tree is the
# tool that produced the archived live evidence (digest verified below).
W3_LANDING_COMMIT = "4822cebc4e3ad5af53bfb120f727f9a3910c785c"
BASE_COMMIT = "b654d57fec04805553a83aaa1aa929f529b5c3fd"
CODE_COMMIT = "d52ebf2f52692d41247bf63a5df91854bd9e1468"
# The archived identity contains the W3 machine-specific claim lock.  It is
# kept out of this public test verbatim: the constants below pin the exact
# archived identity by canonical digest and the reconstructed commit message is
# derived from the same archived task id (CODE_COMMIT already pins the result).
ARCHIVED_IDENTITY_SHA256 = (
    "e65fedabf1406d45674b263da44381fd96f58b1305b40b121a483181ed5a81ce"
)
BASE_ENV = {
    "GIT_AUTHOR_NAME": "HKL Tests",
    "GIT_AUTHOR_EMAIL": "hkl@example.invalid",
    "GIT_COMMITTER_NAME": "HKL Tests",
    "GIT_COMMITTER_EMAIL": "hkl@example.invalid",
    "GIT_AUTHOR_DATE": "1794174574 +0800",
    "GIT_COMMITTER_DATE": "1794174574 +0800",
}
CODE_ENV = {
    "GIT_AUTHOR_NAME": "Hermes Kanban Lanes",
    "GIT_AUTHOR_EMAIL": "hkl@localhost",
    "GIT_COMMITTER_NAME": "Hermes Kanban Lanes",
    "GIT_COMMITTER_EMAIL": "hkl@localhost",
    "GIT_AUTHOR_DATE": "1794174579 +0800",
    "GIT_COMMITTER_DATE": "1794174579 +0800",
}


def _git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    environment = {**os.environ, **env} if env is not None else None
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout.strip()


def _tree_digest(root: Path) -> str:
    """Relative-path-sorted digest of every file under ``root``."""

    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            entries.append(
                (
                    path.relative_to(root).as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
    text = "".join(f"{relative}\t{digest}\n" for relative, digest in entries)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _archived_identity(run_dir: Path) -> RunIdentity:
    """Load the archived identity and pin it by canonical digest."""

    document = json.loads(
        (run_dir / "evidence" / "index.json").read_text(encoding="utf-8")
    )
    identity_document = document["identity"]
    canonical = json.dumps(identity_document, sort_keys=True).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == ARCHIVED_IDENTITY_SHA256
    return RunIdentity.from_dict(identity_document)


def _reconstruct_w3_repo(root: Path, task_id: str) -> Path:
    repo = root / "w3-repo"
    repo.mkdir()
    _git("init", "-q", "--initial-branch=main", cwd=repo)
    (repo / "README.txt").write_text("base\n", encoding="utf-8")
    _git("add", "README.txt", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo, env=BASE_ENV)
    assert _git("rev-parse", "HEAD", cwd=repo) == BASE_COMMIT

    (repo / "RESULT.txt").write_text("hkl-live-ok\n", encoding="utf-8")
    _git("add", "RESULT.txt", cwd=repo)
    _git("commit", "-q", "-m", f"hkl: {task_id} run 1", cwd=repo, env=CODE_ENV)
    assert _git("rev-parse", "HEAD", cwd=repo) == CODE_COMMIT
    return repo


def _extract_w3_source(root: Path) -> Path:
    archive = root / "w3-src.tar"
    _git(
        "archive",
        "--format=tar",
        f"--output={archive}",
        W3_LANDING_COMMIT,
        "src/hermes_kanban_lanes",
        cwd=REPO_ROOT,
    )
    unpacked = root / "w3-src"
    unpacked.mkdir()
    with tarfile.open(archive) as handle:
        handle.extractall(unpacked, filter="data")
    return unpacked / "src" / "hermes_kanban_lanes"


def test_w3_live_archive_verifies_with_default_parameters(tmp_path):
    if not W3_LIVE.is_dir():
        pytest.skip("W3 live archive is not present in this checkout")
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{W3_LANDING_COMMIT}^{{commit}}"],
        cwd=str(REPO_ROOT),
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip("W3 landing commit is not available in this checkout")

    before = _tree_digest(W3_LIVE)
    identity = _archived_identity(W3_LIVE)
    repo = _reconstruct_w3_repo(tmp_path, identity.task_id)
    package_dir = _extract_w3_source(tmp_path)

    # Default arguments only: HKL-W3 requirement, no main ref.
    verdict = verify_evidence(W3_LIVE, identity, package_dir, repo, repo)

    assert verdict.ok is True, (verdict.reasons, verdict.checks)
    assert verdict.reasons == ()
    assert verdict.checks == {
        "index": True,
        "identity": True,
        "files": True,
        "gate_report": True,
        "manifest": True,
        "tool_sha256": True,
        "tip": True,
        "ancestry": True,
    }
    assert verdict.index is not None
    assert verdict.index["code_commit"] == CODE_COMMIT
    assert verdict.index["base_commit"] == BASE_COMMIT
    # Verification is read-only: the archived bytes are unchanged.
    assert _tree_digest(W3_LIVE) == before
