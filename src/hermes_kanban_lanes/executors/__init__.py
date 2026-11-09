"""Executor adapter registry (W5 D2).

``ADAPTERS`` is the single source of truth for the supported executor aliases.
The daemon and the card contract both read it; an unregistered alias is
fail-closed rather than silently downgraded.
"""

from __future__ import annotations

from .base import ExecutorAdapter, ExecutorOutcome
from .claude import ClaudeAdapter
from .codex import CodexAdapter

ADAPTERS = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
}


def adapter_for(alias: str) -> ExecutorAdapter:
    """Instantiate the adapter registered for ``alias``.

    Raises :class:`KeyError` for an unknown alias; callers translate that into
    their own fail-closed error (the daemon uses ``BoardAdapterError``).
    """

    try:
        factory = ADAPTERS[alias]
    except KeyError:
        raise KeyError(f"executor-not-supported:{alias}") from None
    return factory()


__all__ = [
    "ADAPTERS",
    "ClaudeAdapter",
    "CodexAdapter",
    "ExecutorAdapter",
    "ExecutorOutcome",
    "adapter_for",
]
