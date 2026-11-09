"""Shared W4 integration helpers: temporary board, repo, review and release.

Everything here builds temporary boards and repositories only.  The helpers
drive the real product paths (``run_once``, ``queue_release_cards``,
``release_once``) so the tests observe what the lane would actually write.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.config import (
    Executor,
    LaneConfig,
    MergeConfig,
    ReviewConfig,
)
from hermes_kanban_lanes.executors.claude import ClaudeAdapter
from hermes_kanban_lanes.runtime.supervisor import RunSettings, run_once
from hermes_kanban_lanes.verification.evidence import INDEX_RELATIVE_PATH, sha256_file
from hermes_kanban_lanes.workspace.git import repo_head, run_git

PYTHON = sys.executable
REQUIREMENT = "HKL-W4"
REVIEWER = "code-reviewer"
IMPLEMENTER = "hkl:claude"

SETTINGS = RunSettings(
    max_runtime_seconds=60,
    heartbeat_seconds=30,
    poll_seconds=0.05,
    grace_seconds=1,
    kill_grace_seconds=1,
)


def git(*args: str, cwd: Path) -> str:
    result = run_git(args, cwd=cwd, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def w4_config(
    make_config,
    *,
    board_path: Path,
    repo_path: Path,
    executor_argv: list[str],
    merge_enabled: bool = False,
    post_merge_commands: list[list[str]] | None = None,
    max_rework_rounds: int = 3,
    authorization_dir: str | None = None,
) -> LaneConfig:
    base = make_config(
        board_path=board_path, repo_path=repo_path, executor_argv=executor_argv
    )
    review = ReviewConfig(
        reviewer=REVIEWER,
        max_rework_rounds=max_rework_rounds,
        authorization_dir=authorization_dir,
        merge=MergeConfig(
            enabled=merge_enabled,
            main_ref="refs/heads/main",
            post_merge_commands=list(post_merge_commands or []),
            post_merge_timeout_seconds=120.0,
        ),
    )
    return replace(base, review=review)


def with_executor(config: LaneConfig, executor_argv: str) -> LaneConfig:
    """A copy of ``config`` whose ``claude`` executor is ``executor_argv``."""

    executors = dict(config.executors)
    executors["claude"] = Executor(argv=(executor_argv,))
    return replace(config, executors=executors)


def run_dev(
    board,
    config: LaneConfig,
    card,
    task_id: str,
    *,
    executor_argv: str,
    requirement_id: str = REQUIREMENT,
    settings: RunSettings | None = None,
):
    identity = board.claim(task_id)
    assert identity is not None, "claim unexpectedly lost"
    row = board.connection.execute(
        "SELECT id, title, body FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row is not None
    return identity, run_once(
        config=with_executor(config, executor_argv),
        adapter=board,
        board_alias="test",
        task=dict(row),
        card=card,
        identity=identity,
        executor=ClaudeAdapter(),
        settings=settings or SETTINGS,
        package_dir=Path(__file__).resolve().parents[2] / "src" / "hermes_kanban_lanes",
        requirement_id=requirement_id,
    )


def dev_run_dir(work_root: Path, task_id: str, run_id: int) -> Path:
    runs = sorted((Path(work_root) / "runs" / task_id).iterdir())
    matches = [path for path in runs if path.name.startswith(f"{run_id}-")]
    assert len(matches) == 1, matches
    return matches[0]


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def review_metadata(
    run_dir: Path,
    *,
    dev_run_id: int,
    review_run_id: int,
    rework_round: int,
    reviewer: str = REVIEWER,
    requirement_id: str = REQUIREMENT,
) -> dict[str, Any]:
    document = _load(run_dir / "run.json")
    head = document["code_commit"]
    tree = git("rev-parse", f"{head}^{{tree}}", cwd=Path(document["workspace"]))
    index_sha = sha256_file(run_dir / INDEX_RELATIVE_PATH)
    return {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "verdict": "pass",
        "reviewed_head": head,
        "reviewed_tree": tree,
        "dev_run_id": dev_run_id,
        "rework_round": rework_round,
        "reviewer": reviewer,
        "review_run_id": review_run_id,
        "evidence_index_sha256": index_sha,
        "reasons": [],
    }


def review_pass(
    board,
    task_id: str,
    run_dir: Path,
    *,
    dev_run_id: int,
    rework_round: int,
    reviewer: str = REVIEWER,
    requirement_id: str = REQUIREMENT,
) -> tuple[Any, dict[str, Any]]:
    identity = board.claim_review_task(task_id)
    assert identity is not None, "review claim lost"
    metadata = review_metadata(
        run_dir,
        dev_run_id=dev_run_id,
        review_run_id=identity.run_id,
        rework_round=rework_round,
        reviewer=reviewer,
        requirement_id=requirement_id,
    )
    assert board.complete_task(
        identity,
        result="review pass",
        summary="review pass",
        metadata=metadata,
    )
    return identity, metadata


def review_changes(board, task_id: str, reason: str):
    identity = board.claim_review_task(task_id)
    assert identity is not None, "review claim lost"
    return board.request_changes(identity, reason)


def write_authorization(
    work_root: Path,
    task_id: str,
    run_dir: Path,
    *,
    dev_run_id: int,
    review_run_id: int,
    rework_round: int,
    main_commit: str | None = None,
    requirement_id: str = REQUIREMENT,
    **overrides,
) -> Path:
    metadata = review_metadata(
        run_dir,
        dev_run_id=dev_run_id,
        review_run_id=review_run_id,
        rework_round=rework_round,
        requirement_id=requirement_id,
    )
    landing = _load(run_dir / "landing-plan.json")
    now = int(time.time())
    document = {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "task_id": task_id,
        "review_run_id": metadata["review_run_id"],
        "dev_run_id": dev_run_id,
        "rework_round": rework_round,
        "task_base_commit": _load(run_dir / "run.json")["task_base_commit"],
        "reviewed_head": metadata["reviewed_head"],
        "reviewed_tree": metadata["reviewed_tree"],
        "evidence_index_sha256": metadata["evidence_index_sha256"],
        "main_ref": "refs/heads/main",
        "main_commit": main_commit or landing["main_commit"],
        "approver": "operator",
        "approved_at": now - 10,
        "expires_at": now + 3600,
    }
    document.update(overrides)
    target = Path(work_root) / "authorizations" / f"{task_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def queue_merge_card(board, config: LaneConfig, task_id: str) -> tuple[str, Any]:
    from hermes_kanban_lanes.runtime.release import queue_release_cards

    queued = queue_release_cards(
        config.work_root, board, config, requirement_id=REQUIREMENT
    )
    matches = [entry for entry in queued if entry["dev_task_id"] == task_id]
    assert len(matches) == 1, queued
    merge_task_id = matches[0]["merge_task_id"]
    card = load_card(Path(config.work_root) / "cards" / f"{merge_task_id}.json", config)
    return merge_task_id, card


def claim_and_release(board, config: LaneConfig, merge_task_id: str, card):
    from hermes_kanban_lanes.runtime.release import release_once

    identity = board.claim(merge_task_id)
    assert identity is not None, "merge card claim lost"
    outcome = release_once(
        config=config,
        adapter=board,
        card=card,
        identity=identity,
        work_root=config.work_root,
        package_dir=Path(__file__).resolve().parents[2] / "src" / "hermes_kanban_lanes",
        requirement_id=REQUIREMENT,
    )
    return identity, outcome


def task_row(board, task_id: str) -> dict[str, Any]:
    return board.task_row(task_id) or {}


def run_rows(board, task_id: str) -> list[dict[str, Any]]:
    return board.run_rows(task_id)


def release_run_dir(work_root: Path, merge_task_id: str) -> Path:
    runs = sorted((Path(work_root) / "runs" / merge_task_id).iterdir())
    assert len(runs) == 1, runs
    return runs[0]


def review_body(
    *, write: str = "RESULT.txt", content: str = "ok\n", require: str | None = None
) -> str:
    check = ""
    if require is not None:
        check = (
            f"assert Path({require!r}).read_text(encoding='utf-8') == 'ok\\n', "
            f"'continuation base missing' \n"
        )
    return (
        f"{check}"
        f"Path({write!r}).write_text({content!r}, encoding='utf-8')\n"
        "print(json.dumps({"
        '"is_error": False, "result": "done", '
        '"usage": {"input_tokens": 10, "output_tokens": 5}, '
        '"total_cost_usd": 0.01}))\n'
    )


def commit_body(name: str, content: str) -> str:
    return (
        f"Path({name!r}).write_text({content!r}, encoding='utf-8')\n"
        'print(json.dumps({"is_error": False, "result": "done"}))\n'
    )
