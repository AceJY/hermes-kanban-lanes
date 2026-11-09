"""Shared fixtures for W2 board/repository/executor tests.

These fixtures only build **temporary** boards and repositories.  They never
touch the real engineering board or a product repository.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
# No machine-specific checkout is baked in: point the suite at a host source
# with ``HKL_HERMES_SOURCE``, otherwise derive it from the installed
# ``hermes_cli`` package (the Hermes source tree it was imported from).
DEFAULT_HERMES_SOURCE = ""


def _resolve_hermes_source(configured: str) -> Path | None:
    if configured:
        return Path(configured)
    try:
        import hermes_cli  # noqa: PLC0415 - optional host dependency
    except ImportError:
        return None
    module = getattr(hermes_cli, "__file__", None)
    return Path(module).resolve().parent.parent if module else None


HERMES_SOURCE = _resolve_hermes_source(
    os.environ.get("HKL_HERMES_SOURCE", DEFAULT_HERMES_SOURCE)
)
PYTHON = sys.executable


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="session")
def hermes_source() -> str:
    if HERMES_SOURCE is None or not (
        HERMES_SOURCE / "hermes_cli" / "kanban_db.py"
    ).is_file():
        pytest.skip("hermes source not available; set HKL_HERMES_SOURCE")
    return str(HERMES_SOURCE)


@pytest.fixture(scope="session")
def hermes_commit(hermes_source: str) -> str:
    result = _git("rev-parse", "HEAD", cwd=Path(hermes_source))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def board(tmp_path, hermes_source):
    from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter

    path = tmp_path / "board.db"
    adapter = BoardAdapter.create_board(path, hermes_source)
    try:
        yield adapter
    finally:
        adapter.close()


@pytest.fixture
def fresh_adapter(hermes_source):
    """Open a *new* connection to the same board (race/restart tests)."""
    from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter

    opened: list = []

    def _open(board_path) -> "BoardAdapter":
        adapter = BoardAdapter(board_path, hermes_source)
        adapter.open()
        opened.append(adapter)
        return adapter

    yield _open
    for adapter in opened:
        adapter.close()


@pytest.fixture
def make_task(board):
    """Create a ready task assigned to an ``hkl:<alias>`` profile."""

    def _make(
        *,
        title: str = "test task",
        body: str = "do the thing",
        assignee: str = "hkl:claude",
        initial_status: str | None = None,
    ) -> str:
        kwargs = {}
        if initial_status is not None:
            kwargs["initial_status"] = initial_status
        return board._kb.create_task(  # noqa: SLF001 - test-only helper
            board.connection,
            title=title,
            body=body,
            assignee=assignee,
            **kwargs,
        )

    return _make


@pytest.fixture
def make_repo(tmp_path):
    """Create a clean git repository with one initial commit."""

    def _make(name: str = "repo") -> Path:
        path = tmp_path / name
        path.mkdir()
        _git("init", "-q", "-b", "main", cwd=path)
        _git("config", "user.email", "hkl@example.invalid", cwd=path)
        _git("config", "user.name", "HKL Tests", cwd=path)
        (path / "README.txt").write_text("base\n", encoding="utf-8")
        _git("add", "README.txt", cwd=path)
        commit = _git("commit", "-q", "-m", "base", cwd=path)
        assert commit.returncode == 0, commit.stderr
        return path

    return _make


@pytest.fixture
def make_config(tmp_path, hermes_source, hermes_commit):
    from hermes_kanban_lanes.config import LaneConfig

    def _make(
        *,
        board_path,
        repo_path,
        executor_argv,
        work_root=None,
        max_concurrent: int = 1,
        hermes_source_override: str | None = None,
        hermes_commit_override: str | None = None,
        board_path_override=None,
        repo_path_override=None,
        executor_argv_override=None,
        create_work_root: bool = True,
    ) -> LaneConfig:
        root = Path(work_root) if work_root is not None else tmp_path / "work"
        if create_work_root:
            root.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": 1,
            "hermes_source": hermes_source_override or hermes_source,
            "hermes_commit": hermes_commit_override or hermes_commit,
            "boards": {
                "test": str(board_path_override or board_path),
            },
            "repositories": {
                "primary": {
                    "path": str(repo_path_override or repo_path),
                    "trusted": True,
                }
            },
            "executors": {
                "claude": {
                    "argv": [str(part) for part in (executor_argv_override or executor_argv)]
                }
            },
            "work_root": str(root),
            "max_concurrent": max_concurrent,
        }
        return LaneConfig.from_dict(document)

    return _make


@pytest.fixture
def write_card(tmp_path):
    """Write a W1 CardSpec under ``<work_root>/cards/<task_id>.json``."""

    def _write(
        work_root,
        task_id: str,
        *,
        executor: str = "claude",
        repository: str = "primary",
        allow=("RESULT.txt",),
        commands=None,
        card_kind: str = "implementation",
        lineage: str | None = None,
        closure_for=None,
        risk: str = "normal",
    ) -> Path:
        directory = Path(work_root) / "cards"
        directory.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": 1,
            "card_kind": card_kind,
            "lineage": lineage if lineage is not None else task_id,
            "closure_for": closure_for,
            "executor": executor,
            "repository": repository,
            "allow": list(allow),
            "commands": commands
            if commands is not None
            else [{"argv": [PYTHON, "-c", "pass"], "cwd": "."}],
            "risk": risk,
        }
        path = directory / f"{task_id}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def fake_executor(tmp_path):
    """Create an executable fake executor with a ``--version`` fast path."""

    counter = {"n": 0}

    def _make(body: str, *, name: str | None = None) -> str:
        counter["n"] += 1
        path = tmp_path / (name or f"fake-exec-{counter['n']}")
        script = (
            f"#!{PYTHON}\n"
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            # The test interpreter is the Hermes venv, which has ``hermes_cli``
            # installed.  Re-exec without site-packages so the fake models an
            # independent executor binary rather than the host interpreter.
            "if os.environ.get('HKL_FAKE_NO_SITE') != '1':\n"
            "    os.execve(sys.executable,\n"
            "              [sys.executable, '-S', os.path.abspath(__file__), *sys.argv[1:]],\n"
            "              {**os.environ, 'HKL_FAKE_NO_SITE': '1'})\n"
            "os.environ.pop('HKL_FAKE_NO_SITE', None)\n"
            "if any(part == '--version' for part in sys.argv[1:]):\n"
            "    print('fake-exec 1.0')\n"
            "    raise SystemExit(0)\n"
            + textwrap.dedent(body).lstrip()
        )
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    return _make


@pytest.fixture
def success_body():
    return """
