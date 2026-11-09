"""Executor adapter protocol.

An executor adapter knows how to validate its configured argv, append its fixed
protocol arguments, parse the result document and classify a failure.  It never
touches the board, the workspace git metadata or the evidence files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence


@dataclass(frozen=True, slots=True)
class ExecutorOutcome:
    """Raw executor outcome before the supervisor applies gate/evidence rules."""

    exit_code: int
    is_error: bool
    category: str
    block_kind: str | None
    result_text: str = ""
    terminal_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    permission_denials: list[Any] = field(default_factory=list)
    parsed: dict[str, Any] = field(default_factory=dict)


class ExecutorAdapter(Protocol):
    name: str

    def preflight(self, argv: Sequence[str]) -> tuple[bool, str]:
        ...

    def build_argv(
        self,
        argv: Sequence[str],
        *,
        run_dir: str,
        session_id: str,
        prompt: str,
    ) -> list[str]:
        ...

    def settings_document(self) -> dict[str, Any]:
        ...

    def credential_env(self) -> tuple[dict[str, str], str | None]:
        ...

    def parse(self, stdout: str) -> dict[str, Any]:
        ...

    def classify(
        self,
        exit_code: int,
        parsed: dict[str, Any],
        *,
        stderr: str = "",
        timed_out: bool = False,
    ) -> ExecutorOutcome:
        ...


__all__ = ["ExecutorAdapter", "ExecutorOutcome"]
