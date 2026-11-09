"""Structured executor intents, applied atomically by the supervisor.

An executor never owns a board connection.  It writes one JSON document per
intent into ``<run_dir>/intents/``.  The supervisor validates the document,
compares it with its own :class:`RunIdentity`, and applies the side effect and
the idempotency receipt inside a single ``BEGIN IMMEDIATE`` transaction:

    refresh identity -> duplicate check -> side effect -> receipt

A crash after COMMIT but before local bookkeeping therefore replays as
``duplicate_intent`` with no additional side effect.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Mapping

from ..contracts import RunIdentity

RECEIPT_TABLE = "hkl_intent_receipts"
INTENT_KINDS = frozenset({"comment", "create", "link"})
INTENT_KEYS = (
    "schema_version",
    "intent_id",
    "kind",
    "task_id",
    "run_id",
    "claim_lock",
    "payload",
)
_PAYLOAD_KEYS = {
    "comment": ("body",),
    "create": ("title", "body"),
    "link": ("child_id",),
}
_MAX_TEXT = 1_000_000


class IntentError(ValueError):
    """Raised when an intent document is structurally invalid."""


class _Rejected(Exception):
    """Internal: roll the write transaction back and surface ``reason``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _reject_unknown(mapping: Mapping[str, Any], allowed: tuple[str, ...], path: str) -> None:
    unknown = sorted(key for key in mapping if key not in allowed)
    if unknown:
        raise IntentError(f"{path}: unknown field(s): {', '.join(unknown[:8])}")


def _text(value: Any, path: str, *, allow_empty: bool = False, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise IntentError(f"{path}: expected a string")
    if "\x00" in value:
        raise IntentError(f"{path}: must not contain NUL")
    if not allow_empty and not value.strip():
        raise IntentError(f"{path}: must not be empty")
    if len(value) > _MAX_TEXT:
        raise IntentError(f"{path}: value is too large")
    return value


def _schema_version(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value != 1:
        raise IntentError(f"{path}: unsupported schema_version; expected 1")
    return value


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise IntentError(f"{path}: expected a positive integer")
    return value


def validate_intent(data: Any, *, identity: RunIdentity) -> dict[str, Any]:
    """Validate one intent document against the supervisor's own identity.

    The document's embedded identity is *never* trusted: it must equal
    ``identity`` exactly, otherwise the whole document is rejected.
    """

    if not isinstance(data, dict):
        raise IntentError("intent: expected a JSON object")
    _reject_unknown(data, INTENT_KEYS, "intent")
    missing = [key for key in INTENT_KEYS if key not in data]
    if missing:
        raise IntentError(f"intent: missing required field(s): {', '.join(missing)}")

    _schema_version(data["schema_version"], "intent.schema_version")
    intent_id = _text(data["intent_id"], "intent.intent_id")
    kind = data["kind"]
    if kind not in INTENT_KINDS:
        raise IntentError("intent.kind: expected one of comment, create, link")
    task_id = _text(data["task_id"], "intent.task_id")
    run_id = _positive_int(data["run_id"], "intent.run_id")
    claim_lock = _text(data["claim_lock"], "intent.claim_lock")

    if task_id != identity.task_id:
        raise IntentError("intent.task_id: does not match the supervisor run")
    if run_id != identity.run_id:
        raise IntentError("intent.run_id: does not match the supervisor run")
    if claim_lock != identity.claim_lock:
        raise IntentError("intent.claim_lock: does not match the supervisor run")

    payload = data["payload"]
    if not isinstance(payload, dict):
        raise IntentError("intent.payload: expected a JSON object")
    allowed = _PAYLOAD_KEYS[kind]
    _reject_unknown(payload, allowed, "intent.payload")
    missing_payload = [key for key in allowed if key not in payload]
    if missing_payload:
        raise IntentError(
            "intent.payload: missing required field(s): " + ", ".join(missing_payload)
        )

    normalised_payload: dict[str, Any] = {}
    if kind == "comment":
        normalised_payload["body"] = _text(payload["body"], "intent.payload.body")
    elif kind == "create":
        normalised_payload["title"] = _text(payload["title"], "intent.payload.title")
        normalised_payload["body"] = _text(
            payload["body"], "intent.payload.body", allow_empty=True, optional=True
        )
    else:  # link
        normalised_payload["child_id"] = _text(
            payload["child_id"], "intent.payload.child_id"
        )

    return {
        "schema_version": 1,
        "intent_id": intent_id,
        "kind": kind,
        "task_id": task_id,
        "run_id": run_id,
        "claim_lock": claim_lock,
        "payload": normalised_payload,
    }


def ensure_receipts(conn: sqlite3.Connection) -> None:
    """Create the board-resident idempotency table (idempotent DDL)."""

    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} ("
        "intent_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
        "task_id TEXT NOT NULL, applied_at INTEGER NOT NULL)"
    )


def _ownership_matches(
    row: sqlite3.Row | None, identity: RunIdentity
) -> bool:
    if row is None:
        return False
    return (
        row["status"] == "running"
        and row["current_run_id"] is not None
        and int(row["current_run_id"]) == identity.run_id
        and row["claim_lock"] == identity.claim_lock
    )


def _receipt_seen(conn: sqlite3.Connection, intent_id: str) -> bool:
    return (
        conn.execute(
            f"SELECT 1 FROM {RECEIPT_TABLE} WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        is not None
    )


def _record_receipt(conn: sqlite3.Connection, intent: Mapping[str, Any]) -> None:
    conn.execute(
        f"INSERT INTO {RECEIPT_TABLE} (intent_id, kind, task_id, applied_at) "
        "VALUES (?, ?, ?, ?)",
        (
            intent["intent_id"],
            intent["kind"],
            intent["task_id"],
            int(time.time()),
        ),
    )


def _apply_comment(conn, kb, intent: Mapping[str, Any]) -> dict[str, Any]:
    body = intent["payload"]["body"]
    # ``add_comment`` opts into a savepoint under our outer transaction.
    comment_id = kb.add_comment(conn, intent["task_id"], "hkl-executor", body)
    return {"side_effect": "comment", "comment_id": int(comment_id)}


def _apply_create(conn, kb, intent: Mapping[str, Any]) -> dict[str, Any]:
    payload = intent["payload"]
    child_id = kb.create_task(
        conn,
        title=payload["title"],
        body=payload["body"],
        created_by=f"hkl-intent:{intent['intent_id']}",
    )
    return {"side_effect": "create", "child_task_id": child_id}


def _apply_link(conn, kb, intent: Mapping[str, Any]) -> dict[str, Any]:
    task_id = intent["task_id"]
    child_id = intent["payload"]["child_id"]
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (child_id,)).fetchone():
        raise _Rejected("unknown_child")
    child = conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id = ?", (child_id,)
    ).fetchone()
    # The supervisor links as part of its own run; a child that is already
    # running cannot be gated retroactively (same rule as host ``link_tasks``).
    if child["status"] == "running":
        raise _Rejected("running_child")
    if kb._would_cycle(conn, task_id, child_id):  # noqa: SLF001 - adapter boundary
        raise _Rejected("cycle")
    conn.execute(
        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (task_id, child_id),
    )
    parent_status = kb._task_status(conn, task_id)  # noqa: SLF001
    if parent_status not in ("done", "archived"):
        conn.execute(
            "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
            (child_id,),
        )
    kb._append_event(  # noqa: SLF001
        conn,
        child_id,
        "linked",
        {"parent": task_id, "child": child_id, "intent_id": intent["intent_id"]},
    )
    return {"side_effect": "link", "child_task_id": child_id}


