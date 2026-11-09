"""W3-13: W2 leftovers that had no in-repo regression before."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_kanban_lanes.runtime.process import (
    pid_alive,
    process_group_members,
    spawn,
    terminate,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
PYTHON = sys.executable

CLAIM_SCRIPT = """
import json, os, sys, time
from pathlib import Path

from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter

board_path, source, task_id, marker_dir, name = sys.argv[1:6]
markers = Path(marker_dir)
markers.mkdir(parents=True, exist_ok=True)
(markers / f"{name}.started").write_text(str(os.getpid()), encoding="utf-8")
deadline = time.time() + 20
while len(list(markers.glob("*.started"))) < 2 and time.time() < deadline:
    time.sleep(0.005)
adapter = BoardAdapter(board_path, source)
adapter.open()
try:
    identity = adapter.claim(task_id)
    print(json.dumps(identity.to_dict() if identity else None), flush=True)
finally:
    adapter.close()
"""


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    return env


def _row(board, task_id: str) -> dict:
    row = board.connection.execute(
        "SELECT status, current_run_id, claim_lock, block_kind FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return dict(row) if row else {}


def _intent_receipts(board) -> int:
    try:
        return int(
            board.connection.execute(
                "SELECT count(*) FROM hkl_intent_receipts"
            ).fetchone()[0]
        )
    except Exception:  # noqa: BLE001 - table name is host-owned
        return 0


def test_two_os_processes_claim_the_same_task_once(
    tmp_path, board, make_task, hermes_source
):
    task_id = make_task(title="two-process claim")
    markers = tmp_path / "markers"
    processes = [
        subprocess.Popen(
            [
                PYTHON,
                "-c",
                CLAIM_SCRIPT,
                board.board_path,
                hermes_source,
                task_id,
                str(markers),
                name,
            ],
            cwd=tmp_path,
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in ("a", "b")
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout.strip().splitlines()[-1]))

    winners = [result for result in results if result is not None]
    losers = [result for result in results if result is None]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    winner = winners[0]

    row = _row(board, task_id)
    assert row["status"] == "running"
    assert int(row["current_run_id"]) == winner["run_id"]
    assert row["claim_lock"] == winner["claim_lock"]
    # One claim, one run, no side effects.
    assert _intent_receipts(board) == 0
    comments = board.connection.execute(
        "SELECT count(*) FROM task_comments WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    assert comments == 0


def test_reclaim_fences_every_old_identity_write(board, make_task):
    task_id = make_task(title="reclaim CAS")
    identity = board.claim(task_id)
    assert identity is not None

    assert board.reclaim(identity, "test reclaim") is True

    assert board.identity(task_id) is None
    assert board.heartbeat(identity) is False
    assert board.request_review(identity, "stale") is False
    assert board.block(identity, "stale", "needs_input") is False
    assert board.reclaim(identity, "again") is False
    assert _row(board, task_id)["status"] != "running"

    replacement = board.claim(task_id)
    assert replacement is not None
    assert replacement.run_id != identity.run_id
    # The old identity stays dead after another claim exists.
    assert board.heartbeat(identity) is False
    assert board.request_review(identity, "stale") is False


def test_heartbeat_extends_the_claim_and_stale_release_recovers_it(
    board, make_task
):
    task_id = make_task(title="heartbeat")
    identity = board.claim(task_id)
    assert identity is not None

    def expires() -> int | None:
        value = board.connection.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        return int(value) if value is not None else None

    before = expires()
    assert board.heartbeat(identity) is True
    after = expires()
    assert before is not None and after is not None
    assert after >= before

    # A live claim is never released by the stale sweep.
    assert board.release_stale_claims() == 0
    assert _row(board, task_id)["status"] == "running"
    assert board.identity(task_id) == identity

    board.connection.execute(
        "UPDATE tasks SET claim_expires = 1 WHERE id = ?", (task_id,)
    )
    board.connection.commit()
    assert board.release_stale_claims() >= 1
    assert _row(board, task_id)["status"] != "running"
    assert board.heartbeat(identity) is False


def test_terminate_kills_the_whole_group(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    body = (
        "import subprocess,sys,time,pathlib;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid), encoding='utf-8');"
        "print('ready', flush=True);"
        "time.sleep(60)"
    )
    child = spawn(
        [PYTHON, "-c", body],
        env=dict(os.environ),
        cwd=str(tmp_path),
        stdout_path=str(tmp_path / "stdout.log"),
        stderr_path=str(tmp_path / "stderr.log"),
        name="cleanup-test",
    )
    try:
        for _ in range(100):
            if pid_file.is_file():
                break
            time.sleep(0.02)
        assert pid_file.is_file()
        grandchild = int(pid_file.read_text(encoding="utf-8"))
        assert pid_alive(grandchild)

        result = terminate(child, grace_seconds=0.3, kill_grace_seconds=3.0)

        assert result["terminated"] is True, result["witness"]
        assert result["residual_pids"] == []
        group = process_group_members(child.pgid)
        assert group["available"] is True
        assert group["live_pids"] == []
        for _ in range(100):
            if not pid_alive(grandchild):
                break
            time.sleep(0.02)
        assert not pid_alive(grandchild)
    finally:
        if child.process is not None and child.process.poll() is None:
            child.process.kill()
            child.process.wait(timeout=10)
