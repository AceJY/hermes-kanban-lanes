"""W4-01/W4-08: development run -> independent review -> merge card -> release.

The full temporary chain: a real supervised development run writes W4 evidence,
the reviewer run records a pass, the product queues exactly one merge card, and
the release run merges locally with ``--no-ff`` and runs the post-merge suite on
the merge result.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_kanban_lanes.card_contract import CardSpec, load_card
from hermes_kanban_lanes.release.binding import evaluate_binding
from hermes_kanban_lanes.runtime.release import (
    _dev_cards,
    _normalise_review_runs,
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
    review_changes,
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


def _policy_config(config):
    """The same lane with ``review.merge.authorization = "policy"``."""

    return replace(
        config,
        review=replace(
            config.review,
            merge=replace(config.review.merge, authorization="policy"),
        ),
    )


def test_w4_policy_authorization_merges_a_normal_card(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """Policy mode writes its own auditable record, then the real merge runs."""

    facts = _setup(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = _policy_config(facts["config"])
    task_id = facts["task_id"]

    # No operator authorization file is written: ordinary risk is policy-approved.
    merge_task_id, merge_card = queue_merge_card(board, config, task_id)
    _merge_identity, outcome = claim_and_release(
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
    assert report["authorization"]["source"] == "policy"
    assert report["authorization"]["approver"] == "policy:auto"
    assert report["authorization"]["policy"]["generated"] is True

    generated = json.loads(
        (release_dir / "authorization.json").read_text(encoding="utf-8")
    )
    assert generated["source"] == "policy"
    assert generated["approver"] == "policy:auto"
    assert generated["reviewed_head"] == report["reviewed_head"]
    assert generated["reviewed_tree"] == report["reviewed_tree"]
    assert generated["evidence_index_sha256"] == report["evidence_index_sha256"]
    assert generated["main_commit"] == facts["base"]
    assert generated["expires_at"] > generated["approved_at"]

    assert repo_head(facts["repo"]) == merge_sha
    merge_document = json.loads(
        (release_dir / "merge.json").read_text(encoding="utf-8")
    )
    assert merge_document["parents"] == [
        facts["base"],
        merge_document["reviewed_head"],
    ]
    assert task_row(board, merge_task_id)["status"] == "done"


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
    queued = queue_release_cards(
        config.work_root, board, config, requirement_id=REQUIREMENT
    )
    matches = [
        entry for entry in queued if entry.get("dev_task_id") == facts["task_id"]
    ]
    assert len(matches) == 1, queued
    assert matches[0]["skipped"] == "review-metadata-incomplete"
    assert "review-head-not-run" in matches[0]["reason"]
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


# ---------------------------------------------------------------------------
# Scope vs. baseline sync: only the card's own commits may be judged
# ---------------------------------------------------------------------------

#: Round one writes the allowed file *and* advances ``main`` while the
#: executor is running.  The supervisor's baseline sync then merges that new
#: trunk commit into the card branch, so the next round's continuation base
#: contains trunk paths the card never authored.
_ADVANCE_MAIN_DURING_RUN = """
import subprocess
repo = {repo!r}
Path(repo, "pyproject.toml").write_text("version = '9.9.9'\\n", encoding="utf-8")
subprocess.run(
    ["git", "-C", repo, "add", "pyproject.toml"], check=True, capture_output=True
)
subprocess.run(
    [
        "git", "-C", repo,
        "-c", "user.name=HKL Tests",
        "-c", "user.email=hkl@example.invalid",
        "commit", "--no-verify", "-q", "-m", "advance main",
    ],
    check=True,
    capture_output=True,
)
Path("docs/x.md").parent.mkdir(parents=True, exist_ok=True)
Path("docs/x.md").write_text("round one\\n", encoding="utf-8")
print(json.dumps({{"is_error": False, "result": "done"}}))
"""


def _advance_main_during_run(repo: Path) -> str:
    return _ADVANCE_MAIN_DURING_RUN.format(repo=str(repo))


def _scope_with_a_synced_continuation_base(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    *,
    round_two_path: str,
):
    """Round 1 syncs a trunk advance; return the round-2 scope check.

    ``task_base`` (round 1) != ``continuation_base`` (round 2) because the
    baseline sync merged ``main`` into the card branch between rounds.
    """

    repo = make_repo()
    task_base = repo_head(repo)
    round_one = fake_executor(_advance_main_during_run(repo))
    round_two = fake_executor(review_body(write=round_two_path, content="round two\n"))
    config = w4_config(
        make_config,
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[round_one],
        max_rework_rounds=3,
    )
    task_id = make_task(title="scope after sync")
    card = load_card(
        write_card(config.work_root, task_id, allow=("docs/x.md",)), config
    )

    identity_one, outcome_one = run_dev(
        board, config, card, task_id, executor_argv=round_one
    )
    assert outcome_one.category == "success", outcome_one.reason
    run_one = dev_run_dir(config.work_root, task_id, identity_one.run_id)
    document_one = json.loads((run_one / "run.json").read_text(encoding="utf-8"))
    assert document_one["task_base_commit"] == task_base
    assert document_one["continuation_base"] == task_base
    assert document_one["baseline"]["updated"] is True
    continuation_base = document_one["code_commit"]
    assert continuation_base == document_one["baseline"]["merge_commit"]

    # The continuation base really contains the trunk's pyproject change.
    synced = run_git(
        ["diff", "--name-only", task_base, continuation_base], cwd=repo
    ).stdout.split()
    assert "pyproject.toml" in synced

    assert review_changes(board, task_id, "please continue")[0] is True
    identity_two, outcome_two = run_dev(
        board, config, card, task_id, executor_argv=round_two
    )
    run_two = dev_run_dir(config.work_root, task_id, identity_two.run_id)
    document_two = json.loads((run_two / "run.json").read_text(encoding="utf-8"))
    assert document_two["round"] == 2
    assert document_two["task_base_commit"] == task_base
    assert document_two["continuation_base"] == continuation_base

    # A two-dot diff from task_base still shows the trunk's pyproject path:
    # that is exactly what the old scope check judged.
    cumulative = run_git(
        ["diff", "--name-only", task_base, document_two["run_commit"]], cwd=repo
    ).stdout.split()
    assert "pyproject.toml" in cumulative

    gate = json.loads(
        (run_two / document_two["gate"]["report_path"]).read_text(encoding="utf-8")
    )
    scope = next(check for check in gate["checks"] if check["name"] == "scope")
    return outcome_two, scope


def test_scope_ignores_trunk_paths_merged_by_baseline_sync(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """Only the card's own path is judged, even when task_base != continuation."""

    outcome, scope = _scope_with_a_synced_continuation_base(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        round_two_path="docs/x.md",
    )

    assert outcome.category == "success", outcome.reason
    assert scope["ok"] is True, scope
    assert scope["changed_paths"] == ["docs/x.md"]