def apply_intent(
    conn: sqlite3.Connection,
    kb: Any,
    txn_factory: Callable[..., Any],
    intent: Any,
    *,
    identity: RunIdentity,
    validate: bool = True,
) -> dict[str, Any]:
    """Apply one intent atomically; never trust the document's identity.

    Returns ``{ok, reason, intent_id, kind, ...}``.  ``reason`` is one of
    ``stale_run``, ``duplicate_intent``, ``invalid_intent``, ``unknown_child``,
    ``running_child`` or ``cycle``.
    """

    if validate:
        try:
            document = validate_intent(intent, identity=identity)
        except IntentError as exc:
            return {
                "ok": False,
                "reason": "invalid_intent",
                "detail": str(exc),
                "intent_id": intent.get("intent_id") if isinstance(intent, dict) else None,
                "kind": intent.get("kind") if isinstance(intent, dict) else None,
            }
    else:  # already validated by the caller
        document = dict(intent)

    intent_id = document["intent_id"]
    kind = document["kind"]
    try:
        with txn_factory(conn):
            ensure_receipts(conn)
            row = conn.execute(
                "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?",
                (document["task_id"],),
            ).fetchone()
            if not _ownership_matches(row, identity):
                raise _Rejected("stale_run")
            if _receipt_seen(conn, intent_id):
                raise _Rejected("duplicate_intent")
            if kind == "comment":
                details = _apply_comment(conn, kb, document)
            elif kind == "create":
                details = _apply_create(conn, kb, document)
            else:
                details = _apply_link(conn, kb, document)
            _record_receipt(conn, document)
    except _Rejected as exc:
        return {
            "ok": False,
            "reason": exc.reason,
            "intent_id": intent_id,
            "kind": kind,
        }
    except (sqlite3.Error, ValueError) as exc:
        return {
            "ok": False,
            "reason": "invalid_intent",
            "detail": f"{type(exc).__name__}: {exc}",
            "intent_id": intent_id,
            "kind": kind,
        }
    return {"ok": True, "reason": None, "intent_id": intent_id, "kind": kind, **details}


__all__ = [
    "INTENT_KINDS",
    "IntentError",
    "RECEIPT_TABLE",
    "apply_intent",
    "ensure_receipts",
    "validate_intent",
]
