"""W5-01..W5-05: CodexAdapter positive and negative cases.

The adapter is exercised in-process only; no real Codex binary is spawned.  The
``--version`` path uses a tiny executable script so it never depends on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess

import pytest

from hermes_kanban_lanes.executors.codex import (
    EXEC_ONLY_WITH_VALUE,
    LAST_MESSAGE_NAME,
    PASSTHROUGH_WITH_VALUE,
    SANDBOX,
    SINGLE_VALUED,
    CodexAdapter,
)

# Real-CLI parse regression is opt-in so the default suite never spawns Codex
# (docs/11 section 8).  The free ``--help`` parse path checks argument
# placement without a model call, tokens or network.
REAL_CODEX_BIN = os.environ.get("HKL_CODEX_BIN")

SUCCESS_EVENTS = [
    {"type": "thread.started", "thread_id": "thread-abc"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "agent_message", "text": "HKL-OK"},
    },
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 25321,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 8,
            "reasoning_output_tokens": 0,
        },
    },
]


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _executable(tmp_path, name: str = "codex") -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture(autouse=True)
def _no_operator_api_key(monkeypatch):
    """Credential tests must not depend on the host's own environment."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


# ---------------------------------------------------------------------------
# W5-01: preflight rejects invalid/dangerous argv
# ---------------------------------------------------------------------------


def test_preflight_accepts_legal_passthrough(tmp_path):
    binary = _executable(tmp_path)
    adapter = CodexAdapter()
    assert adapter.preflight([binary]) == (True, "ok")
    assert adapter.preflight([binary, "-m", "gpt-5"]) == (True, "ok")
    assert (
        adapter.preflight(
            [
                binary,
                "--model",
                "gpt-5",
                "-c",
                "model_reasoning_effort=high",
                "-p",
                "work",
                "--enable",
                "feature_a",
                "--enable",
                "feature_b",
                "--disable",
                "feature_c",
                "--output-schema",
                "schema.json",
            ]
        )
        == (True, "ok")
    )


def test_preflight_rejects_empty_argv():
    assert CodexAdapter().preflight([]) == (False, "executor-argv-empty")


def test_preflight_rejects_missing_executable(tmp_path):
    adapter = CodexAdapter()
    assert adapter.preflight([str(tmp_path / "missing")]) == (
        False,
        "executor-not-executable",
    )
    assert adapter.preflight(["definitely-not-a-real-codex-binary"]) == (
        False,
        "executor-not-found",
    )


def test_preflight_rejects_unknown_argument(tmp_path):
    binary = _executable(tmp_path)
    assert CodexAdapter().preflight([binary, "--json"]) == (
        False,
        "executor-arg-not-allowed",
    )


def test_preflight_rejects_missing_and_empty_value(tmp_path):
    binary = _executable(tmp_path)
    adapter = CodexAdapter()
    assert adapter.preflight([binary, "-m"]) == (
        False,
        "executor-arg-missing-value",
    )
    assert adapter.preflight([binary, "-m", ""]) == (
        False,
        "executor-arg-empty-value",
    )


@pytest.mark.parametrize(
    "extra",
    [
        ["--sandbox", "workspace-write"],
        ["--sandbox", "danger-full-access"],
        ["-s", "workspace-write"],
        ["--dangerously-bypass-approvals-and-sandbox"],
        ["--dangerously-bypass-hook-trust"],
        ["-c", 'sandbox_mode="danger-full-access"'],
    ],
)
def test_preflight_rejects_dangerous_arguments(tmp_path, extra):
    binary = _executable(tmp_path)
    assert CodexAdapter().preflight([binary, *extra]) == (
        False,
        "executor-dangerous-arg",
    )


# ---------------------------------------------------------------------------
# W6-11 (D10): single-value duplicates and sandbox keys in ``-c/--config``
# ---------------------------------------------------------------------------


def test_preflight_rejects_duplicate_output_schema(tmp_path):
    binary = _executable(tmp_path)
    assert CodexAdapter().preflight(
        [binary, "--output-schema", "a.json", "--output-schema", "b.json"]
    ) == (False, "executor-arg-duplicate")


def test_preflight_allows_repeated_multi_value_options(tmp_path):
    binary = _executable(tmp_path)
    for flag, value in (
        ("-m", "gpt-5"),
        ("--model", "gpt-5"),
        ("-c", "model_reasoning_effort=high"),
        ("--config", "model_reasoning_effort=low"),
        ("-p", "work"),
        ("--profile", "other"),
        ("--enable", "feature_a"),
        ("--disable", "feature_b"),
    ):
        assert CodexAdapter().preflight([binary, flag, value, flag, value]) == (
            True,
            "ok",
        ), flag


