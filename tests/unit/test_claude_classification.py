"""Classification regressions for the Claude Code adapter.

Production incident 2026-11-11 (run 590): a clean ``exit_code=0`` /
``is_error=false`` run was discarded as ``environment_failure`` because the
model's own summary mentioned a pre-existing ``F401 shutil`` lint finding; the
bare substring ``401`` inside ``F401`` matched the auth keyword list.

The classification contract exercised here:

* on a successful run (``is_error=false``) the model's ``result`` text is
  never inspected; on a failed run (``is_error=true``) it is the executor's
  error message and does participate;
* HTTP codes count only as structured ``api_error_status`` or as independent
  tokens on an error/stderr line that carries http/status/error semantics;
* genuine 401/403 (capability) and 429/503/rate-limit/timeout (transient)
  failures are still fail-closed, as are non-zero exits (code_failure).
"""

from __future__ import annotations

import pytest

from hermes_kanban_lanes.executors.claude import ClaudeAdapter


@pytest.fixture()
def adapter() -> ClaudeAdapter:
    return ClaudeAdapter()


# ---------------------------------------------------------------------------
# Acceptance 1: model prose never drives classification
# ---------------------------------------------------------------------------


def test_success_summary_with_f401_passed_count_and_epoch_is_success(adapter):
    """F401 / 1108 passed / 1794335401 in ``result`` must not trip 401."""

    parsed = {
        "is_error": False,
        "subtype": "success",
        "num_turns": 36,
        "total_cost_usd": 1.19,
        "result": (
            "ruff check 仅报一个改动前已存在的 F401 shutil；"
            "全量自测 1108 passed, 9 skipped，时间戳 1794335401。"
        ),
    }
    outcome = adapter.classify(0, parsed)
    assert (outcome.category, outcome.block_kind) == ("success", None)
    assert outcome.is_error is False
    # The summary is still recorded on the outcome for the report.
    assert "F401" in outcome.result_text


def test_success_result_with_f401_401_403_unauthorized_is_success(adapter):
    parsed = {
        "is_error": False,
        "result": "fixed F401; saw 401 and 403 in logs; unauthorized was handled",
    }
    outcome = adapter.classify(0, parsed)
    assert (outcome.category, outcome.block_kind) == ("success", None)


def test_result_text_rate_limit_phrase_does_not_classify(adapter):
    """Even capacity-sounding prose in ``result`` is not evidence."""

    parsed = {"is_error": False, "result": "no rate limit was encountered"}
    assert adapter.classify(0, parsed).category == "success"


def test_stderr_lint_numbers_without_http_context_are_success(adapter):
    parsed = {"is_error": False, "result": "done"}
    for stderr in ("lint: F401 shutil imported but unused", "ts=1794335401 wrote log"):
        outcome = adapter.classify(0, parsed, stderr=stderr)
        assert (outcome.category, outcome.block_kind) == ("success", None), stderr


# ---------------------------------------------------------------------------
# Acceptance 1b: an executor-reported error message *is* evidence
# ---------------------------------------------------------------------------


def test_error_result_rate_limit_is_transient(adapter):
    parsed = {"is_error": True, "result": "rate limit exceeded"}
    outcome = adapter.classify(0, parsed)
    assert (outcome.category, outcome.block_kind) == (
        "environment_failure",
        "transient",
    )


def test_error_result_not_logged_in_is_capability(adapter):
    parsed = {"is_error": True, "result": "Not logged in"}
    outcome = adapter.classify(0, parsed)
    assert (outcome.category, outcome.block_kind) == (
        "environment_failure",
        "capability",
    )


def test_error_result_http_401_is_capability(adapter):
    parsed = {"is_error": True, "result": "HTTP 401 Unauthorized"}
    outcome = adapter.classify(0, parsed)
    assert (outcome.category, outcome.block_kind) == (
        "environment_failure",
        "capability",
    )


def test_error_result_numeric_substrings_are_not_http(adapter):
    """``F401``/``?id=1401``/epochs stay clean even on the error path."""

    parsed = {
        "is_error": True,
        "result": "ruff F401; request failed: https://x.test/?id=1401; ts=1794335401",
    }
    outcome = adapter.classify(1, parsed)
    assert (outcome.category, outcome.block_kind) == ("code_failure", "needs_input")


# ---------------------------------------------------------------------------
# Acceptance 2: genuine HTTP auth failures stay fail-closed
# ---------------------------------------------------------------------------


def test_stderr_http_401_is_capability(adapter):
    outcome = adapter.classify(
        0, {"is_error": False, "result": "done"}, stderr="HTTP 401 Unauthorized"
    )
    assert (outcome.category, outcome.block_kind) == (
        "environment_failure",
        "capability",
    )


def test_stderr_https_url_with_id_1401_is_not_401(adapter):
    """``?id=1401`` contains no independent 401 token."""

    outcome = adapter.classify(
        0,
        {"is_error": False, "result": "done"},
        stderr="request failed: https://x.test/?id=1401",
    )
    assert (outcome.category, outcome.block_kind) == ("success", None)


def test_stderr_http_403_is_capability(adapter):
    outcome = adapter.classify(
        0, {"is_error": False, "result": "done"}, stderr="status 403: Forbidden"
    )
    assert (outcome.category, outcome.block_kind) == (
        "environment_failure",
        "capability",
    )


def test_structured_api_error_status_401_is_capability(adapter):
    parsed = {"is_error": True, "api_error_status": 401, "subtype": "success"}
    outcome = adapter.classify(1, parsed)
    assert (outcome.category, outcome.block_kind) == (
        "environment_failure",
        "capability",
    )


# ---------------------------------------------------------------------------
# Acceptance 3/4: capacity failures stay transient
# ---------------------------------------------------------------------------


def test_structured_api_error_status_429_is_transient(adapter):
    parsed = {"is_error": True, "api_error_status": 429}
    outcome = adapter.classify(1, parsed)
    assert (outcome.category, outcome.block_kind) == (
        "environment_failure",
        "transient",
    )


def test_stderr_rate_limit_is_transient(adapter):
    outcome = adapter.classify(
        0, {"is_error": False, "result": "done"}, stderr="rate limit exceeded"
    )
    assert (outcome.category, outcome.block_kind) == (
        "environment_failure",
        "transient",
    )


def test_stderr_http_503_is_transient(adapter):
    outcome = adapter.classify(
        0, {"is_error": False, "result": "done"}, stderr="HTTP 503"
    )
    assert (outcome.category, outcome.block_kind) == (
        "environment_failure",
        "transient",
    )


def test_stderr_etimedout_is_transient(adapter):
    outcome = adapter.classify(1, {}, stderr="connect ETIMEDOUT")
    assert (outcome.category, outcome.block_kind) == (
        "environment_failure",
        "transient",
    )


# ---------------------------------------------------------------------------
# Exit-code and timeout semantics are unchanged
# ---------------------------------------------------------------------------


def test_nonzero_exit_without_environment_signal_is_code_failure(adapter):
    outcome = adapter.classify(2, {"is_error": True, "result": "boom"})
    assert (outcome.category, outcome.block_kind) == ("code_failure", "needs_input")


def test_timeout_is_timeout_transient(adapter):
    outcome = adapter.classify(1, {}, timed_out=True)
    assert (outcome.category, outcome.block_kind) == ("timeout", "transient")


def test_auth_wins_over_capacity_when_both_present(adapter):
    outcome = adapter.classify(1, {}, stderr="HTTP 401 error after rate limit")
    assert (outcome.category, outcome.block_kind) == (
        "environment_failure",
        "capability",
    )
