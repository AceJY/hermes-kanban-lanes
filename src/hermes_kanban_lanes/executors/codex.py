"""Codex CLI executor adapter (W5 D1-D9).

The adapter is the only executor-specific code for Codex.  It appends the
fixed protocol argument set (D3), parses the JSONL event stream (D5), classifies
failures (D6) and resolves the authentication path (D4).  It never touches the
board, the workspace git metadata, evidence, review or merge code.

Measured CLI behaviour (docs/11 section 2.2, codex-cli 0.167.0-alpha.3.2):

* ``codex exec --json`` streams ``thread.started``, ``turn.started``,
  ``item.completed`` and ``turn.completed``/``turn.failed`` events on stdout;
* ``-o/--output-last-message <file>`` writes the last agent message, without a
  trailing newline -- it is a human-readable copy, never the parse authority;
* authentication comes from ``CODEX_HOME`` (default ``~/.codex``), not
  ``HOME``; ``--ignore-user-config`` keeps the operator's model/MCP/hooks out;
* there is no ``--max-budget-usd``; cost is not reported by the CLI.

Trust boundary: the CLI's ``workspace-write`` sandbox is defence in depth, not
filesystem isolation.  ``CODEX_HOME`` is injected explicitly, so the executor
can read the account store beneath it; same-UID processes are not isolated.
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

# D3: the only operator-supplied options this adapter accepts.  Every one
# takes a non-empty value; ``--enable``/``--disable`` may repeat naturally.
PASSTHROUGH_WITH_VALUE = (
    "-m",
    "--model",
    "-c",
    "--config",
    "-p",
    "--profile",
    "--enable",
    "--disable",
    "--output-schema",
)
# ``--output-schema`` is an ``exec`` subcommand option only (measured against
# codex-cli 0.167.0-alpha.3.2: top level rejects it with "unexpected argument").
# The other whitelisted options exist at both levels, so they keep their
# operator-supplied position before ``exec``.
EXEC_ONLY_WITH_VALUE = ("--output-schema",)
# D3/W5-10: never reachable from operator argv.  Matched as an exact token and
# as a marker inside a value (e.g. ``sandbox_mode="danger-full-access"``).
DANGEROUS_TOKENS = frozenset(
    {
        "-s",
        "--sandbox",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "danger-full-access",
    }
)
DANGEROUS_MARKERS = ("danger-full-access", "dangerously-bypass")
# D3: whitelisted options the CLI accepts at most once.  Measured against
# codex-cli 0.167.0-alpha.3.2: repeating ``--output-schema`` exits rc=2 with
# "cannot be used multiple times", while ``-m``/``-c``/``-p``/``--enable``/
# ``--disable`` repeat cleanly.  Kept separate from ``EXEC_ONLY_WITH_VALUE`` so
# W6 can extend either set independently.
SINGLE_VALUED = ("--output-schema",)
# W6 D10: a ``-c/--config`` value may not widen the sandbox.  Only these
# config keys are rejected; ordinary settings such as
# ``model_reasoning_effort=high`` stay legal.
CONFIG_FLAGS = frozenset({"-c", "--config"})
SANDBOX_CONFIG_MARKERS = (
    "sandbox_mode",
    "sandbox_permissions",
    "sandbox_workspace_write",
    "disk-full-read-access",
    "network_access",
)
SANDBOX = "workspace-write"
LAST_MESSAGE_NAME = "codex-last-message.txt"
SETTINGS_FILENAME = "codex-settings.json"

# Phrase-level auth/capacity signals for the ``turn.failed`` message and
# stderr.  Bare numeric substrings are deliberately absent: ``F401``, ``1401``
# and URLs carrying a numeric ``id`` are not HTTP statuses.  Status codes are
# handled by :func:`_http_signal` with token boundaries and line context.
_AUTH_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\blogin\b",
        r"\bauthentication",
        r"\bunauthorized",
        r"\bforbidden",
        r"\bapi key\b",
        r"\bcredit balance\b",
    )
)
_CAPACITY_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\brate[\s_-]*limit(?:ed)?\b",
        r"\boverloaded\b",
        r"\btoo many requests\b",
        r"\bservice unavailable\b",
        r"\bnetwork\b",
        r"\beconnreset\b",
        r"\betimedout\b",
        r"\btimeout\b",
        r"\btimed out\b",
    )
)
_HTTP_STATUS = re.compile(r"\b(401|403|429|503)\b")
# ``\bhttp\b`` intentionally does not match the ``https`` scheme, so a URL that
# merely contains ``?id=1401`` is never read as a 401.
_HTTP_CONTEXT = re.compile(r"\b(?:http|status|error)\b", re.IGNORECASE)
_AUTH_STATUSES = frozenset({401, 403})
_CAPACITY_STATUSES = frozenset({429, 503})


def _http_signal(*texts: str) -> str | None:
    """Return ``"auth"``/``"capacity"`` for contextual HTTP status tokens.

    A code counts only when it appears as an independent token on a line that
    also carries http/status/error semantics.  Auth wins when both appear.
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
_TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _split_exec_only(passthrough: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split operator passthrough into pre-``exec`` and exec-only pairs.

    ``preflight`` has already validated that every option is followed by a
    non-empty value, so the list is walked in flag/value pairs.  Both returned
    lists preserve the operator's original order.
    """

    before_exec: list[str] = []
    exec_only: list[str] = []
    index = 0
    while index < len(passthrough):
        flag = passthrough[index]
        pair = [flag]
        if index + 1 < len(passthrough):
            pair.append(passthrough[index + 1])
        target = exec_only if flag in EXEC_ONLY_WITH_VALUE else before_exec
        target.extend(pair)
        index += 2
    return before_exec, exec_only


def _resolve_codex_home(home: str | os.PathLike[str] | None = None) -> Path:
    """D4: explicit ``CODEX_HOME`` wins, then ``~/.codex``.

    The path is returned as given; callers ``abspath`` it for the child env and
    the settings document.  No file beneath it is read here.
    """

    if home is not None:
        return Path(home)
    explicit = os.environ.get("CODEX_HOME")
    if explicit:
        return Path(explicit)
    return Path.home() / ".codex"


class CodexAdapter:
    """The same :class:`~hermes_kanban_lanes.executors.base.ExecutorAdapter`."""

    name = "codex"
    settings_filename = SETTINGS_FILENAME

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
        extra = [str(item) for item in argv[1:]]
        seen: set[str] = set()
        index = 0
        while index < len(extra):
            flag = extra[index]
            if self._dangerous(flag):
                return False, "executor-dangerous-arg"
            if flag not in PASSTHROUGH_WITH_VALUE:
                return False, "executor-arg-not-allowed"
            if index + 1 >= len(extra):
                return False, "executor-arg-missing-value"
            value = extra[index + 1]
            if not value:
                return False, "executor-arg-empty-value"
            if self._dangerous(value):
                return False, "executor-dangerous-arg"
            # Walked in flag/value pairs, so a value spelling a flag is never
            # counted as an occurrence.  Only ``SINGLE_VALUED`` options are
            # tracked; repeatable ones (``-m``, ``-c``, ...) are left alone.
            # W6 D10 additionally rejects sandbox-widening ``-c/--config``
            # values, which W5 left to the dangerous-value scan.
            if flag in SINGLE_VALUED:
                if flag in seen:
                    return False, "executor-arg-duplicate"
                seen.add(flag)
            if flag in CONFIG_FLAGS and self._sandbox_config(value):
                return False, "executor-dangerous-arg"
            index += 2
        return True, "ok"

    @staticmethod
    def _dangerous(token: str) -> bool:
        if token in DANGEROUS_TOKENS:
            return True
        return any(marker in token for marker in DANGEROUS_MARKERS)

    @staticmethod
    def _sandbox_config(value: str) -> bool:
        """W6 D10: reject sandbox-widening keys inside ``-c/--config``."""

        return any(marker in value for marker in SANDBOX_CONFIG_MARKERS)

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
        # D3: operator argv first, then the fixed protocol arguments in this
        # order, with the prompt last.  ``--session-id`` does not exist; each
        # run is an independent ephemeral thread.
        #
        # ``--output-schema`` is exec-only, so it moves behind ``exec``; every
        # other operator argument keeps its original position and order.
        del session_id
        last_message = os.path.join(run_dir, LAST_MESSAGE_NAME)
        before_exec, exec_only = _split_exec_only(self.passthrough(argv))
        command = [
            str(argv[0]),
            *before_exec,
            "exec",
            "--json",
            "-o",
            last_message,
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox",
            SANDBOX,
            *exec_only,
        ]
        command.append(str(prompt))
        return command

    def settings_document(self) -> dict[str, Any]:
        """Run-local Codex settings (D5.1); never contains a token."""

        codex_home = _resolve_codex_home()
        auth_path = codex_home / "auth.json"
        readable = (
            codex_home.is_dir()
            and auth_path.is_file()
            and os.access(auth_path, os.R_OK)
        )
        return {
            "schema_version": 1,
            "executor": "codex",
            "sandbox": SANDBOX,
            "ignore_user_config": True,
            "ephemeral": True,
            "codex_home": os.path.abspath(os.fspath(codex_home)) if readable else None,
            "passthrough_keys": ["-m", "--model", "-c", "--config"],
        }

    # -- credentials -------------------------------------------------------

    def credential_env(
        self, home: str | os.PathLike[str] | None = None
    ) -> tuple[dict[str, str], str | None]:
        """Return only the child environment needed for Codex authentication.

        Order (D4): an operator ``OPENAI_API_KEY`` is injected in memory; else
        the resolved ``CODEX_HOME`` directory is injected when it holds a
        readable ``auth.json``.  The file is stat'ed, never read, copied or
        printed; the returned dict contains no token content.
        """

        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key:
            return {"OPENAI_API_KEY": api_key}, None
        codex_home = _resolve_codex_home(home)
        auth_path = codex_home / "auth.json"
        if (
            codex_home.is_dir()
            and auth_path.is_file()
            and os.access(auth_path, os.R_OK)
        ):
            return {"CODEX_HOME": os.path.abspath(os.fspath(codex_home))}, None
        return {}, "codex-auth-missing"

    # -- parsing -----------------------------------------------------------

    def parse(self, stdout: str) -> dict[str, Any]:
        """D5: per-line JSONL; non-JSON lines are ignored, empty is ``{}``."""

        parsed: dict[str, Any] = {}
        for raw in (stdout or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id:
                    parsed["thread_id"] = thread_id
            elif kind == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str):
                        # Last agent message wins.
                        parsed["result"] = text
                elif item_type == "error":
                    message = item.get("message")
                    if isinstance(message, str) and message:
                        parsed.setdefault("warnings", []).append(message)
            elif kind == "turn.failed":
                error = event.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                if not isinstance(message, str):
                    fallback = event.get("message")
                    message = fallback if isinstance(fallback, str) else ""
                # The key itself is the failure marker even when text is empty.
                parsed["failure"] = message
            elif kind == "turn.completed":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    parsed["usage"] = usage
        return parsed

    def usage(self, parsed: dict[str, Any]) -> dict[str, Any]:
        """D6: token counts only; ``cost_usd`` is never estimated."""

        usage = parsed.get("usage") if isinstance(parsed, dict) else None
        usage = usage if isinstance(usage, dict) else {}

        def _token(value: Any) -> int | None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        document = {key: _token(usage.get(key)) for key in _TOKEN_KEYS}
        document["cost_usd"] = None
        return document

    @staticmethod
    def permission_denials(parsed: dict[str, Any]) -> list[Any]:
        # Approval events are not part of the measured JSONL shape; never
        # invent denials that the CLI did not report.
        return []

    # -- classification ----------------------------------------------------

    def classify(
        self,
        exit_code: int,
        parsed: dict[str, Any],
        *,
        stderr: str = "",
        timed_out: bool = False,
    ) -> ExecutorOutcome:
        parsed = parsed if isinstance(parsed, dict) else {}
        usage = self.usage(parsed)

        def outcome(category: str, block_kind: str | None) -> ExecutorOutcome:
            failure = parsed.get("failure")
            return ExecutorOutcome(
                exit_code=int(exit_code),
                is_error=category != "success",
                category=category,
                block_kind=block_kind,
                result_text=str(parsed.get("result") or ""),
                terminal_reason=(
                    failure
                    if isinstance(failure, str) and failure
                    else ("timeout" if timed_out else None)
                ),
                usage=usage,
                permission_denials=self.permission_denials(parsed),
                parsed=parsed,
            )

        if timed_out:
            return outcome("timeout", "transient")

        if "failure" in parsed:
            failure = parsed.get("failure")
            failure_text = failure if isinstance(failure, str) else ""
            # Only the CLI-authored failure message and stderr participate; the
            # last agent message is model prose and never a classification
            # signal.  Numeric tokens get HTTP context checks, so ``1401`` or
            # ``F401`` inside a message cannot masquerade as auth failure.
            signal = _http_signal(failure_text, stderr or "")
            if signal is None:
                signal = _phrase_signal(
                    "\n".join(
                        part for part in (failure_text, stderr or "") if part
                    )
                )
            if signal == "auth":
                return outcome("environment_failure", "capability")
            if signal == "capacity":
                return outcome("environment_failure", "transient")
            return outcome("code_failure", "needs_input")

        if exit_code == 0:
            # ``item.type=error`` entries are warnings only (D6).
            return outcome("success", None)
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


__all__ = [
    "CONFIG_FLAGS",
    "CodexAdapter",
    "DANGEROUS_TOKENS",
    "EXEC_ONLY_WITH_VALUE",
    "LAST_MESSAGE_NAME",
    "PASSTHROUGH_WITH_VALUE",
    "SANDBOX",
    "SANDBOX_CONFIG_MARKERS",
    "SETTINGS_FILENAME",
    "SINGLE_VALUED",
]
