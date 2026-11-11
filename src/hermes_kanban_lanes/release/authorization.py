"""D6: explicit operator authorization for one release.

The authorization file is an input to the release gate, never a signal the
product can synthesize.  It names the exact reviewed commit and evidence, the
main ref/commit it authorizes, an approver distinct from the implementer and
the reviewer, and a bounded validity window.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

AUTHORIZATION_SCHEMA_VERSION = 1
# Owner decision 2026-11-11: authorization may be synthesized by policy for
# ordinary changes.  A synthesized record is auditable: its ``source`` says
# ``policy`` and its approver is the reserved ``policy:auto`` identity, which a
# human/file approval may never use.
AUTHORIZATION_SOURCE_FILE = "file"
AUTHORIZATION_SOURCE_POLICY = "policy"
AUTHORIZATION_SOURCES = frozenset(
    {AUTHORIZATION_SOURCE_FILE, AUTHORIZATION_SOURCE_POLICY}
)
POLICY_APPROVER = "policy:auto"
POLICY_APPROVER_PREFIX = "policy:"
# Synthesized records live in the release run directory, never in the
# operator-owned ``<work_root>/authorizations/`` directory.
POLICY_AUTHORIZATION_FILENAME = "authorization.json"
# ``source`` is optional so every pre-existing authorization document keeps
# loading unchanged; it is not part of the required field tuple.
AUTHORIZATION_OPTIONAL_FIELDS = ("source",)
AUTHORIZATION_FIELDS = (
    "schema_version",
    "requirement_id",
    "task_id",
    "review_run_id",
    "dev_run_id",
    "rework_round",
    "task_base_commit",
    "reviewed_head",
    "reviewed_tree",
    "evidence_index_sha256",
    "main_ref",
    "main_commit",
    "approver",
    "approved_at",
    "expires_at",
)
_BINDING_FIELDS = (
    "requirement_id",
    "task_id",
    "review_run_id",
    "dev_run_id",
    "rework_round",
    "task_base_commit",
    "reviewed_head",
    "reviewed_tree",
    "evidence_index_sha256",
    "main_ref",
    "main_commit",
)


def _is_hex(value: Any, lengths: tuple[int, ...] = (40, 64)) -> bool:
    if not isinstance(value, str) or len(value) not in lengths:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True, slots=True)
class ReleaseAuthorization:
    schema_version: int
    requirement_id: str
    task_id: str
    review_run_id: int
    dev_run_id: int
    rework_round: int
    task_base_commit: str
    reviewed_head: str
    reviewed_tree: str
    evidence_index_sha256: str
    main_ref: str
    main_commit: str
    approver: str
    approved_at: int
    expires_at: int
    note: str | None = None
    source: str | None = None
    path: str | None = None
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        document = {
            "schema_version": self.schema_version,
            "requirement_id": self.requirement_id,
            "task_id": self.task_id,
            "review_run_id": self.review_run_id,
            "dev_run_id": self.dev_run_id,
            "rework_round": self.rework_round,
            "task_base_commit": self.task_base_commit,
            "reviewed_head": self.reviewed_head,
            "reviewed_tree": self.reviewed_tree,
            "evidence_index_sha256": self.evidence_index_sha256,
            "main_ref": self.main_ref,
            "main_commit": self.main_commit,
            "approver": self.approver,
            "approved_at": self.approved_at,
            "expires_at": self.expires_at,
        }
        if self.note is not None:
            document["note"] = self.note
        if self.source is not None:
            document["source"] = self.source
        if self.path is not None:
            document["path"] = self.path
        if self.sha256 is not None:
            document["sha256"] = self.sha256
        return document


def authorization_problems(
    authorization: ReleaseAuthorization | None,
    expected: Mapping[str, Any],
    *,
    now: int | None = None,
    implementer: str | None = None,
    reviewer: str | None = None,
    executor: str | None = None,
    allow_policy_approver: bool = False,
) -> list[str]:
    if authorization is None:
        return ["authorization-missing"]

    current = int(time.time()) if now is None else int(now)
    problems: list[str] = []

    if authorization.expires_at <= current:
        problems.append("authorization-expired")
    if authorization.approved_at > current:
        problems.append("authorization-not-yet-valid")

    approver = authorization.approver
    if not approver.strip():
        problems.append("authorization-approver-invalid")
    elif (
        approver.strip().startswith(POLICY_APPROVER_PREFIX)
        and not allow_policy_approver
    ):
        # A file/human authorization must never claim the reserved policy
        # identity: that would let a forged document bypass operator approval.
        problems.append("authorization-policy-approver-forbidden")
    else:
        forbidden = {
            value
            for value in (implementer, reviewer, executor)
            if isinstance(value, str) and value
        }
        if approver in forbidden:
            problems.append("authorization-approver-invalid")

    for field in _BINDING_FIELDS:
        wanted = expected.get(field)
        if wanted is None:
            continue
        if getattr(authorization, field) != wanted:
            problems.append(f"authorization-mismatch:{field}")

    return list(dict.fromkeys(problems))


def build_policy_authorization(
    expected: Mapping[str, Any],
    *,
    approved_at: int,
    expires_at: int,
) -> ReleaseAuthorization | None:
    """Synthesize the auditable policy record for one already-reviewed binding.

    Every field is read from ``expected``, which the gate builds exclusively
    from the current :class:`ReviewBinding` (never from caller input or raw
    review metadata).  ``None`` means the binding is incomplete, so policy may
    not mint a pass.  The result still has to pass
    :func:`authorization_problems` before it can release anything.
    """

    values = {
        field: expected.get(field)
        for field in _BINDING_FIELDS + ("requirement_id", "main_ref", "main_commit")
    }
    if any(value is None for value in values.values()):
        return None
    try:
        return ReleaseAuthorization(
            schema_version=AUTHORIZATION_SCHEMA_VERSION,
            requirement_id=str(values["requirement_id"]),
            task_id=str(values["task_id"]),
            review_run_id=int(values["review_run_id"]),
            dev_run_id=int(values["dev_run_id"]),
            rework_round=int(values["rework_round"]),
            task_base_commit=str(values["task_base_commit"]),
            reviewed_head=str(values["reviewed_head"]),
            reviewed_tree=str(values["reviewed_tree"]),
            evidence_index_sha256=str(values["evidence_index_sha256"]),
            main_ref=str(values["main_ref"]),
            main_commit=str(values["main_commit"]),
            approver=POLICY_APPROVER,
            approved_at=int(approved_at),
            expires_at=int(expires_at),
            source=AUTHORIZATION_SOURCE_POLICY,
        )
    except (TypeError, ValueError):
        return None


def load_authorization(
    path: str | Path,
) -> tuple[ReleaseAuthorization | None, list[str]]:
    """Read one authorization document; never creates or repairs it."""

    from ..verification.evidence import sha256_file

    target = Path(path)
    if not target.is_file():
        return None, ["authorization-missing"]
    try:
        raw = target.read_text(encoding="utf-8")
        document = json.loads(raw)
    except (OSError, ValueError):
        return None, ["authorization-invalid"]
    if not isinstance(document, dict):
        return None, ["authorization-invalid"]

    problems: list[str] = []
    allowed = (
        set(AUTHORIZATION_FIELDS)
        | {"note"}
        | set(AUTHORIZATION_OPTIONAL_FIELDS)
    )
    unknown = sorted(set(document) - allowed)
    if unknown:
        problems.append("authorization-invalid:" + ",".join(unknown[:8]))
    for field in AUTHORIZATION_FIELDS:
        if field not in document:
            if field == "expires_at":
                problems.append("authorization-no-expiry")
            else:
                problems.append(f"authorization-missing-field:{field}")

    if document.get("schema_version") != AUTHORIZATION_SCHEMA_VERSION:
        problems.append("authorization-schema")
    for field in ("requirement_id", "task_id", "main_ref", "approver"):
        value = document.get(field)
        if not isinstance(value, str) or not value:
            problems.append(f"authorization-field-invalid:{field}")
    note = document.get("note")
    if note is not None and not isinstance(note, str):
        problems.append("authorization-field-invalid:note")
    source = document.get("source")
    if source is not None and (
        not isinstance(source, str) or source not in AUTHORIZATION_SOURCES
    ):
        problems.append("authorization-field-invalid:source")
    for field in ("review_run_id", "dev_run_id", "rework_round"):
        if not _is_positive_int(document.get(field)):
            problems.append(f"authorization-field-invalid:{field}")
    for field in (
        "task_base_commit",
        "reviewed_head",
        "reviewed_tree",
        "main_commit",
    ):
        if not _is_hex(document.get(field)):
            problems.append(f"authorization-field-invalid:{field}")
    if not _is_hex(document.get("evidence_index_sha256"), (64,)):
        problems.append("authorization-field-invalid:evidence_index_sha256")
    for field in ("approved_at", "expires_at"):
        if not isinstance(document.get(field), int) or isinstance(
            document.get(field), bool
        ):
            problems.append(f"authorization-field-invalid:{field}")

    if problems:
        return None, list(dict.fromkeys(problems))

    try:
        digest = sha256_file(target)
    except OSError:
        return None, ["authorization-invalid"]

    return (
        ReleaseAuthorization(
            schema_version=AUTHORIZATION_SCHEMA_VERSION,
            requirement_id=str(document["requirement_id"]),
            task_id=str(document["task_id"]),
            review_run_id=int(document["review_run_id"]),
            dev_run_id=int(document["dev_run_id"]),
            rework_round=int(document["rework_round"]),
            task_base_commit=str(document["task_base_commit"]),
            reviewed_head=str(document["reviewed_head"]),
            reviewed_tree=str(document["reviewed_tree"]),
            evidence_index_sha256=str(document["evidence_index_sha256"]),
            main_ref=str(document["main_ref"]),
            main_commit=str(document["main_commit"]),
            approver=str(document["approver"]),
            approved_at=int(document["approved_at"]),
            expires_at=int(document["expires_at"]),
            note=str(note) if note is not None else None,
            source=str(source) if source is not None else None,
            path=str(target),
            sha256=digest,
        ),
        [],
    )


__all__ = [
    "AUTHORIZATION_FIELDS",
    "AUTHORIZATION_OPTIONAL_FIELDS",
    "AUTHORIZATION_SCHEMA_VERSION",
    "AUTHORIZATION_SOURCE_FILE",
    "AUTHORIZATION_SOURCE_POLICY",
    "AUTHORIZATION_SOURCES",
    "POLICY_APPROVER",
    "POLICY_APPROVER_PREFIX",
    "POLICY_AUTHORIZATION_FILENAME",
    "ReleaseAuthorization",
    "authorization_problems",
    "build_policy_authorization",
    "load_authorization",
]
