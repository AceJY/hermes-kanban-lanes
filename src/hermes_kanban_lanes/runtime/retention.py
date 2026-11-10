"""Log rotation and run/workspace retention (W6 D8, W11c).

Two independent concerns live here:

* :func:`rotate_daemon_log` appends one JSON line through
  ``logging.handlers.RotatingFileHandler`` so ``logs/daemon.log`` rotates in
  process, without a separate cleaner;
* :func:`prune_runs` deletes only *terminal* run directories that are past
  ``retention_days``.  Byte pressure may reclaim a run's disposable ``tmp/``
  and ``home/`` caches, but never a successful run whose evidence is still
  inside the retention window, nor a run whose review has passed and whose
  release has not merged yet.  The most recent ``keep_failed_runs`` non-success
  runs stay protected, and a workspace is only removed when a recorded process
  witness proves the tree is gone.

:func:`remove_run_tmp` is the idempotent tmp purge shared by the supervisor's
terminal cleanup and retention.  Neither function writes the lane
configuration, claims a board task or starts a process.  ``doctor`` calls none
of the mutating helpers.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import shutil
import time
from pathlib import Path
from typing import Any, Mapping

from .process import pid_alive, prove_terminated
from .state import read_state

SCHEMA_VERSION = 1
DAEMON_LOG_NAME = "daemon.log"
PRUNE_STATE_NAME = "prune-state.json"
PRUNE_REPORT_NAME = "prune-report.json"
PRUNE_INTERVAL_SECONDS = 3600
_DAY_SECONDS = 86400
RUN_TMP_NAME = "tmp"
EVIDENCE_DIR_NAME = "evidence"
LEDGER_DIR_NAME = "ledger"
CARDS_DIR_NAME = "cards"
RUNS_DIR_NAME = "runs"

# Only disposable executor caches are reclaimed from ``home/``.  Session logs
# (``.claude/sessions``, ``.hermes/sessions|logs``), identity files and
# memories are deliberately absent from this list.
_HOME_CACHE_RELATIVE_PATHS = (
    ".cache",
    ".npm/_cacache",
    ".npm/_prebuilds",
    ".npm/_npx",
    ".rustup/downloads",
    ".rustup/tmp",
    ".hermes/image_cache",
    ".hermes/audio_cache",
    ".claude/shell-snapshots",
    ".claude/backups",
    "__pycache__",
    ".pytest_cache",
)

_APPROVED_REVIEW_OUTCOMES = frozenset({"approved", "approve", "pass", "passed"})


def rotate_daemon_log(
    log_path: str | os.PathLike[str],
    message: str,
    *,
    max_bytes: int,
    backups: int,
) -> Path:
    """Append ``message`` to ``log_path`` through a rotating handler.

    ``RotatingFileHandler`` rotates *before* the write that would exceed
    ``max_bytes`` and keeps ``backups`` numbered files.  The handler is created
    per record; the daemon writes one report per tick, not one per line.
    """

    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=int(max_bytes),
        backupCount=int(backups),
        encoding="utf-8",
        delay=False,
    )
    try:
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            name="hermes_kanban_lanes.daemon",
            level=logging.INFO,
            pathname=str(path),
            lineno=0,
            msg=message,
            args=(),
            exc_info=None,
        )
        handler.emit(record)
    finally:
        handler.close()
    return path


def last_error_line(log_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Return the last parsed JSON log record that reports an error."""

    path = Path(log_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw in reversed(text.splitlines()):
        line = raw.strip()
        if not line:
            continue
        try:
            document = json.loads(line)
        except ValueError:
            continue
        if not isinstance(document, dict):
            continue
        errors = document.get("errors")
        if (isinstance(errors, list) and errors) or document.get("error"):
            return document
    return None


def _directory_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def _run_sizes(run_dir: Path) -> tuple[int, int, int]:
    """One walk for ``(total, tmp, home-cache)`` bytes."""

    total = tmp_size = cache_size = 0
    cache_paths = tuple(Path(relative) for relative in _HOME_CACHE_RELATIVE_PATHS)
    for root, _dirs, files in os.walk(run_dir):
        root_path = Path(root)
        try:
            relative = root_path.relative_to(run_dir)
        except ValueError:
            continue
        in_tmp = relative.parts[:1] == (RUN_TMP_NAME,)
        home_relative = (
            Path(*relative.parts[1:])
            if relative.parts[:1] == ("home",)
            else None
        )
        in_cache = home_relative is not None and any(
            home_relative == cache or cache in home_relative.parents
            for cache in cache_paths
        )
        for name in files:
            try:
                size = os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            total += size
            if in_tmp:
                tmp_size += size
            if in_cache:
                cache_size += size
    return total, tmp_size, cache_size


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _remove_path(path: Path) -> int:
    """Remove one file/directory tree and return the bytes it occupied."""

    if not (path.exists() or path.is_symlink()):
        return 0
    size = _directory_size(path)
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    except OSError:
        # A concurrent writer or a permission problem must never abort the
        # pass; the next prune retries whatever is left.
        return 0
    return size


def remove_run_tmp(run_dir: str | os.PathLike[str]) -> int:
    """Delete ``run_dir/tmp``; return the bytes removed, idempotently.

    The supervisor calls this once the run is terminal (success or failure)
    and retention calls it for every terminal run it observes.  The run's
    ``run.json``, ``evidence/``, ``stated`` outputs and ``home/`` survive.
    """

    return _remove_path(Path(run_dir) / RUN_TMP_NAME)


def _purge_home_caches(run_dir: Path) -> int:
    """Remove only known disposable caches, keeping sessions and identity."""

    home = run_dir / "home"
    return sum(
        _remove_path(home / relative) for relative in _HOME_CACHE_RELATIVE_PATHS
    )


def _approved_reviews(
    root: Path, task_id: str
) -> list[tuple[int | None, str | None]]:
    """``(dev_run_id, reviewed_head)`` for each passing review of ``task_id``.

    The release observer is the durable, filesystem-visible record of the
    board's review metadata, so retention reads it instead of opening the
    board.  Entries derive ``verdict`` from ``review_outcome`` exactly like the
    release layer does.
    """

    ledger = _load_json(root / LEDGER_DIR_NAME / f"{task_id}.json")
    reviews = ledger.get("reviews") if isinstance(ledger, dict) else None
    if not isinstance(reviews, list):
        return []
    approvals: list[tuple[int | None, str | None]] = []
    for entry in reviews:
        if not isinstance(entry, dict):
            continue
        verdict = entry.get("verdict")
        outcome = entry.get("review_outcome")
        approved = (
            isinstance(verdict, str)
            and verdict.strip().lower() in _APPROVED_REVIEW_OUTCOMES
        ) or (
            isinstance(outcome, str)
            and outcome.strip().lower() in _APPROVED_REVIEW_OUTCOMES
        )
        if not approved:
            continue
        dev_run_id = entry.get("dev_run_id")
        if (
            isinstance(dev_run_id, bool)
            or not isinstance(dev_run_id, int)
            or dev_run_id <= 0
        ):
            dev_run_id = None
        head = entry.get("reviewed_head")
        reviewed_head = head if isinstance(head, str) and head else None
        if dev_run_id is None and reviewed_head is None:
            continue
        approvals.append((dev_run_id, reviewed_head))
    return approvals


def _document_approval(
    document: Mapping[str, Any] | None, run_id: int
) -> tuple[int | None, str | None] | None:
    """A run document that carries its own passing review (defensive form)."""

    if not isinstance(document, dict):
        return None
    containers: list[Mapping[str, Any]] = [document]
    review = document.get("review")
    if isinstance(review, dict):
        containers.append(review)
    for container in containers:
        verdict = container.get("verdict")
        outcome = container.get("review_outcome", container.get("outcome"))
        approved = (
            isinstance(verdict, str)
            and verdict.strip().lower() in _APPROVED_REVIEW_OUTCOMES
        ) or (
            isinstance(outcome, str)
            and outcome.strip().lower() in _APPROVED_REVIEW_OUTCOMES
        )
        if approved:
            head = container.get("reviewed_head")
            return run_id, head if isinstance(head, str) and head else None
    return None


def _completed_merges(root: Path) -> list[tuple[str, str | None, int | None]]:
    """``(dev_task_id, reviewed_head, dev_run_id)`` for finished releases.

    Merge cards are the durable link between a release run and the
    development card it closes; a ``merge.json`` with ``verdict == "merged"``
    (or a release state past ``gated``) proves the release completed.
    """

    merge_cards: dict[str, str] = {}
    cards_root = root / CARDS_DIR_NAME
    if cards_root.is_dir():
        for path in sorted(cards_root.glob("*.json")):
            card = _load_json(path)
            if not isinstance(card, dict) or card.get("card_kind") != "merge":
                continue
            dev_task_id = card.get("closure_for")
            if isinstance(dev_task_id, str) and dev_task_id:
                merge_cards[path.stem] = dev_task_id
    completions: list[tuple[str, str | None, int | None]] = []
    for merge_task_id, dev_task_id in merge_cards.items():
        runs_root = root / RUNS_DIR_NAME / merge_task_id
        if not runs_root.is_dir():
            continue
        for run_dir in sorted(runs_root.iterdir()):
            if not run_dir.is_dir():
                continue
            head: str | None = None
            dev_run_id: int | None = None
            merged = False
            document = _load_json(run_dir / "merge.json")
            if isinstance(document, dict) and document.get("verdict") == "merged":
                merged = True
                raw_head = document.get("reviewed_head")
                head = raw_head if isinstance(raw_head, str) and raw_head else None
                raw_run_id = document.get("dev_run_id")
                if isinstance(raw_run_id, int) and not isinstance(raw_run_id, bool):
                    dev_run_id = raw_run_id
            state = _load_json(run_dir / "state.json")
            if isinstance(state, dict) and state.get("stage") in (
                "merged",
                "postmerge",
            ):
                merged = True
                raw_head = state.get("reviewed_head")
                if head is None and isinstance(raw_head, str) and raw_head:
                    head = raw_head
                raw_run_id = state.get("dev_run_id")
                if dev_run_id is None and isinstance(raw_run_id, int):
                    dev_run_id = raw_run_id
            if merged:
                completions.append((dev_task_id, head, dev_run_id))
    return completions


def _has_evidence(
    run_dir: Path, document: Mapping[str, Any] | None = None
) -> bool:
    """Whether a success is a real, evidenced result rather than a stub.

    The directory is authoritative.  When only metadata survives, the run
    document's ``evidence`` block or a complete ``identity`` is accepted.
    Synthetic fixtures that only carry ``finished_at``/``category`` are not
    releasable results and remain ordinary byte-pressure material.
    """

    if (run_dir / EVIDENCE_DIR_NAME).is_dir():
        return True
    if not isinstance(document, dict):
        return False
    evidence = document.get("evidence")
    if isinstance(evidence, dict) and (
        evidence.get("index_path") or evidence.get("verified")
    ):
        return True
    identity = document.get("identity")
    return isinstance(identity, dict) and bool(
        identity.get("task_id")
    ) and identity.get("run_id") is not None


def _pending_release_keys(root: Path, entries: list[dict[str, Any]]) -> set[str]:
    """Run directories whose passing review has not merged yet."""

    by_task: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_task.setdefault(str(entry["task_id"]), []).append(entry)
    completions = _completed_merges(root)
    merged_by_task: dict[str, list[tuple[str | None, int | None]]] = {}
    for dev_task_id, head, dev_run_id in completions:
        merged_by_task.setdefault(dev_task_id, []).append((head, dev_run_id))

    pending: set[str] = set()
    for task_id, task_entries in by_task.items():
        approvals = _approved_reviews(root, task_id)
        for entry in task_entries:
            document_approval = _document_approval(
                entry["document"], int(entry["run_id"])
            )
            if document_approval is not None:
                approvals.append(document_approval)
        if not approvals:
            continue
        merged = merged_by_task.get(task_id, [])
        for entry in task_entries:
            code_commit = entry.get("code_commit")
            run_id = int(entry["run_id"])
            for approved_run_id, reviewed_head in approvals:
                matches = reviewed_head is not None and code_commit == reviewed_head
                if approved_run_id is not None and approved_run_id == run_id:
                    matches = True
                if not matches:
                    continue
                already_merged = any(
                    (head is not None and code_commit == head)
                    or (merged_run_id is not None and merged_run_id == run_id)
                    for head, merged_run_id in merged
                )
                if not already_merged:
                    pending.add(str(entry["run_dir"]))
    return pending


def _run_finished_at(document: Mapping[str, Any] | None) -> int | None:
    if not isinstance(document, dict):
        return None
    value = document.get("finished_at")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _run_category(document: Mapping[str, Any] | None) -> str:
    if not isinstance(document, dict):
        return "unknown"
    value = document.get("category")
    return value if isinstance(value, str) and value else "unknown"


def _process_witness(
    state: Any, document: Mapping[str, Any] | None
) -> tuple[int | None, str | None, int | None]:
    """Recover ``(pid, fingerprint, pgid)`` from run.json, then state.json."""

    cleanup = document.get("cleanup") if isinstance(document, dict) else None
    cleanup = cleanup if isinstance(cleanup, dict) else {}
    pid = cleanup.get("pid")
    fingerprint = cleanup.get("fingerprint")
    pgid = cleanup.get("pgid")
    if (pid is None or pgid is None) and state is not None and state.child:
        pid = state.child.get("pid")
        fingerprint = state.child.get("fingerprint")
        pgid = state.child.get("pgid")

    def _int(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return value

    return _int(pid), (fingerprint if isinstance(fingerprint, str) else None), _int(pgid)


def _workspace_of(
    work_root: Path, run_dir: Path, state: Any, document: Mapping[str, Any] | None
) -> Path | None:
    raw = None
    if isinstance(document, dict) and isinstance(document.get("workspace"), str):
        raw = document["workspace"]
    elif state is not None and state.workspace:
        raw = state.workspace
    if not raw:
        return None
    candidate = Path(raw)
    try:
        resolved = candidate.resolve()
        workspaces = (work_root / "workspaces").resolve()
    except OSError:
        return None
    if resolved == workspaces or workspaces not in resolved.parents:
        # Only delete inside the lane's own workspace tree.
        return None
    return candidate


def _collect_runs(
    root: Path,
    logging_config: Any,
    current: int,
) -> tuple[
    list[dict[str, Any]], list[dict[str, str]], set[str], list[dict[str, Any]]
]:
    """Read-only classification shared by prune and doctor preview."""

    runs_root = root / "runs"
    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    if runs_root.is_dir():
        for task_dir in sorted(runs_root.iterdir()):
            if not task_dir.is_dir():
                continue
            for run_dir in sorted(task_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                state = read_state(run_dir)
                document = _load_json(run_dir / "run.json")
                if state is None:
                    skipped.append(
                        {"run_dir": str(run_dir), "reason": "unreadable-state"}
                    )
                    continue
                if state.stage != "terminal":
                    skipped.append(
                        {"run_dir": str(run_dir), "reason": "non-terminal"}
                    )
                    continue
                finished_at = _run_finished_at(document)
                if finished_at is None:
                    skipped.append(
                        {"run_dir": str(run_dir), "reason": "missing-finished-at"}
                    )
                    continue
                pid, fingerprint, pgid = _process_witness(state, document)
                if pid_alive(pid):
                    skipped.append({"run_dir": str(run_dir), "reason": "running"})
                    continue
                category = _run_category(document)
                code_commit = (
                    document.get("code_commit") if isinstance(document, dict) else None
                )
                size, tmp_size, cache_size = _run_sizes(run_dir)
                entries.append(
                    {
                        "run_dir": run_dir,
                        "task_id": state.task_id or task_dir.name,
                        "run_id": int(state.run_id),
                        "finished_at": finished_at,
                        "category": category,
                        "failed": category != "success",
                        "size": size,
                        "tmp_size": tmp_size,
                        "cache_size": cache_size,
                        "has_evidence": _has_evidence(run_dir, document),
                        "code_commit": code_commit
                        if isinstance(code_commit, str)
                        else None,
                        "state": state,
                        "document": document,
                        "pid": pid,
                        "fingerprint": fingerprint,
                        "pgid": pgid,
                    }
                )

    # Protect the most recent N failures regardless of age or size.
    failed = sorted(
        (entry for entry in entries if entry["failed"]),
        key=lambda entry: entry["finished_at"],
        reverse=True,
    )
    keep_failed = int(logging_config.keep_failed_runs)
    protected = {
        str(entry["run_dir"]) for entry in failed[: max(0, keep_failed)]
    }
    return entries, skipped, protected, failed


def _size_protected(
    entry: dict[str, Any],
    pending_keys: set[str],
    *,
    retention_cutoff: int,
) -> bool:
    """Runs that byte pressure must shrink rather than delete.

    * a successful run whose evidence is still inside the retention window;
      only ``retention_days`` may remove the whole directory (a success with
      no evidence directory is not a releasable result and stays ordinary
      pressure material);
    * a run with a passing review whose release has not merged yet -- at the
      very least its ``run.json`` and ``evidence/`` must survive so the merge
      queue can still bind it.
    """

    if str(entry["run_dir"]) in pending_keys:
        return True
    return (
        entry["category"] == "success"
        and entry["finished_at"] >= retention_cutoff
        and bool(entry["has_evidence"])
    )


def _plan_deletions(
    entries: list[dict[str, Any]],
    protected: set[str],
    pending_keys: set[str],
    *,
    retention_cutoff: int,
    max_total: int,
) -> dict[str, Any]:
    """Replay the prune order without touching the filesystem.

    Order matters: disposable ``tmp/`` first, then age deletion, then ``home/``
    caches for survivors, and only then whole-directory pressure deletion.
    """

    candidates = sorted(entries, key=lambda entry: entry["finished_at"])
    remaining: dict[str, int] = {}
    tmp_targets: list[dict[str, Any]] = []
    for entry in entries:
        key = str(entry["run_dir"])
        tmp_size = int(entry.get("tmp_size") or 0)
        tmp_path = Path(entry["run_dir"]) / RUN_TMP_NAME
        if tmp_size > 0 or tmp_path.exists() or tmp_path.is_symlink():
            tmp_targets.append(entry)
        remaining[key] = max(0, int(entry["size"]) - tmp_size)

    age_deleted: list[dict[str, Any]] = []
    for entry in candidates:
        key = str(entry["run_dir"])
        if key in protected:
            continue
        if entry["finished_at"] >= retention_cutoff:
            continue
        age_deleted.append(entry)
        remaining.pop(key, None)

    cache_targets: list[dict[str, Any]] = []
    for entry in entries:
        key = str(entry["run_dir"])
        if key not in remaining:
            continue
        cache_size = int(entry.get("cache_size") or 0)
        if cache_size > 0:
            cache_targets.append(entry)
            remaining[key] = max(0, remaining[key] - cache_size)

    size_deleted: list[dict[str, Any]] = []
    total = sum(remaining.values())
    for entry in candidates:
        if total <= max_total:
            break
        key = str(entry["run_dir"])
        if key not in remaining:
            continue
        if key in protected:
            continue
        if _size_protected(entry, pending_keys, retention_cutoff=retention_cutoff):
            continue
        size_deleted.append(entry)
        total -= remaining.pop(key, 0)

    return {
        "age_deleted": age_deleted,
        "size_deleted": size_deleted,
        "tmp_targets": tmp_targets,
        "cache_targets": cache_targets,
        "projected_total": total,
    }


def prune_preview(
    work_root: str | os.PathLike[str],
    logging_config: Any,
    now: int | float | None = None,
) -> dict[str, Any]:
    """Read-only counts for ``doctor``'s ``logs`` check (D8).

    Nothing is deleted; the classification mirrors :func:`prune_runs` exactly.
    """

    root = Path(work_root)
    current = int(now if now is not None else time.time())
    retention_cutoff = current - int(logging_config.retention_days) * _DAY_SECONDS
    max_total = int(logging_config.max_total_bytes)
    entries, skipped, protected, _failed = _collect_runs(
        root, logging_config, current
    )
    pending_keys = _pending_release_keys(root, entries)
    plan = _plan_deletions(
        entries,
        protected,
        pending_keys,
        retention_cutoff=retention_cutoff,
        max_total=max_total,
    )
    prunable = list(plan["age_deleted"]) + list(plan["size_deleted"])
    return {
        "schema_version": SCHEMA_VERSION,
        "now": current,
        "prunable_runs": len(prunable),
        "prunable_bytes": sum(entry["size"] for entry in prunable),
        "reclaimable_tmp_bytes": sum(
            entry["tmp_size"] for entry in plan["tmp_targets"]
        ),
        "reclaimable_cache_bytes": sum(
            entry["cache_size"] for entry in plan["cache_targets"]
        ),
        "protected_pending_release": len(pending_keys),
        "skipped_runs": len(skipped),
        "retained_failed_runs": len(protected),
        "total_runs": len(entries) + len(skipped),
        "total_bytes": sum(entry["size"] for entry in entries),
    }


def prune_runs(
    work_root: str | os.PathLike[str],
    logging_config: Any,
    now: int | float | None = None,
) -> dict[str, Any]:
    """Delete eligible terminal runs; never touches a live or non-terminal one.

    ``logging_config`` is a :class:`~hermes_kanban_lanes.config.LoggingConfig`.
    ``now`` is an injectable clock (epoch seconds) for deterministic tests.
    """

    root = Path(work_root)
    current = int(now if now is not None else time.time())
    retention_cutoff = current - int(logging_config.retention_days) * _DAY_SECONDS
    max_total = int(logging_config.max_total_bytes)
    keep_failed = int(logging_config.keep_failed_runs)

    entries, skipped, protected, failed = _collect_runs(
        root, logging_config, current
    )
    pending_keys = _pending_release_keys(root, entries)
    plan = _plan_deletions(
        entries,
        protected,
        pending_keys,
        retention_cutoff=retention_cutoff,
        max_total=max_total,
    )
    total_before = sum(entry["size"] for entry in entries)

    deleted: list[dict[str, Any]] = []
    tmp_removed: list[dict[str, Any]] = []
    cache_removed: list[dict[str, Any]] = []
    workspace_skipped: list[dict[str, str]] = []
    removed_keys: set[str] = set()

    def _delete(entry: dict[str, Any]) -> int:
        run_dir = entry["run_dir"]
        size = _directory_size(run_dir)
        shutil.rmtree(run_dir)
        removed_keys.add(str(run_dir))
        workspace = _workspace_of(root, run_dir, entry["state"], entry["document"])
        if workspace is not None:
            terminated, _witness = prove_terminated(
                entry["pid"], entry["fingerprint"], entry["pgid"]
            )
            if terminated:
                shutil.rmtree(workspace, ignore_errors=True)
            else:
                workspace_skipped.append(
                    {
                        "workspace": str(workspace),
                        "reason": "workspace-prune-skipped",
                    }
                )
        deleted.append(
            {
                "run_dir": str(run_dir),
                "category": entry["category"],
                "bytes": size,
            }
        )
        return size

    # 1. Every terminal run loses its disposable tmp, idempotently, whether or
    #    not it survives this pass.
    for entry in plan["tmp_targets"]:
        removed = remove_run_tmp(entry["run_dir"])
        if removed:
            tmp_removed.append(
                {"run_dir": str(entry["run_dir"]), "bytes": removed}
            )

    # 2. The retention window still governs whole-directory deletion.
    for entry in plan["age_deleted"]:
        _delete(entry)

    # 3. Survivors give up their home caches before any whole-directory
    #    pressure deletion; session logs and identity are never touched.
    for entry in plan["cache_targets"]:
        removed = _purge_home_caches(entry["run_dir"])
        if removed:
            cache_removed.append(
                {"run_dir": str(entry["run_dir"]), "bytes": removed}
            )

    # 4. Byte pressure may only remove runs the plan found unprotected.
    for entry in plan["size_deleted"]:
        _delete(entry)

    total_after = sum(
        _directory_size(entry["run_dir"])
        for entry in entries
        if str(entry["run_dir"]) not in removed_keys
    )
    retained_failed = [
        {
            "run_dir": str(entry["run_dir"]),
            "category": entry["category"],
            "finished_at": entry["finished_at"],
        }
        for entry in failed[: max(0, keep_failed)]
    ]
    deleted_bytes = sum(item["bytes"] for item in deleted)
    tmp_bytes = sum(item["bytes"] for item in tmp_removed)
    cache_bytes = sum(item["bytes"] for item in cache_removed)

    return {
        "schema_version": SCHEMA_VERSION,
        "now": current,
        "deleted": deleted,
        "skipped": skipped,
        "retained_failed": retained_failed,
        "workspace_skipped": workspace_skipped,
        "tmp_removed": tmp_removed,
        "home_cache_removed": cache_removed,
        "protected_pending_release": sorted(pending_keys),
        "deleted_bytes": deleted_bytes,
        "tmp_freed_bytes": tmp_bytes,
        "home_cache_freed_bytes": cache_bytes,
        "freed_bytes": deleted_bytes + tmp_bytes + cache_bytes,
        "total_bytes_before": total_before,
        "total_bytes_after": total_after,
    }


def maybe_prune_runs(
    work_root: str | os.PathLike[str],
    logging_config: Any,
    now: int | float | None = None,
) -> dict[str, Any]:
    """Run :func:`prune_runs` at most once per :data:`PRUNE_INTERVAL_SECONDS`."""

    root = Path(work_root)
    logs = root / "logs"
    marker = logs / PRUNE_STATE_NAME
    current = int(now if now is not None else time.time())
    previous = _load_json(marker)
    if isinstance(previous, dict):
        last = previous.get("last_pruned_at")
        if isinstance(last, int) and not isinstance(last, bool):
            if current - last < PRUNE_INTERVAL_SECONDS:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "skipped": "interval",
                    "last_pruned_at": last,
                    "now": current,
                }

    report = prune_runs(root, logging_config, now=current)
    logs.mkdir(parents=True, exist_ok=True)
    (logs / PRUNE_REPORT_NAME).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    marker.write_text(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "last_pruned_at": current},
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return report


def rotate_count(log_path: str | os.PathLike[str]) -> int:
    """Number of rotated ``daemon.log.N`` files present."""

    path = Path(log_path)
    return sum(
        1
        for child in path.parent.iterdir()
        if child.is_file() and child.name.startswith(path.name + ".")
        and child.name[len(path.name) + 1 :].isdigit()
    ) if path.parent.is_dir() else 0


__all__ = [
    "DAEMON_LOG_NAME",
    "PRUNE_INTERVAL_SECONDS",
    "PRUNE_REPORT_NAME",
    "PRUNE_STATE_NAME",
    "last_error_line",
    "maybe_prune_runs",
    "prune_preview",
    "prune_runs",
    "remove_run_tmp",
    "rotate_count",
    "rotate_daemon_log",
]
