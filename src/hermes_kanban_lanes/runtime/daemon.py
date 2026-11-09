"""Synchronous daemon: preflight -> recovery -> discovery -> claim -> runs.

One process, a bounded thread pool and SQLite claims.  There is no broker, no
network access and no second writer: every board mutation goes through
:class:`~hermes_kanban_lanes.hermes_adapter.board.BoardAdapter`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

try:  # POSIX advisory locks; Windows has no equivalent in v0.1 (D4).
    import fcntl
except ImportError:  # pragma: no cover - only on non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

from ..card_contract import (
    SYNTHESIS_MISSING,
    CardSpec,
    CardSynthesisError,
    load_card,
    synthesize_card,
)
from ..config import LaneConfig, load_config
from ..contracts import ContractError, RunIdentity
from ..executors import ADAPTERS, adapter_for
from ..hermes_adapter.board import BoardAdapter, BoardAdapterError
from ..workspace.git import GitError, preflight_repo, repo_head
from .release import (
    MERGE_ASSIGNEE,
    observe_reviews,
    queue_release_cards,
    recover_releases,
    release_once,
)
from .retention import maybe_prune_runs, rotate_daemon_log
from .supervisor import RunOutcome, RunSettings, recover_runs, run_once

REQUIREMENT_ID = "HKL-W3"
LOCK_FILENAME = "daemon.lock"
LOCK_SCHEMA_VERSION = 1


class DaemonLockError(RuntimeError):
    """Base class for single-instance lock failures."""


class DaemonLockUnsupported(DaemonLockError):
    """``fcntl`` is unavailable (Windows); exit 2 with ``daemon-lock-unsupported``."""


class DaemonLockHeld(DaemonLockError):
    """Another process holds ``daemon.lock``; exit 3 with ``daemon-lock-held``."""

    def __init__(self, holder: dict[str, Any] | None) -> None:
        self.holder = holder or {}
        super().__init__("daemon-lock-held")


def _read_lock_document(fd: int) -> dict[str, Any] | None:
    try:
        raw = os.pread(fd, 65536, 0)
    except OSError:
        return None
    if not raw:
        return None
    try:
        document = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def config_sha256(config: LaneConfig) -> str:
    """Stable fingerprint of the validated configuration object."""

    payload = json.dumps(config.to_dict(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DaemonLock:
    """A held ``fcntl.flock`` plus its on-disk identity document."""

    def __init__(
        self,
        work_root: str | os.PathLike[str],
        config: LaneConfig,
        *,
        version: str,
        sha256: str | None = None,
    ) -> None:
        self.path = Path(work_root) / LOCK_FILENAME
        self.config = config
        self.version = version
        self.sha256 = sha256 or config_sha256(config)
        self.previous: dict[str, Any] | None = None
        self.stale_reclaimed = False
        self._fd: int | None = None

    @property
    def pid(self) -> int:
        return os.getpid()

    @staticmethod
    def _same_file(fd: int, path: Path) -> bool:
        """True when ``path`` still names the inode locked through ``fd``.

        A release unlinks the lock file while still holding the flock, so a
        waiter that opened the old inode cannot end up licensed by a file the
        path no longer refers to.  This check closes the symmetric race on
        acquire: if the path was replaced between ``open`` and ``flock``,
        retry against the current file instead of treating it as locked.
        """

        try:
            opened = os.fstat(fd)
            current = os.stat(path)
        except OSError:
            return False
        return (opened.st_dev, opened.st_ino) == (
            current.st_dev,
            current.st_ino,
        )

    def acquire(self) -> "DaemonLock":
        if fcntl is None:
            raise DaemonLockUnsupported("daemon-lock-unsupported")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(8):
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                holder = _read_lock_document(fd)
                os.close(fd)
                raise DaemonLockHeld(holder) from None
            if not self._same_file(fd, self.path):
                # The path moved under us (release unlinked the old inode):
                # the lock we just took protects nothing visible.  Retry.
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                continue
            previous = _read_lock_document(fd)
            if previous is not None and previous.get("pid") != os.getpid():
                self.stale_reclaimed = True
            self.previous = previous
            document = {
                "schema_version": LOCK_SCHEMA_VERSION,
                "pid": os.getpid(),
                "started_at": int(time.time()),
                "version": self.version,
                "config_sha256": self.sha256,
                "stale_reclaimed": bool(self.stale_reclaimed),
            }
            payload = json.dumps(document, sort_keys=True).encode("utf-8")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            self._fd = fd
            return self
        raise DaemonLockHeld(None)

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            # Unlink while the flock is still held: any process that reaches
            # ``flock`` from here on either fails against this inode or locks
            # the newly created file; none can hold both.
            try:
                if self._same_file(fd, self.path):
                    self.path.unlink()
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "DaemonLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def acquire_daemon_lock(
    work_root: str | os.PathLike[str],
    config: LaneConfig,
    *,
    version: str | None = None,
    config_sha256: str | None = None,
) -> DaemonLock:
    """Take the exclusive lock or raise :class:`DaemonLockHeld`."""

    from .. import __version__ as installed_version

    return DaemonLock(
        work_root,
        config,
        version=version or installed_version,
        sha256=config_sha256,
    ).acquire()


def probe_daemon_lock(work_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Read-only probe for ``doctor``: never writes ``daemon.lock``."""

    path = Path(work_root) / LOCK_FILENAME
    if fcntl is None:
        return {"state": "unsupported", "holder": None}
    if not path.is_file():
        return {"state": "free", "holder": None}
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return {"state": "free", "holder": None}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"state": "held", "holder": _read_lock_document(fd)}
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return {"state": "free", "holder": _read_lock_document(fd)}
    finally:
        os.close(fd)


