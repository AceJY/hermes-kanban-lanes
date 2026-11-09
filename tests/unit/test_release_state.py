"""W4-11 (unit): release journal is forward-only, atomic and recoverable."""

from __future__ import annotations

import json

import pytest

from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.release import state as state_module
from hermes_kanban_lanes.release.state import (
    MERGE_INTENT_ABORT,
    MERGE_INTENT_ADOPT,
    MERGE_INTENT_DECISIONS,
    MERGE_INTENT_DIVERGED,
    MERGE_INTENT_FILENAME,
    MERGE_INTENT_RETRY,
    MERGE_INTENT_SCHEMA_VERSION,
    RELEASE_STAGES,
    RELEASE_STATE_FILENAME,
    ReleaseState,
    classify_merge_intent,
    merge_intent_path,
    new_merge_intent,
    new_release_state,
    read_merge_intent,
    read_release_state,
    write_merge_intent,
    write_release_state,
)


def _identity(run_id=5, task_id="t_merge"):
    return RunIdentity(
        schema_version=1, task_id=task_id, run_id=run_id, claim_lock="host:1:hkl:x"
    )


def test_stage_order_is_frozen():
    assert RELEASE_STAGES == ("claimed", "gated", "merged", "postmerge", "terminal")


def test_new_state_starts_claimed(tmp_path):
    state = new_release_state(identity=_identity(), dev_task_id="t_dev")
    assert state.stage == "claimed"
    assert state.merge_task_id == "t_merge"
    assert state.dev_task_id == "t_dev"
    assert state.to_dict()["requirement_id"] == "HKL-W4"
    assert read_release_state(tmp_path) is None


def test_state_round_trips_atomically(tmp_path):
    state = new_release_state(identity=_identity(), dev_task_id="t_dev")
    state.advance("gated", main_before="a" * 40, reviewed_head="b" * 40)
    path = write_release_state(tmp_path, state)
    assert path.name == RELEASE_STATE_FILENAME
    assert not list(tmp_path.glob(".release-state.json.*.tmp"))
    loaded = read_release_state(tmp_path)
    assert loaded is not None
    assert loaded.to_dict() == state.to_dict()
    assert loaded.stage == "gated"
    assert loaded.main_before == "a" * 40
    assert loaded.reviewed_head == "b" * 40


def test_advance_rejects_backwards_and_unknown_stages(tmp_path):
    state = new_release_state(identity=_identity(), dev_task_id="t_dev")
    state.advance("merged", merge_sha="c" * 40, merge_tree="d" * 40)
    with pytest.raises(ValueError, match="must not regress"):
        state.advance("gated")
    with pytest.raises(ValueError, match="unknown release stage"):
        state.advance("published")
    with pytest.raises(AttributeError, match="unknown release state field"):
        state.advance("postmerge", published=True)
    assert state.stage == "merged"


def test_advance_to_same_stage_is_idempotent(tmp_path):
    state = new_release_state(identity=_identity(), dev_task_id="t_dev")
    state.advance("gated", main_before="a" * 40)
    state.advance("gated", reviewed_head="b" * 40)
    assert state.stage == "gated"
    assert state.reviewed_head == "b" * 40


def test_read_release_state_rejects_corruption(tmp_path):
    write_release_state(
        tmp_path, new_release_state(identity=_identity(), dev_task_id="t_dev")
    )
    path = tmp_path / RELEASE_STATE_FILENAME
    path.write_text("{", encoding="utf-8")
    assert read_release_state(tmp_path) is None

    document = json.loads(json.dumps({"stage": "merged"}))
    path.write_text(json.dumps(document), encoding="utf-8")
    assert read_release_state(tmp_path) is None

    good = new_release_state(identity=_identity(), dev_task_id="t_dev").to_dict()
    good["schema_version"] = 2
    path.write_text(json.dumps(good), encoding="utf-8")
    assert read_release_state(tmp_path) is None


def test_from_dict_requires_a_valid_identity(tmp_path):
    with pytest.raises(ValueError, match="identity"):
        ReleaseState.from_dict(
            {
                "schema_version": 1,
                "requirement_id": "HKL-W4",
                "identity": {"schema_version": 1},
                "merge_task_id": "t",
                "dev_task_id": "d",
                "stage": "claimed",
            }
        )


# ---------------------------------------------------------------------------
# D9-I rule 5: merge-intent journal and recovery classification
# ---------------------------------------------------------------------------


def _intent(*, reviewed_head="b" * 40, main_before="a" * 40):
    return new_merge_intent(
        identity=_identity(),
        main_ref="refs/heads/main",
        main_before=main_before,
        reviewed_head=reviewed_head,
    )


