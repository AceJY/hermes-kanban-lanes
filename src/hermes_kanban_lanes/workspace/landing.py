"""Pure landing identity checks and reclaim-marker helpers.

This module never opens a database, never signals a process and never imports
the runtime.  It answers two questions from data the caller already holds:

* is this workspace the *current* run's workspace, and does the freshly re-read
  board row still belong to the same run/claim?
* where does the reclaim marker for an old, proven-terminated workspace live?

A path/identity match is necessary but not sufficient to prove filesystem
isolation; v0.1 does not claim same-UID isolation.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..contracts import RunIdentity
from .git import (
    GitError,
    branch_tip,
    current_branch,
    is_ancestor,
    merge_tree_preflight,
    resolve_ref,
)


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.fspath(path))


def check_workspace_identity(
    workspace: str | os.PathLike[str],
    current_workspace: str | os.PathLike[str],
    row: Mapping[str, Any] | None,
    identity: RunIdentity,
) -> dict[str, Any]:
    """Judge one landing request against the current board row.

    The caller must have re-read ``row`` on its own supervisor connection
    immediately before calling this.  The result never writes a marker.
    """

    same_path = _real(workspace) == _real(current_workspace)
    ownership = {
        "task_id": identity.task_id,
        "expected_run_id": identity.run_id,
        "expected_claim_lock": identity.claim_lock,
        "status": row.get("status") if row else None,
        "current_run_id": row.get("current_run_id") if row else None,
        "claim_lock": row.get("claim_lock") if row else None,
        "checked": True,
        "checked_via": "same board connection re-read before marker write",
        "matched": False,
    }
    if row is not None:
        ownership["matched"] = (
            row.get("status") == "running"
            and row.get("current_run_id") is not None
            and int(row["current_run_id"]) == identity.run_id
            and row.get("claim_lock") == identity.claim_lock
        )

    reason: str | None = None
    if not same_path:
        reason = "not-current-run-workspace"
    elif row is None:
        reason = "task-not-found"
    elif not ownership["matched"]:
        reason = "stale-run-or-claim"

    return {
        "ok": reason is None,
        "reason": reason,
        "workspace": _real(workspace),
        "current_workspace": _real(current_workspace),
        "ownership": ownership,
    }


def reclaim_marker_path(
    reclaimable_root: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
) -> Path:
    """Marker written only when the old run is proven fully terminated."""

    return Path(reclaimable_root) / f"{Path(workspace).name}.reclaimable.json"


def unreusable_marker_path(
    reclaimable_root: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
) -> Path:
    """Marker for a workspace whose termination could not be proven."""

    return Path(reclaimable_root) / f"{Path(workspace).name}.unreusable.json"


def _write_marker(
    marker: Path,
    workspace: str | os.PathLike[str],
    *,
    reusable: bool,
    reason: str,
    witness: Mapping[str, Any] | None,
    run_id: int | None,
) -> Path:
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "workspace": os.path.abspath(os.fspath(workspace)),
        "reusable": bool(reusable),
        "reason": reason,
        "run_id": run_id,
        "witness": dict(witness) if witness else None,
        "marker_id": uuid.uuid4().hex,
    }
    temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, marker)
    return marker


def write_reclaim_marker(
    reclaimable_root: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
    *,
    reusable: bool = True,
    reason: str,
    witness: Mapping[str, Any] | None = None,
    run_id: int | None = None,
) -> Path:
    """Write ``<workspace>.reclaimable.json``; never deletes the workspace."""

    return _write_marker(
        reclaim_marker_path(reclaimable_root, workspace),
        workspace,
        reusable=reusable,
        reason=reason,
        witness=witness,
        run_id=run_id,
    )


def write_unreusable_marker(
    reclaimable_root: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
    *,
    reason: str,
    witness: Mapping[str, Any] | None = None,
    run_id: int | None = None,
) -> Path:
    """Write ``<workspace>.unreusable.json``; absence is never used as proof."""

    return _write_marker(
        unreusable_marker_path(reclaimable_root, workspace),
        workspace,
        reusable=False,
        reason=reason,
        witness=witness,
        run_id=run_id,
    )


def is_within_workspace(
    target: str | os.PathLike[str], workspace: str | os.PathLike[str]
) -> bool:
    """True when ``target`` (after realpath) is inside ``workspace``."""

    root = _real(workspace)
    resolved = _real(target)
    return resolved == root or resolved.startswith(root + os.sep)


LANDING_REQUIREMENT_ID = "HKL-W3"
LANDING_PLAN_RELATIVE_PATH = "landing-plan.json"


@dataclass(frozen=True, slots=True)
class LandingPlan:
    """Result of a landing preflight; W3 never merges or updates ``main``."""

    ok: bool
    verdict: str  # "ready" | "not-ready" | "rejected"
    reasons: tuple[str, ...]
    checks: dict[str, bool]
    document: dict[str, Any] | None = None
    plan_path: str | None = None
    merge_tree: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "checks": dict(self.checks),
            "plan_path": self.plan_path,
            "merge_tree": self.merge_tree,
        }


def landing_preflight(
    *,
    run_dir: str | os.PathLike[str],
    identity: RunIdentity,
    workspace: str | os.PathLike[str],
    current_workspace: str | os.PathLike[str],
    row: Mapping[str, Any] | None,
    repo: str | os.PathLike[str],
    main_ref: str,
    package_dir: str | os.PathLike[str],
    base_commit: str,
    sync_base: str,
    code_commit: str,
    branch: str | None = None,
    requirement_id: str = LANDING_REQUIREMENT_ID,
) -> LandingPlan:
    """Judge whether ``code_commit`` could land on ``main_ref``; never merges.

    ``row`` must be re-read on the caller's own board connection immediately
    before this call.  Identity/path/evidence/tip/ancestry failures return
    ``ok=false`` and write **no** plan.  A clean merge-tree writes a
    ``ready`` plan; a conflicting merge-tree writes a ``not-ready`` plan with
    ``merge_tree=null`` and ``reasons=["merge-conflict"]`` for W4 to consume.
    """

    # Lazy import: ``verification`` imports this package through ``gate``.
    from ..verification.evidence import verify_evidence, write_json

    run_path = Path(run_dir)
    checks = {
        "identity": False,
        "workspace": False,
        "evidence": False,
        "tip": False,
        "ancestry": False,
        "merge_tree": False,
    }
    reasons: list[str] = []

    # -- identity: same-connection board row re-read by the caller ----------
    if row is None:
        reasons.append("task-not-found")
    elif (
        row.get("status") == "running"
        and row.get("current_run_id") is not None
        and int(row["current_run_id"]) == identity.run_id
        and row.get("claim_lock") == identity.claim_lock
    ):
        checks["identity"] = True
    else:
        reasons.append("stale-run-or-claim")

    if _real(workspace) == _real(current_workspace):
        checks["workspace"] = True
    else:
        reasons.append("not-current-run-workspace")

    # -- evidence ----------------------------------------------------------
    verification = verify_evidence(
        run_path,
        identity,
        package_dir,
        workspace,
        repo,
        main_ref,
        requirement_id=requirement_id,
    )
    if not verification.ok:
        checks["evidence"] = False
        for reason in verification.reasons or ("unknown",):
            reasons.append(f"evidence:{reason}")
    elif verification.index is not None and (
        verification.index.get("base_commit") != base_commit
        or verification.index.get("sync_base") != sync_base
        or verification.index.get("code_commit") != code_commit
    ):
        reasons.append("evidence:index-values-mismatch")
    else:
        checks["evidence"] = True

    # -- tip / ancestry ----------------------------------------------------
    try:
        tip = branch_tip(workspace)
    except GitError:
        tip = None
        reasons.append("workspace-unreadable")
    if tip is not None and tip == code_commit:
        checks["tip"] = True
    elif tip is not None:
        reasons.append("branch-tip-advanced")

    try:
        base_ok = is_ancestor(base_commit, code_commit, repo=repo)
        sync_ok = is_ancestor(sync_base, code_commit, repo=repo)
    except GitError:
        base_ok = sync_ok = False
    if base_ok and sync_ok:
        checks["ancestry"] = True
    if not base_ok:
        reasons.append("base-not-ancestor")
    if not sync_ok:
        reasons.append("sync-base-not-ancestor")

    blocking = not (
        checks["identity"]
        and checks["workspace"]
        and checks["evidence"]
        and checks["tip"]
        and checks["ancestry"]
    )
    if blocking:
        return LandingPlan(False, "rejected", tuple(reasons), checks)

    # -- main ref + merge-tree (objects only; no ref/worktree change) ------
    main_commit = resolve_ref(main_ref, repo=repo)
    if main_commit is None:
        reasons.append("main-ref-unresolved")
        return LandingPlan(False, "rejected", tuple(reasons), checks)
    try:
        clean, tree_or_reason = merge_tree_preflight(
            main_commit, code_commit, repo=repo
        )
    except GitError as exc:
        reasons.append(f"merge-tree-error:{type(exc).__name__}")
        return LandingPlan(False, "rejected", tuple(reasons), checks)

    if clean:
        checks["merge_tree"] = True
        verdict = "ready"
        merge_tree: str | None = tree_or_reason
        reason_list: list[str] = []
    elif tree_or_reason == "merge-conflict":
        verdict = "not-ready"
        merge_tree = None
        reason_list = ["merge-conflict"]
    else:
        reasons.append(tree_or_reason)
        return LandingPlan(False, "rejected", tuple(reasons), checks)

    resolved_branch = branch if branch is not None else current_branch(workspace)
    document = {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "identity": identity.to_dict(),
        "workspace": os.path.abspath(os.fspath(workspace)),
        "main_ref": main_ref,
        "main_commit": main_commit,
        "base_commit": base_commit,
        "sync_base": sync_base,
        "code_commit": code_commit,
        "branch": resolved_branch,
        "merge_tree": merge_tree,
        "verdict": verdict,
        "reasons": reason_list,
        "checks": checks,
    }
    write_json(run_path / LANDING_PLAN_RELATIVE_PATH, document)
    return LandingPlan(
        verdict == "ready",
        verdict,
        tuple(reason_list),
        checks,
        document,
        LANDING_PLAN_RELATIVE_PATH,
        merge_tree,
    )


__all__ = [
    "LANDING_PLAN_RELATIVE_PATH",
    "LANDING_REQUIREMENT_ID",
    "LandingPlan",
    "check_workspace_identity",
    "is_within_workspace",
    "landing_preflight",
    "reclaim_marker_path",
    "unreusable_marker_path",
    "write_reclaim_marker",
    "write_unreusable_marker",
]
