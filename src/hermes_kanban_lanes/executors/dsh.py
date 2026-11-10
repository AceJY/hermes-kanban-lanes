"""DeepSeek Harness (``dsh``) headless executor adapter.

``dsh --profile headless --json <task>`` answers one task and exits, writing
newline-delimited run events to stdout.  The configured argv names only the
launcher (bare ``dsh`` or an absolute path); this adapter appends the fixed
protocol shape after it.  A cold run never passes ``--session-id`` (``dsh``
rejects an unknown id) and never needs a patch overlay: the run-local worktree
is the isolation boundary.

The event stream carries the Session id in a leading
``{"type": "session", "sessionId": ...}`` event and the assistant text in
``{"type": "text", ...}`` / ``{"type": "final", ...}`` events.  Nothing else
about the CLI is inferred from the stream: there is no structured usage
envelope, so usage stays unaccounted instead of guessed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Sequence

from .base import ExecutorOutcome

# Anything that lets an operator smuggle a second command into one argv token.
# The executor runs without a shell, but a rejected token keeps a config typo
# from looking like a supported command line.
SHELL_OPERATORS = (";", "&&", "||", "|", "&", ">", "<", "`", "$(", "\n")


class DshAdapter:
    """The :class:`~hermes_kanban_lanes.executors.base.ExecutorAdapter` for dsh."""

    name = "dsh"
    settings_filename = "dsh-settings.json"

    # -- configuration -----------------------------------------------------

    def preflight(self, argv: Sequence[str]) -> tuple[bool, str]:
        if not argv:
            return False, "executor-argv-empty"
        executable = str(argv[0])
        if os.path.basename(executable) != "dsh":
            return False, "executor-wrong-binary"
        for token in argv:
            if any(operator in str(token) for operator in SHELL_OPERATORS):
                return False, "executor-shell-operator"
        if len(argv) > 1:
            # dsh's own options are not part of the launcher contract: the
            # adapter appends the headless protocol after argv[0], so a second
            # token would be parsed by the launcher and fail before the run.
            # Fail closed instead of guessing where an operator option belongs.
            return False, "executor-argv-unsupported"
        if os.path.sep in executable:
            if not (os.path.isfile(executable) and os.access(executable, os.X_OK)):
                return False, "executor-not-executable"
        elif shutil.which(executable) is None:
            return False, "executor-not-found"
        return True, "ok"

    def build_argv(
        self,
        argv: Sequence[str],
        *,
        run_dir: str,
        session_id: str,
        prompt: str,
    ) -> list[str]:
        # A cold run names no session: dsh only accepts ``--session-id`` for an
        # id that already exists.  The prompt stays a positional argument.
        del run_dir, session_id
        command = [str(item) for item in argv]
        command.extend(["--profile", "headless", "--json"])
        command.append(str(prompt))
        return command

    def settings_document(self) -> dict[str, Any]:
        """dsh reads its own layered configuration; there is nothing to add."""

        return {}

    # -- credentials -------------------------------------------------------

    def credential_env(
        self, home: str | os.PathLike[str] | None = None
    ) -> tuple[dict[str, str], str | None]:
        """Reuse the host environment; the adapter never writes credentials."""

        del home
        return {}, None

    # -- parsing -----------------------------------------------------------

    def parse(self, stdout: str) -> dict[str, Any]:
        """Read the NDJSON event stream; junk lines are ignored."""

        parsed: dict[str, Any] = {}
        last_text = ""
        final_text = ""
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
            if kind == "session":
                # Only the first session event names the run's Session.
                if "session_id" not in parsed:
                    session_id = event.get("sessionId") or event.get("session_id")
                    if isinstance(session_id, str) and session_id:
                        parsed["session_id"] = session_id
                        # The supervisor records ``thread_id`` as the executor
                        # session id; expose the same value under its seam.
                        parsed["thread_id"] = session_id
            elif kind == "text":
                text = event.get("text")
                if isinstance(text, str):
                    last_text = text
            elif kind == "final":
                text = event.get("text")
                if isinstance(text, str):
                    final_text = text
        # The final answer wins; a stream that only emitted assistant text
        # falls back to the last such block.  An absent answer is the empty
        # string, never an inferred success envelope.
        parsed["result"] = final_text or last_text
        return parsed

    # -- classification ----------------------------------------------------

    def classify(
        self,
        exit_code: int,
        parsed: dict[str, Any],
        *,
        stderr: str = "",
        timed_out: bool = False,
    ) -> ExecutorOutcome:
        del stderr
        parsed = parsed if isinstance(parsed, dict) else {}
        result_text = str(parsed.get("result") or "")

        def outcome(
            category: str,
            block_kind: str | None,
            *,
            terminal_reason: str | None = None,
        ) -> ExecutorOutcome:
            return ExecutorOutcome(
                exit_code=int(exit_code),
                is_error=category != "success",
                category=category,
                block_kind=block_kind,
                result_text=result_text,
                terminal_reason=terminal_reason,
                parsed=parsed,
            )

        if timed_out:
            return outcome("timeout", "transient", terminal_reason="timeout")
        if int(exit_code) == 0:
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


__all__ = ["DshAdapter", "SHELL_OPERATORS"]