def test_merge_intent_round_trips_atomically(tmp_path):
    intent = _intent()
    path = write_merge_intent(tmp_path, intent)
    assert path == merge_intent_path(tmp_path)
    assert path.name == MERGE_INTENT_FILENAME
    assert path.name == "merge-intent.json"
    assert not list(tmp_path.glob(".merge-intent.json.*.tmp"))
    loaded = read_merge_intent(tmp_path)
    assert loaded is not None
    assert loaded.to_dict() == intent.to_dict()
    assert loaded.identity == intent.identity
    assert loaded.main_ref == "refs/heads/main"
    assert loaded.main_before == "a" * 40
    assert loaded.reviewed_head == "b" * 40
    assert loaded.schema_version == MERGE_INTENT_SCHEMA_VERSION
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["identity"]["claim_lock"] == intent.claim_lock


def test_merge_intent_rewrite_never_exposes_a_partial_document(
    tmp_path, monkeypatch
):
    first = _intent()
    write_merge_intent(tmp_path, first)
    original = merge_intent_path(tmp_path).read_bytes()

    def failing_replace(source, target):
        raise OSError("simulated: cannot replace")

    monkeypatch.setattr(state_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="cannot replace"):
        write_merge_intent(tmp_path, _intent(reviewed_head="c" * 40))
    monkeypatch.undo()

    # The durable file is still the previous complete document.
    assert merge_intent_path(tmp_path).read_bytes() == original
    loaded = read_merge_intent(tmp_path)
    assert loaded is not None and loaded.reviewed_head == "b" * 40
    # A failed replace may leave its own temporary behind; remove that artifact
    # so the next assertion measures the successful write only.
    for leftover in tmp_path.glob(".merge-intent.json.*.tmp"):
        leftover.unlink()
    # A successful rewrite leaves no temporary files behind.
    write_merge_intent(tmp_path, _intent(reviewed_head="c" * 40))
    assert not list(tmp_path.glob(".merge-intent.json.*.tmp"))
    assert read_merge_intent(tmp_path).reviewed_head == "c" * 40


@pytest.mark.parametrize(
    "document",
    [
        "{",
        "[]",
        "null",
        json.dumps({"schema_version": 1}),
        json.dumps({"schema_version": 2, "identity": _identity().to_dict()}),
        json.dumps(
            {
                **_intent().to_dict(),
                "identity": {"schema_version": 1},
            }
        ),
        json.dumps({**_intent().to_dict(), "reviewed_head": ""}),
        json.dumps({**_intent().to_dict(), "run_id": "5"}),
    ],
)
def test_read_merge_intent_rejects_invalid_documents(tmp_path, document):
    merge_intent_path(tmp_path).write_text(document, encoding="utf-8")
    assert read_merge_intent(tmp_path) is None


def test_read_merge_intent_missing_is_none(tmp_path):
    assert read_merge_intent(tmp_path) is None


def test_classify_merge_intent_covers_all_four_decisions():
    main_before = "a" * 40
    reviewed_head = "b" * 40
    assert MERGE_INTENT_DECISIONS == (
        MERGE_INTENT_ADOPT,
        MERGE_INTENT_RETRY,
        MERGE_INTENT_ABORT,
        MERGE_INTENT_DIVERGED,
    )

    def classify(**overrides):
        facts = {
            "repo_head": "c" * 40,
            "parents": [main_before, reviewed_head],
            "merge_head_present": False,
            "main_before": main_before,
            "reviewed_head": reviewed_head,
        }
        facts.update(overrides)
        return classify_merge_intent(**facts)

    assert classify() == MERGE_INTENT_ADOPT
    # Undecided head is not adopt, but the branch is untouched -> retry.
    assert (
        classify(repo_head=main_before, parents=["d" * 40]) == MERGE_INTENT_RETRY
    )
    # An in-progress merge always aborts, even with the old head.
    assert (
        classify(
            repo_head=main_before,
            parents=["d" * 40],
            merge_head_present=True,
        )
        == MERGE_INTENT_ABORT
    )
    # HEAD moved somewhere else entirely -> diverge (never merge again).
    assert (
        classify(repo_head="e" * 40, parents=["d" * 40]) == MERGE_INTENT_DIVERGED
    )
    # A single-parent commit with the reviewed head is not the merge commit.
    assert (
        classify(repo_head=reviewed_head, parents=[main_before])
        == MERGE_INTENT_DIVERGED
    )
    # Three parents do not match the two-parent merge shape.
    assert (
        classify(
            repo_head="f" * 40,
            parents=[main_before, reviewed_head, "g" * 40],
        )
        == MERGE_INTENT_DIVERGED
    )
    # A None head cannot be adopted.
    assert classify(repo_head=None, parents=[]) == MERGE_INTENT_DIVERGED
