"""W6 D4/W6-05: single-instance lock across real processes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"


@pytest.fixture
def lane(tmp_path, board, make_repo, make_config, fake_executor):
    repo = make_repo()
    config = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[fake_executor("")],
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
    return config, config_path


def _env() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC_DIR)
    return environment


def _command(config_path: Path, *args: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "hermes_kanban_lanes.cli",
        *args,
        "--config",
        str(config_path),
    ]


def _run(config_path: Path, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        _command(config_path, *args),
        env=_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


def _start_run(config_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        _command(config_path, "run", "--poll-seconds", "0.25"),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
    )


def _wait_for(predicate, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached before timeout")


def _wait_until_running(config, process: subprocess.Popen) -> None:
    work_root = Path(config.work_root)
    _wait_for(
        lambda: process.poll() is None
        and (work_root / "daemon.lock").is_file()
        and (work_root / "logs" / "daemon.log").is_file()
    )


def test_second_run_exits_three_and_reports_holder(lane):
    config, config_path = lane
    first = _start_run(config_path)
    try:
        _wait_until_running(config, first)
        second = _run(config_path, "run")
        assert second.returncode == 3
        assert second.stdout == ""
        payload = json.loads(second.stderr)
        assert payload["error"] == "daemon-lock-held"
        assert payload["holder"]["pid"] == first.pid
        assert first.poll() is None
    finally:
        first.terminate()
        stdout, stderr = first.communicate(timeout=60)
    assert first.returncode == 0, stderr
    assert not (Path(config.work_root) / "daemon.lock").exists()


def test_tick_is_rejected_while_run_holds_the_lock(lane):
    config, config_path = lane
    first = _start_run(config_path)
    try:
        _wait_until_running(config, first)
        tick = _run(config_path, "tick")
        assert tick.returncode == 3
        payload = json.loads(tick.stderr)
        assert payload["error"] == "daemon-lock-held"
        assert payload["holder"]["pid"] == first.pid
    finally:
        first.terminate()
        first.communicate(timeout=60)


def test_sigkill_leaves_a_stale_lock_that_the_next_tick_reclaims(lane):
    config, config_path = lane
    first = _start_run(config_path)
    _wait_until_running(config, first)
    first.send_signal(signal.SIGKILL)
    first.communicate(timeout=60)
    lock_path = Path(config.work_root) / "daemon.lock"
    assert lock_path.is_file(), "SIGKILL must leave the lock document for reclaim"

    tick = _run(config_path, "tick")
    assert tick.returncode == 0, tick.stderr
    report = json.loads(tick.stdout)
    assert report["errors"] == []
    assert not lock_path.exists()

    records = [
        json.loads(line)
        for line in (Path(config.work_root) / "logs" / "daemon.log")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    stale = [record for record in records if record.get("record") == "daemon-lock"]
    assert stale, "stale reclaim must be recorded in daemon.log"
    assert stale[-1]["stale_reclaimed"] is True
    assert stale[-1]["previous_pid"] is not None


def test_sequential_ticks_release_the_lock(lane):
    config, config_path = lane
    for _ in range(2):
        result = _run(config_path, "tick")
        assert result.returncode == 0, result.stderr
        assert not (Path(config.work_root) / "daemon.lock").exists()
