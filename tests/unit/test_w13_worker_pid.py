"""W13: withdraw the executor pid once the executor exits, before the gate.

The host's ``detect_crashed_workers`` reclaims a ``running`` card whose
``worker_pid`` is dead.  A finished executor's pid is dead by definition, so a
card that keeps it published while the supervisor runs the gate/review finish
is indistinguishable from a crashed worker and is reclaimed (three production
incidents).  These tests pin the call site in ``run_once`` and the resulting
board state.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.executors.claude import ClaudeAdapter
from hermes_kanban_lanes.runtime import supervisor as supervisor_module
from hermes_kanban_lanes.runtime.process import pid_alive
from hermes_kanban_lanes.runtime.supervisor import RunSettings

PYTHON = sys.executable

SETTINGS = RunSettings(
    max_runtime_seconds=30,
    heartbeat_seconds=30,
    poll_seconds=0.05,
    grace_seconds=1,
    kill_grace_seconds=1,
)

FAILURE_BODY = """
Path("RESULT.txt").write_text("partial\\n", encoding="utf-8")
print(json.dumps({"is_error": True, "result": "boom"}))
raise SystemExit(1)
"""

PASS_COMMAND = [{"argv": [PYTHON, "-c", "pass"], "cwd": "."}]


def _dead_pid() -> int:
    process = subprocess.Popen([PYTHON, "-c", "pass"])
    process.wait(timeout=10)
    return int(process.pid)


def _run_pid(board, task_id: str, run_id: int):
    row = board.connection.execute(
        "SELECT worker_pid FROM task_runs WHERE id = ? AND task_id = ?",
        (run_id, task_id),
    ).fetchone()
    return row[0] if row is not None else None


def _load_card(config, task_id: str):
    return load_card(Path(config.work_root) / "cards" / f"{task_id}.json", config)


def test_clear_before_gate_at_call_site(
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    success_body,
    claim_and_run,
    monkeypatch,
):
    """The clear runs before the gate, for this card/run only.

    Negating the implementation (deleting the ``adapter.clear_worker`` call
    from ``run_once``) makes this test red: the spy never fires and the gate
    still observes the dead executor pid on the board.
    """

    repo = make_repo()
    executor = fake_executor(success_body)
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="w13 call site")
    write_card(config.work_root, task_id, allow=("RESULT.txt",), commands=PASS_COMMAND)
    card = _load_card(config, task_id)

    # A sibling claim with its own (dead) worker pid must be left untouched.
    other_id = make_task(title="w13 sibling")
    other_identity = board.claim(other_id)
    assert other_identity is not None
    other_pid = _dead_pid()
    board.record_worker(other_identity, other_pid)

    clear_calls: list[tuple[str, int]] = []
    real_clear = board.clear_worker

    def clear_spy(identity):
        clear_calls.append((identity.task_id, identity.run_id))
        return real_clear(identity)

    monkeypatch.setattr(board, "clear_worker", clear_spy)

    observed: dict[str, object] = {}
    real_run_gate = supervisor_module.run_gate

    def gate_spy(*args, **kwargs):
        identity = kwargs["identity"]
        observed["clear_calls_at_gate"] = list(clear_calls)
        observed["task"] = board.task_row(task_id)
        observed["run_pid"] = _run_pid(board, task_id, identity.run_id)
        observed["other_task"] = board.task_row(other_id)
        observed["other_run_pid"] = _run_pid(
            board, other_id, other_identity.run_id
        )
        return real_run_gate(*args, **kwargs)

    monkeypatch.setattr(supervisor_module, "run_gate", gate_spy)

    identity, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=SETTINGS
    )

    assert outcome.category == "success", outcome.reason
    assert observed, "the success path must reach the gate"
    # The clear happened before the gate and targeted exactly this run.
    assert observed["clear_calls_at_gate"] == [(task_id, identity.run_id)]
    current = observed["task"]
    assert current["worker_pid"] is None
    assert current["worker_started_at"] is None
    assert observed["run_pid"] is None
    # The sibling card and its run row are not touched.
    assert observed["other_task"]["worker_pid"] == other_pid
    assert observed["other_run_pid"] == other_pid


def test_hkl_claim_survives_dead_executor_pid(
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    success_body,
    claim_and_run,
    monkeypatch,
):
    """A cleared card is invisible to the real crash sweep; a control is not."""

    repo = make_repo()
    executor = fake_executor(success_body)
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="w13 main")
    write_card(config.work_root, task_id, allow=("RESULT.txt",), commands=PASS_COMMAND)
    card = _load_card(config, task_id)

    # Control: a claimed card that still publishes a dead executor pid.
    control_id = make_task(title="w13 control")
    control_identity = board.claim(control_id)
    assert control_identity is not None
    control_pid = _dead_pid()
    board.record_worker(control_identity, control_pid)
    with board.connection:
        board.connection.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?",
            (int(time.time()) - 3600, control_id),
        )
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    observed: dict[str, object] = {}
    real_run_gate = supervisor_module.run_gate

    def gate_spy(*args, **kwargs):
        # The gate is still in flight (run_once has not reached any terminal
        # write), yet the real host sweep runs here.
        crashed = board.detect_crashed_workers()
        observed["crashed"] = crashed
        observed["main"] = board.task_row(task_id)
        observed["control"] = board.task_row(control_id)
        observed["control_run"] = board.connection.execute(
            "SELECT status FROM task_runs WHERE id = ? AND task_id = ?",
            (control_identity.run_id, control_id),
        ).fetchone()
        return real_run_gate(*args, **kwargs)

    monkeypatch.setattr(supervisor_module, "run_gate", gate_spy)

    _, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=SETTINGS
    )

    assert outcome.category == "success", outcome.reason
    # Control: the host reclaims the still-published dead pid and records crash.
    assert control_id in observed["crashed"]
    assert observed["control"]["status"] != "running"
    assert observed["control_run"]["status"] == "crashed"
    # Main: cleared before the gate, so the crash sweep leaves it alone.
    assert task_id not in observed["crashed"]
    assert observed["main"]["status"] == "running"
    assert observed["main"]["worker_pid"] is None
    assert not pid_alive(control_pid)


def test_failure_path_clears_worker_pid(
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    claim_and_run,
    monkeypatch,
):
    """A non-zero executor exit clears the pid before the block/terminal write."""

    repo = make_repo()
    executor = fake_executor(FAILURE_BODY)
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="w13 failure")
    write_card(config.work_root, task_id, allow=("RESULT.txt",), commands=PASS_COMMAND)
    card = _load_card(config, task_id)

    clear_calls: list[tuple[str, int]] = []
    real_clear = board.clear_worker

    def clear_spy(identity):
        clear_calls.append((identity.task_id, identity.run_id))
        return real_clear(identity)

    monkeypatch.setattr(board, "clear_worker", clear_spy)

    observed: dict[str, object] = {}
    real_block = board.block

    def block_spy(identity, reason, kind):
        observed["worker_pid_at_block"] = board.task_row(task_id)["worker_pid"]
        observed["run_pid_at_block"] = _run_pid(
            board, task_id, identity.run_id
        )
        return real_block(identity, reason, kind)

    monkeypatch.setattr(board, "block", block_spy)

    identity, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=SETTINGS
    )

    assert outcome.exit_code == 1
    assert outcome.category == "code_failure", outcome.reason
    assert outcome.terminal == "block"
    assert clear_calls == [(task_id, identity.run_id)]
    # The board was already clear when the terminal write ran.
    assert observed["worker_pid_at_block"] is None
    assert observed["run_pid_at_block"] is None
    row = board.task_row(task_id)
    assert row["status"] == "blocked"
    assert row["worker_pid"] is None
    assert row["worker_started_at"] is None
    assert _run_pid(board, task_id, identity.run_id) is None


class _ExplodingClassifier:
    """Delegate to ClaudeAdapter but fail after the executor has exited."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def classify(self, *args, **kwargs):
        raise RuntimeError("classify boom")