Path("RESULT.txt").write_text("ok\\n", encoding="utf-8")
print(json.dumps({
    "is_error": False,
    "result": "done",
    "terminal_reason": "completed",
    "usage": {"input_tokens": 10, "output_tokens": 5},
    "total_cost_usd": 0.01,
    "permission_denials": [{"tool_name": "Bash", "reason": "test"}],
}))
"""


@pytest.fixture
def load_run_json():
    def _load(run_dir) -> dict:
        return json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def claim_and_run(board):
    """Claim an existing ready task, then run one supervised run."""

    def _run(
        *,
        config,
        card,
        task_id: str,
        settings=None,
        stop_event=None,
        executor=None,
        members_fn=None,
    ):
        from hermes_kanban_lanes.executors.claude import ClaudeAdapter
        from hermes_kanban_lanes.runtime.supervisor import RunSettings, run_once

        identity = board.claim(task_id)
        assert identity is not None, "claim unexpectedly lost"
        row = board.connection.execute(
            "SELECT id, title, body FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        task = dict(row)
        kwargs = {}
        if members_fn is not None:
            kwargs["members_fn"] = members_fn
        outcome = run_once(
            config=config,
            adapter=board,
            board_alias="test",
            task=task,
            card=card,
            identity=identity,
            executor=executor or ClaudeAdapter(),
            settings=settings or RunSettings(
                max_runtime_seconds=30,
                heartbeat_seconds=30,
                poll_seconds=0.05,
                grace_seconds=1,
                kill_grace_seconds=1,
            ),
            stop_event=stop_event,
            **kwargs,
        )
        return identity, outcome

    return _run


@pytest.fixture
def task_status(board):
    def _status(task_id: str) -> dict:
        row = board.connection.execute(
            "SELECT status, current_run_id, claim_lock, block_kind FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        return dict(row) if row else {}

    return _status


def pytest_collection_modifyitems(config, items):
    """Skip tests that read the private evidence archive when it is absent.

    ``artifacts/`` holds private run evidence and is deliberately excluded from
    the public export (see ``tools/public_export.py``).  The archive-backed
    assertions still run everywhere the archive exists; only an export tree
    skips them, and the skip is reported by pytest.
    """

    if (REPO_ROOT / "artifacts" / "w6" / "receipt.json").is_file():
        return
    skip = pytest.mark.skip(reason="private W6 evidence archive is not exported")
    for item in items:
        path = getattr(item, "path", None)
        if path is not None and Path(str(path)).name == "test_w6_receipt.py":
            item.add_marker(skip)