@pytest.mark.parametrize(
    "value",
    [
        "sandbox_mode=workspace-write",
        "sandbox_mode=\"read-only\"",
        "sandbox_permissions=full",
        "sandbox_workspace_write.network_access=true",
        "disk-full-read-access=true",
        "network_access=true",
    ],
)
def test_preflight_rejects_sandbox_keys_in_config_values(tmp_path, value):
    binary = _executable(tmp_path)
    assert CodexAdapter().preflight([binary, "-c", value]) == (
        False,
        "executor-dangerous-arg",
    )
    assert CodexAdapter().preflight([binary, "--config", value]) == (
        False,
        "executor-dangerous-arg",
    )


def test_preflight_allows_ordinary_config_values(tmp_path):
    binary = _executable(tmp_path)
    for value in (
        "model_reasoning_effort=high",
        "model_provider=openai",
        "approval_policy=on-request",
        "sandbox_workspace=nope",
    ):
        assert CodexAdapter().preflight([binary, "-c", value]) == (True, "ok"), value


# W6 D10: free real-CLI parse path only (``--help``), never a model call.
# Opt-in because the binary is not part of the product environment.
REAL_CODEX = os.environ.get("HKL_CODEX_BIN") or shutil.which("codex")


@pytest.mark.skipif(
    not REAL_CODEX, reason="real codex CLI not on PATH; set HKL_CODEX_BIN"
)
def test_real_codex_cli_parses_legal_and_rejects_duplicate_schema():
    legal = subprocess.run(
        [
            REAL_CODEX,
            "exec",
            "--json",
            "-m",
            "gpt-5",
            "-c",
            "model_reasoning_effort=high",
            "--enable",
            "feature_a",
            "--enable",
            "feature_b",
            "--output-schema",
            "schema.json",
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert legal.returncode == 0, legal.stderr

    duplicate = subprocess.run(
        [
            REAL_CODEX,
            "exec",
            "--output-schema",
            "a.json",
            "--output-schema",
            "b.json",
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert duplicate.returncode == 2, duplicate.stderr


# ---------------------------------------------------------------------------
# W5: the merged single-value guard must stay in place
# ---------------------------------------------------------------------------


def test_preflight_rejects_duplicate_output_schema_adjacent(tmp_path):
    """Measured: ``codex exec ... --output-schema a --output-schema b`` is rc=2."""

    binary = _executable(tmp_path)
    assert CodexAdapter().preflight(
        [binary, "--output-schema", "a.json", "--output-schema", "b.json"]
    ) == (False, "executor-arg-duplicate")


def test_preflight_rejects_duplicate_output_schema_separated(tmp_path):
    """A repeated flag is rejected wherever it appears in the argv."""

    binary = _executable(tmp_path)
    assert CodexAdapter().preflight(
        [
            binary,
            "--output-schema",
            "a.json",
            "-m",
            "gpt-5",
            "--output-schema",
            "b.json",
        ]
    ) == (False, "executor-arg-duplicate")


@pytest.mark.parametrize(
    "extra",
    [
        ["-m", "aaa", "-m", "bbb"],
        ["--model", "aaa", "--model", "bbb"],
        ["-c", "a=b", "-c", "c=d"],
        ["--config", "a=b", "--config", "c=d"],
        ["-p", "a", "-p", "b"],
        ["--profile", "a", "--profile", "b"],
        ["--enable", "a", "--enable", "b"],
        ["--disable", "a", "--disable", "b"],
    ],
)
def test_preflight_allows_repeatable_options(tmp_path, extra):
    """Measured rc=0: these options may repeat and must not be rejected."""

    binary = _executable(tmp_path)
    assert CodexAdapter().preflight([binary, *extra]) == (True, "ok")


def test_preflight_counts_flag_value_pairs_not_raw_tokens(tmp_path):
    """A value that happens to spell ``--output-schema`` is not an occurrence."""

    binary = _executable(tmp_path)
    assert CodexAdapter().preflight([binary, "-m", "--output-schema"]) == (
        True,
        "ok",
    )


def test_preflight_accepts_single_output_schema(tmp_path):
    binary = _executable(tmp_path)
    assert CodexAdapter().preflight([binary, "--output-schema", "schema.json"]) == (
        True,
        "ok",
    )


def test_single_valued_options_are_whitelisted():
    assert SINGLE_VALUED == ("--output-schema",)
    assert set(SINGLE_VALUED) <= set(PASSTHROUGH_WITH_VALUE)


# ---------------------------------------------------------------------------
# W5-02: fixed argv order, prompt last, no --max-budget-usd
# ---------------------------------------------------------------------------


def test_build_argv_fixed_order_and_prompt_last(tmp_path):
    binary = _executable(tmp_path)
    run_dir = str(tmp_path / "run")
    command = CodexAdapter().build_argv(
        [binary], run_dir=run_dir, session_id="ignored", prompt="DO IT"
    )
    expected = [
        binary,
        "exec",
        "--json",
        "-o",
        os.path.join(run_dir, LAST_MESSAGE_NAME),
        "--ephemeral",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--sandbox",
        SANDBOX,
        "DO IT",
    ]
    assert command == expected
    assert command[-1] == "DO IT"
    assert command.count("--sandbox") == 1
    assert SANDBOX == "workspace-write"
    assert "--max-budget-usd" not in command
    assert "--session-id" not in command
    # ``-o`` is immediately followed by the run-local human-readable copy.
    assert command[command.index("-o") + 1] == os.path.join(
        run_dir, LAST_MESSAGE_NAME
    )


def test_build_argv_keeps_operator_passthrough_before_protocol(tmp_path):
    binary = _executable(tmp_path)
    command = CodexAdapter().build_argv(
        [binary, "-m", "gpt-5"],
        run_dir=str(tmp_path / "run"),
        session_id="ignored",
        prompt="prompt",
    )
    assert command[:3] == [binary, "-m", "gpt-5"]
    assert command[3] == "exec"
    assert command[-1] == "prompt"


def test_build_argv_places_output_schema_after_exec(tmp_path):
    """F1: ``--output-schema`` is exec-only and must follow the subcommand."""

    binary = _executable(tmp_path)
    schema = str(tmp_path / "schema.json")
    command = CodexAdapter().build_argv(
        [binary, "-m", "gpt-5", "--output-schema", schema],
        run_dir=str(tmp_path / "run"),
        session_id="ignored",
        prompt="DO IT",
    )
    assert command.index("--output-schema") > command.index("exec")
    assert command[command.index("--output-schema") + 1] == schema
    assert command.count("--output-schema") == 1
    # Other whitelisted operator options keep their original pre-exec order.
    assert command[1:3] == ["-m", "gpt-5"]
    assert command[3] == "exec"
    # The prompt stays last and the fixed D3 arguments are untouched.
    assert command[-1] == "DO IT"
    assert command[-3:-1] == ["--output-schema", schema]
    assert command[command.index("--sandbox") + 1] == SANDBOX


def test_build_argv_moves_only_exec_only_options_behind_exec(tmp_path):
    binary = _executable(tmp_path)
    schema = str(tmp_path / "schema.json")
    command = CodexAdapter().build_argv(
        [binary, "--output-schema", schema, "-c", "model_reasoning_effort=high"],
        run_dir=str(tmp_path / "run"),
        session_id="ignored",
        prompt="prompt",
    )
    exec_index = command.index("exec")
    # ``-c`` was supplied after ``--output-schema`` but stays before ``exec``
    # with its relative order preserved.
    assert command[1:4] == ["-c", "model_reasoning_effort=high", "exec"]
    assert command.index("--output-schema") > exec_index
    assert command[-2:] == [schema, "prompt"]


def test_exec_only_options_are_a_subset_of_the_whitelist():
    assert EXEC_ONLY_WITH_VALUE == ("--output-schema",)
    assert set(EXEC_ONLY_WITH_VALUE) <= set(PASSTHROUGH_WITH_VALUE)


@pytest.mark.skipif(
    not REAL_CODEX_BIN,
    reason="real Codex parse regression is opt-in (set HKL_CODEX_BIN)",
)
def test_real_codex_parses_built_argv_with_output_schema(tmp_path):
    """Free parse path: real CLI, built argv, no model call / tokens.

    ``--help`` makes clap parse the whole argument vector and exit before
    execution.  The pre-rework order returned ``unexpected argument
    '--output-schema'`` with rc=2; the built order must be accepted (rc=0).
    """

    binary = str(REAL_CODEX_BIN)
    schema = str(tmp_path / "schema.json")
    command = CodexAdapter().build_argv(
        [
            binary,
            "-m",
            "gpt-5",
            "-c",
            "model_reasoning_effort=high",
            "-p",
            "work",
            "--enable",
            "feature_a",
            "--disable",
            "feature_b",
            "--output-schema",
            schema,
        ],
        run_dir=str(tmp_path / "run"),
        session_id="ignored",
        prompt="HKL-PARSE-ONLY",
    )
    command.append("--help")
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    version = CodexAdapter().version([binary])
    assert result.returncode == 0, (
        f"real Codex rejected built argv (version={version!r}): "
        f"{result.stderr.strip()}"
    )
    assert "unexpected argument" not in result.stderr
    assert "usage: codex exec" in (result.stdout or "").lower()


# ---------------------------------------------------------------------------
# W5-03: JSONL parsing
# ---------------------------------------------------------------------------


def test_parse_success_shape():
    parsed = CodexAdapter().parse(_jsonl(*SUCCESS_EVENTS))
    assert parsed["thread_id"] == "thread-abc"
    assert parsed["result"] == "HKL-OK"
    assert parsed["usage"] == SUCCESS_EVENTS[-1]["usage"]
    assert "failure" not in parsed
    assert "warnings" not in parsed


def test_parse_takes_last_agent_message():
    stdout = _jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "last"}},
    )
    assert CodexAdapter().parse(stdout)["result"] == "last"


def test_parse_ignores_junk_and_bad_json():
    stdout = "\n".join(
        [
            "not json at all",
            "{broken",
            "",
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            "[1, 2, 3]",
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}),
        ]
    )
    parsed = CodexAdapter().parse(stdout)
    assert parsed["thread_id"] == "thread-1"
    assert parsed["result"] == "ok"


