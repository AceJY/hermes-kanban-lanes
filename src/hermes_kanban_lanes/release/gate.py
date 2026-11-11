"""D5: the fixed-order, fail-closed release gate.

The gate writes ``release-report.json`` even when it refuses: the operator
needs the whole record, not only a thrown error.  It never merges, never moves
a ref and never writes to the board.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import RunIdentity
from ..verification.evidence import verify_evidence
from ..workspace.git import (
    GitError,
    changed_paths,
    current_branch,
    diff_paths,
    merge_tree_preflight,
    resolve_ref,
)
from .authorization import (
    AUTHORIZATION_SOURCE_FILE,
    AUTHORIZATION_SOURCE_POLICY,
    POLICY_AUTHORIZATION_FILENAME,
    ReleaseAuthorization,
    authorization_problems,
    build_policy_authorization,
    load_authorization,
)
from .binding import (
    BindingEvaluation,
    ReviewBinding,
    evaluate_binding,
)

RELEASE_REQUIREMENT_ID = "HKL-W4"
RELEASE_SCHEMA_VERSION = 1
RELEASE_REPORT_FILENAME = "release-report.json"
RELEASE_CHECKS = (
    "workspace",
    "review",
    "evidence",
    "rounds",
    "authorization",
    "merge_enabled",
    "main_unchanged",
    "merge_tree",
)
RELEASE_VERDICTS = ("release-ready", "refused", "blocked")


@dataclass(frozen=True, slots=True)
class ReleaseDecision:
    verdict: str
    report: dict[str, Any]
    report_path: str
    reasons: tuple[str, ...]
    binding: ReviewBinding | None = None
    authorization: ReleaseAuthorization | None = None
    evaluation: BindingEvaluation | None = None
    main_commit: str | None = None
    dev_run_dir: str | None = None
    released_head: str | None = None
    merge_tree: str | None = None

    @property
    def ready(self) -> bool:
        return self.verdict == "release-ready"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _short_ref(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _run_relative_path(path: str | os.PathLike[str]) -> str:
    """Report a work-root file as ``authorizations/<name>`` (spec §5.1)."""

    target = Path(path)
    return (Path(target.parent.name) / target.name).as_posix()


def _policy_high_risk_reasons(
    card_risk: str | None,
    binding: ReviewBinding | None,
    repo: Path,
    required_paths: Sequence[str],
) -> list[str]:
    """Why policy may not auto-authorize this change (empty means low risk).

    Fail closed: a configured path prefix whose touched paths cannot be
    computed keeps the change on the file/human approval path.
    """

    reasons: list[str] = []
    if card_risk == "high":
        reasons.append("risk:high")
    if required_paths:
        if binding is None:
            reasons.append("changed-paths-unavailable")
            return reasons
        try:
            touched = diff_paths(
                binding.task_base_commit,
                binding.reviewed_head,
                cwd=repo,
            )
        except GitError:
            reasons.append("changed-paths-unavailable")
            return reasons
        matched = [
            path
            for path in touched
            if any(path.startswith(prefix) for prefix in required_paths)
        ]
        if matched:
            reasons.append("paths:" + ",".join(matched[:8]))
    return reasons


def release_preflight(
    *,
    identity: RunIdentity,
    task_row: Mapping[str, Any] | None,
    runs: Sequence[Mapping[str, Any]],
    dev_run_dir: str | os.PathLike[str],
    repo: str | os.PathLike[str],
    package_dir: str | os.PathLike[str],
    main_ref: str,
    authorization_path: str | os.PathLike[str],
    merge_enabled: bool,
    post_merge_commands: Sequence[Sequence[str]],
    max_rework_rounds: int,
    release_run_dir: str | os.PathLike[str],
    requirement_id: str = RELEASE_REQUIREMENT_ID,
    executor_profile: str | None = None,
    implementer_profile: str | None = None,
    changes_events: Sequence[Mapping[str, Any]] | None = None,
    previous_code_commit: str | None = None,
    authorization_mode: str = AUTHORIZATION_SOURCE_FILE,
    require_authorization_paths: Sequence[str] = (),
    authorization_window_seconds: int = 900,
    card_risk: str | None = None,
    now: int | None = None,
) -> ReleaseDecision:
    repo_path = Path(repo)
    run_dir = Path(release_run_dir)
    dev_dir = Path(dev_run_dir)
    reasons: list[str] = []
    checks: list[dict[str, Any]] = []

    def add_check(name: str, ok: bool, **detail: Any) -> None:
        checks.append({"name": name, "ok": bool(ok), **detail})

    # -- 1. workspace ------------------------------------------------------
    workspace_ok = True
    workspace_detail = "ok"
    main_commit: str | None = None
    if not repo_path.is_dir():
        workspace_ok = False
        workspace_detail = "workspace-missing"
    else:
        branch = current_branch(repo_path)
        if branch is None:
            workspace_ok = False
            workspace_detail = "workspace-unreadable"
        elif branch != _short_ref(main_ref) and branch != main_ref:
            workspace_ok = False
            workspace_detail = "workspace-not-main"
        if workspace_ok:
            try:
                dirty = changed_paths(repo_path)
            except GitError:
                dirty = None
            if dirty is None:
                workspace_ok = False
                workspace_detail = "workspace-unreadable"
            elif dirty:
                workspace_ok = False
                workspace_detail = "workspace-dirty"
        if workspace_ok:
            main_commit = resolve_ref(main_ref, repo=repo_path)
            if main_commit is None:
                workspace_ok = False
                workspace_detail = "main-unresolved"
    add_check("workspace", workspace_ok, detail=workspace_detail)
    if not workspace_ok:
        reasons.append(workspace_detail)

    # -- 2. review binding -------------------------------------------------
    try:
        evaluation = evaluate_binding(
            task_row=task_row,
            runs=runs,
            dev_run_dir=dev_dir,
            repo=repo_path,
            requirement_id=requirement_id,
            package_dir=package_dir,
            executor_profile=executor_profile,
        )
        evaluation_error = None
    except Exception as exc:  # noqa: BLE001 - fail closed, still write a report
        evaluation = BindingEvaluation(None, ())
        evaluation_error = f"review-evaluation-error:{type(exc).__name__}"
    review_problems = list(evaluation.problems)
    if evaluation_error is not None:
        review_problems.append(evaluation_error)
    if (
        evaluation.binding is not None
        and implementer_profile is not None
        and evaluation.binding.reviewer == implementer_profile
    ):
        review_problems.append("reviewer-is-implementer")
    review_problems = list(dict.fromkeys(review_problems))
    add_check(
        "review",
        not review_problems,
        detail="ok" if not review_problems else ";".join(review_problems),
        reasons=review_problems,
    )
    reasons.extend(review_problems)
    binding = evaluation.binding if not review_problems else None

    # -- 3. evidence -------------------------------------------------------
    evidence_problems: list[str] = []
    if evaluation.run_document is None:
        evidence_problems.append("evidence:run-missing")
    else:
        try:
            run_identity = RunIdentity.from_dict(
                evaluation.run_document.get("identity")
            )
        except Exception:
            run_identity = None
        if run_identity is None:
            evidence_problems.append("evidence:identity-invalid")
        else:
            workspace = str(
                evaluation.run_document.get("workspace") or dev_dir / "workspace"
            )
            try:
                verification = verify_evidence(
                    dev_dir,
                    run_identity,
                    package_dir,
                    workspace,
                    repo_path,
                    main_ref,
                    requirement_id=requirement_id,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed, still report
                evidence_problems.append(
                    f"evidence:verifier-error:{type(exc).__name__}"
                )
            else:
                evidence_problems.extend(
                    f"evidence:{reason}" for reason in verification.reasons
                )
    add_check(
        "evidence",
        not evidence_problems,
        detail="ok" if not evidence_problems else ";".join(evidence_problems),
        reasons=evidence_problems,
    )
    reasons.extend(evidence_problems)

    # -- 4. rounds ---------------------------------------------------------
    round_problems: list[str] = []
    events = list(changes_events or [])
    if binding is None:
        round_problems.append("rounds-unavailable")
    else:
        if binding.rework_round > max_rework_rounds:
            round_problems.append(f"rework-cap:{max_rework_rounds}")
        expected_round = (
            len([run for run in runs if run.get("outcome") == "changes_requested"])
            + 1
        )
        if binding.rework_round != expected_round:
            round_problems.append("rework-round-mismatch")
        if len(events) >= 2:
            last = events[-1].get("reason") if isinstance(events[-1], dict) else None
            prior = (
                events[-2].get("reason") if isinstance(events[-2], dict) else None
            )
            if (
                isinstance(last, str)
                and last
                and last == prior
                and previous_code_commit is not None
                and previous_code_commit == binding.reviewed_head
            ):
                round_problems.append("rework-stalled-same-reason")
    add_check(
        "rounds",
        not round_problems,
        detail="ok" if not round_problems else ";".join(round_problems),
        reasons=round_problems,
        round=binding.rework_round if binding is not None else None,
    )
    reasons.extend(round_problems)

    # -- 5. authorization --------------------------------------------------
    expected_main = evaluation.main_commit
    expected = {
        "requirement_id": requirement_id,
        "task_id": task_row.get("id") if task_row is not None else None,
        "review_run_id": binding.review_run_id if binding is not None else None,
        "dev_run_id": binding.dev_run_id if binding is not None else None,
        "rework_round": binding.rework_round if binding is not None else None,
        "task_base_commit": binding.task_base_commit if binding is not None else None,
        "reviewed_head": binding.reviewed_head if binding is not None else None,
        "reviewed_tree": binding.reviewed_tree if binding is not None else None,
        "evidence_index_sha256": binding.evidence_index_sha256
        if binding is not None
        else None,
        "main_ref": main_ref,
        "main_commit": expected_main,
    }
    # Unknown modes fail closed to the historical file semantics.
    if authorization_mode not in (
        AUTHORIZATION_SOURCE_FILE,
        AUTHORIZATION_SOURCE_POLICY,
    ):
        authorization_mode = AUTHORIZATION_SOURCE_FILE
    authorization_source = AUTHORIZATION_SOURCE_FILE
    policy_generated = False
    policy_reasons: list[str] = []

    authorization, authorization_shape = load_authorization(authorization_path)
    authorization_issues = list(authorization_shape)
    high_risk = False

    if authorization_mode == AUTHORIZATION_SOURCE_POLICY:
        policy_reasons = _policy_high_risk_reasons(
            card_risk, binding, repo_path, require_authorization_paths
        )
        high_risk = bool(policy_reasons)
        if not high_risk:
            current = int(time.time()) if now is None else int(now)
            window = authorization_window_seconds
            if (
                isinstance(window, bool)
                or not isinstance(window, int)
                or window <= 0
            ):
                authorization_issues.append("authorization-window-invalid")
            else:
                generated = build_policy_authorization(
                    expected,
                    approved_at=current,
                    expires_at=current + window,
                )
                if generated is None:
                    authorization_issues.append(
                        "authorization-required:binding-unavailable"
                    )
                else:
                    # Write the record, then reload it so the exact document
                    # the merge is authorized by passes the same strict
                    # loader as an operator-written file.
                    generated_path = run_dir / POLICY_AUTHORIZATION_FILENAME
                    _write_json(generated_path, generated.to_dict())
                    authorization, generated_shape = load_authorization(
                        generated_path
                    )
                    authorization_issues = list(generated_shape)
                    authorization_source = AUTHORIZATION_SOURCE_POLICY
                    policy_generated = True
                    if authorization is not None:
                        authorization_issues.extend(
                            authorization_problems(
                                authorization,
                                expected,
                                now=now,
                                implementer=implementer_profile,
                                reviewer=binding.reviewer
                                if binding is not None
                                else None,
                                executor=executor_profile,
                                allow_policy_approver=True,
                            )
                        )
        else:
            # High risk falls back to file/human authorization; a forged
            # ``policy:`` approver is rejected here as it is in file mode.
            file_issues = list(authorization_shape)
            if authorization is not None:
                file_issues.extend(
                    authorization_problems(
                        authorization,
                        expected,
                        now=now,
                        implementer=implementer_profile,
                        reviewer=binding.reviewer
                        if binding is not None
                        else None,
                        executor=executor_profile,
                    )
                )
            file_issues = list(dict.fromkeys(file_issues))
            if file_issues or authorization is None:
                # Make the policy refusal the primary reason (release_once
                # reports reasons[0]) while keeping the audit detail.
                authorization_issues = [
                    "authorization-required:high-risk",
                    *file_issues,
                ]
            else:
                authorization_issues = []
    elif authorization is not None:
        authorization_issues.extend(
            authorization_problems(
                authorization,
                expected,
                now=now,
                implementer=implementer_profile,
                reviewer=binding.reviewer if binding is not None else None,
                executor=executor_profile,
            )
        )
    authorization_issues = list(dict.fromkeys(authorization_issues))
    add_check(
        "authorization",
        not authorization_issues and authorization is not None,
        detail="ok" if not authorization_issues else ";".join(authorization_issues),
        reasons=authorization_issues,
        source=authorization_source,
    )
    reasons.extend(authorization_issues)

    # -- 6. merge enabled + post-merge commands ----------------------------
    merge_problems: list[str] = []
    if not merge_enabled:
        merge_problems.append("merge-disabled")
    elif not post_merge_commands:
        merge_problems.append("postmerge-commands-missing")
    add_check(
        "merge_enabled",
        not merge_problems,
        detail="ok" if not merge_problems else ";".join(merge_problems),
        reasons=merge_problems,
    )
    reasons.extend(merge_problems)

    # -- 7. main unchanged -------------------------------------------------
    main_problems: list[str] = []
    if expected_main is None:
        main_problems.append("main-commit-unknown")
    elif main_commit is not None and main_commit != expected_main:
        main_problems.append("main-moved")
    add_check(
        "main_unchanged",
        not main_problems,
        detail="ok" if not main_problems else ";".join(main_problems),
        reasons=main_problems,
        main_commit=main_commit,
    )
    reasons.extend(main_problems)

    # -- 8. merge tree -----------------------------------------------------
    merge_tree_problems: list[str] = []
    merge_tree: str | None = None
    if binding is None:
        merge_tree_problems.append("review-missing")
    elif main_commit is not None:
        try:
            clean, detail = merge_tree_preflight(
                main_ref, binding.reviewed_head, repo=repo_path
            )
        except GitError as exc:
            clean, detail = False, f"merge-tree-error:{exc}"
        if clean:
            merge_tree = detail
        else:
            merge_tree_problems.append(detail)
    else:
        merge_tree_problems.append("main-unresolved")
    add_check(
        "merge_tree",
        not merge_tree_problems,
        detail="ok" if not merge_tree_problems else ";".join(merge_tree_problems),
        reasons=merge_tree_problems,
        merge_tree=merge_tree,
    )
    reasons.extend(merge_tree_problems)

    # -- verdict + report --------------------------------------------------
    reasons = list(dict.fromkeys(reasons))
    blocked_details = {
        "workspace-missing",
        "workspace-unreadable",
        "main-unresolved",
    }
    if not workspace_ok and workspace_detail in blocked_details:
        verdict = "blocked"
    elif any(not check["ok"] for check in checks):
        verdict = "refused"
    else:
        verdict = "release-ready"

    run_document = evaluation.run_document or {}
    report = {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "requirement_id": requirement_id,
        "identity": identity.to_dict(),
        "task_id": task_row.get("id") if task_row is not None else None,
        "dev_run_id": binding.dev_run_id if binding is not None else None,
        "rework_round": binding.rework_round if binding is not None else None,
        "task_base_commit": binding.task_base_commit if binding is not None else None,
        "continuation_base": run_document.get("continuation_base")
        or run_document.get("base_commit"),
        "reviewed_head": binding.reviewed_head if binding is not None else None,
        "reviewed_tree": binding.reviewed_tree if binding is not None else None,
        "evidence_index_sha256": binding.evidence_index_sha256
        if binding is not None
        else None,
        "tool_sha256": binding.tool_sha256 if binding is not None else None,
        "main_ref": main_ref,
        "main_commit": main_commit,
        "authorization": {
            "path": (
                Path(authorization.path).name
                if policy_generated
                and authorization is not None
                and authorization.path is not None
                else _run_relative_path(
                    authorization.path
                    if authorization is not None and authorization.path is not None
                    else authorization_path
                )
            ),
            "sha256": authorization.sha256 if authorization is not None else None,
            "approver": authorization.approver if authorization is not None else None,
            "source": authorization_source,
            "policy": {
                "mode": authorization_mode,
                "generated": policy_generated,
                "high_risk": high_risk,
                "high_risk_reasons": list(policy_reasons),
                "required_paths": list(require_authorization_paths),
            },
        },
        "checks": checks,
        "verdict": verdict,
        "reasons": reasons,
        "generated_at": int(time.time() if now is None else now),
    }
    report_path = run_dir / RELEASE_REPORT_FILENAME
    _write_json(report_path, report)

    return ReleaseDecision(
        verdict=verdict,
        report=report,
        report_path=str(report_path),
        reasons=tuple(reasons),
        binding=binding,
        authorization=authorization,
        evaluation=evaluation,
        main_commit=main_commit,
        dev_run_dir=str(dev_dir),
        released_head=binding.reviewed_head if binding is not None else None,
        merge_tree=merge_tree,
    )


__all__ = [
    "RELEASE_CHECKS",
    "RELEASE_REPORT_FILENAME",
    "RELEASE_REQUIREMENT_ID",
    "RELEASE_SCHEMA_VERSION",
    "RELEASE_VERDICTS",
    "ReleaseDecision",
    "release_preflight",
]
