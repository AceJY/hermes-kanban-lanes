"""W4-01/W4-08: development run -> independent review -> merge card -> release.

The full temporary chain: a real supervised development run writes W4 evidence,
the reviewer run records a pass, the product queues exactly one merge card, and
the release run merges locally with ``--no-ff`` and runs the post-merge suite on
the merge result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_kanban_lanes.card_contract import CardSpec, load_card
from hermes_kanban_lanes.runtime.release import (
    _dev_cards,
    observe_reviews,
    queue_release_cards,
)
from hermes_kanban_lanes.workspace.git import repo_head, run_git

from w4_helpers import (
    PYTHON,
    REQUIREMENT,
    REVIEWER,
    claim_and_release,
    dev_run_dir,
    queue_merge_card,
    release_run_dir,
    review_body,
    review_pass,
    run_dev,
    run_rows,
    task_row,
    w4_config,
    write_authorization,
)


def _postmerge_python() -> list[str]:
    return [
        PYTHON,
        "-c",
        "from pathlib import Path; assert Path('RESULT.txt').read_text() == 'ok\\n'",
    ]


def _setup(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    *,
    merge_enabled=True,
    review_metadata_overrides=None,
):
    repo = make_repo()
    base = repo_head(repo)
    executor = fake_executor(review_body())
    config = w4_config(
        make_config,
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[executor],
        merge_enabled=merge_enabled,
        post_merge_commands=[_postmerge_python()],
    )
    task_id = make_task(title="w4 chain")
    card = load_card(
        write_card(config.work_root, task_id, allow=("RESULT.txt",)), config
    )
    identity, outcome = run_dev(
        board, config, card, task_id, executor_argv=executor
    )
    assert outcome.category == "success", outcome.reason
    assert outcome.terminal == "request_review", outcome.terminal
    assert task_row(board, task_id)["status"] == "review"
    assert task_row(board, task_id)["assignee"] == REVIEWER
    run_dir = dev_run_dir(config.work_root, task_id, identity.run_id)

    review_identity, metadata = review_pass(
        board, task_id, run_dir, dev_run_id=identity.run_id, rework_round=1
    )
    if review_metadata_overrides:
        metadata.update(review_metadata_overrides)
        # Replace the recorded declaration with the tampered one.
        board.connection.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (json.dumps(metadata), review_identity.run_id),
        )
        board.connection.commit()
    return {
        "repo": repo,
        "base": base,
        "config": config,
        "task_id": task_id,
        "card": card,
        "identity": identity,
        "outcome": outcome,
        "run_dir": run_dir,
        "review_identity": review_identity,
        "review_metadata": metadata,
    }


def test_w4_01_full_review_release_chain(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _setup(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    task_id = facts["task_id"]

    # An explicit card keeps the pointer and the declaration in run.json.
    run_document = json.loads(
        (facts["run_dir"] / "run.json").read_text(encoding="utf-8")
    )
    assert run_document["card_path"] == str(
        Path(config.work_root) / "cards" / f"{task_id}.json"
    )
    assert run_document["card"] == facts["card"].to_dict()

    write_authorization(
        Path(config.work_root),
        task_id,
        facts["run_dir"],
        dev_run_id=facts["identity"].run_id,
        review_run_id=facts["review_identity"].run_id,
        rework_round=1,
    )

    # observe_reviews records the pass without writing the board.
    observed = observe_reviews(
        Path(config.work_root), board, config, requirement_id=REQUIREMENT
    )
    assert any(entry["task_id"] == task_id for entry in observed)

    merge_task_id, merge_card = queue_merge_card(board, config, task_id)
    row = task_row(board, merge_task_id)
    assert row["status"] == "ready"
    assert row["assignee"] == "hkl:merge"
    assert board.parent_ids(merge_task_id) == [task_id]
    assert row["idempotency_key"].startswith(f"hkl-release:{task_id}:")
    card_document = json.loads(
        (Path(config.work_root) / "cards" / f"{merge_task_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert card_document["card_kind"] == "merge"
    assert card_document["closure_for"] == task_id
    assert card_document["allow"] == ["RESULT.txt"]
    assert card_document["repository"] == "primary"
    assert card_document["commands"] == []
    # Repeated scans return the same card and create no duplicate.
    assert queue_release_cards(
        config.work_root, board, config, requirement_id=REQUIREMENT
    ) == []
    assert board.child_ids(task_id) == [merge_task_id]

    merge_identity, outcome = claim_and_release(
        board, config, merge_task_id, merge_card
    )
    assert outcome["action"] == "merged", outcome
    assert outcome["terminal_ok"] is True
    merge_sha = outcome["merge_sha"]
    assert merge_sha

    release_dir = release_run_dir(config.work_root, merge_task_id)
    report = json.loads(
        (release_dir / "release-report.json").read_text(encoding="utf-8")
    )
    assert report["verdict"] == "release-ready"
    assert [check["name"] for check in report["checks"]] == [
        "workspace",
        "review",
        "evidence",
        "rounds",
        "authorization",
        "merge_enabled",
        "main_unchanged",
        "merge_tree",
    ]
    merge_document = json.loads(
        (release_dir / "merge.json").read_text(encoding="utf-8")
    )
    assert merge_document["verdict"] == "merged"
    assert merge_document["parents"] == [facts["base"], merge_document["reviewed_head"]]
    postmerge = json.loads(
        (release_dir / "postmerge" / "report.json").read_text(encoding="utf-8")
    )
    assert postmerge["verdict"] == "pass"

    # The local main moved to the merge commit, and only there.
    assert repo_head(facts["repo"]) == merge_sha
    parents = run_git(
        ["rev-list", "--parents", "-n", "1", merge_sha], cwd=facts["repo"]
    ).stdout.split()
    assert parents[0] == merge_sha
    assert set(parents[1:]) == {facts["base"], merge_document["reviewed_head"]}

    state = json.loads(
        (release_dir / "release-state.json").read_text(encoding="utf-8")
    )
    assert state["stage"] == "terminal"
    assert state["merge_sha"] == merge_sha

    # The merge card is complete with the full handoff metadata.
    assert task_row(board, merge_task_id)["status"] == "done"
    merge_runs = run_rows(board, merge_task_id)
    assert [run["outcome"] for run in merge_runs] == ["completed"]
    handoff = merge_runs[0]["metadata"]["hkl"]
    assert handoff["merge_sha"] == merge_sha
    assert handoff["dev_task_id"] == task_id
    assert handoff["reviewed_head"] == merge_document["reviewed_head"]
    assert handoff["evidence_index_sha256"] == facts["review_metadata"][
        "evidence_index_sha256"
    ]
    assert handoff["release_report_sha256"]
    assert handoff["postmerge_report_sha256"]

    # Exactly one merge run directory and one merge card-child.
    assert len(list((Path(config.work_root) / "runs" / merge_task_id).iterdir())) == 1
    assert len(board.child_ids(task_id)) == 1


def test_tampered_review_declaration_never_queues_a_merge_card(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _setup(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        review_metadata_overrides={"reviewed_head": "0" * 40},
    )
    config = facts["config"]
    write_authorization(
        Path(config.work_root),
        facts["task_id"],
        facts["run_dir"],
        dev_run_id=facts["identity"].run_id,
        review_run_id=facts["review_identity"].run_id,
        rework_round=1,
    )
    assert queue_release_cards(
        config.work_root, board, config, requirement_id=REQUIREMENT
    ) == []
    assert board.child_ids(facts["task_id"]) == []
    assert repo_head(facts["repo"]) == facts["base"]


# ---------------------------------------------------------------------------
# W8 D2b: a card that only exists as a body lane block still reaches release
# ---------------------------------------------------------------------------

_LANE_BODY = """\
Synthetic card: only the body declares the lane.

