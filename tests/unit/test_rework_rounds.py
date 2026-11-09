"""W4-05/W4-06 (unit): rework round math, caps, stalls and missing bases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_kanban_lanes.runtime.supervisor import (
    previous_run_summaries,
    rework_context,
)
from hermes_kanban_lanes.workspace.git import repo_head, run_git

TASK_ID = "t_rework"


def _git(*args: str, cwd: Path) -> str:
    result = run_git(args, cwd=cwd, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "hkl@example.invalid", cwd=path)
    _git("config", "user.name", "HKL Tests", cwd=path)
    (path / "README.txt").write_text("base\n", encoding="utf-8")
    _git("add", "README.txt", cwd=path)
    _git("commit", "-q", "-m", "base", cwd=path)
    return path


def _run_document(
    run_id: int,
    *,
    code_commit: str | None,
    round_number: int = 1,
    task_base_commit: str | None = None,
    base_commit: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "identity": {"schema_version": 1, "task_id": TASK_ID, "run_id": run_id},
        "round": round_number,
        "task_base_commit": task_base_commit,
        "continuation_base": base_commit,
        "base_commit": base_commit,
        "code_commit": code_commit,
    }


def _write_run(work_root: Path, run_id: int, document: dict) -> None:
    directory = work_root / "runs" / TASK_ID / f"{run_id}-run"
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(
        json.dumps(document, sort_keys=True), encoding="utf-8"
    )


def _context(work_root, repo, *, current_run_id, events, max_rounds=3):
    return rework_context(
        work_root=work_root,
        task_id=TASK_ID,
        current_run_id=current_run_id,
        repository=str(repo),
        events=events,
        max_rounds=max_rounds,
    )


def test_round_one_starts_at_the_current_main_head(tmp_path, repo):
    base = repo_head(repo)
    context = _context(tmp_path, repo, current_run_id=1, events=[])
    assert context == {
        "round": 1,
        "task_base_commit": base,
        "continuation_base": base,
        "error": None,
        "error_kind": None,
    }


def test_rework_round_continues_from_previous_code_commit(tmp_path, repo):
    base = repo_head(repo)
    (repo / "one.txt").write_text("one\n", encoding="utf-8")
    _git("add", "one.txt", cwd=repo)
    _git("commit", "-q", "-m", "run 1", cwd=repo)
    first_code = repo_head(repo)
    _write_run(
        tmp_path,
        1,
        _run_document(1, code_commit=first_code, task_base_commit=base, base_commit=base),
    )
    context = _context(
        tmp_path, repo, current_run_id=2, events=[{"reason": "fix the bug"}]
    )
    assert context["round"] == 2
    assert context["task_base_commit"] == base
    assert context["continuation_base"] == first_code
    assert context["error"] is None


def test_round_three_uses_latest_run_and_keeps_round_one_base(tmp_path, repo):
    base = repo_head(repo)
    first = "1" * 40
    second = "2" * 40
    _write_run(
        tmp_path,
        1,
        _run_document(1, code_commit=first, task_base_commit=base, base_commit=base),
    )
    _write_run(
        tmp_path,
        2,
        _run_document(
            2,
            code_commit=second,
            round_number=2,
            task_base_commit=base,
            base_commit=first,
        ),
    )
    context = _context(
        tmp_path,
        repo,
        current_run_id=3,
        events=[{"reason": "one"}, {"reason": "two"}],
    )
    assert context["round"] == 3
    assert context["task_base_commit"] == base
    assert context["continuation_base"] == second


def test_round_cap_is_checked_before_any_base_lookup(tmp_path, repo):
    context = _context(
        tmp_path,
        repo,
        current_run_id=4,
        events=[{"reason": "a"}, {"reason": "b"}, {"reason": "c"}],
        max_rounds=3,
    )
    assert context["round"] == 4
    assert context["error"] == "rework-cap:3"
    assert context["error_kind"] == "needs_input"


def test_same_reason_and_same_head_is_a_stall(tmp_path, repo):
    base = repo_head(repo)
    same = "3" * 40
    for run_id, round_number in ((1, 1), (2, 2)):
        _write_run(
            tmp_path,
            run_id,
            _run_document(
                run_id,
                code_commit=same,
                round_number=round_number,
                task_base_commit=base,
                base_commit=base,
            ),
        )
    context = _context(
        tmp_path,
        repo,
        current_run_id=3,
        events=[{"reason": "same defect"}, {"reason": "same defect"}],
    )
    assert context["error"] == "rework-stalled-same-reason"
    assert context["error_kind"] == "needs_input"


def test_different_head_or_reason_is_not_a_stall(tmp_path, repo):
    base = repo_head(repo)
    _write_run(
        tmp_path,
        1,
        _run_document(1, code_commit="4" * 40, task_base_commit=base, base_commit=base),
    )
    _write_run(
        tmp_path,
        2,
        _run_document(
            2,
            code_commit="5" * 40,
            round_number=2,
            task_base_commit=base,
            base_commit="4" * 40,
        ),
    )
    same_reason = _context(
        tmp_path,
        repo,
        current_run_id=3,
        events=[{"reason": "same"}, {"reason": "same"}],
    )
    assert same_reason["error"] is None
    assert same_reason["continuation_base"] == "5" * 40

    other_reason = _context(
        tmp_path,
        repo,
        current_run_id=3,
        events=[{"reason": "one"}, {"reason": "two"}],
    )
    assert other_reason["error"] is None


def test_rework_without_a_previous_run_has_no_base(tmp_path, repo):
    context = _context(
        tmp_path, repo, current_run_id=2, events=[{"reason": "fix"}]
    )
    assert context["error"] == "rework-base-missing"
    assert context["error_kind"] == "capability"


def test_previous_run_without_code_commit_has_no_base(tmp_path, repo):
    _write_run(
        tmp_path,
        1,
        _run_document(1, code_commit=None, task_base_commit="a" * 40),
    )
    context = _context(
        tmp_path, repo, current_run_id=2, events=[{"reason": "fix"}]
    )
    assert context["error"] == "rework-base-missing"


def test_round_one_unreadable_repository_is_a_base_failure(tmp_path):
    context = rework_context(
        work_root=tmp_path,
        task_id=TASK_ID,
        current_run_id=1,
        repository=str(tmp_path / "missing"),
        events=[],
        max_rounds=3,
    )
    assert context["error"] == "rework-base-missing"
    assert context["error_kind"] == "capability"


def test_previous_run_summaries_are_ordered_and_exclude_current(tmp_path):
    for run_id in (5, 1, 3, 7):
        _write_run(tmp_path, run_id, _run_document(run_id, code_commit="a" * 40))
    summaries = previous_run_summaries(tmp_path, TASK_ID, current_run_id=5)
    assert [document["identity"]["run_id"] for document in summaries] == [1, 3]