class StopFlag:
    """A stop event that also carries the reclaim reason."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: str | None = None

    def set(self, reason: str = "cancelled") -> None:
        if self.reason is None:
            self.reason = reason
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


def _executable_available(executable: str) -> bool:
    if os.path.sep in executable:
        return os.path.isfile(executable) and os.access(executable, os.X_OK)
    return shutil.which(executable) is not None


def _executor_alias_supported(alias: str) -> bool:
    """D2: the registry is the only list of supported executor aliases."""

    return alias in ADAPTERS


def executor_for(alias: str) -> Any:
    """Instantiate the registered adapter; unknown aliases fail closed."""

    try:
        return adapter_for(alias)
    except KeyError:
        raise BoardAdapterError(f"executor-not-supported:{alias}") from None


def preflight(config: LaneConfig) -> list[str]:
    """Read-only checks; any entry means exit 2 before claim/spawn (D7/W2-10)."""

    errors: list[str] = []

    source = config.hermes_source
    if not os.path.isdir(source):
        errors.append("hermes-source-missing")
    else:
        try:
            head = repo_head(source)
        except GitError:
            errors.append("hermes-source-git-failed")
        else:
            if head != config.hermes_commit:
                errors.append("hermes-commit-mismatch")

    for alias, board_path in config.boards.items():
        ok, reason = BoardAdapter.preflight_file(board_path)
        if not ok:
            errors.append(f"board:{alias}:{reason}")

    work_root = config.work_root
    if not os.path.isdir(work_root):
        errors.append("work-root-missing")
    elif not os.access(work_root, os.W_OK | os.X_OK):
        errors.append("work-root-not-writable")

    for alias, repository in config.repositories.items():
        # Uncommitted WIP in a team checkout must not block startup/claiming:
        # runs read HEAD and work in their own worktree.  The merge path keeps
        # its own require_clean=True gate.
        ok, reason = preflight_repo(repository.path, require_clean=False)
        if not ok:
            errors.append(f"repository:{alias}:{reason}")

    for alias, executor in config.executors.items():
        ok = _executable_available(executor.argv[0])
        if not ok:
            errors.append(f"executor:{alias}:executor-not-resolvable")
            continue
        if _executor_alias_supported(alias):
            supported, reason = executor_for(alias).preflight(executor.argv)
            if not supported:
                errors.append(f"executor:{alias}:{reason}")
    return errors


def _card_for(
    config: LaneConfig,
    work_root: Path,
    task_id: str,
    alias: str,
    *,
    body: str | None = None,
) -> tuple[CardSpec | None, str, str | None]:
    """Load one card, synthesising it from the body when no JSON exists.

    Explicit ``cards/<task_id>.json`` always wins (D2).  A synthesis failure
    returns a stable reason and never claims; ``body=None`` keeps the
    historical ``card-missing`` answer for callers without a body (e.g. the
    recovery path).
    """

    path = work_root / "cards" / f"{task_id}.json"
    if path.is_file():
        try:
            card = load_card(path, config)
        except ContractError as exc:
            return None, "card-invalid", str(exc)
    elif body is not None:
        try:
            card = synthesize_card(body, task_id, alias, config)
        except CardSynthesisError as exc:
            return None, exc.reason, exc.detail
        except ContractError as exc:
            return None, "card-invalid", str(exc)
    else:
        return None, "card-missing", None
    if card.lineage != task_id:
        return None, "card-lineage-mismatch", None
    if card.card_kind != "implementation":
        return None, "card-kind-mismatch", None
    if card.executor != alias:
        return None, "card-executor-mismatch", None
    if not _executor_alias_supported(card.executor):
        return None, "unsupported-executor", None
    if card.repository not in config.repositories:
        # ``load_card`` already checks this, kept as defence in depth.
        return None, "repository-not-configured", None
    return card, "ok", None


def _merge_card_for(
    config: LaneConfig, work_root: Path, task_id: str
) -> tuple[CardSpec | None, str, str | None]:
    """Load a merge card; it must close over a real implementation card."""

    path = work_root / "cards" / f"{task_id}.json"
    if not path.is_file():
        return None, "card-missing", None
    try:
        card = load_card(path, config)
    except ContractError as exc:
        return None, "card-invalid", str(exc)
    if card.lineage != task_id:
        return None, "card-lineage-mismatch", None
    if card.card_kind != "merge":
        return None, "card-kind-mismatch", None
    if not card.closure_for:
        return None, "closure-for-missing", None
    if card.executor not in config.executors:
        return None, "executor-not-configured", None
    if card.repository not in config.repositories:
        return None, "repository-not-configured", None
    return card, "ok", None


def _run_job(
    *,
    config: LaneConfig,
    board_alias: str,
    task: Mapping[str, Any],
    card: CardSpec,
    identity: RunIdentity,
    settings: RunSettings,
    stop_event: Any,
    package_dir: str | os.PathLike[str] | None,
    runner: Callable[..., RunOutcome],
    requirement_id: str = REQUIREMENT_ID,
) -> dict[str, Any]:
    adapter = BoardAdapter(
        config.boards[board_alias],
        config.hermes_source,
        name=board_alias,
    )
    try:
        adapter.open()
        outcome = runner(
            config=config,
            adapter=adapter,
            board_alias=board_alias,
            task=task,
            card=card,
            identity=identity,
            executor=executor_for(card.executor),
            settings=settings,
            stop_event=stop_event,
            package_dir=package_dir,
            requirement_id=requirement_id,
        )
        return outcome.to_dict()
    finally:
        adapter.close()


def _release_phase(
    *,
    config: LaneConfig,
    board_alias: str,
    work_root: Path,
    package_dir: str | os.PathLike[str] | None,
    requirement_id: str,
    stop_event: Any,
) -> list[dict[str, Any]]:
    """Claim ready ``hkl:merge`` cards and release them synchronously."""

    results: list[dict[str, Any]] = []
    adapter = BoardAdapter(
        config.boards[board_alias],
        config.hermes_source,
        name=board_alias,
    )
    try:
        adapter.open()
        try:
            tasks = adapter.list_ready(MERGE_ASSIGNEE)
        except Exception as exc:  # noqa: BLE001 - report, keep the tick alive
            return [{"board": board_alias, "error": type(exc).__name__}]
        for task in tasks:
            task_id = str(task.get("id") or "")
            card, reason, detail = _merge_card_for(config, work_root, task_id)
            if card is None:
                results.append(
                    {
                        "board": board_alias,
                        "task_id": task_id,
                        "action": "skipped",
                        "reason": reason,
                        **({"detail": detail} if detail else {}),
                    }
                )
                continue
            if stop_event is not None and stop_event.is_set():
                results.append(
                    {
                        "board": board_alias,
                        "task_id": task_id,
                        "action": "skipped",
                        "reason": "daemon-stopping",
                    }
                )
                continue
            identity = adapter.claim(task_id)
            if identity is None:
                results.append(
                    {
                        "board": board_alias,
                        "task_id": task_id,
                        "action": "skipped",
                        "reason": "claim-lost",
                    }
                )
                continue
            try:
                outcome = release_once(
                    config=config,
                    adapter=adapter,
                    card=card,
                    identity=identity,
                    work_root=work_root,
                    package_dir=package_dir,
                    requirement_id=requirement_id,
                )
            except Exception as exc:  # noqa: BLE001 - never fake a release
                results.append(
                    {
                        "board": board_alias,
                        "task_id": task_id,
                        "action": "error",
                        "reason": type(exc).__name__,
                    }
                )
                continue
            results.append({"board": board_alias, **outcome})
    finally:
        adapter.close()
    return results


def tick(
    config: LaneConfig,
    *,
    settings: RunSettings | None = None,
    stop_event: Any = None,
    adapters: Mapping[str, BoardAdapter] | None = None,
    tick_index: int = 0,
    package_dir: str | os.PathLike[str] | None = None,
    runner: Callable[..., RunOutcome] = run_once,
    max_workers: int | None = None,
    requirement_id: str = REQUIREMENT_ID,
) -> dict[str, Any]:
    """One discovery/claim/run cycle.  Never accepts an invalid card."""

    settings = settings or RunSettings()
    work_root = Path(config.work_root)
    started_at = int(time.time())
    report: dict[str, Any] = {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "tick": int(tick_index),
        "started_at": started_at,
        "finished_at": None,
        "recovery": {},
        "recovered": [],
        "release_recovered": [],
        "release_observed": [],
        "release_queued": [],
        "discovered": [],
        "skipped": [],
        "claimed": [],
        "runs": [],
        "releases": [],
        "errors": [],
    }

    owned: dict[str, BoardAdapter] = {}
    if adapters is not None:
        boards = dict(adapters)
    else:
        boards = {}
        for alias, board_path in config.boards.items():
            adapter = BoardAdapter(
                board_path,
                config.hermes_source,
                name=alias,
            )
            try:
                adapter.open()
            except BoardAdapterError as exc:
                report["errors"].append(f"board:{alias}:{exc}")
                continue
            owned[alias] = adapter
            boards[alias] = adapter

    jobs: list[tuple[str, Mapping[str, Any], CardSpec, RunIdentity]] = []
    try:
        # -- recovery ---------------------------------------------------
        # Order is a correctness rule (spec 6.1 / W3-10): evidence-complete runs
        # are finalized *before* any stale-claim/crash sweep.  Reclaiming first
        # would release a task whose executor PID is (correctly) dead, and the
        # next discovery pass would execute it a second time.
        for alias, adapter in boards.items():
            entry: dict[str, Any] = {}
            # D10: release recovery, observation and queueing happen before the
            # W3 recovery/discovery pass.
            try:
                release_recovered = recover_releases(
                    work_root,
                    adapter,
                    config,
                    requirement_id=requirement_id,
                )
            except Exception as exc:  # noqa: BLE001 - report, never fake success
                entry["release_recover_error"] = type(exc).__name__
                report["errors"].append(f"recovery:{alias}:release")
            else:
                report["release_recovered"].extend(
                    {"board": alias, **item} for item in release_recovered
                )
                entry["release_recovered"] = len(release_recovered)
            try:
                observed = observe_reviews(
                    work_root, adapter, config, requirement_id=requirement_id
                )
            except Exception as exc:  # noqa: BLE001
                entry["observe_error"] = type(exc).__name__
                report["errors"].append(f"observe:{alias}")
            else:
                report["release_observed"].extend(
                    {"board": alias, **item} for item in observed
                )
                entry["observed"] = len(observed)
            try:
                queued = queue_release_cards(
                    work_root, adapter, config, requirement_id=requirement_id
                )
            except Exception as exc:  # noqa: BLE001
                entry["queue_error"] = type(exc).__name__
                report["errors"].append(f"queue:{alias}")
            else:
                report["release_queued"].extend(
                    {"board": alias, **item} for item in queued
                )
                entry["queued"] = len(queued)
            try:
                recovered = recover_runs(
                    work_root,
                    adapter,
                    package_dir,
                    requirement_id=requirement_id,
                    reviewer=config.review_options().reviewer,
                )
            except Exception as exc:  # noqa: BLE001 - report, never fake success
                entry["recover_error"] = type(exc).__name__
                report["errors"].append(f"recovery:{alias}:recover")
            else:
                report["recovered"].extend(
                    {"board": alias, **item} for item in recovered
                )
                entry["recovered"] = len(recovered)
            try:
                entry["released"] = adapter.release_stale_claims()
            except Exception as exc:  # noqa: BLE001 - report, continue
                entry["release_error"] = type(exc).__name__
                report["errors"].append(f"recovery:{alias}:release")
            try:
                entry["crashed"] = adapter.detect_crashed_workers()
            except Exception as exc:  # noqa: BLE001
                entry["crash_error"] = type(exc).__name__
                report["errors"].append(f"recovery:{alias}:crashed")
            report["recovery"][alias] = entry

        # -- discovery / claim -------------------------------------------
        slots = max_workers if max_workers is not None else config.max_concurrent
        stop = stop_event is not None and stop_event.is_set()
        # One task row can only match one assignee, but overlapping assignee
        # lists across aliases must not let this process discover it twice.
        seen_tasks: set[tuple[str, str]] = set()
        for alias, adapter in boards.items():
            if stop or len(jobs) >= slots:
                break
            for executor_alias, executor_config in config.executors.items():
                if stop or len(jobs) >= slots:
                    break
                for assignee in executor_config.effective_assignees(executor_alias):
                    if stop or len(jobs) >= slots:
                        break
                    try:
                        tasks = adapter.list_ready(assignee)
                    except Exception as exc:  # noqa: BLE001
                        report["errors"].append(
                            f"discovery:{alias}:{type(exc).__name__}"
                        )
                        continue
                    for task in tasks:
                        task_id = str(task.get("id") or "")
                        key = (alias, task_id)
                        if key in seen_tasks:
                            continue
                        seen_tasks.add(key)
                        report["discovered"].append(
                            {"board": alias, "assignee": assignee, "task_id": task_id}
                        )
                        if stop or len(jobs) >= slots:
                            report["skipped"].append(
                                {
                                    "board": alias,
                                    "task_id": task_id,
                                    "reason": "slots-full",
                                }
                            )
                            continue
                        card_path = work_root / "cards" / f"{task_id}.json"
                        explicit_card = card_path.is_file()
                        card, reason, detail = _card_for(
                            config,
                            work_root,
                            task_id,
                            executor_alias,
                            body=str(task.get("body") or ""),
                        )
                        if card is None:
                            entry = {
                                "board": alias,
                                "task_id": task_id,
                                "reason": reason,
                            }
                            if detail:
                                entry["detail"] = detail
                            report["skipped"].append(entry)
                            # D2: a body that cannot be synthesised must not be
                            # silent.  A plain body with no lane block is not an
                            # HKL card at all, so it keeps the historical
                            # ``card-missing`` answer and gets no event.
                            if reason != SYNTHESIS_MISSING and not explicit_card:
                                try:
                                    adapter.record_skipped(task_id, reason, detail)
                                except Exception as exc:  # noqa: BLE001
                                    report["errors"].append(
                                        f"discovery:{alias}:skipped-event:"
                                        f"{type(exc).__name__}"
                                    )
                            continue
                        if not explicit_card and card.review_forced:
                            # D3: the forced-review override is visible on the
                            # card even before the claim.
                            try:
                                adapter.record_review_forced(
                                    task_id,
                                    card.review_forced_reason
                                    or "review-forced",
                                )
                            except Exception as exc:  # noqa: BLE001
                                report["errors"].append(
                                    f"discovery:{alias}:card-event:"
                                    f"{type(exc).__name__}"
                                )
                        identity = adapter.claim(task_id)
                        if identity is None:
                            report["skipped"].append(
                                {
                                    "board": alias,
                                    "task_id": task_id,
                                    "reason": "claim-lost",
                                }
                            )
                            continue
                        if stop_event is not None and stop_event.is_set():
                            adapter.reclaim(identity, "daemon-stop")
                            report["skipped"].append(
                                {
                                    "board": alias,
                                    "task_id": task_id,
                                    "reason": "daemon-stopping",
                                }
                            )
                            continue
                        report["claimed"].append(
                            {
                                "board": alias,
                                "task_id": task_id,
                                "run_id": identity.run_id,
                            }
                        )
                        jobs.append((alias, task, card, identity))

        # Discovery connections are not shared with run threads.
        for adapter in owned.values():
            adapter.close()
        owned.clear()

        if jobs:
            with ThreadPoolExecutor(max_workers=max(1, slots)) as pool:
                futures = [
                    (
                        job[0],
                        job[3],
                        pool.submit(
                            _run_job,
                            config=config,
                            board_alias=job[0],
                            task=job[1],
                            card=job[2],
                            identity=job[3],
                            settings=settings,
                            stop_event=stop_event,
                            package_dir=package_dir,
                            runner=runner,
                            requirement_id=requirement_id,
                        ),
                    )
                    for job in jobs
                ]
                for board_alias, identity, future in futures:
                    try:
                        report["runs"].append(future.result())
                    except Exception as exc:  # noqa: BLE001 - keep the tick alive
                        report["errors"].append(
                            f"run:{board_alias}:{identity.task_id}:{type(exc).__name__}"
                        )

        # -- release (hkl:merge) -----------------------------------------
        # Releases run after all development jobs of this tick finished, with a
        # fresh connection: the discovery connection was closed above.
        for alias in config.boards:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                releases = _release_phase(
                    config=config,
                    board_alias=alias,
                    work_root=work_root,
                    package_dir=package_dir,
                    requirement_id=requirement_id,
                    stop_event=stop_event,
                )
            except Exception as exc:  # noqa: BLE001 - report, keep tick alive
                report["errors"].append(f"release:{alias}:{type(exc).__name__}")
                continue
            report["releases"].extend(releases)
    finally:
        for adapter in owned.values():
            adapter.close()
        report["finished_at"] = int(time.time())
    return report


def _append_log(
    work_root: str | os.PathLike[str],
    record: Mapping[str, Any],
    *,
    logging_config: Any = None,
) -> None:
    """Append one JSON line; rotate per W6 D8 when a config is supplied."""

    logs = Path(work_root) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    message = json.dumps(dict(record), sort_keys=True)
    if logging_config is None:
        with open(logs / "daemon.log", "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
        return
    rotate_daemon_log(
        logs / "daemon.log",
        message,
        max_bytes=int(logging_config.max_log_bytes),
        backups=int(logging_config.backups),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hermes_kanban_lanes.runtime.daemon",
        description="HKL-W3 synchronous lane daemon (single writer).",
    )
    parser.add_argument("--config", required=True, help="LaneConfig JSON path")
    parser.add_argument("--once", action="store_true", help="run one tick and exit")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--max-runtime-seconds", type=float, default=3600.0)
    parser.add_argument("--command-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=60.0)
    parser.add_argument("--requirement-id", default=REQUIREMENT_ID)
    return parser.parse_args(argv)


def log_lock_event(
    work_root: str | os.PathLike[str],
    lock: DaemonLock,
    *,
    logging_config: Any = None,
) -> None:
    """Record a reclaimed stale lock once (D4); a clean start is silent."""

    if not lock.stale_reclaimed:
        return
    previous = lock.previous or {}
    _append_log(
        work_root,
        {
            "record": "daemon-lock",
            "stale_reclaimed": True,
            "previous_pid": previous.get("pid"),
            "pid": lock.pid,
            "version": lock.version,
        },
        logging_config=logging_config,
    )


def run_daemon(
    config: LaneConfig,
    *,
    settings: RunSettings | None = None,
    poll_seconds: float = 5.0,
    once: bool = False,
    requirement_id: str = REQUIREMENT_ID,
    stop: StopFlag | None = None,
) -> dict[str, Any]:
    """The single polling loop reused by both entry points (W6 D2/D6).

    The caller owns the single-instance lock; this function only runs ticks,
    appends the report, runs the interval-gated retention pass and handles
    SIGTERM/SIGINT by returning the last report.
    """

    settings = settings or RunSettings()
    stop = stop or StopFlag()

    def _handle_signal(signum, frame) -> None:  # noqa: ANN001 - signal API
        stop.set("daemon-stop")

    previous_term = signal.signal(signal.SIGTERM, _handle_signal)
    previous_int = signal.signal(signal.SIGINT, _handle_signal)
    logging_config = config.logging_options()
    index = 0
    try:
        while True:
            report = tick(
                config,
                settings=settings,
                stop_event=stop,
                tick_index=index,
                requirement_id=requirement_id,
            )
            _append_log(
                config.work_root, report, logging_config=logging_config
            )
            try:
                maybe_prune_runs(config.work_root, logging_config)
            except Exception:  # noqa: BLE001 - retention never kills the daemon
                pass
            if once:
                return report
            index += 1
            if stop.is_set():
                return report
            stop.wait(max(0.0, poll_seconds))
            if stop.is_set():
                return report
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        config = load_config(args.config)
    except ContractError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2

    errors = preflight(config)
    if errors:
        print(json.dumps({"ok": False, "errors": errors}), file=sys.stderr)
        return 2

    settings = RunSettings(
        max_runtime_seconds=args.max_runtime_seconds,
        command_timeout_seconds=args.command_timeout_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
    )
    report = run_daemon(
        config,
        settings=settings,
        poll_seconds=args.poll_seconds,
        once=args.once,
        requirement_id=args.requirement_id,
    )
    if args.once:
        print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())


__all__ = [
    "DaemonLock",
    "DaemonLockError",
    "DaemonLockHeld",
    "DaemonLockUnsupported",
    "LOCK_FILENAME",
    "StopFlag",
    "acquire_daemon_lock",
    "config_sha256",
    "executor_for",
    "log_lock_event",
    "main",
    "preflight",
    "probe_daemon_lock",
    "run_daemon",
    "tick",
]