def test_parse_empty_stdout_is_empty_document():
    assert CodexAdapter().parse("") == {}
    assert CodexAdapter().parse("   \n\n  ") == {}
    assert CodexAdapter().parse(None) == {}


def test_parse_warning_and_failure_events():
    stdout = _jsonl(
        {"type": "thread.started", "thread_id": "thread-2"},
        {
            "type": "item.completed",
            "item": {"type": "error", "message": "warning text"},
        },
        {
            "type": "turn.failed",
            "error": {"message": "The 'x' model is not supported"},
        },
    )
    parsed = CodexAdapter().parse(stdout)
    assert parsed["warnings"] == ["warning text"]
    assert parsed["failure"] == "The 'x' model is not supported"


# ---------------------------------------------------------------------------
# W5-04: classification matrix and token usage
# ---------------------------------------------------------------------------


def test_classify_success_ignores_warnings():
    adapter = CodexAdapter()
    parsed = adapter.parse(
        _jsonl(
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.completed", "item": {"type": "error", "message": "warn"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
        )
    )
    outcome = adapter.classify(0, parsed)
    assert outcome.category == "success"
    assert outcome.block_kind is None
    assert outcome.is_error is False
    # A warning item is recorded but never turns a clean exit into a failure.
    assert parsed["warnings"] == ["warn"]


