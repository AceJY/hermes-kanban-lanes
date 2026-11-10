"""The one board adapter (the only host-compatible reader/writer).

Everything the product needs from the Hermes kanban board goes through this
class.  The class is deliberately defensive:

* ``hermes_cli`` is imported lazily, from the configured ``hermes_source``, so
  importing the package (and building its wheel) never requires the host.
* every terminal write carries a run identity; the host calls that accept
  ``expected_run_id`` are always given it.
* ``reclaim`` has no host-side fence argument, so it performs an equivalent
  run/claim CAS inside its own ``BEGIN IMMEDIATE`` transaction.
* intents are applied by :mod:`hermes_kanban_lanes.hermes_adapter.intents`
  inside the same transaction as their side effect and receipt.
* :meth:`BoardAdapter.write_fence` exposes the host's own write transaction
  (``BEGIN IMMEDIATE``).  D9-I uses it as the one mutual-exclusion point shared
  with every host takeover path; no product-side advisory lock participates.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..contracts import RunIdentity
from .intents import apply_intent as _apply_intent

SCHEMA_VERSION = 1
_HOST_MODULES: dict[str, tuple[Any, Any, Any]] = {}


class BoardAdapterError(RuntimeError):
    """Raised when the board/host cannot be opened or used safely."""


class RunIdentityError(BoardAdapterError):
    """Raised when the board does not expose the expected run identity."""


def _resolved_source(hermes_source: str) -> str:
    path = os.path.realpath(str(hermes_source))
    if not os.path.isdir(path):
        raise BoardAdapterError("hermes_source: not a directory")
    return path


def _load_host(hermes_source: str):
    """Import the three host modules from the configured source tree."""

    source = _resolved_source(hermes_source)
    cached = _HOST_MODULES.get(source)
    if cached is not None:
        return cached

    if source not in sys.path:
        sys.path.insert(0, source)
    kb = importlib.import_module("hermes_cli.kanban_db")
    kbc = importlib.import_module("hermes_cli.kanban_db_connect")
    kbd = importlib.import_module("hermes_cli.kanban_db_dispatch")
    for module in (kb, kbc, kbd):
        origin = os.path.realpath(getattr(module, "__file__", "") or "")
        if not origin.startswith(source + os.sep):
            raise BoardAdapterError(
                "hermes_cli was imported from outside the configured hermes_source"
            )
    _HOST_MODULES[source] = (kb, kbc, kbd)
    return _HOST_MODULES[source]


class BoardAdapter:
    """One connection to one board, plus the host-module handles it needs."""

    def __init__(
        self,
        board_path: str | os.PathLike[str],
        hermes_source: str | os.PathLike[str],
        *,
        name: str = "board",
    ) -> None:
        self.board_path = os.path.abspath(os.fspath(board_path))
        self.hermes_source = _resolved_source(os.fspath(hermes_source))
        self.name = name
        self._kb, self._kbc, self._kbd = _load_host(self.hermes_source)
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise BoardAdapterError("board adapter is not open")
        return self._conn

    def open(self) -> "BoardAdapter":
        if self._conn is not None:
            return self
        path = Path(self.board_path)
        if not path.is_file():
            raise BoardAdapterError("board: database file does not exist")
        try:
            self._conn = self._kbc.connect(db_path=path)
        except Exception as exc:  # host-specific exception surface
            raise BoardAdapterError(f"board: cannot open database ({type(exc).__name__})") from None
        return self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "BoardAdapter":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextlib.contextmanager
    def write_fence(self):
        """D9-I: hold the host's board-wide write transaction.

        This is the single mutual-exclusion point between a release critical
        section and every host takeover path (``claim_task``,
        ``reclaim_task``, ``release_stale_claims``, ``heartbeat_claim``,
        ``complete_task``, ...).  They all need the same SQLite write lock, so
        exclusivity does not depend on any caller remembering a product-side
        convention.

        Inside the fence no other board write is legal: a nested ``write_txn``
        raises the host's ``RuntimeError`` unless it explicitly asks for
        savepoint semantics, and a savepoint is not visible to another
        connection until this outer transaction commits.
        """

        with self._kbc.write_txn(self.connection):
            yield

    # -- setup helpers used by tests / the daemon --------------------------

    @classmethod
    def create_board(
        cls,
        board_path: str | os.PathLike[str],
        hermes_source: str | os.PathLike[str],
        *,
        name: str = "board",
    ) -> "BoardAdapter":
        """Create (or open) a board schema; never used on a real board."""

        adapter = cls.__new__(cls)
        adapter.board_path = os.path.abspath(os.fspath(board_path))
        adapter.hermes_source = _resolved_source(os.fspath(hermes_source))
        adapter.name = name
        adapter._kb, adapter._kbc, adapter._kbd = _load_host(adapter.hermes_source)
        path = Path(adapter.board_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        adapter._conn = adapter._kbc.connect(db_path=path)
        return adapter

    @staticmethod
    def preflight_file(board_path: str | os.PathLike[str]) -> tuple[bool, str]:
        """Check a board without creating or migrating anything.

        Read-only URI mode means a missing/foreign file is never turned into a
        new database and no ``tasks`` table is created on failure.
        """

        path = Path(board_path)
        if not path.is_file():
            return False, "board-missing"
        uri = f"file:{path}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
            try:
                conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return False, f"board-unreadable: {type(exc).__name__}"
        return True, "ok"

    def preflight(self) -> tuple[bool, str]:
        return self.preflight_file(self.board_path)

    # -- discovery / claim -------------------------------------------------

    def list_ready(self, assignee: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id, title, body, assignee, status, priority, created_at "
            "FROM tasks WHERE status = 'ready' AND assignee = ? "
            "ORDER BY priority DESC, created_at ASC, id ASC",
            (assignee,),
        ).fetchall()
        return [dict(row) for row in rows]

    def claim(self, task_id: str) -> RunIdentity | None:
        """Atomically claim ``ready -> running``; ``None`` means someone won."""

        claimer = f"{self._kb._claimer_id()}:hkl:{uuid.uuid4().hex[:10]}"
        try:
            task = self._kb.claim_task(self.connection, task_id, claimer=claimer)
        except Exception as exc:
            raise BoardAdapterError(
                f"claim: host call failed ({type(exc).__name__})"
            ) from None
        if task is None or task.current_run_id is None or not task.claim_lock:
            return None
        return RunIdentity(
            schema_version=SCHEMA_VERSION,
            task_id=task.id,
            run_id=int(task.current_run_id),
            claim_lock=str(task.claim_lock),
        )

    def identity(self, task_id: str) -> RunIdentity | None:
        row = self.connection.execute(
            "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None or row["status"] != "running" or row["current_run_id"] is None:
            return None
        lock = row["claim_lock"]
        if not lock:
            return None
        return RunIdentity(
            schema_version=SCHEMA_VERSION,
            task_id=task_id,
            run_id=int(row["current_run_id"]),
            claim_lock=str(lock),
        )

    def owns(self, identity: RunIdentity) -> bool:
        current = self.identity(identity.task_id)
        return current is not None and current == identity

    def heartbeat(self, identity: RunIdentity) -> bool:
        try:
            return bool(
                self._kb.heartbeat_claim(
                    self.connection,
                    identity.task_id,
                    claimer=identity.claim_lock,
                )
            )
        except Exception as exc:
            raise BoardAdapterError(
                f"heartbeat: host call failed ({type(exc).__name__})"
            ) from None

    def record_worker(self, identity: RunIdentity, pid: int) -> bool:
        """Register the executor pid with both run id and claim lock.

        Note host behaviour H04: a guarded UPDATE that matches zero rows may
        still append a misleading ``spawned`` event.  Consumers must therefore
        cross-check run/claim instead of trusting that event.
        """

        self._kbd._set_worker_pid(  # noqa: SLF001 - adapter boundary
            self.connection,
            identity.task_id,
            int(pid),
            run_id=identity.run_id,
            claim_lock=identity.claim_lock,
        )
        return True

    def clear_worker(self, identity: RunIdentity) -> bool:
        """Withdraw this run's worker pid after its executor has exited.

        The host's ``detect_crashed_workers`` reclaims a ``running`` card whose
        ``worker_pid`` is dead.  A finished executor's pid is dead by
        definition, so leaving it published would let the host reclaim the
        card while the gate/review finishing still owns it.  Only this run is
        touched: the task update is fenced by run id and claim lock, and the
        ``task_runs`` mirror update is keyed by the same run id and task but
        only runs when that fenced task update actually matched.

        Returns ``False`` when the card is no longer this run's claim (nothing
        was cleared, in either table); database failures raise so the caller
        can route them through its normal failure path.
        """

        try:
            with self._kbc.write_txn(self.connection):
                cur = self.connection.execute(
                    "UPDATE tasks SET worker_pid = NULL, worker_started_at = NULL "
                    "WHERE id = ? AND current_run_id = ? AND claim_lock = ?",
                    (identity.task_id, identity.run_id, identity.claim_lock),
                )
                cleared = cur.rowcount == 1
                # After losing the claim we write nothing (D2/D6): the doomed
                # pid stays published for the host's own takeover, and the
                # task_runs mirror must not diverge from the task row.
                if cleared:
                    self.connection.execute(
                        "UPDATE task_runs SET worker_pid = NULL, worker_started_at = NULL "
                        "WHERE id = ? AND task_id = ?",
                        (identity.run_id, identity.task_id),
                    )
        except Exception as exc:  # host/sqlite exception surface
            raise BoardAdapterError(
                f"clear_worker: host call failed ({type(exc).__name__})"
            ) from None
        return cleared

    # -- side effects ------------------------------------------------------

    def apply_intent(
        self, identity: RunIdentity, intent: Mapping[str, Any]
    ) -> dict[str, Any]:
        return _apply_intent(
            self.connection,
            self._kb,
            self._kbc.write_txn,
            intent,
            identity=identity,
        )

    def apply_intents(
        self, identity: RunIdentity, intents: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        return [self.apply_intent(identity, intent) for intent in intents]

    def request_review(
        self,
        identity: RunIdentity,
        summary: str | None,
        metadata: Mapping[str, Any] | None = None,
        *,
        reviewer: str | None = None,
    ) -> bool:
        return bool(
            self._kb.request_review(
                self.connection,
                identity.task_id,
                summary=summary,
                metadata=dict(metadata) if metadata else None,
                reviewer=reviewer,
                expected_run_id=identity.run_id,
            )
        )

    def block(self, identity: RunIdentity, reason: str, kind: str) -> bool:
        return bool(
            self._kb.block_task(
                self.connection,
                identity.task_id,
                reason=reason,
                kind=kind,
                expected_run_id=identity.run_id,
            )
        )

    def record_event_once(
        self,
        task_id: str,
        kind: str,
        reason: str,
        detail: str | None = None,
    ) -> None:
        """Append one ``kind`` event unless the last one says the same.

        Discovery runs every few seconds; without the dedupe a persistent
        refusal (or a forced-review note) would be appended forever.
        """

        payload: dict[str, Any] = {"reason": reason}
        if detail:
            payload["detail"] = detail
        with self._kbc.write_txn(self.connection):
            row = self.connection.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                "AND kind = ? ORDER BY id DESC LIMIT 1",
                (task_id, kind),
            ).fetchone()
            if row is not None:
                raw = row["payload"]
                try:
                    previous = json.loads(raw) if isinstance(raw, str) and raw else raw
                except ValueError:
                    previous = None
                if isinstance(previous, dict) and previous.get("reason") == reason:
                    return
            self._kb._append_event(  # noqa: SLF001 - adapter boundary
                self.connection, task_id, kind, payload
            )

    def record_skipped(self, task_id: str, reason: str, detail: str | None = None) -> None:
        """Append one ``skipped`` event (W8 D2) unless the last one says the same.

        A card whose body cannot be synthesised stays ``ready``; without an
        event the refusal would be silent.
        """

        self.record_event_once(task_id, "skipped", reason, detail)

    def record_review_forced(
        self, task_id: str, reason: str, detail: str | None = None
    ) -> None:
        """Record W8 D3's ``review-forced`` override on the card."""

        self.record_event_once(task_id, "review-forced", reason, detail)

    # -- review/release read and write helpers -----------------------------

    def task_row(self, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def run_rows(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id, task_id, profile, status, started_at, ended_at, "
            "outcome, summary, metadata, error FROM task_runs "
            "WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            raw = entry.get("metadata")
            if isinstance(raw, str) and raw:
                try:
                    entry["metadata"] = json.loads(raw)
                except ValueError:
                    entry["metadata"] = None
            result.append(entry)
        return result

    def review_runs(self, task_id: str) -> list[dict[str, Any]]:
        """Runs carrying a reviewer verdict document (D1 metadata).

        Reviewer cards have drifted between ``reviewed_head`` and
        ``reviewed_commit`` for the same fact.  Both keys identify the reviewed
        commit, so either marks a review record and the returned entry always
        carries a normalised ``reviewed_head`` (the original keys stay for
        audit).  When both keys are present but disagree nothing is guessed:
        the entry is flagged as a conflict and the release layer skips it.
        """

        reviews: list[dict[str, Any]] = []
        for entry in self.run_rows(task_id):
            metadata = entry.get("metadata")
            if not isinstance(metadata, dict):
                continue
            has_head = "reviewed_head" in metadata
            has_commit = "reviewed_commit" in metadata
            if not has_head and not has_commit:
                continue
            if (
                has_head
                and has_commit
                and metadata["reviewed_head"] != metadata["reviewed_commit"]
            ):
                # Fail closed: never pick one spelling over the other.
                entry["reviewed_head"] = None
                entry["reviewed_head_conflict"] = {
                    "reviewed_head": metadata["reviewed_head"],
                    "reviewed_commit": metadata["reviewed_commit"],
                }
                reviews.append(entry)
                continue
            reviewed_head = (
                metadata["reviewed_head"] if has_head else metadata["reviewed_commit"]
            )
            entry["reviewed_head"] = reviewed_head
            if not has_head:
                # Add the normalised key without overwriting ``reviewed_commit``.
                normalised = dict(metadata)
                normalised["reviewed_head"] = reviewed_head
                entry["metadata"] = normalised
            reviews.append(entry)
        return reviews

    def changes_requested_events(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'changes_requested' ORDER BY id",
            (task_id,),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            raw = row["payload"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
            if isinstance(raw, str) and raw:
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {}
            elif isinstance(raw, dict):
                payload = raw
            else:
                payload = {}
            events.append(payload if isinstance(payload, dict) else {})
        return events

    def parent_ids(self, task_id: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
            (task_id,),
        ).fetchall()
        return [str(row["parent_id"]) for row in rows]

    def child_ids(self, task_id: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
            (task_id,),
        ).fetchall()
        return [str(row["child_id"]) for row in rows]

    def create_task(
        self,
        *,
        title: str,
        body: str | None = None,
        assignee: str | None = None,
        parents: Iterable[str] = (),
        idempotency_key: str | None = None,
    ) -> str:
        return str(
            self._kb.create_task(
                self.connection,
                title=title,
                body=body,
                assignee=assignee,
                created_by="hermes-kanban-lanes",
                parents=tuple(parents),
                idempotency_key=idempotency_key,
            )
        )

    def link_tasks(self, parent_id: str, child_id: str) -> bool:
        self._kb.link_tasks(self.connection, parent_id, child_id)
        return True

    def complete_task(
        self,
        identity: RunIdentity,
        *,
        result: str,
        summary: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        return bool(
            self._kb.complete_task(
                self.connection,
                identity.task_id,
                result=result,
                summary=summary,
                metadata=dict(metadata) if metadata else None,
                expected_run_id=identity.run_id,
            )
        )

    def request_changes(
        self, identity: RunIdentity, reason: str
    ) -> tuple[bool, str | None]:
        """Close the reviewer run and hand the task back to the implementer."""

        return self._kb.request_changes(
            self.connection,
            identity.task_id,
            reason=reason,
            expected_run_id=identity.run_id,
        )

    def claim_review_task(
        self, task_id: str, *, claimer: str | None = None
    ) -> RunIdentity | None:
        task = self._kb.claim_review_task(
            self.connection,
            task_id,
            claimer=claimer or f"hkl-review:{uuid.uuid4().hex[:10]}",
        )
        if task is None:
            return None
        return RunIdentity(
            schema_version=1,
            task_id=task.id,
            run_id=int(task.current_run_id),
            claim_lock=str(task.claim_lock),
        )

    def reclaim(self, identity: RunIdentity, reason: str) -> bool:
        """Fenced reclaim: run/claim CAS inside its own host write transaction.

        The CAS and the reclaim record share one ``BEGIN IMMEDIATE``.  D9-I
        mutual exclusion with a release merge comes from the database write
        lock itself: this transaction and the release ``write_fence`` cannot
        hold it at the same time.
        """

        kb = self._kb
        with self._kbc.write_txn(self.connection):
            row = self.connection.execute(
                "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?",
                (identity.task_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "running"
                or row["current_run_id"] is None
                or int(row["current_run_id"]) != identity.run_id
                or row["claim_lock"] != identity.claim_lock
            ):
                return False
            retry_status = kb._retry_status_for_run(  # noqa: SLF001
                self.connection, identity.task_id
            )
            cur = self.connection.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND status = 'running' AND current_run_id = ? AND claim_lock = ?",
                (retry_status, identity.task_id, identity.run_id, identity.claim_lock),
            )
            if cur.rowcount != 1:
                return False
            kb._record_reclaim(  # noqa: SLF001
                self.connection,
                identity.task_id,
                {"reason": reason, "terminated_by": "supervisor"},
                error=f"supervisor_reclaim: {reason}",
                payload={
                    "manual": True,
                    "reason": reason,
                    "retry_status": retry_status,
                },
            )
        # Reclaiming a claim resets the operator-visible failure counter.
        kb._clear_failure_counter(self.connection, identity.task_id)  # noqa: SLF001
        return True

    # -- recovery ----------------------------------------------------------

    def release_stale_claims(self) -> int:
        """Sweep expired claims through the host path (no product-side lock).

        The host takes its own write transaction per reclaim; the database
        write lock is what serialises it against a release critical section.
        """

        return int(self._kb.release_stale_claims(self.connection))

    def detect_crashed_workers(self) -> list[str]:
        return list(self._kbd.detect_crashed_workers(self.connection))

    # -- landing -----------------------------------------------------------

    def landing_witness(
        self,
        identity: RunIdentity,
        workspace: str | os.PathLike[str],
        current_workspace: str | os.PathLike[str],
        *,
        judge: Any = None,
    ) -> dict[str, Any]:
        """Re-read ownership on this connection and judge a landing request.

        ``judge`` is the pure path predicate from ``workspace.landing``; it is
        injected by the runtime so this module never imports another product
        subpackage.  No marker is written here.
        """

        row = self.connection.execute(
            "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?",
            (identity.task_id,),
        ).fetchone()
        row_dict = dict(row) if row is not None else None
        if judge is None:
            # Minimal inline fallback: path equality plus the run/claim fence.
            same = os.path.realpath(str(workspace)) == os.path.realpath(
                str(current_workspace)
            )
            matched = (
                row_dict is not None
                and row_dict["status"] == "running"
                and row_dict["current_run_id"] is not None
                and int(row_dict["current_run_id"]) == identity.run_id
                and row_dict["claim_lock"] == identity.claim_lock
            )
            if not same:
                ok, reason = False, "not-current-run-workspace"
            elif row_dict is None:
                ok, reason = False, "task-not-found"
            elif not matched:
                ok, reason = False, "stale-run-or-claim"
            else:
                ok, reason = True, None
            witness = {
                "ok": ok,
                "reason": reason,
                "ownership": {
                    "status": row_dict["status"] if row_dict else None,
                    "current_run_id": row_dict["current_run_id"] if row_dict else None,
                    "claim_lock": row_dict["claim_lock"] if row_dict else None,
                    "matched": matched,
                },
            }
        else:
            witness = judge(
                workspace=workspace,
                current_workspace=current_workspace,
                row=row_dict,
                identity=identity,
            )
        witness["workspace"] = os.path.abspath(os.fspath(workspace))
        witness["current_run_workspace"] = os.path.abspath(
            os.fspath(current_workspace)
        )
        return witness


__all__ = ["BoardAdapter", "BoardAdapterError", "RunIdentityError"]
