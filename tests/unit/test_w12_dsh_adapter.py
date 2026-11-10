"""W12: DshAdapter shapes, registration and fail-closed configuration.

The adapter is exercised in-process only.  ``--version`` uses a tiny executable
script so the suite never spawns the real ``dsh`` CLI.
"""

from __future__ import annotations

import json
import stat

import pytest

from hermes_kanban_lanes.card_contract import CardSpec
from hermes_kanban_lanes.config import LaneConfig
from hermes_kanban_lanes.contracts import ContractError
from hermes_kanban_lanes.executors import ADAPTERS, DshAdapter, adapter_for
from hermes_kanban_lanes.executors.dsh import SHELL_OPERATORS


def _executable(tmp_path, name: str = "dsh") -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _config(*, executors=None) -> LaneConfig:
    return LaneConfig.from_dict(
        {
            "schema_version": 1,
            "hermes_source": "/srv/hermes/source",
            "hermes_commit": "a" * 40,
            "boards": {"engineering": "/srv/hermes/kanban/engineering.db"},
            "repositories": {
                "primary": {"path": "/srv/hermes/repos/primary", "trusted": True}
            },
            "executors": executors or {"claude": {"argv": ["claude"]}},
            "work_root": "/srv/hermes/work",
            "max_concurrent": 2,
        }
    )


