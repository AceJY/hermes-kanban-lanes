"""W2-06/10/11: process-group reclaim and gate scope safety."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

PYTHON = sys.executable

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.runtime.daemon import StopFlag
from hermes_kanban_lanes.runtime.process import process_group_members
from hermes_kanban_lanes.runtime.supervisor import RunSettings
from hermes_kanban_lanes.workspace.git import create_run_worktree, repo_head


def test_gate_rejects_symlink_escape_even_for_allowed_path(
    tmp_path, make_repo, make_config, write_card
):
    from hermes_kanban_lanes.contracts import RunIdentity
    from hermes_kanban_lanes.verification.gate import run_gate

    repo = make_repo()
    base = repo_head(repo)
    workspace = tmp_path / "ws"
    create_run_worktree(repo, base, "hkl/t_link/run-1", workspace)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    os.symlink(outside, workspace / "RESULT.txt")

    config = make_config(
        board_path=tmp_path / "board.db",
        repo_path=repo,
        executor_argv=[PYTHON, "-c", "pass"],
    )
    card = load_card(
        write_card(config.work_root, "t_link", allow=("RESULT.txt",)), config
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    identity = RunIdentity(
        schema_version=1, task_id="t_link", run_id=1, claim_lock="lock"
    )
    result = run_gate(card, workspace, base, run_dir, identity=identity)
    assert result.verdict == "fail"
    scope = next(c for c in result.report["checks"] if c["name"] == "scope")
    assert scope["ok"] is False
    assert "symlink" in scope["detail"]


def test_timeout_kills_the_whole_group_and_blocks(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    claim_and_run,
    task_status,
):
    repo = make_repo()
    executor = fake_executor("time.sleep(60)\nprint('late')\n")
    config = make_config(board_path=board.board_path, repo_path=repo, executor_argv=[executor])
    task_id = make_task(title="timeout")
    card = load_card(write_card(config.work_root, task_id), config)
    _, outcome = claim_and_run(
        config=config,
        card=card,
        task_id=task_id,
        settings=RunSettings(
            max_runtime_seconds=0.5,
            heartbeat_seconds=30,
            poll_seconds=0.05,
            grace_seconds=0.3,
            kill_grace_seconds=1,
        ),
    )
    assert outcome.category == "timeout"
    assert outcome.terminal == "block"
    status = task_status(task_id)
    assert status["status"] == "blocked"
    assert status["block_kind"] == "transient"
    assert outcome.cleanup["terminated"] is True
    assert outcome.cleanup["residual_pids"] == []
    members = process_group_members(outcome.cleanup["pgid"])
    assert members["live_pids"] == []
    assert Path(outcome.workspace).is_dir()


def test_cancel_reclaims_to_ready(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    claim_and_run,
    task_status,
):
    repo = make_repo()
    executor = fake_executor("time.sleep(60)\n")
    config = make_config(board_path=board.board_path, repo_path=repo, executor_argv=[executor])
    task_id = make_task(title="cancel")
    card = load_card(write_card(config.work_root, task_id), config)
    stop = StopFlag()
    timer = threading.Timer(0.4, lambda: stop.set("cancelled"))
    timer.start()
    try:
        _, outcome = claim_and_run(
            config=config,
            card=card,
            task_id=task_id,
            stop_event=stop,
            settings=RunSettings(
                max_runtime_seconds=30,
                heartbeat_seconds=30,
                poll_seconds=0.05,
                grace_seconds=0.3,
                kill_grace_seconds=1,
            ),
        )
    finally:
        timer.cancel()
    assert outcome.category == "cancelled"
    assert outcome.terminal == "reclaim"
    status = task_status(task_id)
    assert status["status"] == "ready"
    assert status["claim_lock"] is None
    assert outcome.cleanup["terminated"] is True
    assert Path(outcome.workspace).is_dir()


def test_unproven_cleanup_is_environment_failure_and_unreusable(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    claim_and_run,
    task_status,
):
    repo = make_repo()
    executor = fake_executor("print('done')\n")
    config = make_config(board_path=board.board_path, repo_path=repo, executor_argv=[executor])
    task_id = make_task(title="cleanup")
    card = load_card(write_card(config.work_root, task_id), config)

    def stuck_group(pgid):
        return {
            "available": True,
            "pgid": pgid,
            "method": "injected",
            "members": [{"pid": 999999, "zombie": False}],
            "live_members": [{"pid": 999999, "zombie": False}],
            "live_pids": [999999],
            "zombie_pids": [],
            "error": None,
        }

    _, outcome = claim_and_run(
        config=config,
        card=card,
        task_id=task_id,
        members_fn=stuck_group,
        settings=RunSettings(
            max_runtime_seconds=5,
            heartbeat_seconds=30,
            poll_seconds=0.05,
            grace_seconds=0.2,
            kill_grace_seconds=0.3,
        ),
    )
    assert outcome.category == "environment_failure"
    assert outcome.terminal == "block"
    status = task_status(task_id)
    assert status["status"] == "blocked"
    assert status["block_kind"] == "capability"
    assert outcome.cleanup["terminated"] is False
    marker = Path(config.work_root) / "reclaimable" / (
        Path(outcome.workspace).name + ".unreusable.json"
    )
    reclaim_marker = Path(config.work_root) / "reclaimable" / (
        Path(outcome.workspace).name + ".reclaimable.json"
    )
    assert marker.is_file()
    assert not reclaim_marker.exists()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["reusable"] is False
    assert payload["reason"] == "process-group-cleanup-incomplete"
    assert Path(outcome.workspace).is_dir()
