"""W3-10: the run stage journal (atomic writes, stages, read fallback)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.runtime.state import (
    STAGES,
    STATE_FILENAME,
    STATE_REQUIREMENT_ID,
    RunState,
    new_state,
    read_state,
    state_path,
    write_state,
)

IDENTITY = RunIdentity(
    schema_version=1,
    task_id="t_state",
    run_id=2,
    claim_lock="lock-state",
)


def _state(tmp_path: Path) -> RunState:
    return new_state(
        identity=IDENTITY,
        board="test",
        run_dir=tmp_path / "run",
        workspace=tmp_path / "workspace",
        supervisor_pid=4242,
        supervisor_fingerprint="fingerprint-a",
    )


def test_stage_order_is_frozen():
    assert STAGES == (
        "claimed",
        "workspace",
        "executor",
        "executor_exited",
        "committed",
        "synced",
        "gated",
        "evidence",
        "terminal",
    )


def test_new_state_defaults(tmp_path):
    state = _state(tmp_path)
    assert state.stage == "claimed"
    assert state.schema_version == 1
    assert state.requirement_id == STATE_REQUIREMENT_ID == "HKL-W3"
    assert state.identity is IDENTITY
    assert state.task_id == IDENTITY.task_id
    assert state.run_id == IDENTITY.run_id
    assert state.claim_lock == IDENTITY.claim_lock
    assert state.board == "test"
    assert state.supervisor_pid == 4242
    assert state.child is None
    assert state.base_commit is None


def test_advance_moves_forward_and_records_fields(tmp_path):
    state = _state(tmp_path)
    state.updated_at = 0

    state.advance("workspace", base_commit="a" * 40)

    assert state.stage == "workspace"
    assert state.base_commit == "a" * 40
    assert state.updated_at > 0
    # Re-writing the same stage is allowed (idempotent journal update).
    state.advance("workspace", base_commit="b" * 40)
    assert state.base_commit == "b" * 40


def test_advance_never_regresses(tmp_path):
    state = _state(tmp_path)
    state.advance("terminal")
    with pytest.raises(ValueError, match="must not regress"):
        state.advance("executor")


def test_advance_rejects_unknown_stage_and_field(tmp_path):
    state = _state(tmp_path)
    with pytest.raises(ValueError, match="unknown run stage"):
        state.advance("paused")
    with pytest.raises(AttributeError, match="unknown run state field"):
        state.advance("workspace", nope=True)


def test_set_child_records_pid_pgid_and_fingerprint(tmp_path):
    state = _state(tmp_path)
    state.set_child(pid=123, pgid=123, fingerprint="fp")

    assert state.child == {"pid": 123, "pgid": 123, "fingerprint": "fp"}
    document = state.to_dict()
    assert document["child"] == {"pid": 123, "pgid": 123, "fingerprint": "fp"}

    # A later observation replaces, never silently drops, the identity.
    state.set_child(pid=124, pgid=124, fingerprint="fp2")
    assert state.child == {"pid": 124, "pgid": 124, "fingerprint": "fp2"}


def test_write_state_is_atomic_and_round_trips(tmp_path):
    run_dir = tmp_path / "run"
    state = _state(tmp_path)
    state.advance("workspace", base_commit="a" * 40)
    state.advance("executor").set_child(pid=7, pgid=7, fingerprint="fp")
    state.advance("committed", run_commit="b" * 40)

    path = write_state(run_dir, state)

    assert path == state_path(run_dir) == run_dir / STATE_FILENAME
    assert sorted(entry.name for entry in run_dir.iterdir()) == [STATE_FILENAME]
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["requirement_id"] == "HKL-W3"
    assert document["schema_version"] == 1
    assert document["stage"] == "committed"
    assert document["run_commit"] == "b" * 40
    assert document["child"] == {"pid": 7, "pgid": 7, "fingerprint": "fp"}
    assert document["supervisor_pid"] == 4242

    restored = read_state(run_dir)
    assert restored is not None
    assert restored.to_dict() == document
    assert restored.identity.to_dict() == IDENTITY.to_dict()


def test_write_state_replaces_the_previous_stage(tmp_path):
    run_dir = tmp_path / "run"
    state = _state(tmp_path)
    write_state(run_dir, state)
    state.advance("terminal")
    write_state(run_dir, state)

    document = json.loads((run_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    assert document["stage"] == "terminal"
    assert sorted(entry.name for entry in run_dir.iterdir()) == [STATE_FILENAME]


@pytest.mark.parametrize(
    "content",
    [None, "", "{", "[]", json.dumps({"schema_version": 2}), json.dumps({
        "schema_version": 1,
        "requirement_id": "HKL-W3",
        "identity": IDENTITY.to_dict(),
        "stage": "nope",
    })],
)
def test_read_state_returns_none_for_missing_or_invalid(tmp_path, content):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    if content is not None:
        (run_dir / STATE_FILENAME).write_text(content, encoding="utf-8")
    assert read_state(run_dir) is None


def test_read_state_accepts_a_minimal_document(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / STATE_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "identity": IDENTITY.to_dict(),
                "stage": "evidence",
            }
        ),
        encoding="utf-8",
    )

    state = read_state(run_dir)

    assert state is not None
    assert state.stage == "evidence"
    assert state.task_id == IDENTITY.task_id
    assert state.run_id == IDENTITY.run_id
    assert state.claim_lock == IDENTITY.claim_lock
    assert state.requirement_id == "HKL-W3"