def _card(**overrides):
    data = {
        "schema_version": 1,
        "card_kind": "implementation",
        "lineage": "lineage-1",
        "closure_for": None,
        "executor": "claude",
        "repository": "primary",
        "allow": ["src/", "tests/"],
        "commands": [{"argv": ["pytest", "-q"], "cwd": "."}],
        "risk": "normal",
        "artifacts": [],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_dsh_is_registered_and_instantiated():
    assert ADAPTERS["dsh"] is DshAdapter
    adapter = adapter_for("dsh")
    assert isinstance(adapter, DshAdapter)
    assert adapter.name == "dsh"


def test_unknown_alias_still_rejected():
    with pytest.raises(KeyError):
        adapter_for("other")


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def test_preflight_accepts_the_dsh_binary(tmp_path):
    binary = _executable(tmp_path)
    assert DshAdapter().preflight([binary]) == (True, "ok")


def test_preflight_rejects_operator_options(tmp_path):
    # Only the launcher is configurable; the adapter owns the rest of argv.
    binary = _executable(tmp_path)
    assert DshAdapter().preflight([binary, "--model", "x"]) == (
        False,
        "executor-argv-unsupported",
    )
    assert DshAdapter().preflight(["dsh", "--dump-config"]) == (
        False,
        "executor-argv-unsupported",
    )


def test_preflight_rejects_empty_argv():
    assert DshAdapter().preflight([]) == (False, "executor-argv-empty")


def test_preflight_requires_the_dsh_basename(tmp_path):
    other = _executable(tmp_path, name="not-dsh")
    assert DshAdapter().preflight([other]) == (False, "executor-wrong-binary")


def test_preflight_rejects_missing_executable(tmp_path):
    assert DshAdapter().preflight([str(tmp_path / "dsh")]) == (
        False,
        "executor-not-executable",
    )
    assert DshAdapter().preflight(["dsh-not-on-path"]) == (
        False,
        "executor-wrong-binary",
    )


@pytest.mark.parametrize("operator", SHELL_OPERATORS)
def test_preflight_rejects_shell_operators(tmp_path, operator):
    binary = _executable(tmp_path)
    assert DshAdapter().preflight([binary, f"safe{operator}else"]) == (
        False,
        "executor-shell-operator",
    )


# ---------------------------------------------------------------------------
# argv construction: fixed protocol, positional prompt, cold start
# ---------------------------------------------------------------------------


def test_build_argv_and_classify(tmp_path):
    binary = _executable(tmp_path)
    command = DshAdapter().build_argv(
        [binary],
        run_dir=str(tmp_path / "run"),
        session_id="ignored-cold-session",
        prompt="DO IT",
    )
    assert command == [binary, "--profile", "headless", "--json", "DO IT"]
    assert command[-1] == "DO IT"
    assert "--session-id" not in command
    assert "--patch" not in command

    outcome = DshAdapter().classify(0, {"result": "DO IT"})
    assert (outcome.category, outcome.block_kind) == ("success", None)
    assert outcome.is_error is False
    assert outcome.result_text == "DO IT"


def test_build_argv_keeps_bare_launcher_and_prompt_verbatim(tmp_path):
    binary = _executable(tmp_path)
    prompt = "line one\nline two  with spaces"
    command = DshAdapter().build_argv(
        [binary],
        run_dir=str(tmp_path / "run"),
        session_id="ignored",
        prompt=prompt,
    )
    assert command == [binary, "--profile", "headless", "--json", prompt]
    assert command[-1] == prompt


# ---------------------------------------------------------------------------
# NDJSON parsing
# ---------------------------------------------------------------------------


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def test_parse_session_and_last_assistant_text():
    parsed = DshAdapter().parse(
        _jsonl(
            {"type": "session", "sessionId": "session-abc", "cwd": "/work"},
            {"type": "status", "phase": "turn_start", "turn": 1},
            {"type": "text", "text": "first"},
            {"type": "text", "text": "last"},
        )
    )
    assert parsed["session_id"] == "session-abc"
    assert parsed["thread_id"] == "session-abc"
    assert parsed["result"] == "last"
    assert "usage" not in parsed


def test_parse_final_text_wins_over_assistant_text():
    parsed = DshAdapter().parse(
        _jsonl(
            {"type": "session", "sessionId": "session-1"},
            {"type": "text", "text": "draft"},
            {"type": "final", "text": "answer"},
        )
    )
    assert parsed["session_id"] == "session-1"
    assert parsed["result"] == "answer"


def test_parse_tolerates_noise():
    stdout = "\n".join(
        [
            "not json at all",
            "{broken",
            "",
            "[1, 2, 3]",
            json.dumps({"type": "text", "text": "ok"}),
        ]
    )
    parsed = DshAdapter().parse(stdout)
    assert parsed["result"] == "ok"
    assert "session_id" not in parsed


def test_parse_empty_stdout_has_empty_session_and_text():
    for stdout in ("", "   \n\n  ", None):
        parsed = DshAdapter().parse(stdout)
        assert parsed.get("session_id") is None
        assert parsed["result"] == ""


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def test_classify_nonzero_is_code_failure():
    outcome = DshAdapter().classify(1, {"result": ""}, stderr="boom")
    assert (outcome.category, outcome.block_kind) == (
        "code_failure",
        "needs_input",
    )
    assert outcome.is_error is True
    assert outcome.exit_code == 1


def test_classify_timeout_is_timeout():
    outcome = DshAdapter().classify(1, {}, timed_out=True)
    assert (outcome.category, outcome.block_kind) == ("timeout", "transient")
    assert outcome.terminal_reason == "timeout"


def test_classify_zero_without_envelope_is_success():
    # A clean exit is a success even when the stream was empty: the adapter
    # never invents a failure class it did not observe.
    for parsed in ({}, {"result": ""}):
        outcome = DshAdapter().classify(0, parsed)
        assert (outcome.category, outcome.block_kind) == ("success", None)
        assert outcome.is_error is False


# ---------------------------------------------------------------------------
# configuration and card contract
# ---------------------------------------------------------------------------


def test_config_accepts_dsh_with_assignees():
    config = _config(executors={"dsh": {"argv": ["dsh"], "assignees": ["dsh"]}})
    executor = config.executors["dsh"]
    assert executor.argv == ("dsh",)
    assert executor.effective_assignees("dsh") == ("dsh",)


def test_config_unknown_alias_still_rejected():
    with pytest.raises(ContractError) as excinfo:
        _config(executors={"other": {"argv": ["other"]}})
    assert "executors" in str(excinfo.value)


def test_card_contract_accepts_dsh_executor():
    config = _config(executors={"dsh": {"argv": ["dsh"]}})
    card = CardSpec.from_dict(_card(executor="dsh"), config)
    assert card.executor == "dsh"


def test_card_contract_still_rejects_unknown_executor():
    with pytest.raises(ContractError) as excinfo:
        CardSpec.from_dict(_card(executor="other"), _config())
    assert "executor" in str(excinfo.value)
