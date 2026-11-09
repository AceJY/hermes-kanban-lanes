"""W2-03/04: intent validation and atomic dedupe at the document/DB layer."""

from __future__ import annotations

import pytest

from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.hermes_adapter.intents import IntentError, validate_intent

IDENTITY = RunIdentity(
    schema_version=1,
    task_id="t_unit",
    run_id=7,
    claim_lock="host:1:hkl:unit",
)


def _intent(**overrides):
    document = {
        "schema_version": 1,
        "intent_id": "unit-comment",
        "kind": "comment",
        "task_id": IDENTITY.task_id,
        "run_id": IDENTITY.run_id,
        "claim_lock": IDENTITY.claim_lock,
        "payload": {"body": "hello"},
    }
    document.update(overrides)
    return document


@pytest.mark.parametrize(
    ("case", "fragment"),
    [
        ("unknown_field", "unknown field"),
        ("missing_body", "missing required field"),
        ("task_mismatch", "task_id"),
        ("run_mismatch", "run_id"),
        ("lock_mismatch", "claim_lock"),
        ("nul", "NUL"),
        ("empty_body", "empty"),
        ("unknown_payload", "unknown field"),
        ("bad_kind", "kind"),
        ("bool_run_id", "positive integer"),
        ("schema", "schema_version"),
    ],
)
def test_validate_intent_rejects_invalid_documents(case, fragment):
    document = _intent()
    if case == "unknown_field":
        document["extra"] = 1
    elif case == "missing_body":
        document["payload"] = {}
    elif case == "task_mismatch":
        document["task_id"] = "t_other"
    elif case == "run_mismatch":
        document["run_id"] = IDENTITY.run_id + 1
    elif case == "lock_mismatch":
        document["claim_lock"] = "other-lock"
    elif case == "nul":
        document["payload"] = {"body": "a\x00b"}
    elif case == "empty_body":
        document["payload"] = {"body": "   "}
    elif case == "unknown_payload":
        document["payload"] = {"body": "x", "extra": "y"}
    elif case == "bad_kind":
        document["kind"] = "archive"
    elif case == "bool_run_id":
        document["run_id"] = True
    elif case == "schema":
        document["schema_version"] = 2
    with pytest.raises(IntentError) as excinfo:
        validate_intent(document, identity=IDENTITY)
    assert fragment in str(excinfo.value)


def test_validate_intent_accepts_all_supported_kinds():
    cases = {
        "comment": {"body": "hello"},
        "create": {"title": "child", "body": None},
        "link": {"child_id": "t_child"},
    }
    for index, (kind, payload) in enumerate(cases.items()):
        document = _intent(kind=kind, payload=payload, intent_id=f"unit-{index}")
        assert validate_intent(document, identity=IDENTITY)["kind"] == kind


def test_duplicate_intent_applies_side_effect_once(board, make_task):
    task_id = make_task(title="unit-intent")
    identity = board.claim(task_id)
    assert identity is not None
    document = _intent(
        task_id=identity.task_id,
        run_id=identity.run_id,
        claim_lock=identity.claim_lock,
        intent_id="unit-dedupe",
        payload={"body": "once"},
    )

    def comment_count() -> int:
        return board.connection.execute(
            "SELECT count(*) FROM task_comments WHERE task_id = ?", (task_id,)
        ).fetchone()[0]

    assert comment_count() == 0
    first = board.apply_intent(identity, document)
    assert first["ok"] is True, first
    assert first["intent_id"] == "unit-dedupe"
    assert first["kind"] == "comment"
    assert comment_count() == 1

    replay = board.apply_intent(identity, document)
    assert replay["ok"] is False
    assert replay["reason"] == "duplicate_intent"
    assert comment_count() == 1
    receipts = board.connection.execute(
        "SELECT count(*) FROM hkl_intent_receipts WHERE intent_id = ?",
        ("unit-dedupe",),
    ).fetchone()[0]
    assert receipts == 1
