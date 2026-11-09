"""Log rotation and run/workspace retention (W6 D8).

Two independent concerns live here:

* :func:`rotate_daemon_log` appends one JSON line through
  ``logging.handlers.RotatingFileHandler`` so ``logs/daemon.log`` rotates in
  process, without a separate cleaner;
* :func:`prune_runs` deletes only *terminal* run directories that are past
  ``retention_days`` or needed to satisfy ``max_total_bytes``, always protecting
  the most recent ``keep_failed_runs`` non-success runs, and only removes a
  workspace when a recorded process witness proves the tree is gone.

Neither function writes the lane configuration, claims a board task or starts a
process.  ``doctor`` calls none of the mutating helpers.
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


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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
                entries.append(
                    {
                        "run_dir": run_dir,
                        "finished_at": finished_at,
                        "category": category,
                        "failed": category != "success",
                        "size": _directory_size(run_dir),
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


def _eligible(
    entries: list[dict[str, Any]],
    protected: set[str],
    *,
    retention_cutoff: int,
    max_total: int,
) -> list[dict[str, Any]]:
    """Return the entries a prune would delete, oldest first."""

    candidates = sorted(entries, key=lambda entry: entry["finished_at"])
    chosen: list[dict[str, Any]] = []
    remaining = {str(entry["run_dir"]): entry["size"] for entry in entries}
    for entry in candidates:
        key = str(entry["run_dir"])
        if key in protected:
            continue
        if entry["finished_at"] >= retention_cutoff:
            continue
        chosen.append(entry)
        remaining.pop(key, None)
    total = sum(remaining.values())
    if total > max_total:
        for entry in candidates:
            if total <= max_total:
                break
            key = str(entry["run_dir"])
            if key in protected or key not in remaining:
                continue
            chosen.append(entry)
            total -= remaining.pop(key, 0)
    return chosen


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
    prunable = _eligible(
        entries,
        protected,
        retention_cutoff=retention_cutoff,
        max_total=max_total,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "now": current,
        "prunable_runs": len(prunable),
        "prunable_bytes": sum(entry["size"] for entry in prunable),
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
    total_before = sum(entry["size"] for entry in entries)

    candidates = sorted(entries, key=lambda entry: entry["finished_at"])
    deleted: list[dict[str, Any]] = []
    workspace_skipped: list[dict[str, str]] = []

    def _delete(entry: dict[str, Any]) -> int:
        run_dir = entry["run_dir"]
        size = entry["size"]
        shutil.rmtree(run_dir)
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

    remaining = {str(entry["run_dir"]): entry["size"] for entry in entries}
    for entry in candidates:
        key = str(entry["run_dir"])
        if key in protected:
            continue
        if entry["finished_at"] >= retention_cutoff:
            continue
        _delete(entry)
        remaining.pop(key, None)

    total_after_age = sum(remaining.values())
    if total_after_age > max_total:
        for entry in candidates:
            if total_after_age <= max_total:
                break
            key = str(entry["run_dir"])
            if key in protected or key not in remaining:
                continue
            _delete(entry)
            removed = remaining.pop(key, 0)
            total_after_age -= removed

    total_after = sum(remaining.values())
    retained_failed = [
        {
            "run_dir": str(entry["run_dir"]),
            "category": entry["category"],
            "finished_at": entry["finished_at"],
        }
        for entry in failed[: max(0, keep_failed)]
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "now": current,
        "deleted": deleted,
        "skipped": skipped,
        "retained_failed": retained_failed,
        "workspace_skipped": workspace_skipped,
        "freed_bytes": sum(item["bytes"] for item in deleted),
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
    "rotate_count",
    "rotate_daemon_log",
]