def test_classify_failure_matrix():
    adapter = CodexAdapter()

    def failed(message: str):
        return adapter.parse(
            _jsonl({"type": "turn.failed", "error": {"message": message}})
        )

    auth = adapter.classify(1, failed("401 unauthorized: please login"))
    assert (auth.category, auth.block_kind) == ("environment_failure", "capability")

    api_key = adapter.classify(1, failed("The API key is invalid"))
    assert (api_key.category, api_key.block_kind) == (
        "environment_failure",
        "capability",
    )

    capacity = adapter.classify(1, failed("429 rate limit exceeded"))
    assert (capacity.category, capacity.block_kind) == (
        "environment_failure",
        "transient",
    )

    network = adapter.classify(1, failed("network error: ECONNRESET"))
    assert (network.category, network.block_kind) == (
        "environment_failure",
        "transient",
    )

    code = adapter.classify(1, failed("The 'nope' model is not supported"))
    assert (code.category, code.block_kind) == ("code_failure", "needs_input")

    plain_rc1 = adapter.classify(1, {})
    assert (plain_rc1.category, plain_rc1.block_kind) == (
        "code_failure",
        "needs_input",
    )

    timeout = adapter.classify(1, failed("anything"), timed_out=True)
    assert (timeout.category, timeout.block_kind) == ("timeout", "transient")