def test_abnormal_exit_path_clears_worker_pid(
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    success_body,
    claim_and_run,
    monkeypatch,
):
    """An exception after the executor exited still clears before the block."""

    repo = make_repo()
    executor = fake_executor(success_body)
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="w13 abnormal")
    write_card(config.work_root, task_id, allow=("RESULT.txt",), commands=PASS_COMMAND)
    card = _load_card(config, task_id)

    clear_calls: list[tuple[str, int]] = []
    real_clear = board.clear_worker

    def clear_spy(identity):
        clear_calls.append((identity.task_id, identity.run_id))
        return real_clear(identity)

    monkeypatch.setattr(board, "clear_worker", clear_spy)

    _, outcome = claim_and_run(
        config=config,
        card=card,
        task_id=task_id,
        settings=SETTINGS,
        executor=_ExplodingClassifier(ClaudeAdapter()),
    )

    assert outcome.category == "environment_failure"
    assert outcome.terminal == "block"
    assert "classify boom" in outcome.reason
    assert clear_calls == [(task_id, outcome.run_id)]
    row = board.task_row(task_id)
    assert row["status"] == "blocked"
    assert row["worker_pid"] is None
    assert row["worker_started_at"] is None
    assert _run_pid(board, task_id, outcome.run_id) is None