def test_scope_still_rejects_a_card_authored_path_outside_allow(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """Excluding trunk paths must not turn the gate green for a real violation."""

    outcome, scope = _scope_with_a_synced_continuation_base(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        round_two_path="CHANGELOG.md",
    )

    assert outcome.category == "code_failure", outcome.reason
    assert scope["ok"] is False
    assert "path outside allow: CHANGELOG.md" in scope["detail"]
    assert "CHANGELOG.md" in scope["changed_paths"]
    # The trunk's own pyproject change is still not attributed to the card.
    assert "pyproject.toml" not in scope["changed_paths"]


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


# ---------------------------------------------------------------------------
# W11e: the reviewer's run profile is the mechanical reviewer fact
# ---------------------------------------------------------------------------

_SRC_DIR = Path(__file__).resolve().parents[2] / "src" / "hermes_kanban_lanes"


def _rewrite_review_metadata(board, run_id: int, metadata: dict) -> None:
    board.connection.execute(
        "UPDATE task_runs SET metadata = ? WHERE id = ?",
        (json.dumps(metadata), run_id),
    )
    board.connection.commit()


def _real_native_review_metadata(facts) -> dict:
    """The metadata Hermes native reviewer runs actually record.

    No ``reviewer`` and no ``dev_run_id``: the run identity carries the former
    and the development run index carries the latter.
    """

    run_document = json.loads(
        (facts["run_dir"] / "run.json").read_text(encoding="utf-8")
    )
    return {
        "verdict": "pass",
        "review_outcome": "approved",
        "reviewed_commit": run_document["code_commit"],
        "reviewed_tree": facts["review_metadata"]["reviewed_tree"],
        "review_round": 1,
    }


def _evaluate(facts, board):
    config = facts["config"]
    task_id = facts["task_id"]
    evaluation_runs = _normalise_review_runs(
        config.work_root,
        task_id,
        board.run_rows(task_id),
        config=config,
        requirement_id=REQUIREMENT,
    )
    return evaluate_binding(
        task_row=board.task_row(task_id),
        runs=evaluation_runs,
        dev_run_dir=facts["run_dir"],
        repo=config.repositories["primary"].path,
        requirement_id=REQUIREMENT,
        package_dir=_SRC_DIR,
        executor_profile=facts["card"].executor,
    )


def _set_review_profile(board, run_id: int, profile: str | None) -> None:
    board.connection.execute(
        "UPDATE task_runs SET profile = ? WHERE id = ?", (profile, run_id)
    )
    board.connection.commit()


def _queue_for(facts, board):
    config = facts["config"]
    queued = queue_release_cards(
        config.work_root, board, config, requirement_id=REQUIREMENT
    )
    return [
        entry
        for entry in queued
        if entry.get("dev_task_id") == facts["task_id"]
    ]


def test_native_review_without_reviewer_key_uses_the_run_profile(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """The run row's profile fills ``reviewer`` -- never a name from prose."""

    facts = _setup(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    review_run_id = facts["review_identity"].run_id
    metadata = _real_native_review_metadata(facts)
    assert "reviewer" not in metadata
    assert "dev_run_id" not in metadata
    _rewrite_review_metadata(board, review_run_id, metadata)

    review_run = next(
        run
        for run in board.run_rows(facts["task_id"])
        if run["id"] == review_run_id
    )
    assert review_run["profile"] == REVIEWER

    evaluation = _evaluate(facts, board)
    assert evaluation.problems == ()
    assert evaluation.binding is not None
    assert evaluation.binding.reviewer == REVIEWER

    matches = _queue_for(facts, board)
    assert len(matches) == 1, matches
    entry = matches[0]
    assert "skipped" not in entry
    merge_task_id = entry["merge_task_id"]
    assert task_row(board, merge_task_id)["assignee"] == "hkl:merge"
    assert board.parent_ids(merge_task_id) == [facts["task_id"]]


def test_missing_reviewer_and_profile_is_reported_not_silent(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """No metadata reviewer and no run profile: a named, visible skip."""

    facts = _setup(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    review_run_id = facts["review_identity"].run_id
    _rewrite_review_metadata(
        board, review_run_id, _real_native_review_metadata(facts)
    )
    _set_review_profile(board, review_run_id, None)

    evaluation = _evaluate(facts, board)
    assert "review-reviewer-invalid" in evaluation.problems
    assert evaluation.binding is None

    matches = _queue_for(facts, board)
    assert len(matches) == 1, matches
    assert matches[0]["skipped"] == "review-metadata-incomplete"
    assert "reviewer" in matches[0]["reason"]
    assert board.child_ids(facts["task_id"]) == []


def test_reviewer_profile_matching_the_implementer_is_still_invalid(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """Deriving from the run profile must not weaken the implementer check."""

    facts = _setup(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    review_run_id = facts["review_identity"].run_id
    _rewrite_review_metadata(
        board, review_run_id, _real_native_review_metadata(facts)
    )
    dev_profile = next(
        run["profile"]
        for run in board.run_rows(facts["task_id"])
        if run["id"] == facts["identity"].run_id
    )
    _set_review_profile(board, review_run_id, dev_profile)

    evaluation = _evaluate(facts, board)
    assert "reviewer-is-implementer" in evaluation.problems

    matches = _queue_for(facts, board)
    assert len(matches) == 1, matches
    assert matches[0]["skipped"] == "review-metadata-incomplete"
    assert "reviewer-is-implementer" in matches[0]["reason"]
    assert board.child_ids(facts["task_id"]) == []


def test_declared_reviewer_matching_the_implementer_is_invalid(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """An explicit ``reviewer`` equal to the implementer never binds."""

    facts = _setup(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    review_run_id = facts["review_identity"].run_id
    dev_profile = next(
        run["profile"]
        for run in board.run_rows(facts["task_id"])
        if run["id"] == facts["identity"].run_id
    )
    metadata = _real_native_review_metadata(facts)
    metadata["reviewer"] = dev_profile
    _rewrite_review_metadata(board, review_run_id, metadata)

    evaluation = _evaluate(facts, board)
    assert "reviewer-is-implementer" in evaluation.problems
    assert "review-reviewer-mismatch" in evaluation.problems

    matches = _queue_for(facts, board)
    assert len(matches) == 1, matches
    assert matches[0]["skipped"] == "review-metadata-incomplete"
    assert board.child_ids(facts["task_id"]) == []
