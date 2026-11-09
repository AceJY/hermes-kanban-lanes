"""W4 release orchestration: observe reviews, queue, gate, merge, regress.

The daemon drives each step in the D10 order.  Nothing here writes the board
directly: observation is read-only, the merge card is created through the
adapter, and terminal transitions use the run-identity fence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..card_contract import CardSpec
from ..config import LaneConfig
from ..contracts import RunIdentity
from ..hermes_adapter.board import BoardAdapter
from ..verification.evidence import sha256_file
from ..workspace.git import GitError, abort_merge, repo_head, run_git
from ..release.binding import evaluate_binding
from ..release.gate import RELEASE_REQUIREMENT_ID, release_preflight
from ..release.merge import (
    MERGE_TIMEOUT,
    merge_release,
    record_existing_merge,
    run_post_merge,
)
from ..release.state import (
    MERGE_INTENT_ABORT,
    MERGE_INTENT_ADOPT,
    MERGE_INTENT_DIVERGED,
    MERGE_INTENT_RETRY,
    RELEASE_STATE_FILENAME,
    ReleaseState,
    classify_merge_intent,
    new_merge_intent,
    new_release_state,
    read_merge_intent,
    read_release_state,
    write_merge_intent,
    write_release_state,
)
from .supervisor import allocate_run_paths

LEDGER_SCHEMA_VERSION = 1
OBSERVE_LEDGER_REQUIREMENT_ID = "HKL-W4"
MERGE_ASSIGNEE = "hkl:merge"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _card_path(work_root: str | os.PathLike[str], task_id: str) -> Path:
    return Path(work_root) / "cards" / f"{task_id}.json"


def _load_card(
    work_root: str | os.PathLike[str], config: LaneConfig, task_id: str
) -> tuple[dict[str, Any] | None, CardSpec | None]:
    document = _read_json(_card_path(work_root, task_id))
    if not isinstance(document, dict):
        return None, None
    try:
        return document, CardSpec.from_dict(document, config)
    except Exception:
        return document, None


def _snapshot_card(
    document: Any, config: LaneConfig
) -> tuple[dict[str, Any], CardSpec] | None:
    """Validate one ``run.json`` ``card`` snapshot; ``None`` when unusable."""

    if not isinstance(document, dict):
        return None
    try:
        card = CardSpec.from_dict(document, config)
    except Exception:
        return None
    if card.card_kind != "implementation":
        return None
    return document, card


def _run_snapshot_key(run_dir: Path, document: dict[str, Any]) -> int:
    identity = document.get("identity")
    if isinstance(identity, dict) and isinstance(identity.get("run_id"), int):
        return int(identity["run_id"])
    return -1


def _dev_cards(work_root: str | os.PathLike[str], config: LaneConfig) -> list[tuple[str, dict[str, Any], CardSpec]]:
    """Implementation cards from explicit JSON *and* run-card snapshots.

    D2b: synthesised cards never write ``cards/<task_id>.json`` (the decision
    in §6.1 rejects that ledger), so ``run.json`` is the only durable record of
    the declaration they used.  An explicit card always wins for the same
    ``task_id``; unreadable/invalid snapshots are skipped, never guessed.
    """

    root = Path(work_root)
    cards_root = root / "cards"
    explicit: dict[str, tuple[dict[str, Any], CardSpec]] = {}
    explicit_ids: set[str] = set()
    if cards_root.is_dir():
        for path in sorted(cards_root.glob("*.json")):
            explicit_ids.add(path.stem)
            document = _read_json(path)
            if not isinstance(document, dict):
                continue
            try:
                card = CardSpec.from_dict(document, config)
            except Exception:
                continue
            if card.card_kind != "implementation":
                continue
            explicit[path.stem] = (document, card)

    snapshots: dict[str, tuple[dict[str, Any], CardSpec]] = {}
    runs_root = root / "runs"
    if runs_root.is_dir():
        for task_dir in sorted(runs_root.iterdir()):
            if not task_dir.is_dir():
                continue
            task_id = task_dir.name
            if task_id in explicit_ids:
                continue
            best: tuple[int, tuple[dict[str, Any], CardSpec]] | None = None
            for run_dir in sorted(task_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                run_document = _read_json(run_dir / "run.json")
                if not isinstance(run_document, dict):
                    continue
                candidate = _snapshot_card(run_document.get("card"), config)
                if candidate is None:
                    continue
                rank = _run_snapshot_key(run_dir, run_document)
                if best is None or rank > best[0]:
                    best = (rank, candidate)
            if best is not None:
                snapshots[task_id] = best[1]

    merged: dict[str, tuple[dict[str, Any], CardSpec]] = dict(explicit)
    for task_id, candidate in snapshots.items():
        merged.setdefault(task_id, candidate)
    return [(task_id, document, card) for task_id, (document, card) in sorted(merged.items())]


def _find_dev_run(
    work_root: str | os.PathLike[str], task_id: str, dev_run_id: int
) -> tuple[Path | None, dict[str, Any] | None]:
    runs_root = Path(work_root) / "runs" / task_id
    if not runs_root.is_dir():
        return None, None
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        document = _read_json(entry / "run.json")
        if not isinstance(document, dict):
            continue
        identity = document.get("identity")
        if isinstance(identity, dict) and identity.get("run_id") == dev_run_id:
            return entry, document
    return None, None


def _previous_dev_code_commit(
    work_root: str | os.PathLike[str], task_id: str, dev_run_id: int
) -> str | None:
    runs_root = Path(work_root) / "runs" / task_id
    if not runs_root.is_dir():
        return None
    candidates: list[tuple[int, str]] = []
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        document = _read_json(entry / "run.json")
        if not isinstance(document, dict):
            continue
        identity = document.get("identity")
        if not isinstance(identity, dict):
            continue
        run_id = identity.get("run_id")
        code_commit = document.get("code_commit")
        if (
            isinstance(run_id, int)
            and run_id < dev_run_id
            and isinstance(code_commit, str)
            and code_commit
        ):
            candidates.append((run_id, code_commit))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


# ---------------------------------------------------------------------------
# 1. observe reviews (read-only)
# ---------------------------------------------------------------------------


def observe_reviews(
    work_root: str | os.PathLike[str],
    adapter: BoardAdapter,
    config: LaneConfig,
    *,
    requirement_id: str = RELEASE_REQUIREMENT_ID,
) -> list[dict[str, Any]]:
    root = Path(work_root)
    ledger_root = root / "ledger"
    observed: list[dict[str, Any]] = []
    for task_id, _, _ in _dev_cards(root, config):
        reviews = adapter.review_runs(task_id)
        if not reviews:
            continue
        ledger_path = ledger_root / f"{task_id}.json"
        ledger = _read_json(ledger_path)
        if not isinstance(ledger, dict):
            ledger = {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "requirement_id": requirement_id,
                "task_id": task_id,
                "reviews": [],
            }
        seen = {
            int(entry.get("review_run_id") or 0)
            for entry in ledger.get("reviews", [])
            if isinstance(entry, dict)
        }
        changed = False
        for run in reviews:
            review_run_id = int(run.get("id") or 0)
            if review_run_id in seen:
                continue
            metadata = run.get("metadata") or {}
            entry = {
                "review_run_id": review_run_id,
                "dev_run_id": metadata.get("dev_run_id"),
                "verdict": metadata.get("verdict"),
                "reviewed_head": metadata.get("reviewed_head"),
                "evidence_index_sha256": metadata.get("evidence_index_sha256"),
                "reviewer": metadata.get("reviewer"),
            }
            ledger.setdefault("reviews", []).append(entry)
            observed.append(dict(entry, task_id=task_id))
            changed = True
            seen.add(review_run_id)
        if changed:
            _write_json(ledger_path, ledger)
    return observed


# ---------------------------------------------------------------------------
# 2. queue release (merge) cards
# ---------------------------------------------------------------------------


def _merge_idempotency_key(task_id: str, binding) -> str:
    return f"hkl-release:{task_id}:{binding.reviewed_head}:{binding.evidence_index_sha256}"


def _merge_card_readback_problems(
    adapter: BoardAdapter,
    merge_task_id: str,
    *,
    parent_id: str,
    key: str,
) -> list[str]:
    """D9: the handover counts only after the board reads back as created."""

    row = adapter.task_row(merge_task_id)
    problems: list[str] = []
    if row is None:
        return ["merge-card-missing"]
    if parent_id not in adapter.parent_ids(merge_task_id):
        problems.append("merge-card-parent-mismatch")
    if row.get("assignee") != MERGE_ASSIGNEE:
        problems.append("merge-card-assignee-mismatch")
    if row.get("idempotency_key") != key:
        problems.append("merge-card-idempotency-mismatch")
    return problems


def _run_task_ids(root: Path) -> list[str]:
    """Task ids with a run directory, for visible snapshot diagnostics."""

    runs_root = root / "runs"
    if not runs_root.is_dir():
        return []
    return sorted(entry.name for entry in runs_root.iterdir() if entry.is_dir())


def _snapshot_skip(task_id: str, reason: str, detail: str | None = None) -> dict[str, Any]:
    """A visible, non-fatal reason a reviewed card did not reach the queue."""

    return {
        "dev_task_id": task_id,
        "skipped": reason,
        "reason": f"{reason}:{detail}" if detail else reason,
    }


def queue_release_cards(
    work_root: str | os.PathLike[str],
    adapter: BoardAdapter,
    config: LaneConfig,
    *,
    requirement_id: str = RELEASE_REQUIREMENT_ID,
) -> list[dict[str, Any]]:
    root = Path(work_root)
    review_config = config.review_options()
    if not review_config.merge.enabled:
        return []
    queued: list[dict[str, Any]] = []
    cards = {task_id: card for task_id, _, card in _dev_cards(root, config)}
    for task_id in sorted(set(cards) | set(_run_task_ids(root))):
        row = adapter.task_row(task_id)
        if row is None or row.get("status") != "done":
            continue
        runs = adapter.run_rows(task_id)
        reviews = adapter.review_runs(task_id)
        if not reviews:
            continue
        latest = max(reviews, key=lambda run: int(run.get("id") or 0))
        metadata = latest.get("metadata") or {}
        dev_run_id = metadata.get("dev_run_id")
        if not isinstance(dev_run_id, int):
            continue
        explicit_card = _card_path(root, task_id).is_file()
        dev_dir, dev_document = _find_dev_run(root, task_id, dev_run_id)
        if explicit_card:
            dev_card = cards.get(task_id)
            if dev_card is None or dev_dir is None:
                continue
        else:
            # D2b/§6.1: a synthesised card's declaration is exactly the one
            # recorded on the reviewed run.  Never fall back to another round's
            # snapshot and never guess when it is missing or invalid.
            if dev_dir is None:
                queued.append(
                    _snapshot_skip(
                        task_id,
                        "card-snapshot-missing",
                        f"runs/{task_id}/run.json (run_id={dev_run_id})",
                    )
                )
                continue
            snapshot = dev_document.get("card") if isinstance(dev_document, dict) else None
            if snapshot is None:
                queued.append(
                    _snapshot_skip(
                        task_id,
                        "card-snapshot-missing",
                        f"runs/{task_id}/{dev_dir.name}/run.json",
                    )
                )
                continue
            try:
                dev_card = CardSpec.from_dict(snapshot, config)
            except Exception as exc:  # noqa: BLE001 - report, never raise
                queued.append(
                    _snapshot_skip(
                        task_id,
                        "card-snapshot-invalid",
                        f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            if dev_card.card_kind != "implementation":
                queued.append(
                    _snapshot_skip(
                        task_id,
                        "card-snapshot-invalid",
                        f"card_kind={dev_card.card_kind}",
                    )
                )
                continue
        evaluation = evaluate_binding(
            task_row=row,
            runs=runs,
            dev_run_dir=dev_dir,
            repo=config.repositories[dev_card.repository].path,
            requirement_id=requirement_id,
            package_dir=Path(__file__).resolve().parents[1],
            executor_profile=dev_card.executor,
        )
        if evaluation.problems or evaluation.binding is None:
            continue
        binding = evaluation.binding
        key = _merge_idempotency_key(task_id, binding)

        existing = None
        for child_id in adapter.child_ids(task_id):
            child = adapter.task_row(child_id)
            if child is None:
                continue
            if child.get("idempotency_key") == key:
                existing = child_id
                break
            child_document = _read_json(_card_path(root, child_id))
            if isinstance(child_document, dict):
                if (
                    child_document.get("card_kind") == "merge"
                    and child_document.get("closure_for") == task_id
                    and child_document.get("lineage") == child_id
                ):
                    existing = child_id
                    break
        if existing is not None:
            continue

        merge_task_id = adapter.create_task(
            title=f"Release {task_id}: {binding.reviewed_head[:12]}",
            body=(
                f"Reviewed head {binding.reviewed_head}; "
                f"evidence index {binding.evidence_index_sha256}"
            ),
            assignee=MERGE_ASSIGNEE,
            parents=[task_id],
            idempotency_key=key,
        )
        card_document = {
            "schema_version": 1,
            "card_kind": "merge",
            "lineage": merge_task_id,
            "closure_for": task_id,
            "executor": dev_card.executor,
            "repository": dev_card.repository,
            "allow": list(dev_card.allow),
            "commands": [],
            "risk": dev_card.risk,
            "artifacts": [],
        }
        _write_json(_card_path(root, merge_task_id), card_document)

        if task_id not in adapter.parent_ids(merge_task_id):
            adapter.link_tasks(task_id, merge_task_id)
        merge_row = adapter.task_row(merge_task_id)
        problems = _merge_card_readback_problems(
            adapter, merge_task_id, parent_id=task_id, key=key
        )
        if problems:
            # D9/§6.1: a card that fails read-back is not a completed handover;
            # surface the failure to the tick instead of reporting success.
            raise RuntimeError(";".join(problems))
        queued.append(
            {
                "merge_task_id": merge_task_id,
                "dev_task_id": task_id,
                "idempotency_key": key,
                "reviewed_head": binding.reviewed_head,
                "evidence_index_sha256": binding.evidence_index_sha256,
                "status": merge_row.get("status") if merge_row else None,
                "assignee": merge_row.get("assignee") if merge_row else None,
            }
        )
    return queued


# ---------------------------------------------------------------------------
# 3. release_once
# ---------------------------------------------------------------------------


def _release_reason_kind(reasons: Sequence[str], *, default: str = "needs_input") -> str:
    for reason in reasons:
        if reason in ("main-moved", "merge-abort-failed", "workspace-dirty", "workspace-unreadable"):
            return "capability"
        if reason.startswith(("workspace-", "main-unresolved")):
            return "capability"
    return default


def _owns(adapter: BoardAdapter, identity: RunIdentity) -> bool:
    try:
        return bool(adapter.owns(identity))
    except Exception:
        return False


def _heartbeat(adapter: BoardAdapter, identity: RunIdentity) -> bool:
    try:
        return bool(adapter.heartbeat(identity))
    except Exception:
        return False


def _blocked(
    adapter: BoardAdapter,
    identity: RunIdentity,
    reason: str,
    kind: str,
) -> bool:
    try:
        if adapter.identity(identity.task_id) == identity:
            return adapter.block(identity, reason, kind)
    except Exception:
        return False
    return False


def _terminal_state(
    state: ReleaseState,
    run_dir: Path,
    **fields: Any,
) -> None:
    if state.stage != "terminal":
        state.advance("terminal", **fields)
    else:
        for name, value in fields.items():
            if hasattr(state, name):
                setattr(state, name, value)
    write_release_state(run_dir, state)


def release_once(
    *,
    config: LaneConfig,
    adapter: BoardAdapter,
    card: CardSpec,
    identity: RunIdentity,
    work_root: str | os.PathLike[str] | None = None,
    package_dir: str | os.PathLike[str] | None = None,
    requirement_id: str = RELEASE_REQUIREMENT_ID,
    inherited_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root = Path(work_root or config.work_root)
    review_config = config.review_options()
    merge_config = review_config.merge
    package_root = (
        Path(package_dir).resolve()
        if package_dir is not None
        else Path(__file__).resolve().parents[1]
    )
    dev_task_id = card.closure_for or ""
    run_dir, _ = allocate_run_paths(root, identity.task_id, identity.run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "evidence").mkdir(exist_ok=True)

    outcome: dict[str, Any] = {
        "merge_task_id": identity.task_id,
        "dev_task_id": dev_task_id,
        "action": None,
        "reason": None,
        "merge_sha": None,
        "merge_tree": None,
        "run_dir": str(run_dir),
    }
    state = new_release_state(
        identity=identity,
        dev_task_id=dev_task_id,
        requirement_id=requirement_id,
    )
    write_release_state(run_dir, state)

    dev_document, dev_card = _load_card(root, config, dev_task_id)
    if dev_card is None:
        reason = "dev-card-missing"
        _terminal_state(state, run_dir)
        outcome.update(action="block", reason=reason)
        outcome["terminal_ok"] = _blocked(adapter, identity, reason, "capability")
        return outcome

    dev_row = adapter.task_row(dev_task_id)
    runs = adapter.run_rows(dev_task_id)
    reviews = adapter.review_runs(dev_task_id)
    if not reviews:
        reason = "review-missing"
        _terminal_state(state, run_dir)
        outcome.update(action="block", reason=reason)
        outcome["terminal_ok"] = _blocked(adapter, identity, reason, "needs_input")
        return outcome
    latest = max(reviews, key=lambda run: int(run.get("id") or 0))
    metadata = latest.get("metadata") or {}
    dev_run_id = metadata.get("dev_run_id")
    if not isinstance(dev_run_id, int):
        reason = "review-dev-run-invalid"
        _terminal_state(state, run_dir)
        outcome.update(action="block", reason=reason)
        outcome["terminal_ok"] = _blocked(adapter, identity, reason, "needs_input")
        return outcome
    dev_dir, _ = _find_dev_run(root, dev_task_id, dev_run_id)
    if dev_dir is None or dev_row is None:
        reason = "rework-base-missing"
        _terminal_state(state, run_dir)
        outcome.update(action="block", reason=reason)
        outcome["terminal_ok"] = _blocked(adapter, identity, reason, "capability")
        return outcome

    repo = config.repositories[dev_card.repository].path
    authorization_path = (
        Path(review_config.resolved_authorization_dir(root)) / f"{dev_task_id}.json"
    )
    changes_events = adapter.changes_requested_events(dev_task_id)
    previous_code = _previous_dev_code_commit(root, dev_task_id, dev_run_id)

    decision = release_preflight(
        identity=identity,
        task_row=dev_row,
        runs=runs,
        dev_run_dir=dev_dir,
        repo=repo,
        package_dir=package_root,
        main_ref=merge_config.main_ref,
        authorization_path=authorization_path,
        merge_enabled=merge_config.enabled,
        post_merge_commands=merge_config.post_merge_commands,
        max_rework_rounds=review_config.max_rework_rounds,
        release_run_dir=run_dir,
        requirement_id=requirement_id,
        executor_profile=dev_card.executor,
        changes_events=changes_events,
        previous_code_commit=previous_code,
    )
    report_sha = sha256_file(Path(decision.report_path))
    outcome["release_report_sha256"] = report_sha
    outcome["release_verdict"] = decision.verdict
    outcome["release_reasons"] = list(decision.reasons)

    if not decision.ready:
        detail = decision.reasons[0] if decision.reasons else decision.verdict
        reason = f"release:{detail}"
        _terminal_state(state, run_dir, release_report_sha256=report_sha)
        kind = (
            "capability"
            if decision.verdict == "blocked"
            else _release_reason_kind(decision.reasons)
        )
        outcome.update(action="block", reason=reason)
        outcome["terminal_ok"] = _blocked(adapter, identity, reason, kind)
        return outcome

    binding = decision.binding
    assert binding is not None

    main_before = decision.main_commit or metadata.get("main_commit") or ""

    # D9-I §6.3 step 2: journal the gate result before the critical section.
    state.advance(
        "gated",
        main_before=main_before,
        reviewed_head=binding.reviewed_head,
        release_report_sha256=report_sha,
    )
    write_release_state(run_dir, state)

    # D9-I rule 3: the heartbeat is a board write, so it stays outside the
    # fence.  Inside it only the four fixed steps are allowed.
    _heartbeat(adapter, identity)

    # D9-I §6.3 step 3: the board's own write transaction is the single
    # mutual-exclusion point.  Every host takeover path needs the same SQLite
    # write lock, so no takeover can complete between the re-check and the
    # local merge commit.
    merge = None
    stale = False
    with adapter.write_fence():
        # ① re-check run + claim field by field under the write lock.
        if not _owns(adapter, identity):
            stale = True
        else:
            # ② the write-ahead intent: the only prior record recovery reads.
            write_merge_intent(
                run_dir,
                new_merge_intent(
                    identity=identity,
                    main_ref=merge_config.main_ref,
                    main_before=main_before,
                    reviewed_head=binding.reviewed_head,
                    requirement_id=requirement_id,
                ),
            )
            # ③ the bounded local merge (no push/tag/reset/rebase/force).
            merge = merge_release(
                repo=repo,
                main_ref=merge_config.main_ref,
                reviewed_head=binding.reviewed_head,
                main_before=main_before,
                allow=dev_card.allow,
                run_dir=run_dir,
                identity=identity,
                requirement_id=requirement_id,
                timeout=MERGE_TIMEOUT,
            )
            # ④ the merge journal, still inside the transaction.
            if merge.ok:
                state.advance(
                    "merged",
                    merge_sha=merge.merge_sha,
                    merge_tree=merge.merge_tree,
                )
                write_release_state(run_dir, state)

    if stale:
        # ①② failed: roll back with zero git side effects.
        _terminal_state(state, run_dir, release_report_sha256=report_sha)
        outcome.update(action="stale_run", reason="stale_run")
        outcome["terminal_ok"] = False
        return outcome

    assert merge is not None
    outcome["merge_reasons"] = list(merge.reasons)
    if not merge.ok:
        reason = merge.reasons[0] if merge.reasons else "merge-failed"
        _terminal_state(
            state, run_dir, release_report_sha256=report_sha
        )
        kind = _release_reason_kind(merge.reasons)
        outcome.update(action="block", reason=reason)
        outcome["terminal_ok"] = _blocked(adapter, identity, reason, kind)
        return outcome

    outcome["merge_sha"] = merge.merge_sha
    outcome["merge_tree"] = merge.merge_tree

    # D9-I §6.3 step 4: after the fence, heartbeat and re-read the claim.  A
    # stale run must not run post-merge commands or complete the merge card;
    # the occurred merge is recorded and left to an operator.
    _heartbeat(adapter, identity)
    if not _owns(adapter, identity):
        _terminal_state(state, run_dir, release_report_sha256=report_sha)
        outcome.update(action="stale_run", reason="stale_run")
        outcome["terminal_ok"] = False
        return outcome

    postmerge = run_post_merge(
        repo=repo,
        merge_sha=merge.merge_sha or "",
        merge_tree=merge.merge_tree,
        run_dir=run_dir,
        commands=merge_config.post_merge_commands,
        timeout_seconds=merge_config.post_merge_timeout_seconds,
        identity=identity,
        requirement_id=requirement_id,
        inherited_env=inherited_env,
    )
    postmerge_sha = sha256_file(run_dir / "postmerge" / "report.json")
    state.advance(
        "postmerge",
        postmerge_report_sha256=postmerge_sha,
    )
    write_release_state(run_dir, state)
    outcome["postmerge_report_sha256"] = postmerge_sha
    outcome["postmerge"] = postmerge

    if postmerge.get("verdict") != "pass":
        reasons = postmerge.get("reasons") or ["postmerge-failed"]
        reason = reasons[0]
        _terminal_state(
            state, run_dir, postmerge_report_sha256=postmerge_sha
        )
        outcome.update(action="block", reason=reason)
        outcome["terminal_ok"] = _blocked(adapter, identity, reason, "needs_input")
        return outcome

    _heartbeat(adapter, identity)
    completed = adapter.complete_task(
        identity,
        result=(
            f"{requirement_id} release merged {merge.merge_sha} "
            f"(reviewed {binding.reviewed_head})"
        ),
        summary=f"{requirement_id} release {dev_task_id}",
        metadata={
            "hkl": {
                "requirement_id": requirement_id,
                "dev_task_id": dev_task_id,
                "dev_run_id": binding.dev_run_id,
                "review_run_id": binding.review_run_id,
                "rework_round": binding.rework_round,
                "reviewed_head": binding.reviewed_head,
                "evidence_index_sha256": binding.evidence_index_sha256,
                "merge_sha": merge.merge_sha,
                "merge_tree": merge.merge_tree,
                "parents": list(merge.document.get("parents") or []),
                "release_report_sha256": report_sha,
                "postmerge_report_sha256": postmerge_sha,
            }
        },
    )
    _terminal_state(
        state,
        run_dir,
        release_report_sha256=report_sha,
        postmerge_report_sha256=postmerge_sha,
    )
    outcome.update(
        action="merged" if completed else "merged-unrecorded",
        reason=None if completed else "stale_run",
    )
    outcome["terminal_ok"] = completed
    return outcome


# ---------------------------------------------------------------------------
# 4. recovery
# ---------------------------------------------------------------------------


def _release_card_for(
    root: Path, config: LaneConfig, task_id: str
) -> CardSpec | None:
    _, card = _load_card(root, config, task_id)
    return card


def _commit_parents(repo: str | os.PathLike[str], commit: str) -> list[str]:
    try:
        result = run_git(
            ["rev-list", "--parents", "-n", "1", commit], cwd=repo, timeout=30
        )
    except GitError:
        return []
    if result.returncode != 0:
        return []
    fields = result.stdout.split()
    return fields[1:]


def _merge_head_present(repo: str | os.PathLike[str]) -> bool:
    try:
        result = run_git(
            ["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=repo, timeout=30
        )
    except GitError:
        return False
    return result.returncode == 0


def _write_refused_merge(
    run_dir: Path,
    *,
    identity: RunIdentity,
    requirement_id: str,
    main_before: str,
    reviewed_head: str,
    reason: str,
) -> None:
    """Record a refused merge without ever touching ``git merge``."""

    document = {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "identity": identity.to_dict(),
        "task_id": identity.task_id,
        "main_before": main_before,
        "reviewed_head": reviewed_head,
        "merge_sha": None,
        "merge_tree": None,
        "parents": [],
        "main_before_is_ancestor": False,
        "changed_paths": [],
        "outside_allow": 0,
        "reviewed_paths_changed_by_merge": [],
        "protected_roots_touched": 0,
        "command": {
            "argv": [],
            "exit_code": None,
            "timed_out": False,
            "stdout_log": None,
            "stdout_sha256": None,
            "stdout_size": None,
            "stderr_log": None,
            "stderr_sha256": None,
            "stderr_size": None,
            "recovered": True,
        },
        "verdict": "refused",
        "reasons": [reason],
        "aborted": True,
    }
    _write_json(run_dir / "merge.json", document)


def recover_releases(
    work_root: str | os.PathLike[str],
    adapter: BoardAdapter,
    config: LaneConfig,
    *,
    requirement_id: str = RELEASE_REQUIREMENT_ID,
    inherited_env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    root = Path(work_root)
    runs_root = root / "runs"
    recovered: list[dict[str, Any]] = []
    if not runs_root.is_dir():
        return recovered
    review_config = config.review_options()
    merge_config = review_config.merge
    for task_dir in sorted(runs_root.iterdir()):
        if not task_dir.is_dir():
            continue
        for run_path in sorted(task_dir.iterdir()):
            if not run_path.is_dir():
                continue
            state = read_release_state(run_path)
            if state is None:
                continue
            intent = read_merge_intent(run_path)
            # A ``claimed``/``gated`` journal with an intent means the process
            # died inside the critical section; recovery must classify, never
            # merge again.
            crashed = state.stage in ("claimed", "gated") and intent is not None
            if state.stage not in ("merged", "postmerge") and not crashed:
                continue
            entry: dict[str, Any] = {
                "merge_task_id": state.merge_task_id,
                "dev_task_id": state.dev_task_id,
                "stage": state.stage,
                "action": None,
            }
            row = adapter.task_row(state.merge_task_id)
            if row is None or row.get("status") != "running":
                entry["action"] = "not-running"
                recovered.append(entry)
                continue
            if adapter.identity(state.merge_task_id) != state.identity:
                entry["action"] = "stale_run"
                recovered.append(entry)
                continue
            card = _release_card_for(root, config, state.merge_task_id)
            if card is None or card.card_kind != "merge" or not card.closure_for:
                _terminal_state(state, run_path)
                entry["action"] = "blocked:merge-card-missing"
                _blocked(
                    adapter,
                    state.identity,
                    "merge-card-missing",
                    "capability",
                )
                recovered.append(entry)
                continue
            repo = config.repositories[card.repository].path
            if crashed:
                assert intent is not None
                try:
                    current_head = repo_head(repo)
                except GitError:
                    current_head = None
                parents = (
                    _commit_parents(repo, current_head) if current_head else []
                )
                decision = classify_merge_intent(
                    repo_head=current_head,
                    parents=parents,
                    merge_head_present=_merge_head_present(repo),
                    main_before=intent.main_before,
                    reviewed_head=intent.reviewed_head,
                )
                entry["intent"] = decision
                if decision == MERGE_INTENT_RETRY:
                    # The merge never happened (repo head is ``main_before``
                    # with no MERGE_HEAD).  Leave the claim to the normal TTL
                    # sweep; recovery never starts a new merge.
                    entry["action"] = "retry"
                    recovered.append(entry)
                    continue
                if decision == MERGE_INTENT_ABORT:
                    abort_merge(repo)
                    _write_refused_merge(
                        run_path,
                        identity=state.identity,
                        requirement_id=requirement_id,
                        main_before=intent.main_before,
                        reviewed_head=intent.reviewed_head,
                        reason="merge-conflict",
                    )
                    _terminal_state(state, run_path)
                    entry["action"] = "blocked:merge-conflict"
                    _blocked(
                        adapter, state.identity, "merge-conflict", "needs_input"
                    )
                    recovered.append(entry)
                    continue
                if decision == MERGE_INTENT_DIVERGED:
                    _terminal_state(state, run_path)
                    entry["action"] = "blocked:release-diverged"
                    _blocked(
                        adapter,
                        state.identity,
                        "release-diverged",
                        "capability",
                    )
                    recovered.append(entry)
                    continue
                # MERGE_INTENT_ADOPT: the merge commit already exists.  Rebuild
                # the journal from git facts -- never run a second merge.
                assert decision == MERGE_INTENT_ADOPT, decision
                record = record_existing_merge(
                    repo=repo,
                    main_ref=intent.main_ref,
                    reviewed_head=intent.reviewed_head,
                    main_before=intent.main_before,
                    allow=card.allow,
                    run_dir=run_path,
                    identity=state.identity,
                    requirement_id=requirement_id,
                )
                if not record.ok:
                    _terminal_state(state, run_path)
                    entry["action"] = "blocked:release-diverged"
                    _blocked(
                        adapter,
                        state.identity,
                        "release-diverged",
                        "capability",
                    )
                    recovered.append(entry)
                    continue
                state.advance(
                    "merged",
                    main_before=intent.main_before,
                    reviewed_head=intent.reviewed_head,
                    merge_sha=record.merge_sha,
                    merge_tree=record.merge_tree,
                )
                write_release_state(run_path, state)
                entry["adopted"] = True
            if state.merge_sha is None:
                _terminal_state(state, run_path)
                entry["action"] = "blocked:merge-sha-missing"
                _blocked(
                    adapter, state.identity, "release-diverged", "capability"
                )
                recovered.append(entry)
                continue
            try:
                head = repo_head(repo)
            except GitError:
                head = None
            if head != state.merge_sha:
                _terminal_state(state, run_path)
                entry["action"] = "blocked:release-diverged"
                _blocked(
                    adapter, state.identity, "release-diverged", "capability"
                )
                recovered.append(entry)
                continue

            postmerge = run_post_merge(
                repo=repo,
                merge_sha=state.merge_sha,
                merge_tree=state.merge_tree,
                run_dir=run_path,
                commands=merge_config.post_merge_commands,
                timeout_seconds=merge_config.post_merge_timeout_seconds,
                identity=state.identity,
                requirement_id=requirement_id,
                inherited_env=inherited_env,
            )
            postmerge_sha = sha256_file(run_path / "postmerge" / "report.json")
            if state.stage != "postmerge":
                state.advance("postmerge", postmerge_report_sha256=postmerge_sha)
            write_release_state(run_path, state)
            if postmerge.get("verdict") != "pass":
                reasons = postmerge.get("reasons") or ["postmerge-failed"]
                reason = reasons[0]
                _terminal_state(
                    state, run_path, postmerge_report_sha256=postmerge_sha
                )
                entry["action"] = f"blocked:{reason}"
                _blocked(adapter, state.identity, reason, "needs_input")
                recovered.append(entry)
                continue

            completed = adapter.complete_task(
                state.identity,
                result=f"{requirement_id} recovered release {state.merge_sha}",
                summary=f"{requirement_id} release {state.dev_task_id}",
                metadata={
                    "hkl": {
                        "requirement_id": requirement_id,
                        "dev_task_id": state.dev_task_id,
                        "merge_sha": state.merge_sha,
                        "merge_tree": state.merge_tree,
                        "postmerge_report_sha256": postmerge_sha,
                        "recovered": True,
                    }
                },
            )
            _terminal_state(
                state, run_path, postmerge_report_sha256=postmerge_sha
            )
            entry["action"] = "completed" if completed else "stale_run"
            recovered.append(entry)
    return recovered


def main(argv: Sequence[str] | None = None) -> int:
    """Operator one-shot entry: observe, queue and recover for one board."""

    from ..config import load_config

    parser = argparse.ArgumentParser(
        prog="hermes-kanban-lanes-release",
        description="Observe reviews, queue merge cards and recover releases.",
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--requirement-id", default=RELEASE_REQUIREMENT_ID)
    namespace = parser.parse_args(argv)
    config = load_config(namespace.config)
    if namespace.board not in config.boards:
        print(
            json.dumps({"ok": False, "error": f"unknown board: {namespace.board}"}),
            file=sys.stderr,
        )
        return 2
    adapter = BoardAdapter(
        config.boards[namespace.board],
        config.hermes_source,
        name=namespace.board,
    ).open()
    try:
        root = Path(config.work_root)
        observed = observe_reviews(
            root, adapter, config, requirement_id=namespace.requirement_id
        )
        queued = queue_release_cards(
            root, adapter, config, requirement_id=namespace.requirement_id
        )
        recovered = recover_releases(
            root, adapter, config, requirement_id=namespace.requirement_id
        )
    finally:
        adapter.close()
    print(
        json.dumps(
            {
                "observed": observed,
                "queued": queued,
                "recovered": recovered,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry
    sys.exit(main())


__all__ = [
    "MERGE_ASSIGNEE",
    "OBSERVE_LEDGER_REQUIREMENT_ID",
    "observe_reviews",
    "queue_release_cards",
    "recover_releases",
    "release_once",
]