def test_classify_auth_wins_over_capacity_when_both_match():
    parsed = CodexAdapter().parse(
        _jsonl({"type": "turn.failed", "error": {"message": "401 rate limit"}})
    )
    outcome = CodexAdapter().classify(1, parsed)
    assert (outcome.category, outcome.block_kind) == (
        "environment_failure",
        "capability",
    )


def test_usage_reports_tokens_and_never_estimates_cost():
    parsed = {
        "usage": {
            "input_tokens": 11,
            "cached_input_tokens": 3,
            "cache_write_input_tokens": 4,
            "output_tokens": 5,
            "reasoning_output_tokens": 6,
            "total_cost_usd": 99.0,
        }
    }
    assert CodexAdapter().usage(parsed) == {
        "input_tokens": 11,
        "cached_input_tokens": 3,
        "cache_write_input_tokens": 4,
        "output_tokens": 5,
        "reasoning_output_tokens": 6,
        "cost_usd": None,
    }
    # Missing/negative/non-integer values are "not accounted", never zero.
    assert CodexAdapter().usage({})["cost_usd"] is None
    assert CodexAdapter().usage({"usage": {"input_tokens": -1}})["input_tokens"] is None
    assert CodexAdapter().usage({"usage": {"input_tokens": True}})["input_tokens"] is None


# ---------------------------------------------------------------------------
# W5-05: credential_env returns paths/key names only, never file content
# ---------------------------------------------------------------------------


def _codex_home(tmp_path, content: str = '{"not": "read"}'):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "auth.json").write_text(content, encoding="utf-8")
    return home


def test_credential_env_explicit_codex_home(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, "SECRET-ACCOUNT-BLOB")
    monkeypatch.setenv("CODEX_HOME", str(home))
    env, reason = CodexAdapter().credential_env()
    assert reason is None
    assert env == {"CODEX_HOME": str(home)}
    # The returned mapping is a path only: no account store content leaks.
    assert "SECRET-ACCOUNT-BLOB" not in json.dumps(env)


def test_credential_env_default_home(tmp_path, monkeypatch):
    home = _codex_home(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    env, reason = CodexAdapter().credential_env()
    assert reason is None
    assert env == {"CODEX_HOME": str(home)}


def test_credential_env_auth_missing(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert CodexAdapter().credential_env() == ({}, "codex-auth-missing")


def test_credential_env_openai_api_key_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-value")
    home = _codex_home(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))
    env, reason = CodexAdapter().credential_env()
    assert reason is None
    assert env == {"OPENAI_API_KEY": "sk-test-value"}


def test_credential_env_does_not_parse_or_copy_auth_json(tmp_path, monkeypatch):
    # Invalid JSON proves the adapter only stats the file; it never reads it.
    home = _codex_home(tmp_path, "this is not json and must never be parsed")
    monkeypatch.setenv("CODEX_HOME", str(home))
    before = sorted(path.name for path in home.iterdir())
    env, reason = CodexAdapter().credential_env()
    after = sorted(path.name for path in home.iterdir())
    assert reason is None
    assert env == {"CODEX_HOME": str(home)}
    assert before == after == ["auth.json"]
    assert (home / "auth.json").read_text(encoding="utf-8").startswith(
        "this is not json"
    )


def test_settings_document_has_no_secret_and_codex_metadata(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, "TOKEN-CONTENT")
    monkeypatch.setenv("CODEX_HOME", str(home))
    document = CodexAdapter().settings_document()
    assert document == {
        "schema_version": 1,
        "executor": "codex",
        "sandbox": "workspace-write",
        "ignore_user_config": True,
        "ephemeral": True,
        "codex_home": str(home),
        "passthrough_keys": ["-m", "--model", "-c", "--config"],
    }
    assert "TOKEN-CONTENT" not in json.dumps(document)


def test_settings_document_codex_home_none_when_auth_missing(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert CodexAdapter().settings_document()["codex_home"] is None


def test_version_reads_first_stdout_line(tmp_path):
    path = tmp_path / "codex-version"
    path.write_text(
        "#!/bin/sh\nprintf 'codex-cli 9.9.9\\nextra\\n'\n", encoding="utf-8"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    assert CodexAdapter().version([str(path)]) == "codex-cli 9.9.9"
    assert CodexAdapter().version([]) is None


def test_adapter_declares_shared_seam_attributes():
    adapter = CodexAdapter()
    assert adapter.name == "codex"
    assert adapter.settings_filename == "codex-settings.json"
