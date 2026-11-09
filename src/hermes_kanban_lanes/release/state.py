"""D9: the release stage journal (atomic, forward-only)."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import RunIdentity

RELEASE_STAGES = ("claimed", "gated", "merged", "postmerge", "terminal")
RELEASE_STAGE_INDEX = {name: index for index, name in enumerate(RELEASE_STAGES)}
RELEASE_STATE_SCHEMA_VERSION = 1
RELEASE_STATE_REQUIREMENT_ID = "HKL-W4"
RELEASE_STATE_FILENAME = "release-state.json"

# D9-I §5.6: the write-ahead intent recorded inside the critical section.  It
# is the only durable prior record that a merge attempt may have started; crash
# recovery reads it instead of guessing from logs.
MERGE_INTENT_SCHEMA_VERSION = 1
MERGE_INTENT_REQUIREMENT_ID = RELEASE_STATE_REQUIREMENT_ID
MERGE_INTENT_FILENAME = "merge-intent.json"

# Recovery decisions (D9-I rule 5).  No decision ever starts a new merge.
MERGE_INTENT_ADOPT = "adopt"
MERGE_INTENT_RETRY = "retry"
MERGE_INTENT_ABORT = "abort"
MERGE_INTENT_DIVERGED = "diverged"
MERGE_INTENT_DECISIONS = (
    MERGE_INTENT_ADOPT,
    MERGE_INTENT_RETRY,
    MERGE_INTENT_ABORT,
    MERGE_INTENT_DIVERGED,
)


@dataclass(slots=True)
class ReleaseState:
    schema_version: int
    requirement_id: str
    identity: RunIdentity
    merge_task_id: str
    dev_task_id: str
    stage: str
    main_before: str | None = None
    reviewed_head: str | None = None
    merge_sha: str | None = None
    merge_tree: str | None = None
    release_report_sha256: str | None = None
    postmerge_report_sha256: str | None = None
    updated_at: int = field(default_factory=lambda: int(time.time()))

    def __post_init__(self) -> None:
        if self.stage not in RELEASE_STAGE_INDEX:
            raise ValueError(f"unknown release stage: {self.stage}")
        if not isinstance(self.identity, RunIdentity):
            raise ValueError("release identity must be a RunIdentity")

    def advance(self, stage: str, **fields: Any) -> "ReleaseState":
        if stage not in RELEASE_STAGE_INDEX:
            raise ValueError(f"unknown release stage: {stage}")
        if RELEASE_STAGE_INDEX[stage] < RELEASE_STAGE_INDEX[self.stage]:
            raise ValueError(
                f"release stage must not regress: {self.stage} -> {stage}"
            )
        for name in fields:
            if not hasattr(self, name):
                raise AttributeError(f"unknown release state field: {name}")
        self.stage = stage
        for name, value in fields.items():
            setattr(self, name, value)
        self.updated_at = int(time.time())
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "requirement_id": self.requirement_id,
            "identity": self.identity.to_dict(),
            "merge_task_id": self.merge_task_id,
            "dev_task_id": self.dev_task_id,
            "stage": self.stage,
            "main_before": self.main_before,
            "reviewed_head": self.reviewed_head,
            "merge_sha": self.merge_sha,
            "merge_tree": self.merge_tree,
            "release_report_sha256": self.release_report_sha256,
            "postmerge_report_sha256": self.postmerge_report_sha256,
            "updated_at": int(self.updated_at),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "ReleaseState":
        from ..contracts import ContractError

        try:
            identity = RunIdentity.from_dict(document.get("identity"))
        except (ContractError, ValueError, TypeError):
            raise ValueError("release identity is invalid") from None
        stage = document.get("stage")
        if stage not in RELEASE_STAGE_INDEX:
            raise ValueError("release stage is invalid")
        if document.get("schema_version") != RELEASE_STATE_SCHEMA_VERSION:
            raise ValueError("release schema_version is invalid")
        return cls(
            schema_version=RELEASE_STATE_SCHEMA_VERSION,
            requirement_id=str(
                document.get("requirement_id") or RELEASE_STATE_REQUIREMENT_ID
            ),
            identity=identity,
            merge_task_id=str(document.get("merge_task_id") or identity.task_id),
            dev_task_id=str(document.get("dev_task_id") or ""),
            stage=str(stage),
            main_before=document.get("main_before"),
            reviewed_head=document.get("reviewed_head"),
            merge_sha=document.get("merge_sha"),
            merge_tree=document.get("merge_tree"),
            release_report_sha256=document.get("release_report_sha256"),
            postmerge_report_sha256=document.get("postmerge_report_sha256"),
            updated_at=int(document.get("updated_at") or 0),
        )


def new_release_state(
    *,
    identity: RunIdentity,
    dev_task_id: str,
    requirement_id: str = RELEASE_STATE_REQUIREMENT_ID,
) -> ReleaseState:
    return ReleaseState(
        schema_version=RELEASE_STATE_SCHEMA_VERSION,
        requirement_id=requirement_id,
        identity=identity,
        merge_task_id=identity.task_id,
        dev_task_id=dev_task_id,
        stage="claimed",
    )


def release_state_path(run_dir: str | os.PathLike[str]) -> Path:
    return Path(run_dir) / RELEASE_STATE_FILENAME


def write_release_state(
    run_dir: str | os.PathLike[str], state: ReleaseState
) -> Path:
    target = release_state_path(run_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def read_release_state(run_dir: str | os.PathLike[str]) -> ReleaseState | None:
    try:
        document = json.loads(
            release_state_path(run_dir).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    try:
        return ReleaseState.from_dict(document)
    except (ValueError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# merge-intent.json (D9-I §5.6)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MergeIntent:
    """The write-ahead record that a merge attempt may have started."""

    identity: RunIdentity
    main_ref: str
    main_before: str
    reviewed_head: str
    task_id: str
    run_id: int
    claim_lock: str
    schema_version: int = MERGE_INTENT_SCHEMA_VERSION
    requirement_id: str = MERGE_INTENT_REQUIREMENT_ID
    intent_at: int = field(default_factory=lambda: int(time.time()))

    def __post_init__(self) -> None:
        if not isinstance(self.identity, RunIdentity):
            raise ValueError("merge intent identity must be a RunIdentity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "requirement_id": self.requirement_id,
            "identity": self.identity.to_dict(),
            "task_id": self.task_id,
            "run_id": self.run_id,
            "claim_lock": self.claim_lock,
            "main_ref": self.main_ref,
            "main_before": self.main_before,
            "reviewed_head": self.reviewed_head,
            "intent_at": int(self.intent_at),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "MergeIntent":
        from ..contracts import ContractError

        try:
            identity = RunIdentity.from_dict(document.get("identity"))
        except (ContractError, ValueError, TypeError):
            raise ValueError("merge intent identity is invalid") from None
        if document.get("schema_version") != MERGE_INTENT_SCHEMA_VERSION:
            raise ValueError("merge intent schema_version is invalid")
        task_id = document.get("task_id")
        run_id = document.get("run_id")
        claim_lock = document.get("claim_lock")
        main_ref = document.get("main_ref")
        main_before = document.get("main_before")
        reviewed_head = document.get("reviewed_head")
        if (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(run_id, int)
            or not isinstance(claim_lock, str)
            or not claim_lock
            or not isinstance(main_ref, str)
            or not main_ref
            or not isinstance(main_before, str)
            or not main_before
            or not isinstance(reviewed_head, str)
            or not reviewed_head
        ):
            raise ValueError("merge intent fields are invalid")
        return cls(
            identity=identity,
            main_ref=main_ref,
            main_before=main_before,
            reviewed_head=reviewed_head,
            task_id=task_id,
            run_id=run_id,
            claim_lock=claim_lock,
            requirement_id=str(
                document.get("requirement_id") or MERGE_INTENT_REQUIREMENT_ID
            ),
            intent_at=int(document.get("intent_at") or 0),
        )


def new_merge_intent(
    *,
    identity: RunIdentity,
    main_ref: str,
    main_before: str,
    reviewed_head: str,
    requirement_id: str = MERGE_INTENT_REQUIREMENT_ID,
) -> MergeIntent:
    return MergeIntent(
        identity=identity,
        main_ref=main_ref,
        main_before=main_before,
        reviewed_head=reviewed_head,
        task_id=identity.task_id,
        run_id=identity.run_id,
        claim_lock=identity.claim_lock,
        requirement_id=requirement_id,
    )


def merge_intent_path(run_dir: str | os.PathLike[str]) -> Path:
    return Path(run_dir) / MERGE_INTENT_FILENAME


def write_merge_intent(
    run_dir: str | os.PathLike[str], intent: MergeIntent
) -> Path:
    """Atomically replace ``merge-intent.json`` (never a partial document)."""

    target = merge_intent_path(run_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(intent.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def read_merge_intent(run_dir: str | os.PathLike[str]) -> MergeIntent | None:
    try:
        document = json.loads(
            merge_intent_path(run_dir).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    try:
        return MergeIntent.from_dict(document)
    except (ValueError, KeyError, TypeError):
        return None


def classify_merge_intent(
    *,
    repo_head: str | None,
    parents: Sequence[str],
    merge_head_present: bool,
    main_before: str,
    reviewed_head: str,
) -> str:
    """D9-I rule 5: decide what an interrupted critical section left behind.

    Pure: the caller supplies the observed git facts.  The result is one of
    ``MERGE_INTENT_DECISIONS``; callers must never re-run a new merge.
    """

    parent_set = set(parents)
    if (
        repo_head is not None
        and len(parents) == 2
        and parent_set == {main_before, reviewed_head}
    ):
        return MERGE_INTENT_ADOPT
    if repo_head == main_before and not merge_head_present:
        return MERGE_INTENT_RETRY
    if merge_head_present:
        return MERGE_INTENT_ABORT
    return MERGE_INTENT_DIVERGED


__all__ = [
    "MERGE_INTENT_ABORT",
    "MERGE_INTENT_ADOPT",
    "MERGE_INTENT_DECISIONS",
    "MERGE_INTENT_DIVERGED",
    "MERGE_INTENT_FILENAME",
    "MERGE_INTENT_REQUIREMENT_ID",
    "MERGE_INTENT_RETRY",
    "MERGE_INTENT_SCHEMA_VERSION",
    "MergeIntent",
    "RELEASE_STAGES",
    "RELEASE_STAGE_INDEX",
    "RELEASE_STATE_FILENAME",
    "RELEASE_STATE_REQUIREMENT_ID",
    "ReleaseState",
    "classify_merge_intent",
    "merge_intent_path",
    "new_merge_intent",
    "new_release_state",
    "read_merge_intent",
    "read_release_state",
    "release_state_path",
    "write_merge_intent",
    "write_release_state",
]
