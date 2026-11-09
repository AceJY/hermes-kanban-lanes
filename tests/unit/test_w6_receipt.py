"""D-2: every evidence path named by the W6 receipt must reach the commit tree.

The receipt is the deliverable's index: a reader of ``git archive HEAD`` (or a
fresh clone) must be able to open every path it names.  A path git ignores can
never be committed, so it is rejected here even while it still exists on disk.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_DIR = REPO_ROOT / "artifacts" / "w6"
RECEIPT_JSON = RECEIPT_DIR / "receipt.json"
RECEIPT_MD = RECEIPT_DIR / "receipt.md"

_JSON_PATH = re.compile(r"artifacts/w6/[A-Za-z0-9._/-]+")
_MD_EVIDENCE_PATH = re.compile(r"evidence/[A-Za-z0-9._/-]+")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def _document_paths(value, found: set[str]) -> set[str]:
    if isinstance(value, dict):
        for item in value.values():
            _document_paths(item, found)
    elif isinstance(value, list):
        for item in value:
            _document_paths(item, found)
    elif isinstance(value, str):
        found.update(_JSON_PATH.findall(value))
    return found


def _receipt_json_paths() -> list[str]:
    document = json.loads(RECEIPT_JSON.read_text(encoding="utf-8"))
    return sorted(_document_paths(document, set()))


def _would_be_committed(paths: list[str]) -> set[str]:
    """Paths git would include in the next commit (tracked or addable)."""

    if not paths:
        return set()
    tracked = _git("ls-files", "--", *paths).stdout.splitlines()
    addable = _git(
        "ls-files", "--others", "--exclude-standard", "--", *paths
    ).stdout.splitlines()
    return {line.strip() for line in tracked + addable if line.strip()}


def test_receipt_json_paths_exist_in_commit_tree():
    paths = _receipt_json_paths()
    assert paths, "receipt.json names no artifacts/w6 path"

    assert [path for path in paths if path.endswith(".log")] == []
    missing = [path for path in paths if not (REPO_ROOT / path).is_file()]
    assert missing == []

    uncommittable = sorted(set(paths) - _would_be_committed(paths))
    assert uncommittable == [], (
        "receipt paths git would never commit (ignored or outside the tree): %s"
        % uncommittable
    )


def test_receipt_md_evidence_paths_exist_and_are_not_logs():
    references = sorted(
        set(_MD_EVIDENCE_PATH.findall(RECEIPT_MD.read_text(encoding="utf-8")))
    )
    assert references, "receipt.md names no evidence path"

    assert [path for path in references if path.endswith(".log")] == []
    missing = [
        path for path in references if not (RECEIPT_DIR / path).is_file()
    ]
    assert missing == []

    repository_paths = [
        str(Path("artifacts/w6") / path) for path in references
    ]
    uncommittable = sorted(
        set(repository_paths) - _would_be_committed(repository_paths)
    )
    assert uncommittable == [], (
        "receipt.md evidence git would never commit: %s" % uncommittable
    )
