"""W4-07/W4-09/W4-10: release refusals, merge failures and post-merge regression.

Every refusal path is driven through the real release entry point
(``release_once``) on a temporary board and repository.  The gate must write
``release-report.json`` even when it refuses, and a refusal must never move
``main``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.workspace.git import repo_head, run_git

from w4_helpers import (
    PYTHON,
    claim_and_release,
    dev_run_dir,
    git,
    queue_merge_card,
    release_run_dir,
    review_body,
    review_pass,
    run_dev,
    task_row,
    w4_config,
    write_authorization,
)


def _chain(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    *,
    merge_enabled: bool = True,
    post_merge_commands: list[list[str]] | None = None,
    authorization: bool = True,
    authorization_overrides: dict | None = None,
    max_rework_rounds: int = 3,
) -> dict:
    repo = make_repo()
    base = repo_head(repo)
    executor = fake_executor(review_body())
    commands = (
        [[PYTHON, "-c", "pass"]]
        if post_merge_commands is None
        else post_merge_commands
    )
    config = w4_config(
        make_config,
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[executor],
        merge_enabled=merge_enabled,
        post_merge_commands=commands,
        max_rework_rounds=max_rework_rounds,
    )
    task_id = make_task(title="w4 release failure")
    card = load_card(
        write_card(config.work_root, task_id, allow=("RESULT.txt",)), config
    )
    identity, outcome = run_dev(board, config, card, task_id, executor_argv=executor)
    assert outcome.category == "success", outcome.reason
    assert outcome.terminal == "request_review", outcome.terminal
    run_dir = dev_run_dir(config.work_root, task_id, identity.run_id)
    review_identity, _ = review_pass(
        board, task_id, run_dir, dev_run_id=identity.run_id, rework_round=1
    )
    if authorization:
        write_authorization(
            Path(config.work_root),
            task_id,
            run_dir,
            dev_run_id=identity.run_id,
            review_run_id=review_identity.run_id,
            rework_round=1,
            **(authorization_overrides or {}),
        )
    merge_task_id, merge_card = queue_merge_card(board, config, task_id)
    return {
        "board": board,
        "repo": repo,
        "base": base,
        "executor": executor,
        "config": config,
        "task_id": task_id,
        "dev_card": card,
        "run_dir": run_dir,
        "dev_run_id": identity.run_id,
        "review_run_id": review_identity.run_id,
        "merge_task_id": merge_task_id,
        "merge_card": merge_card,
    }


def _release(facts: dict):
    return claim_and_release(
        facts["board"], facts["config"], facts["merge_task_id"], facts["merge_card"]
    )


def _report(facts: dict) -> tuple[Path, dict]:
    release_dir = release_run_dir(
        Path(facts["config"].work_root), facts["merge_task_id"]
    )
    report = json.loads(
        (release_dir / "release-report.json").read_text(encoding="utf-8")
    )
    return release_dir, report


def _check(report: dict, name: str) -> dict:
    return next(check for check in report["checks"] if check["name"] == name)


def _assert_refused(
    facts: dict,
    outcome: dict,
    *,
    reason: str,
    kind: str = "needs_input",
    expected_verdict: str | None = None,
) -> tuple[Path, dict]:
    assert outcome["action"] == "block", outcome
    assert outcome["reason"] == reason, outcome
    assert outcome["terminal_ok"] is True
    row = task_row(facts["board"], facts["merge_task_id"])
    assert row["status"] == "blocked", row
    assert row["block_kind"] == kind, row
    release_dir, report = _report(facts)
    if expected_verdict is not None:
        assert report["verdict"] == expected_verdict, report
    else:
        assert report["verdict"] in ("refused", "blocked"), report
    # The refusal is recorded even though nothing merged.
    assert report["reasons"], report
    assert not (release_dir / "merge.json").exists()
    state = json.loads(
        (release_dir / "release-state.json").read_text(encoding="utf-8")
    )
    assert state["stage"] == "terminal", state
    assert not any(
        run["outcome"] == "completed"
        for run in facts["board"].run_rows(facts["merge_task_id"])
    )
    return release_dir, report


# ---------------------------------------------------------------------------
# W4-07: every gate refusal writes a report and never merges
# ---------------------------------------------------------------------------


def test_merge_disabled_refuses_despite_valid_authorization(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    # The card is queued while merging is enabled, then the operator disables
    # merging before the release run claims it.
    facts = _chain(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
    )
    facts["config"] = w4_config(
        make_config,
        board_path=board.board_path,
        repo_path=facts["repo"],
        executor_argv=[facts["executor"]],
        merge_enabled=False,
        post_merge_commands=[[PYTHON, "-c", "pass"]],
    )
    _, outcome = _release(facts)
    release_dir, report = _assert_refused(
        facts,
        outcome,
        reason="release:merge-disabled",
        expected_verdict="refused",
    )
    assert _check(report, "merge_enabled")["reasons"] == ["merge-disabled"]
    assert repo_head(facts["repo"]) == facts["base"]


def test_missing_authorization_refuses(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        authorization=False,
    )
    _, outcome = _release(facts)
    _, report = _assert_refused(
        facts, outcome, reason="release:authorization-missing"
    )
    assert _check(report, "authorization")["reasons"] == ["authorization-missing"]
    assert repo_head(facts["repo"]) == facts["base"]


def test_expired_authorization_refuses(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        authorization_overrides={"expires_at": int(time.time()) - 1},
    )
    _, outcome = _release(facts)
    _, report = _assert_refused(
        facts, outcome, reason="release:authorization-expired"
    )
    assert _check(report, "authorization")["reasons"] == ["authorization-expired"]
    assert repo_head(facts["repo"]) == facts["base"]


def test_authorization_that_is_not_yet_valid_refuses(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    now = int(time.time())
    facts = _chain(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        authorization_overrides={
            "approved_at": now + 3600,
            "expires_at": now + 7200,
        },
    )
    _, outcome = _release(facts)
    _, report = _assert_refused(
        facts, outcome, reason="release:authorization-not-yet-valid"
    )
    assert _check(report, "authorization")["reasons"] == [
        "authorization-not-yet-valid"
    ]


def test_authorization_field_mismatch_refuses(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        authorization_overrides={"reviewed_head": "0" * 40},
    )
    _, outcome = _release(facts)
    _, report = _assert_refused(
        facts,
        outcome,
        reason="release:authorization-mismatch:reviewed_head",
    )
    assert _check(report, "authorization")["reasons"] == [
        "authorization-mismatch:reviewed_head"
    ]
    assert repo_head(facts["repo"]) == facts["base"]


def test_authorization_approver_must_be_independent(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        authorization_overrides={"approver": "claude"},
    )
    _, outcome = _release(facts)
    _, report = _assert_refused(
        facts, outcome, reason="release:authorization-approver-invalid"
    )
    assert _check(report, "authorization")["reasons"] == [
        "authorization-approver-invalid"
    ]


def test_main_moved_after_the_queue_refuses(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    (facts["repo"] / "EXTRA.txt").write_text("moved\n", encoding="utf-8")
    git("add", "EXTRA.txt", cwd=facts["repo"])
    git("commit", "-q", "-m", "main moved", cwd=facts["repo"])
    moved = repo_head(facts["repo"])
    _, outcome = _release(facts)
    _, report = _assert_refused(
        facts, outcome, reason="release:main-moved", kind="capability"
    )
    assert _check(report, "main_unchanged")["reasons"] == ["main-moved"]
    assert repo_head(facts["repo"]) == moved


def test_empty_post_merge_commands_refuse(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        post_merge_commands=[],
    )
    _, outcome = _release(facts)
    _, report = _assert_refused(
        facts, outcome, reason="release:postmerge-commands-missing"
    )
    assert _check(report, "merge_enabled")["reasons"] == [
        "postmerge-commands-missing"
    ]
    assert repo_head(facts["repo"]) == facts["base"]


def test_deleted_evidence_refuses(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    (facts["run_dir"] / "evidence" / "manifest.json").unlink()
    _, outcome = _release(facts)
    assert outcome["reason"].startswith("release:evidence:"), outcome
    _, report = _assert_refused(facts, outcome, reason=outcome["reason"])
    evidence = _check(report, "evidence")
    assert evidence["ok"] is False
    assert any(reason.startswith("evidence:") for reason in evidence["reasons"])
    assert repo_head(facts["repo"]) == facts["base"]


# ---------------------------------------------------------------------------
# W4-09: before-merge refusals, mid-merge failure and abort
# ---------------------------------------------------------------------------


def test_dirty_main_refuses_before_merging(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    (facts["repo"] / "DIRTY.txt").write_text("dirty\n", encoding="utf-8")
    _, outcome = _release(facts)
    _, report = _assert_refused(
        facts,
        outcome,
        reason="release:workspace-dirty",
        kind="capability",
        expected_verdict="refused",
    )
    assert _check(report, "workspace")["detail"] == "workspace-dirty"
    assert repo_head(facts["repo"]) == facts["base"]


def test_merge_tree_conflict_refuses_before_merging(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    # ``main`` adds RESULT.txt with different content: the dry-run merge tree
    # conflicts and the gate refuses without running ``git merge``.
    (facts["repo"] / "RESULT.txt").write_text("main\n", encoding="utf-8")
    git("add", "RESULT.txt", cwd=facts["repo"])
    git("commit", "-q", "-m", "conflicting main", cwd=facts["repo"])
    moved = repo_head(facts["repo"])
    _, outcome = _release(facts)
    _, report = _assert_refused(
        facts,
        outcome,
        reason="release:main-moved",
        kind="capability",
    )
    assert _check(report, "merge_tree")["detail"] == "merge-conflict"
    assert repo_head(facts["repo"]) == moved


def test_mid_merge_failure_aborts_and_keeps_main(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    # The gate passes (status and merge-tree do not need the index lock) and
    # then ``git merge`` fails before creating any commit.
    (facts["repo"] / ".git" / "index.lock").write_text("", encoding="utf-8")
    _, outcome = _release(facts)
    assert outcome["action"] == "block", outcome
    assert outcome["reason"] == "merge-failed", outcome
    assert outcome["merge_sha"] is None, outcome
    assert outcome["terminal_ok"] is True
    row = task_row(board, facts["merge_task_id"])
    assert row["status"] == "blocked"
    assert row["block_kind"] == "needs_input"

    release_dir, report = _report(facts)
    assert report["verdict"] == "release-ready", report
    merge = json.loads((release_dir / "merge.json").read_text(encoding="utf-8"))
    assert merge["verdict"] == "refused"
    assert merge["reasons"] == ["merge-failed"], merge
    assert merge["merge_sha"] is None
    assert merge["command"]["exit_code"] not in (0, None)
    assert (release_dir / "merge.stdout.log").is_file()
    assert (release_dir / "merge.stderr.log").is_file()
    # Nothing moved; no MERGE_HEAD was left behind.
    assert repo_head(facts["repo"]) == facts["base"]
    probe = run_git(
        ["rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=facts["repo"],
        timeout=30,
    )
    assert probe.returncode != 0


# ---------------------------------------------------------------------------
# W4-10: post-merge regression failure blocks without rollback
# ---------------------------------------------------------------------------


def test_postmerge_failure_blocks_without_rollback(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    commands = [
        [PYTHON, "-c", "import sys; sys.exit(3)"],
        [PYTHON, "-c", "pass"],
    ]
    facts = _chain(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        post_merge_commands=commands,
    )
    _, outcome = _release(facts)
    assert outcome["action"] == "block", outcome
    assert outcome["reason"] == "postmerge-failed:0", outcome
    assert outcome["terminal_ok"] is True
    merge_sha = outcome["merge_sha"]
    assert merge_sha

    row = task_row(board, facts["merge_task_id"])
    assert row["status"] == "blocked"
    assert row["block_kind"] == "needs_input"
    assert not any(
        run["outcome"] == "completed"
        for run in board.run_rows(facts["merge_task_id"])
    )

    release_dir, _ = _report(facts)
    merge = json.loads((release_dir / "merge.json").read_text(encoding="utf-8"))
    assert merge["verdict"] == "merged"
    postmerge = json.loads(
        (release_dir / "postmerge" / "report.json").read_text(encoding="utf-8")
    )
    assert postmerge["verdict"] == "fail"
    assert postmerge["reasons"] == ["postmerge-failed:0"]
    # Every configured command ran; the failing index is named.
    assert [entry["exit_code"] for entry in postmerge["commands"]] == [3, 0]
    assert (release_dir / "postmerge" / "0.stdout.log").is_file()
    assert (release_dir / "postmerge" / "0.stderr.log").is_file()
    assert (release_dir / "postmerge" / "1.stdout.log").is_file()

    # The merge commit is retained on main (no automatic rollback).
    assert repo_head(facts["repo"]) == merge_sha
    state = json.loads(
        (release_dir / "release-state.json").read_text(encoding="utf-8")
    )
    assert state["stage"] == "terminal", state
    assert state["merge_sha"] == merge_sha
