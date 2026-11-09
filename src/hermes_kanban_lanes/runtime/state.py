"""Run-level stage journal (W3 D7).

``state.json`` lives in the run directory and is replaced atomically on every
transition (temp file + ``os.replace``).  The supervisor writes the executor
child identity immediately after ``spawn`` succeeds, so a crashed supervisor
leaves enough durable evidence for the next run to prove (or fail to prove)
that the old process group is gone.

The journal is a *journal*, not a substitute for ``run.json``: a terminated
run's final summary is still ``run.json``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..contracts import RunIdentity

STAGES = (
    "claimed",
    "workspace",
    "executor",
    "executor_exited",
    "committed",
    "synced",
    "gated",
    "evidence",
    "terminal",
)
STAGE_INDEX = {name: index for index, name in enumerate(STAGES)}
STATE_SCHEMA_VERSION = 1
STATE_REQUIREMENT_ID = "HKL-W3"
STATE_FILENAME = "state.json"


@dataclass(slots=True)
class RunState:
    """One durable stage journal record."""

    schema_version: int
    requirement_id: str
    identity: RunIdentity
    board: str
    task_id: str
    run_id: int
    claim_lock: str
    run_dir: str
    workspace: str
    stage: str
    supervisor_pid: int | None = None
    supervisor_fingerprint: str | None = None
    child: dict[str, Any] | None = None
    base_commit: str | None = None
    sync_base: str | None = None
    run_commit: str | None = None
    code_commit: str | None = None
    gate_verdict: str | None = None
    round: int = 1
    task_base_commit: str | None = None
    continuation_base: str | None = None
    review: dict[str, Any] | None = None
    updated_at: int = field(default_factory=lambda: int(time.time()))

    def __post_init__(self) -> None:
        if self.stage not in STAGE_INDEX:
            raise ValueError(f"unknown run stage: {self.stage}")
        if not isinstance(self.identity, RunIdentity):
            raise ValueError("state identity must be a RunIdentity")

    def advance(self, stage: str, **fields: Any) -> "RunState":
        """Move forward (or re-write the current stage); never regress."""

        if stage not in STAGE_INDEX:
            raise ValueError(f"unknown run stage: {stage}")
        if STAGE_INDEX[stage] < STAGE_INDEX[self.stage]:
            raise ValueError(
                f"run stage must not regress: {self.stage} -> {stage}"
            )
        self.stage = stage
        for name, value in fields.items():
            if not hasattr(self, name):
                raise AttributeError(f"unknown run state field: {name}")
            setattr(self, name, value)
        self.updated_at = int(time.time())
        return self

    def set_child(
        self,
        *,
        pid: int | None,
        pgid: int | None,
        fingerprint: str | None,
    ) -> "RunState":
        """Record the child identity; never remove an unproven child."""

        self.child = {
            "pid": int(pid) if pid else None,
            "pgid": int(pgid) if pgid else None,
            "fingerprint": fingerprint,
        }
        self.updated_at = int(time.time())
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "requirement_id": self.requirement_id,
            "identity": self.identity.to_dict(),
            "board": self.board,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "claim_lock": self.claim_lock,
            "run_dir": self.run_dir,
            "workspace": self.workspace,
            "stage": self.stage,
            "supervisor_pid": self.supervisor_pid,
            "supervisor_fingerprint": self.supervisor_fingerprint,
            "child": dict(self.child) if self.child else None,
            "base_commit": self.base_commit,
            "sync_base": self.sync_base,
            "run_commit": self.run_commit,
            "code_commit": self.code_commit,
            "gate_verdict": self.gate_verdict,
            "round": int(self.round),
            "task_base_commit": self.task_base_commit,
            "continuation_base": self.continuation_base,
            "review": dict(self.review) if self.review else None,
            "updated_at": int(self.updated_at),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "RunState":
        identity = RunIdentity.from_dict(document.get("identity"))
        stage = document.get("stage")
        if stage not in STAGE_INDEX:
            raise ValueError("state stage is invalid")
        schema_version = document.get("schema_version")
        if schema_version != STATE_SCHEMA_VERSION:
            raise ValueError("state schema_version is invalid")
        child = document.get("child")
        if child is not None and not isinstance(child, dict):
            raise ValueError("state child is invalid")
        review = document.get("review")
        if review is not None and not isinstance(review, dict):
            raise ValueError("state review is invalid")
        round_number = document.get("round", 1)
        if isinstance(round_number, bool) or not isinstance(round_number, int):
            raise ValueError("state round is invalid")
        if round_number < 1:
            raise ValueError("state round is invalid")
        return cls(
            schema_version=STATE_SCHEMA_VERSION,
            requirement_id=str(
                document.get("requirement_id") or STATE_REQUIREMENT_ID
            ),
            identity=identity,
            board=str(document.get("board") or ""),
            task_id=str(document.get("task_id") or identity.task_id),
            run_id=int(document.get("run_id") or identity.run_id),
            claim_lock=str(document.get("claim_lock") or identity.claim_lock),
            run_dir=str(document.get("run_dir") or ""),
            workspace=str(document.get("workspace") or ""),
            stage=str(stage),
            supervisor_pid=document.get("supervisor_pid"),
            supervisor_fingerprint=document.get("supervisor_fingerprint"),
            child=dict(child) if child else None,
            base_commit=document.get("base_commit"),
            sync_base=document.get("sync_base"),
            run_commit=document.get("run_commit"),
            code_commit=document.get("code_commit"),
            gate_verdict=document.get("gate_verdict"),
            round=round_number,
            task_base_commit=document.get("task_base_commit"),
            continuation_base=document.get("continuation_base"),
            review=dict(review) if review else None,
            updated_at=int(document.get("updated_at") or 0),
        )


def new_state(
    *,
    identity: RunIdentity,
    board: str,
    run_dir: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
    supervisor_pid: int | None = None,
    supervisor_fingerprint: str | None = None,
    requirement_id: str = STATE_REQUIREMENT_ID,
    round: int = 1,
    task_base_commit: str | None = None,
    continuation_base: str | None = None,
    review: Mapping[str, Any] | None = None,
) -> RunState:
    return RunState(
        schema_version=STATE_SCHEMA_VERSION,
        requirement_id=requirement_id,
        identity=identity,
        board=board,
        task_id=identity.task_id,
        run_id=identity.run_id,
        claim_lock=identity.claim_lock,
        run_dir=str(run_dir),
        workspace=str(workspace),
        stage="claimed",
        supervisor_pid=supervisor_pid,
        supervisor_fingerprint=supervisor_fingerprint,
        round=round,
        task_base_commit=task_base_commit,
        continuation_base=continuation_base,
        review=dict(review) if review else None,
    )


def state_path(run_dir: str | os.PathLike[str]) -> Path:
    return Path(run_dir) / STATE_FILENAME


def write_state(
    run_dir: str | os.PathLike[str], state: RunState
) -> Path:
    """Atomically replace ``state.json`` (temp file + rename)."""

    target = state_path(run_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def read_state(run_dir: str | os.PathLike[str]) -> RunState | None:
    """Read ``state.json``; ``None`` when absent or unreadable."""

    try:
        document = json.loads(
            state_path(run_dir).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    try:
        return RunState.from_dict(document)
    except (ValueError, KeyError, TypeError):
        return None


__all__ = [
    "STAGES",
    "STAGE_INDEX",
    "STATE_FILENAME",
    "STATE_REQUIREMENT_ID",
    "RunState",
    "new_state",
    "read_state",
    "state_path",
    "write_state",
]
