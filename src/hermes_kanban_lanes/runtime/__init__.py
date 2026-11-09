"""Synchronous execution kernel: process lifecycle, supervisor and daemon."""

from __future__ import annotations

from .process import (
    ManagedChild,
    process_group_members,
    prove_terminated,
    spawn,
    terminate,
)
from .state import RunState, new_state, read_state, write_state
from .supervisor import (
    RunOutcome,
    RunSettings,
    recover_run,
    recover_runs,
    run_once,
)

__all__ = [
    "ManagedChild",
    "RunOutcome",
    "RunSettings",
    "RunState",
    "new_state",
    "process_group_members",
    "prove_terminated",
    "read_state",
    "recover_run",
    "recover_runs",
    "run_once",
    "spawn",
    "terminate",
    "write_state",
]
