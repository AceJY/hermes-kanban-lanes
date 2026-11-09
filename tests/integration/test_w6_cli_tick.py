"""W6 D2/W6-04: one CLI tick and a preflight failure that never writes."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"


def _env() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC_DIR)
    return environment


def _run(config_path: Path, *args: str, timeout: float = 60.0):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_kanban_lanes.cli",
            *args,
            "--config",
            str(config_path),
        ],
        env=_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


def _write(tmp_path: Path, config) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_tick_runs_once_and_prints_the_daemon_report(tmp_path, board, make_repo, make_config, fake_executor):
    config = make_config(
        board_path=board.board_path,
        repo_path=make_repo(),
        executor_argv=[fake_executor("")],
    )
    config_path = _write(tmp_path, config)

    result = _run(config_path, "tick", "--json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["tick"] == 0
    assert report["errors"] == []
    assert report["claimed"] == []
    assert report["runs"] == []
    assert result.stderr == ""

    log_lines = (
        Path(config.work_root) / "logs" / "daemon.log"
    ).read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["tick"] == 0
    assert not (Path(config.work_root) / "daemon.lock").exists()


def test_tick_allows_dirty_repository(
    tmp_path, board, make_repo, make_config, fake_executor
):
    """W9: working-tree WIP must not block a tick or write the board."""

    repo = make_repo()
    (repo / "WIP.txt").write_text("wip\n", encoding="utf-8")
    config = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[fake_executor("")],
    )
    config_path = _write(tmp_path, config)
    board_path = Path(board.board_path)
    before_hash = _sha(board_path)

    result = _run(config_path, "tick", "--json")

    assert result.returncode == 0, result.stderr
    assert "repository-dirty" not in result.stderr
    report = json.loads(result.stdout)
    assert report["claimed"] == []
    assert report["runs"] == []
    assert report["errors"] == []
    # Read-only on both the board and the team checkout (WIP left untouched).
    assert _sha(board_path) == before_hash
    assert (repo / "WIP.txt").read_text(encoding="utf-8") == "wip\n"
    assert not (Path(config.work_root) / "daemon.lock").exists()


def test_preflight_failure_exits_two_and_never_touches_the_board(
    tmp_path, board, make_repo, make_config, fake_executor, make_task, task_status
):
    task_id = make_task(assignee="hkl:claude")
    config = make_config(
        board_path=board.board_path,
        repo_path=make_repo(),
        executor_argv=[fake_executor("")],
        hermes_commit_override="b" * 40,
    )
    config_path = _write(tmp_path, config)
    board_path = Path(board.board_path)
    before_hash = _sha(board_path)
    before_status = task_status(task_id)
    work_root = Path(config.work_root)
    before_entries = sorted(
        str(path.relative_to(work_root)) for path in work_root.rglob("*")
    )

    result = _run(config_path, "tick", "--json")

    assert result.returncode == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert "hermes-commit-mismatch" in payload["errors"]
    assert _sha(board_path) == before_hash
    assert task_status(task_id) == before_status
    assert task_status(task_id)["status"] == "ready"
    after_entries = sorted(
        str(path.relative_to(work_root)) for path in work_root.rglob("*")
    )
    assert after_entries == before_entries
    assert not (work_root / "daemon.lock").exists()


def test_missing_board_preflight_exits_two(tmp_path, board, make_repo, make_config, fake_executor):
    config = make_config(
        board_path=board.board_path,
        repo_path=make_repo(),
        executor_argv=[fake_executor("")],
        board_path_override=tmp_path / "missing.db",
    )
    config_path = _write(tmp_path, config)
    result = _run(config_path, "tick")
    assert result.returncode == 2
    payload = json.loads(result.stderr)
    assert any("board-missing" in error for error in payload["errors"])
    assert not (tmp_path / "missing.db").exists()


def test_invalid_config_exits_two_with_json_error(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    result = _run(path, "tick")
    assert result.returncode == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert "invalid JSON" in payload["error"]
    assert "Traceback" not in result.stderr
