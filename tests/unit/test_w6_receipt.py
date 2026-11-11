"""D-2: every evidence path named by the W6 receipt must reach the commit tree.

The receipt is the deliverable's index: a reader of ``git archive HEAD`` (or a
fresh clone) must be able to open every path it names.  A path git ignores can
never be committed, so it is rejected here even while it still exists on disk.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_DIR = REPO_ROOT / "artifacts" / "w6"
RECEIPT_JSON = RECEIPT_DIR / "receipt.json"
RECEIPT_MD = RECEIPT_DIR / "receipt.md"
CONFTEST_PATH = Path(__file__).resolve().parents[1] / "conftest.py"

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


# --------------------------------------------------------------------------
# Regression coverage for the collection guard itself.
#
# The guard used to key off "receipt exists on disk", while these assertions
# key off "git would commit it".  A checkout where ``artifacts/`` exists but is
# ignored (``.git/info/exclude``) fell between the two.  These tests pin the
# guard to git committability without weakening the assertions above.
# --------------------------------------------------------------------------


def _load_tests_conftest():
    spec = importlib.util.spec_from_file_location(
        "_hkl_tests_conftest", CONFTEST_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeItem:
    def __init__(self, name: str) -> None:
        self.name = name
        self.path = Path("tests/unit/test_w6_receipt.py")
        self.markers: list = []

    def add_marker(self, marker) -> None:
        self.markers.append(marker)


def _git_ok(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )


def _make_w6_repo(
    root: Path,
    *,
    ignore_artifacts: bool,
    track_receipt: bool,
    ignore_evidence: bool = False,
) -> None:
    _git_ok(root, "init", "-q")
    _git_ok(root, "config", "user.email", "w6@example.invalid")
    _git_ok(root, "config", "user.name", "W6 Regression")

    receipt_dir = root / "artifacts" / "w6"
    (receipt_dir / "evidence").mkdir(parents=True)
    (receipt_dir / "evidence" / "acceptance.txt").write_text(
        "accepted\n", encoding="utf-8"
    )
    (receipt_dir / "receipt.json").write_text(
        json.dumps({"evidence": "artifacts/w6/evidence/acceptance.txt"}),
        encoding="utf-8",
    )
    (receipt_dir / "receipt.md").write_text(
        "Evidence: evidence/acceptance.txt\n", encoding="utf-8"
    )

    ignored = []
    if ignore_artifacts:
        ignored.append("artifacts/")
    if ignore_evidence:
        # Untracked *and* ignored: git will never commit it.
        ignored.append("artifacts/w6/evidence/")
    if ignored:
        exclude = root / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(ignored) + "\n")

    if track_receipt:
        _git_ok(root, "add", "artifacts/w6/receipt.json")
        _git_ok(root, "commit", "-qm", "track W6 receipt")


def test_guard_skips_when_receipt_exists_but_is_ignored(
    tmp_path, monkeypatch
):
    repo = tmp_path / "ignored"
    repo.mkdir()
    _make_w6_repo(repo, ignore_artifacts=True, track_receipt=False)

    conftest = _load_tests_conftest()
    # The file is on disk (the old guard would have run the assertions) ...
    assert (repo / "artifacts" / "w6" / "receipt.json").is_file()
    # ... but git will never commit it, so the guard must skip.
    assert conftest._w6_receipt_is_committed(repo) is False

    monkeypatch.setattr(conftest, "REPO_ROOT", repo)
    archive_item = _FakeItem("test_receipt_json_paths_exist_in_commit_tree")
    regression_item = _FakeItem(
        "test_guard_skips_when_receipt_exists_but_is_ignored"
    )
    conftest.pytest_collection_modifyitems(
        None, [archive_item, regression_item]
    )

    assert [marker.kwargs["reason"] for marker in archive_item.markers] == [
        "private W6 evidence archive is not committed in this checkout"
    ]
    # The self-contained regression cases are never skipped.
    assert regression_item.markers == []


def test_assertions_still_fail_when_receipt_tracked_but_evidence_untracked(
    tmp_path, monkeypatch
):
    repo = tmp_path / "tracked"
    repo.mkdir()
    _make_w6_repo(
        repo,
        ignore_artifacts=False,
        track_receipt=True,
        ignore_evidence=True,
    )

    conftest = _load_tests_conftest()
    assert conftest._w6_receipt_is_committed(repo) is True

    module = sys.modules[__name__]
    receipt_dir = repo / "artifacts" / "w6"
    monkeypatch.setattr(module, "REPO_ROOT", repo)
    monkeypatch.setattr(module, "RECEIPT_DIR", receipt_dir)
    monkeypatch.setattr(module, "RECEIPT_JSON", receipt_dir / "receipt.json")
    monkeypatch.setattr(module, "RECEIPT_MD", receipt_dir / "receipt.md")

    # The receipt is committable, so the guard runs the assertions; the named
    # evidence file is untracked (and ignored) and must still be rejected.
    with pytest.raises(AssertionError, match="git would never commit"):
        test_receipt_json_paths_exist_in_commit_tree()
    with pytest.raises(AssertionError, match="git would never commit"):
        test_receipt_md_evidence_paths_exist_and_are_not_logs()
