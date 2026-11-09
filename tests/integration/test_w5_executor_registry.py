"""W5-06/W5-09/W5-10: registry, fail-closed preflight and shared seams.

Everything runs on a temporary board and repository.  The Codex binary is a
fake executable that emits the measured JSONL shape; no real Codex is spawned.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.config import Executor
from hermes_kanban_lanes.executors import (
    ADAPTERS,
    ClaudeAdapter,
    CodexAdapter,
    adapter_for,
)
from hermes_kanban_lanes.hermes_adapter.board import BoardAdapterError
from hermes_kanban_lanes.runtime import daemon
from hermes_kanban_lanes.runtime.supervisor import RunSettings
from hermes_kanban_lanes.workspace.git import repo_head

SETTINGS = RunSettings(
    max_runtime_seconds=30,
    heartbeat_seconds=30,
    poll_seconds=0.05,
    grace_seconds=1,
    kill_grace_seconds=1,
)
THREAD_ID = "thread-codex-integration"


def _codex_body() -> str:
    return f"""
Path("RESULT.txt").write_text("ok\\n", encoding="utf-8")
print(json.dumps({{"type": "thread.started", "thread_id": {THREAD_ID!r}}}))
print(json.dumps({{"type": "turn.started"}}))
print(json.dumps({{"type": "item.completed",
                  "item": {{"id": "item_0", "type": "agent_message", "text": "done"}}}}))
print(json.dumps({{"type": "turn.completed",
                  "usage": {{"input_tokens": 10, "cached_input_tokens": 2,
                            "cache_write_input_tokens": 0, "output_tokens": 5,
                            "reasoning_output_tokens": 1}}}}))
"""


def _with_executor(config, alias: str, argv):
    executors = dict(config.executors)
    executors[alias] = Executor(argv=tuple(str(part) for part in argv))
    return replace(config, executors=executors)


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path, monkeypatch):
    """Deterministic credential path: CODEX_HOME wins, no host API key."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    home = tmp_path / "operator" / ".codex"
    home.mkdir(parents=True)
    (home / "auth.json").write_text(
        '{"sentinel": "ACCOUNT-STORE-SENTINEL"}', encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(home))


# ---------------------------------------------------------------------------
# W5-06: registry
# ---------------------------------------------------------------------------


def test_registry_is_the_single_source_of_truth():
    assert ADAPTERS == {"claude": ClaudeAdapter, "codex": CodexAdapter}
    assert isinstance(adapter_for("claude"), ClaudeAdapter)
    assert isinstance(adapter_for("codex"), CodexAdapter)
    assert isinstance(daemon.executor_for("codex"), CodexAdapter)
    assert daemon._executor_alias_supported("claude") is True
    assert daemon._executor_alias_supported("codex") is True
    assert daemon._executor_alias_supported("gemini") is False


@pytest.mark.parametrize("alias", ["gemini", "codex-2", "", "Claude"])
def test_unknown_alias_fails_closed(alias):
    with pytest.raises(BoardAdapterError) as excinfo:
        daemon.executor_for(alias)
    assert str(excinfo.value) == f"executor-not-supported:{alias}"


def test_codex_missing_binary_fails_preflight_without_claiming(
    tmp_path, board, make_task, make_repo, make_config, write_card, task_status
):
    repo = make_repo()
    missing = str(tmp_path / "no-such-codex")
    config = _with_executor(
        make_config(board_path=board.board_path, repo_path=repo, executor_argv=["claude"]),
        "codex",
        [missing],
    )
    task_id = make_task(title="missing codex", assignee="hkl:codex")
    write_card(config.work_root, task_id, executor="codex")

    errors = daemon.preflight(config)
    assert "executor:codex:executor-not-resolvable" in errors
    # Preflight is read-only: the ready task was never claimed.
    assert task_status(task_id)["status"] == "ready"
    assert task_status(task_id)["current_run_id"] is None


@pytest.mark.parametrize(
    "extra",
    [
        ("--sandbox", "danger-full-access"),
        ("--dangerously-bypass-approvals-and-sandbox",),
        ("-s", "workspace-write"),
        ("-c", 'sandbox_mode="danger-full-access"'),
    ],
)
def test_dangerous_codex_argv_is_rejected_by_preflight(
    tmp_path, board, make_repo, make_config, fake_executor, extra
):
    repo = make_repo()
    binary = fake_executor('raise SystemExit(0)\n')
    config = _with_executor(
        make_config(board_path=board.board_path, repo_path=repo, executor_argv=["claude"]),
        "codex",
        [binary, *extra],
    )
    assert "executor:codex:executor-dangerous-arg" in daemon.preflight(config)


# ---------------------------------------------------------------------------
# W5-09: settings file and executor_session_id seam
# ---------------------------------------------------------------------------


def test_claude_run_keeps_claude_settings_and_null_session_id(
    board, make_task, make_repo, make_config, write_card, claim_and_run, fake_executor, success_body, load_run_json
):
    repo = make_repo()
    executor = fake_executor(success_body)
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="claude seam")
    card = load_card(write_card(config.work_root, task_id), config)
    identity, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=SETTINGS
    )
    assert outcome.category == "success", outcome.reason
    run_dir = Path(outcome.run_dir)
    assert (run_dir / "claude-settings.json").is_file()
    assert not (run_dir / "codex-settings.json").exists()
    document = load_run_json(run_dir)
    assert document["executor"]["name"] == "claude"
    assert "executor_session_id" in document["executor"]
    assert document["executor"]["executor_session_id"] is None


def test_codex_run_writes_codex_settings_and_records_thread_id(
    tmp_path, board, make_task, make_repo, make_config, write_card, claim_and_run, fake_executor, load_run_json
):
    repo = make_repo()
    executor = fake_executor(_codex_body())
    config = _with_executor(
        make_config(board_path=board.board_path, repo_path=repo, executor_argv=["claude"]),
        "codex",
        [executor],
    )
    task_id = make_task(title="codex seam", assignee="hkl:codex")
    card = load_card(
        write_card(config.work_root, task_id, executor="codex"), config
    )
    identity, outcome = claim_and_run(
        config=config,
        card=card,
        task_id=task_id,
        settings=SETTINGS,
        executor=CodexAdapter(),
    )
    assert outcome.category == "success", outcome.reason
    assert outcome.terminal == "request_review"
    run_dir = Path(outcome.run_dir)
    assert (run_dir / "codex-settings.json").is_file()
    assert not (run_dir / "claude-settings.json").exists()
    settings = json.loads(
        (run_dir / "codex-settings.json").read_text(encoding="utf-8")
    )
    assert settings["executor"] == "codex"
    assert settings["sandbox"] == "workspace-write"
    assert settings["codex_home"]
    # D4/W5-05: no account store content or copy reaches the run directory.
    assert not any(path.name == "auth.json" for path in run_dir.rglob("*"))
    assert "ACCOUNT-STORE-SENTINEL" not in json.dumps(settings)

    document = load_run_json(run_dir)
    assert document["executor"]["name"] == "codex"
    assert document["executor"]["executor_session_id"] == THREAD_ID
    assert document["executor"]["credential_keys"] == ["CODEX_HOME"]
    assert document["executor"]["credential_reason"] is None
    assert document["usage"]["input_tokens"] == 10
    assert document["usage"]["output_tokens"] == 5
    assert document["usage"]["cost_usd"] is None
    # The repo never moved during a development run.
    assert repo_head(repo) == document["base_commit"]
