"""W6-07 live: real macOS launchd lifecycle on a temporary user label.

Opt-in only: ``HKL_LIVE_SERVICE=1`` and macOS.  The test creates a temporary
work_root, board, repository, unit label and config, installs a *user-level*
agent, checks status/restart, then uninstalls and verifies both the unit file
and the launchd job are gone.  It never touches the engineering board, a
production service or another user's agent.

Run it from an interpreter where this distribution is installed (the rendered
unit executes ``<python> -m hermes_kanban_lanes.cli run``), for example a clean
wheel venv created with ``--system-site-packages`` so pytest stays available.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from hermes_kanban_lanes.config import LaneConfig
from hermes_kanban_lanes.runtime.service import (
    RunnerResult,
    default_runner,
    install_service,
    render_unit,
    restart_service,
    service_status,
    uninstall_service,
    user_unit_path,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("HKL_LIVE_SERVICE") != "1" or sys.platform != "darwin",
    reason="live launchd lifecycle is opt-in (HKL_LIVE_SERVICE=1, macOS)",
)


def _uid() -> int:
    return os.getuid()


def _print_job(label: str) -> RunnerResult:
    return default_runner(["launchctl", "print", f"gui/{_uid()}/{label}"])


def _unit_path(config: LaneConfig, config_path: Path) -> Path:
    unit = render_unit("launchd", config, str(config_path))
    return user_unit_path("launchd", unit)


@pytest.fixture
def live_lane(tmp_path, hermes_source, hermes_commit, make_repo):
    from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter

    board_path = tmp_path / "board.db"
    BoardAdapter.create_board(board_path, hermes_source).close()
    repo = make_repo()
    executor = tmp_path / "fake-claude"
    executor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executor.chmod(0o755)
    label = f"com.hermes-kanban-lanes.live.{uuid.uuid4().hex[:8]}"
    work_root = tmp_path / "work"
    work_root.mkdir()
    document = {
        "schema_version": 1,
        "hermes_source": hermes_source,
        "hermes_commit": hermes_commit,
        "boards": {"live": str(board_path)},
        "repositories": {"primary": {"path": str(repo), "trusted": True}},
        "executors": {"claude": {"argv": [str(executor)]}},
        "work_root": str(work_root),
        "max_concurrent": 1,
        "service": {
            "platform": "launchd",
            "label": label,
            "log_root": str(work_root / "logs"),
            "restart": "never",
        },
        "logging": {"retention_days": 1, "backups": 1},
    }
    config = LaneConfig.from_dict(document)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
    return config, config_path, label, work_root


def test_live_launchd_install_status_restart_uninstall(live_lane):
    from hermes_kanban_lanes.runtime.daemon import preflight

    config, config_path, label, work_root = live_lane
    # Fail closed before touching launchd: a preflight failure would make the
    # agent exit immediately.
    assert preflight(config) == []

    installed = False
    try:
        result = install_service("launchd", config, str(config_path))
        installed = True
        assert result["ok"] is True, result
        target = _unit_path(config, config_path)
        assert target.is_file()
        assert Path(result["unit_path"]) == target

        status = service_status("launchd", config, str(config_path))
        assert status["installed"] is True
        assert status["running"] is True, status

        restarted = restart_service("launchd", config, str(config_path))
        assert restarted["ok"] is True, restarted

        deadline = time.monotonic() + 30
        log_path = work_root / "logs" / "daemon.log"
        while time.monotonic() < deadline and not log_path.is_file():
            time.sleep(0.25)
        assert log_path.is_file(), "the installed daemon must run one tick"
        assert (work_root / "daemon.lock").is_file()
    finally:
        if installed:
            uninstall_service("launchd", config, str(config_path))
        else:
            default_runner(["launchctl", "bootout", f"gui/{_uid()}/{label}"])

    assert not _unit_path(config, config_path).exists()
    assert _print_job(label).returncode != 0, "launchd job must be gone"
    status = service_status("launchd", config, str(config_path))
    assert status["installed"] is False
    # Uninstall keeps run evidence and user repositories.
    assert work_root.is_dir()
