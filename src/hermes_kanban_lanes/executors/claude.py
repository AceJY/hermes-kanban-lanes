"""Claude Code executor adapter.

The adapter appends a fixed protocol argument set (D8), parses the JSON result
document and classifies failures per spec section 6.3.  It reads the user's own
``~/.claude/settings.json`` only to copy ``ANTHROPIC_*`` entries into the child
environment **in memory**; it never writes, copies or prints credentials.

Authentication material is not a sandbox: the executor is still a same-UID
process.  The allow/deny settings file is defence in depth, not isolation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .base import ExecutorOutcome

PASSTHROUGH_WITH_VALUE = ("--model", "--max-budget-usd", "--effort")
# Phrase-level auth/capacity signals.  Matching is done against structured
# fields, stderr, and (only when ``is_error=true``) the executor's own error
# message in ``result`` -- never the model's summary on a successful run.  HTTP
# codes are handled separately below with token boundaries and line context.
# Bare substrings such as "401" are intentionally absent: ``F401`` or a
# 10-digit epoch must not look like an authentication failure.
_AUTH_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bnot logged in\b",
        r"\binvalid api key\b",
        r"\boauth",
        r"\bcredit balance\b",
        r"\bauthentication",
        r"\bunauthorized",
        r"\bforbidden",
    )
)
_CAPACITY_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\brate[\s_-]*limit(?:ed)?\b",
        r"\boverloaded\b",
        r"\btoo many requests\b",
        r"\bservice unavailable\b",
        r"\beconnreset\b",
        r"\betimedout\b",
        r"\bnetwork\b",
        r"\btimeout\b",
        r"\btimed out\b",
    )
)
# An HTTP status counts only when it is an independent numeric token on a line
# that also carries http/status/error semantics.  ``\b`` keeps ``F401``,
# ``?id=1401`` and ``1794335401`` clean; ``\bhttp\b`` does not match the
# ``https`` scheme, so ordinary URLs never count as context.
_HTTP_STATUS = re.compile(r"\b(401|403|429|503)\b")
_HTTP_CONTEXT = re.compile(r"\b(?:http|status|error)\b", re.IGNORECASE)
_AUTH_STATUSES = frozenset({401, 403})
_CAPACITY_STATUSES = frozenset({429, 503})


def _structured_status(parsed: dict[str, Any]) -> int | None:
    """Return ``api_error_status`` as an integer when it is a real code."""

    value = parsed.get("api_error_status") if isinstance(parsed, dict) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _http_signal(*texts: str) -> str | None:
    """Classify HTTP status tokens found in diagnostic text.

    Returns ``"auth"`` (401/403), ``"capacity"`` (429/503) or ``None``.  Auth
    wins when both appear so a genuine credential failure is never downgraded
    to a retryable one.
    """

    auth = False
    capacity = False
    for text in texts:
        for line in (text or "").splitlines():
            if not _HTTP_CONTEXT.search(line):
                continue
            for match in _HTTP_STATUS.finditer(line):
                code = int(match.group(1))
                if code in _AUTH_STATUSES:
                    auth = True
                elif code in _CAPACITY_STATUSES:
                    capacity = True
    if auth:
        return "auth"
    if capacity:
        return "capacity"
    return None


def _phrase_signal(text: str) -> str | None:
    """Return the auth/capacity phrase signal in ``text`` (auth wins)."""

    lowered = (text or "").lower()
    if any(pattern.search(lowered) for pattern in _AUTH_PATTERNS):
        return "auth"
    if any(pattern.search(lowered) for pattern in _CAPACITY_PATTERNS):
        return "capacity"
    return None


class ClaudeAdapter:
    name = "claude"
    # W5 D7 shared seam: the supervisor names the run-local settings file from
    # this attribute.  Claude keeps its historical file name.
    settings_filename = "claude-settings.json"

    # -- configuration -----------------------------------------------------

    def preflight(self, argv: Sequence[str]) -> tuple[bool, str]:
        if not argv:
            return False, "executor-argv-empty"
        executable = str(argv[0])
        if os.path.sep in executable:
            if not (os.path.isfile(executable) and os.access(executable, os.X_OK)):
                return False, "executor-not-executable"
        elif shutil.which(executable) is None:
            return False, "executor-not-found"
        extra = list(argv[1:])
        index = 0
        while index < len(extra):
            flag = extra[index]
            if flag not in PASSTHROUGH_WITH_VALUE:
                return False, "executor-arg-not-allowed"
            if index + 1 >= len(extra):
                return False, "executor-arg-missing-value"
            if not str(extra[index + 1]):
                return False, "executor-arg-empty-value"
            index += 2
        return True, "ok"

    @staticmethod
    def passthrough(argv: Sequence[str]) -> list[str]:
        return [str(item) for item in argv[1:]]

    def build_argv(
        self,
        argv: Sequence[str],
        *,
        run_dir: str,
        session_id: str,
        prompt: str,
    ) -> list[str]:
        settings_path = os.path.join(run_dir, "claude-settings.json")
        command = [
            str(argv[0]),
            *self.passthrough(argv),
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
            "--permission-prompts",
            "none",
            "--setting-sources",
            "",
            "--session-id",
            session_id,
            "--settings",
            settings_path,
        ]
        command.append(str(prompt))
        return command

    def settings_document(self) -> dict[str, Any]:
        """Run-local permissions table; defence in depth, not a sandbox."""

        return {
            "permissions": {
                "allow": ["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
                "deny": [
                    "Bash(hermes:*)",
                    "Bash(sqlite3:*)",
                    "Bash(git commit:*)",
                    "Bash(git push:*)",
                    "Bash(git checkout:*)",
                    "Bash(git reset:*)",
                    "WebFetch",
                    "WebSearch",
                ],
            }
        }

    # -- credentials -------------------------------------------------------

    def credential_env(self, home: str | os.PathLike[str] | None = None) -> tuple[dict[str, str], str | None]:
        """Return only ``ANTHROPIC_*`` entries from the user's settings file.

        Returns ``({}, reason)`` when the file or the keys are unavailable; the
        reason is a key-level description and never a value.
        """

        settings_path = Path(home or Path.home()) / ".claude" / "settings.json"
        try:
            document = json.loads(settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, "claude-settings-missing"
        except (OSError, ValueError):
            return {}, "claude-settings-unreadable"
        if not isinstance(document, dict):
            return {}, "claude-settings-invalid"
        env = document.get("env")
        if not isinstance(env, dict):
            return {}, "claude-settings-env-missing"
        credentials = {
            str(key): str(value)
            for key, value in env.items()
            if isinstance(key, str)
            and key.startswith("ANTHROPIC_")
            and isinstance(value, str)
            and value
        }
        if not credentials:
            return {}, "claude-credentials-missing"
        return credentials, None

    # -- parsing -----------------------------------------------------------

    def parse(self, stdout: str) -> dict[str, Any]:
        text = (stdout or "").strip()
        if not text:
            return {}
        for candidate in (text, *reversed(text.splitlines())):
            candidate = candidate.strip()
            if not candidate or not candidate.startswith("{"):
                continue
            try:
                value = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                value = json.loads(text[start : end + 1])
            except ValueError:
                return {}
            if isinstance(value, dict):
                return value
        return {}

    def usage(self, parsed: dict[str, Any]) -> dict[str, Any]:
        usage = parsed.get("usage") if isinstance(parsed, dict) else None
        usage = usage if isinstance(usage, dict) else {}

        def _token(value: Any) -> int | None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        cost = parsed.get("total_cost_usd") if isinstance(parsed, dict) else None
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
            cost = None
        return {
            "input_tokens": _token(usage.get("input_tokens")),
            "output_tokens": _token(usage.get("output_tokens")),
            "cost_usd": cost,
        }

    def permission_denials(self, parsed: dict[str, Any]) -> list[Any]:
        denials = parsed.get("permission_denials") if isinstance(parsed, dict) else None
        return list(denials) if isinstance(denials, list) else []

    # -- classification ----------------------------------------------------

    def classify(
        self,
        exit_code: int,
        parsed: dict[str, Any],
        *,
        stderr: str = "",
        timed_out: bool = False,
    ) -> ExecutorOutcome:
        if timed_out:
            return ExecutorOutcome(
                exit_code=int(exit_code),
                is_error=True,
                category="timeout",
                block_kind="transient",
                terminal_reason="timeout",
                parsed=parsed,
            )

        is_error = bool(parsed.get("is_error")) if isinstance(parsed, dict) else False
        result_text = str(parsed.get("result") or "") if isinstance(parsed, dict) else ""
        terminal_reason = (
            parsed.get("terminal_reason") if isinstance(parsed, dict) else None
        )
        # Structured reasons are CLI-authored, not model prose, so they may be
        # inspected.  ``result_text`` only joins them when the host itself
        # flagged the run as failed (``is_error=true``): then it is the
        # executor's error message, not the model's summary.  A successful
        # summary mentioning ``F401 shutil`` or ``1108 passed`` must never be
        # classified and used to discard a completed run.
        error_text = result_text if is_error else ""
        structured_reasons = "\n".join(
            part
            for part in (
                str(terminal_reason or ""),
                str(parsed.get("subtype") or "") if isinstance(parsed, dict) else "",
                str(parsed.get("stop_reason") or "") if isinstance(parsed, dict) else "",
            )
            if part
        )
        evidence = "\n".join(
            part for part in (structured_reasons, error_text, stderr or "") if part
        )

        def outcome(category: str, block_kind: str | None) -> ExecutorOutcome:
            return ExecutorOutcome(
                exit_code=int(exit_code),
                is_error=is_error,
                category=category,
                block_kind=block_kind,
                result_text=result_text,
                terminal_reason=str(terminal_reason) if terminal_reason else None,
                usage=self.usage(parsed),
                permission_denials=self.permission_denials(parsed),
                parsed=parsed,
            )

        signal = None
        status = _structured_status(parsed)
        if status in _AUTH_STATUSES:
            signal = "auth"
        elif status in _CAPACITY_STATUSES:
            signal = "capacity"
        else:
            signal = _http_signal(structured_reasons, error_text, stderr or "")
        if signal is None:
            signal = _phrase_signal(evidence)
        if signal == "auth":
            return outcome("environment_failure", "capability")
        if signal == "capacity":
            return outcome("environment_failure", "transient")
        if exit_code == 0 and not is_error:
            return outcome("success", None)
        if exit_code == 0 and is_error:
            # The host reported an error without a recognisable class.
            return outcome("environment_failure", "capability")
        return outcome("code_failure", "needs_input")

    # -- metadata ----------------------------------------------------------

    @staticmethod
    def version(argv: Sequence[str], *, timeout: float = 10.0) -> str | None:
        if not argv:
            return None
        try:
            result = subprocess.run(
                [str(argv[0]), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        line = (result.stdout or "").strip().splitlines()
        return line[0] if line else None


__all__ = ["ClaudeAdapter", "PASSTHROUGH_WITH_VALUE"]