```lane
project: hermes-kanban-lanes
allow:
- RESULT.txt
test_cmd: python -c pass
artifacts: []
review: true
```
"""


def _synthetic_card(config, task_id: str, *, allow=("RESULT.txt",)) -> CardSpec:
    """The declaration D2 synthesis produces from the body lane block."""

    return CardSpec.from_dict(
        {
            "schema_version": 1,
            "card_kind": "implementation",
            "lineage": task_id,
            "closure_for": None,
            "executor": "claude",
            "repository": "primary",
            "allow": list(allow),
            "commands": [{"argv": [PYTHON, "-c", "pass"], "cwd": "."}],
            "risk": "normal",
            "artifacts": [],
        },
        config,
    )


def _synthetic_chain(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
):
    repo = make_repo()
    executor = fake_executor(review_body())
    config = w4_config(
        make_config,
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[executor],
        merge_enabled=True,
    )
    task_id = make_task(title="w8 d2b synthetic", body=_LANE_BODY)
    card = _synthetic_card(config, task_id)
    identity, outcome = run_dev(
        board, config, card, task_id, executor_argv=executor
    )
    assert outcome.category == "success", outcome.reason
    assert outcome.terminal == "request_review", outcome.terminal
    run_dir = dev_run_dir(config.work_root, task_id, identity.run_id)
    review_identity, metadata = review_pass(
        board, task_id, run_dir, dev_run_id=identity.run_id, rework_round=1
    )
    return {
        "config": config,
        "task_id": task_id,
        "card": card,
        "identity": identity,
        "run_dir": run_dir,
        "review_identity": review_identity,
        "review_metadata": metadata,
    }


def test_w8_d2b_synthetic_card_reaches_merge_queue(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _synthetic_chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    task_id = facts["task_id"]

    # D2b/§6.1: the synthesised card is never persisted under cards/.
    assert not (Path(config.work_root) / "cards" / f"{task_id}.json").exists()
    run_document = json.loads(
        (facts["run_dir"] / "run.json").read_text(encoding="utf-8")
    )
    assert run_document["card_path"] is None
    assert run_document["card"] == facts["card"].to_dict()

    # The run snapshot alone must make the task visible to release.
    dev_cards = _dev_cards(Path(config.work_root), config)
    matches = [entry for entry in dev_cards if entry[0] == task_id]
    assert len(matches) == 1
    assert matches[0][1] == facts["card"].to_dict()
    assert matches[0][2] == facts["card"]

    observed = observe_reviews(
        Path(config.work_root), board, config, requirement_id=REQUIREMENT
    )
    task_observations = [
        entry for entry in observed if entry["task_id"] == task_id
    ]
    assert len(task_observations) == 1
    assert task_observations[0]["review_run_id"] == facts["review_identity"].run_id

    queued = queue_release_cards(
        Path(config.work_root), board, config, requirement_id=REQUIREMENT
    )
    matches = [entry for entry in queued if entry.get("dev_task_id") == task_id]
    assert len(matches) == 1, queued
    entry = matches[0]
    merge_task_id = entry["merge_task_id"]
    row = task_row(board, merge_task_id)
    assert board.parent_ids(merge_task_id) == [task_id]
    assert row["assignee"] == "hkl:merge"
    assert row["idempotency_key"] == entry["idempotency_key"]
    assert entry["idempotency_key"] == (
        f"hkl-release:{task_id}:"
        f"{facts['review_metadata']['reviewed_head']}:"
        f"{facts['review_metadata']['evidence_index_sha256']}"
    )
    merge_document = json.loads(
        (
            Path(config.work_root) / "cards" / f"{merge_task_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert merge_document["card_kind"] == "merge"
    assert merge_document["closure_for"] == task_id


def test_w8_d2b_explicit_card_wins_over_run_snapshot(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _synthetic_chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    task_id = facts["task_id"]

    # Add an explicit declaration after the synthetic run.  The run snapshot
    # still says RESULT.txt; release must use the explicit card.
    write_card(config.work_root, task_id, allow=("RESULT.txt", "EXPLICIT.txt"))

    queued = queue_release_cards(
        Path(config.work_root), board, config, requirement_id=REQUIREMENT
    )
    matches = [entry for entry in queued if entry.get("dev_task_id") == task_id]
    assert len(matches) == 1, queued
    merge_task_id = matches[0]["merge_task_id"]
    merge_document = json.loads(
        (Path(config.work_root) / "cards" / f"{merge_task_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert merge_document["allow"] == ["RESULT.txt", "EXPLICIT.txt"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing", "card-snapshot-missing"),
        ("invalid", "card-snapshot-invalid"),
    ],
)
def test_w8_d2b_missing_or_invalid_snapshot_is_reported(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    mutation,
    reason,
):
    facts = _synthetic_chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    task_id = facts["task_id"]

    run_path = facts["run_dir"] / "run.json"
    document = json.loads(run_path.read_text(encoding="utf-8"))
    if mutation == "missing":
        document.pop("card")
    else:
        document["card"] = {"schema_version": 1, "card_kind": "implementation"}
    run_path.write_text(json.dumps(document), encoding="utf-8")

    queued = queue_release_cards(
        Path(config.work_root), board, config, requirement_id=REQUIREMENT
    )
    matches = [entry for entry in queued if entry.get("dev_task_id") == task_id]
    assert len(matches) == 1, queued
    assert matches[0]["skipped"] == reason
    assert reason in matches[0]["reason"]
    assert board.child_ids(task_id) == []