@pytest.mark.parametrize("fence", ["claim_lock", "current_run_id"])
def test_clear_worker_lost_claim_writes_nothing(board, make_task, fence):
    """A stale run clears neither the task row nor its ``task_runs`` mirror.

    Falsifies both fences independently: mutate the claim lock (run id still
    matches) or advance ``current_run_id`` (claim lock still matches).  Drop
    either predicate from the task UPDATE and that case clears the row, so
    ``cleared``/the dead pid assertions go red.
    """

    task_id = make_task(title="w13 lost claim")
    identity = board.claim(task_id)
    assert identity is not None
    dead_pid = _dead_pid()
    board.record_worker(identity, dead_pid)
    assert board.task_row(task_id)["worker_pid"] == dead_pid
    assert _run_pid(board, task_id, identity.run_id) == dead_pid

    with board.connection:
        if fence == "claim_lock":
            board.connection.execute(
                "UPDATE tasks SET claim_lock = ? WHERE id = ?",
                ("someone-else:1", task_id),
            )
        else:
            board.connection.execute(
                "UPDATE tasks SET current_run_id = current_run_id + 1000 WHERE id = ?",
                (task_id,),
            )

    assert board.clear_worker(identity) is False

    row = board.task_row(task_id)
    assert row["worker_pid"] == dead_pid
    assert row["worker_started_at"] is not None
    assert _run_pid(board, task_id, identity.run_id) == dead_pid


def test_clear_worker_failure_blocks_without_gate(
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    success_body,
    claim_and_run,
    monkeypatch,
):
    """A DB failure while clearing routes to block and never reaches the gate."""

    repo = make_repo()
    executor = fake_executor(success_body)
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="w13 clear failure")
    write_card(config.work_root, task_id, allow=("RESULT.txt",), commands=PASS_COMMAND)
    card = _load_card(config, task_id)

    gate_calls: list[int] = []

    def gate_spy(*args, **kwargs):
        gate_calls.append(1)
        raise AssertionError("the gate must not run after a failed pid clear")

    monkeypatch.setattr(supervisor_module, "run_gate", gate_spy)

    def clear_boom(identity):
        raise RuntimeError("db down")

    monkeypatch.setattr(board, "clear_worker", clear_boom)

    _, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=SETTINGS
    )

    assert gate_calls == []
    assert outcome.category == "environment_failure"
    assert outcome.terminal == "block"
    assert "worker-pid-clear:RuntimeError" in outcome.reason
    assert board.task_row(task_id)["status"] == "blocked"


def test_executor_pid_stays_published_while_running(
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    success_body,
    claim_and_run,
    monkeypatch,
):
    """The pid must survive heartbeats: it is the crash-recovery window.

    Clearing any earlier than after the executor exits (e.g. right after
    ``record_worker``) makes the in-flight snapshot ``None`` and this red.
    """

    repo = make_repo()
    executor = fake_executor("time.sleep(0.6)\n" + success_body)
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="w13 live pid")
    write_card(config.work_root, task_id, allow=("RESULT.txt",), commands=PASS_COMMAND)
    card = _load_card(config, task_id)

    settings = RunSettings(
        max_runtime_seconds=30,
        heartbeat_seconds=0.1,
        poll_seconds=0.02,
        grace_seconds=1,
        kill_grace_seconds=1,
    )

    snapshots: list[object] = []
    real_heartbeat = board.heartbeat

    def heartbeat_spy(identity):
        snapshots.append(board.task_row(identity.task_id)["worker_pid"])
        return real_heartbeat(identity)

    monkeypatch.setattr(board, "heartbeat", heartbeat_spy)

    _, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=settings
    )

    assert outcome.category == "success", outcome.reason
    assert snapshots, "a heartbeat must fire while the executor is running"
    assert all(pid is not None for pid in snapshots)
    assert board.task_row(task_id)["worker_pid"] is None
