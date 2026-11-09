"""D10: ``run_command`` proves the process group empty before returning."""

from __future__ import annotations

import os
import sys

import pytest

from hermes_kanban_lanes.verification import commands
from hermes_kanban_lanes.verification.commands import (
    CommandGroupError,
    run_command,
)

PYTHON = sys.executable


def _group_empty(pgid: int) -> bool:
    try:
        os.killpg(int(pgid), 0)
    except ProcessLookupError:
        return True
    return False


def _spawn_body(pid_file: str, *, ignore_term: bool = False) -> str:
    """A leader that records the group and exits while a descendant lives."""

    setup = "import signal;" if ignore_term else "import signal;"
    ignore = (
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        if ignore_term
        else ""
    )
    return (
        "import os,pathlib,subprocess,sys;"
        + setup
        + f"pathlib.Path({pid_file!r}).write_text(str(os.getpid()), encoding='utf-8');"
        "child=subprocess.Popen("
        "[sys.executable,'-c',"
        f"'import signal,time;{ignore}time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path({pid_file!r}).write_text("
        "f'{os.getpid()} {child.pid}', encoding='utf-8');"
        "os._exit(0)"
    )


def test_leader_exits_but_descendant_is_killed_before_return(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "GROUP_TERM_SECONDS", 1.0)
    monkeypatch.setattr(commands, "GROUP_KILL_SECONDS", 1.0)
    pid_file = tmp_path / "pids"
    log = tmp_path / "command.log"

    result = run_command(
        [PYTHON, "-c", _spawn_body(str(pid_file))],
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=log,
    )

    assert result.exit_code == 0
    assert result.timed_out is False
    pgid, grandchild = (int(part) for part in pid_file.read_text().split())
    # No post-hoc waiting: the proof must already hold at return time.
    assert _group_empty(pgid), "run_command returned with a live group member"
    assert not _alive(grandchild)
    assert log.is_file()


def test_sigterm_resistant_descendant_is_still_gone_at_return(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(commands, "GROUP_TERM_SECONDS", 0.2)
    monkeypatch.setattr(commands, "GROUP_KILL_SECONDS", 1.0)
    pid_file = tmp_path / "pids"

    result = run_command(
        [PYTHON, "-c", _spawn_body(str(pid_file), ignore_term=True)],
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "command.log",
    )

    assert result.exit_code == 0
    pgid, grandchild = (int(part) for part in pid_file.read_text().split())
    assert _group_empty(pgid), "SIGTERM-resistant descendant survived the return"
    assert not _alive(grandchild)


def test_unknown_group_state_is_an_explicit_failure_and_keeps_logs(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(commands, "GROUP_TERM_SECONDS", 0.01)
    monkeypatch.setattr(commands, "GROUP_KILL_SECONDS", 0.01)
    monkeypatch.setattr(
        commands, "_group_status", lambda pgid: (True, "permission-denied")
    )
    log = tmp_path / "command.log"

    with pytest.raises(CommandGroupError) as caught:
        run_command(
            [PYTHON, "-c", "print('kept')"],
            cwd=tmp_path,
            env=dict(os.environ),
            log_path=log,
        )

    assert caught.value.reason == "permission-denied"
    assert caught.value.result.exit_code == 0
    assert "kept" in log.read_text(encoding="utf-8")
    assert caught.value.result.timed_out is False


def _alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    return True
