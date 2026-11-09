"""W3-10: stage-journal crash windows and orphan recovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.runtime import daemon
from hermes_kanban_lanes.runtime.process import pid_alive
from hermes_kanban_lanes.runtime.state import (
    new_state,
    read_state,
    write_state,
)
from hermes_kanban_lanes.runtime.supervisor import (
    assess_previous_runs,
    recover_run,
)
from hermes_kanban_lanes.verification.evidence import (
    build_manifest,
    sha256_file,
    write_evidence_index,
    write_json,
)
from hermes_kanban_lanes.workspace.git import (
    create_run_worktree,
    repo_head,
    run_git,
)

PYTHON = sys.executable
SRC_DIR = Path(__file__).resolve().parents[2] / "src"


CRASH_AFTER_EVIDENCE = r"""
import json, os, sys
from pathlib import Path

config_path, task_id, package_dir = sys.argv[1:4]

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.config import load_config
from hermes_kanban_lanes.executors.claude import ClaudeAdapter
from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter
from hermes_kanban_lanes.runtime.supervisor import RunSettings, run_once

config = load_config(config_path)
work_root = Path(config.work_root)
adapter = BoardAdapter(config.boards["test"], config.hermes_source, name="test")
adapter.open()
try:
    card = load_card(work_root / "cards" / f"{task_id}.json", config)
    row = adapter.connection.execute(
        "SELECT id, title, body FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    identity = adapter.claim(task_id)
    if identity is None:
        os._exit(3)

    def _crash(*args, **kwargs):
        # The evidence stage is already journaled; die exactly before the
        # terminal board write, as a killed supervisor would.
        os._exit(7)

    adapter.request_review = _crash
    run_once(
        config=config,
        adapter=adapter,
        board_alias="test",
        task=dict(row),
        card=card,
        identity=identity,
        executor=ClaudeAdapter(),
        settings=RunSettings(
            max_runtime_seconds=60,
            command_timeout_seconds=60,
            heartbeat_seconds=30,
            poll_seconds=0.05,
            grace_seconds=1,
            kill_grace_seconds=1,
        ),
        package_dir=package_dir,
    )
finally:
    adapter.close()
"""


def _commit(repo: Path, message: str = "run") -> str:
    added = run_git(["add", "-A"], cwd=repo)
    assert added.returncode == 0, added.stderr
    committed = run_git(
        [
            "-c",
            "user.name=HKL Tests",
            "-c",
            "user.email=hkl@example.invalid",
            "commit",
            "--no-verify",
            "-m",
            message,
        ],
        cwd=repo,
    )
    assert committed.returncode == 0, committed.stderr
    return repo_head(repo)


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 0\n", encoding="utf-8")
    return package


def _evidence(
    tmp_path: Path,
    *,
    repo: Path,
    workspace: Path,
    identity: RunIdentity,
    base: str,
    package: Path,
) -> str:
    code = repo_head(workspace)
    run_dir = tmp_path / "run"
    (run_dir / "evidence").mkdir(parents=True, exist_ok=True)
    gate_path = "evidence/gate-report.json"
    write_json(
        run_dir / gate_path,
        {
            "schema_version": 2,
            "requirement_id": "HKL-W3",
            "identity": identity.to_dict(),
            "base_commit": base,
            "run_commit": code,
            "sync_base": base,
            "checks": [],
            "verdict": "pass",
        },
    )
    gate_sha = sha256_file(run_dir / gate_path)
    manifest = build_manifest(
        identity,
        base_commit=base,
        code_commit=code,
        gate_report_path=gate_path,
        gate_report_sha256=gate_sha,
        verdict="pass",
        package_dir=package,
    )
    write_json(run_dir / "evidence" / "manifest.json", manifest.to_dict())
    write_evidence_index(
        run_dir,
        identity=identity,
        base_commit=base,
        sync_base=base,
        code_commit=code,
        tool_sha256=manifest.tool_sha256,
        gate_report_path=gate_path,
        gate_report_sha256=gate_sha,
        verdict="pass",
    )
    return code


def _dead_pid() -> int:
    process = subprocess.Popen([PYTHON, "-c", "pass"])
    process.wait(timeout=10)
    return int(process.pid)


def _start_sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [PYTHON, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )


def _state_for(
    *,
    identity: RunIdentity,
    run_dir: Path,
    workspace: Path,
    stage: str,
    supervisor_pid: int,
    supervisor_fingerprint: str | None,
    **fields,
):
    state = new_state(
        identity=identity,
        board="test",
        run_dir=run_dir,
        workspace=workspace,
        supervisor_pid=supervisor_pid,
        supervisor_fingerprint=supervisor_fingerprint,
    )
    state.advance(stage, **fields)
    write_state(run_dir, state)
    return state


def _running_row(board, task_id: str) -> dict:
    row = board.connection.execute(
        "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return dict(row)


def test_evidence_stage_is_completed_idempotently(
    tmp_path,
    board,
    make_task,
    make_repo,
):
    repo = make_repo()
    base = repo_head(repo)
    workspace = tmp_path / "worktree"
    create_run_worktree(repo, base, "hkl/run", workspace)
    (workspace / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    _commit(workspace, "run")

    task_id = make_task(title="recover evidence")
    identity = board.claim(task_id)
    assert identity is not None
    package = _package(tmp_path)
    code = _evidence(
        tmp_path,
        repo=repo,
        workspace=workspace,
        identity=identity,
        base=base,
        package=package,
    )
    run_dir = tmp_path / "run"
    _state_for(
        identity=identity,
        run_dir=run_dir,
        workspace=workspace,
        stage="evidence",
        supervisor_pid=_dead_pid(),
        supervisor_fingerprint="ps-lstart:dead",
        base_commit=base,
        sync_base=base,
        run_commit=code,
        code_commit=code,
        gate_verdict="pass",
    )
    entries_before = {path.name for path in tmp_path.iterdir()}

    first = recover_run(
        adapter=board, board_alias="test", run_dir=run_dir, package_dir=package
    )

    assert first["action"] == "request_review"
    assert _running_row(board, task_id)["status"] == "review"
    state = read_state(run_dir)
    assert state is not None and state.stage == "terminal"
    # No executor was re-run and no new run directory appeared.
    assert {path.name for path in tmp_path.iterdir()} == entries_before
    assert repo_head(workspace) == code

    second = recover_run(
        adapter=board, board_alias="test", run_dir=run_dir, package_dir=package
    )
    assert second["action"] == "noop"


def test_supervisor_alive_is_never_taken_over(
    tmp_path, board, make_task, make_repo
):
    repo = make_repo()
    base = repo_head(repo)
    workspace = tmp_path / "worktree"
    create_run_worktree(repo, base, "hkl/run", workspace)
    (workspace / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    _commit(workspace, "run")
    task_id = make_task(title="recover alive")
    identity = board.claim(task_id)
    assert identity is not None

    from hermes_kanban_lanes.runtime.process import start_fingerprint

    run_dir = tmp_path / "run"
    _state_for(
        identity=identity,
        run_dir=run_dir,
        workspace=workspace,
        stage="evidence",
        supervisor_pid=os.getpid(),
        supervisor_fingerprint=start_fingerprint(os.getpid()),
    )

    result = recover_run(
        adapter=board, board_alias="test", run_dir=run_dir, package_dir=None
    )

    assert result["action"] == "supervisor-alive"
    assert _running_row(board, task_id)["status"] == "running"
    state = read_state(run_dir)
    assert state is not None and state.stage == "evidence"


def test_a_run_from_another_board_is_never_finished(
    tmp_path, board, make_task, make_repo
):
    repo = make_repo()
    base = repo_head(repo)
    workspace = tmp_path / "worktree"
    create_run_worktree(repo, base, "hkl/run", workspace)
    task_id = make_task(title="foreign board")
    identity = board.claim(task_id)
    assert identity is not None
    run_dir = tmp_path / "run"
    state = new_state(
        identity=identity,
        board="another-board",
        run_dir=run_dir,
        workspace=workspace,
        supervisor_pid=_dead_pid(),
        supervisor_fingerprint="ps-lstart:dead",
    )
    state.advance("evidence")
    write_state(run_dir, state)

    result = recover_run(
        adapter=board, board_alias="test", run_dir=run_dir, package_dir=None
    )

    assert result["action"] == "foreign-board"
    assert _running_row(board, task_id)["status"] == "running"
    assert read_state(run_dir).stage == "evidence"


def test_committed_stage_is_left_for_reclaim(
    tmp_path, board, make_task, make_repo
):
    repo = make_repo()
    base = repo_head(repo)
    workspace = tmp_path / "worktree"
    create_run_worktree(repo, base, "hkl/run", workspace)
    (workspace / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    code = _commit(workspace, "run")
    task_id = make_task(title="recover committed")
    identity = board.claim(task_id)
    assert identity is not None
    run_dir = tmp_path / "run"
    _state_for(
        identity=identity,
        run_dir=run_dir,
        workspace=workspace,
        stage="committed",
        supervisor_pid=_dead_pid(),
        supervisor_fingerprint="ps-lstart:dead",
        base_commit=base,
        run_commit=code,
    )

    result = recover_run(
        adapter=board, board_alias="test", run_dir=run_dir, package_dir=None
    )

    assert result["action"] == "left-for-reclaim"
    assert result["child"] is None
    assert _running_row(board, task_id)["status"] == "running"
    state = read_state(run_dir)
    assert state is not None and state.stage == "committed"


def test_spawn_orphan_window_uses_state_child_identity(
    tmp_path,
):
    task_id = "t_orphan"
    work_root = tmp_path / "work"
    previous = work_root / "runs" / task_id / "1-deadbeef"
    previous.mkdir(parents=True)
    workspace = work_root / "workspaces" / task_id / "run-1-deadbeef"
    workspace.mkdir(parents=True)
    sleeper = _start_sleeper()
    try:
        from hermes_kanban_lanes.runtime.process import start_fingerprint

        fingerprint = start_fingerprint(sleeper.pid)
        assert fingerprint
        identity = RunIdentity(
            schema_version=1,
            task_id=task_id,
            run_id=1,
            claim_lock="old-lock",
        )
        state = new_state(
            identity=identity,
            board="test",
            run_dir=previous,
            workspace=workspace,
            supervisor_pid=_dead_pid(),
            supervisor_fingerprint="ps-lstart:dead",
        )
        state.advance("workspace", base_commit="a" * 40)
        state.advance("executor").set_child(
            pid=sleeper.pid, pgid=sleeper.pid, fingerprint=fingerprint
        )
        write_state(previous, state)
        # No run.json exists: state.json is the only witness.
        assert not (previous / "run.json").exists()

        assessments = assess_previous_runs(work_root, task_id, 2)

        assert len(assessments) == 1
        entry = assessments[0]
        assert entry["witness_source"] == "state.json"
        assert entry["terminated"] is False
        assert entry["reusable"] is False
        assert Path(entry["marker"]).name.endswith(".unreusable.json")
        assert Path(entry["marker"]).is_file()

        sleeper.terminate()
        sleeper.wait(timeout=10)
        time.sleep(0.1)

        assessments = assess_previous_runs(work_root, task_id, 2)
        entry = assessments[0]
        assert entry["terminated"] is True
        assert entry["reusable"] is True
        assert Path(entry["marker"]).name.endswith(".reclaimable.json")
        assert Path(entry["marker"]).is_file()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=10)


@pytest.mark.parametrize("stale_kind", ["dead-worker", "claim-expired"])
def test_daemon_tick_recovers_evidence_before_any_stale_sweep(
    stale_kind,
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    monkeypatch,
):
    """W3-10: a real ``daemon.tick`` finalizes evidence before reclaiming.

    The crashed supervisor ran the executor exactly once and died after the
    evidence stage was journaled.  Its executor PID is dead and either the
    crash grace (/worker PID) or the claim TTL has expired, so the old tick
    order would reclaim the task and run the executor a second time.  Recovery
    must run first and the next two ticks must add no run.
    """

    repo = make_repo()
    package = _package(tmp_path)
    executions = tmp_path / "executions.txt"
    body = (
        "import json,pathlib\n"
        f"counter=pathlib.Path({str(executions)!r})\n"
        "n=int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(n+1))\n"
        "pathlib.Path('RESULT.txt').write_text('ok\\n')\n"
        "print(json.dumps({'is_error': False, 'result': 'done'}))\n"
    )
    executor = fake_executor(body)
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title=f"w3 tick recovery {stale_kind}")
    write_card(
        config.work_root,
        task_id,
        allow=("RESULT.txt",),
        commands=[{"argv": [PYTHON, "-c", "pass"], "cwd": "."}],
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config.to_dict()), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    crashed = subprocess.run(
        [PYTHON, "-c", CRASH_AFTER_EVIDENCE, str(config_path), task_id, str(package)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert crashed.returncode == 7, (crashed.stdout, crashed.stderr)

    task_runs = Path(config.work_root) / "runs" / task_id
    run_dirs = sorted(task_runs.iterdir())
    assert len(run_dirs) == 1
    state = read_state(run_dirs[0])
    assert state is not None and state.stage == "evidence"
    assert read_state(run_dirs[0]).supervisor_pid != os.getpid()
    identity = state.identity
    assert executions.read_text(encoding="utf-8") == "1"

    row = _running_row(board, task_id)
    assert row["status"] == "running"
    assert int(row["current_run_id"]) == identity.run_id
    worker_pid = board.connection.execute(
        "SELECT worker_pid FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()[0]
    assert worker_pid and not pid_alive(int(worker_pid))

    now = int(time.time())
    with board.connection:
        board.connection.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?", (now - 3600, task_id)
        )
        if stale_kind == "claim-expired":
            board.connection.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (now - 60, task_id),
            )
    # The dead worker must also be past the crash grace in this tick.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    first = daemon.tick(config, tick_index=0, package_dir=package)
    assert first["errors"] == [], first["errors"]
    actions = {
        (entry.get("task_id"), entry.get("run_id"), entry.get("action"))
        for entry in first["recovered"]
    }
    assert (task_id, identity.run_id, "request_review") in actions, first["recovered"]
    # Recovery ran before the sweeps: no re-claim and no second execution.
    assert first["claimed"] == [], first["claimed"]
    assert first["runs"] == [], first["runs"]
    assert _running_row(board, task_id)["status"] == "review"
    assert read_state(run_dirs[0]).stage == "terminal"
    assert executions.read_text(encoding="utf-8") == "1"
    assert len(list(task_runs.iterdir())) == 1

    second = daemon.tick(config, tick_index=1, package_dir=package)
    assert second["errors"] == [], second["errors"]
    assert second["claimed"] == [], second["claimed"]
    assert second["runs"] == [], second["runs"]
    assert _running_row(board, task_id)["status"] == "review"
    assert read_state(run_dirs[0]).stage == "terminal"
    assert executions.read_text(encoding="utf-8") == "1"
    assert len(list(task_runs.iterdir())) == 1
