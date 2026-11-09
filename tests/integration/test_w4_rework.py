"""W4-04/W4-05: rework rounds continue from the previous code commit.

Round 1 starts at the repository head.  A ``changes_requested`` round starts a
new worktree at the previous run's ``code_commit``, keeps the round-1 task base
for the cumulative scope check, and records ``round``/``task_base_commit``/
``continuation_base`` in both ``state.json`` and ``run.json``.  Past the cap the
supervisor blocks before spawning anything.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.runtime.release import queue_release_cards
from hermes_kanban_lanes.workspace.git import repo_head

from w4_helpers import (
    IMPLEMENTER,
    REQUIREMENT,
    dev_run_dir,
    review_body,
    review_changes,
    run_dev,
    task_row,
    w4_config,
)


def _run_json(run_dir: Path) -> dict:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def _state_json(run_dir: Path) -> dict:
    return json.loads((run_dir / "state.json").read_text(encoding="utf-8"))


def _setup(tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor):
    repo = make_repo()
    base = repo_head(repo)
    round_one = fake_executor(review_body())
    round_two = fake_executor(
        review_body(write="RESULT2.txt", content="two\n", require="RESULT.txt")
    )
    config = w4_config(
        make_config,
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[round_one],
        max_rework_rounds=3,
    )
    task_id = make_task(title="w4 rework")
    card = load_card(
        write_card(
            config.work_root,
            task_id,
            allow=("RESULT.txt", "RESULT2.txt"),
        ),
        config,
    )
    identity_one, outcome_one = run_dev(
        board, config, card, task_id, executor_argv=round_one
    )
    assert outcome_one.category == "success", outcome_one.reason
    assert outcome_one.terminal == "request_review"
    run_one = dev_run_dir(config.work_root, task_id, identity_one.run_id)
    return {
        "repo": repo,
        "base": base,
        "config": config,
        "task_id": task_id,
        "card": card,
        "round_one": round_one,
        "round_two": round_two,
        "identity_one": identity_one,
        "run_one": run_one,
    }


def test_round_two_continues_from_the_previous_code_commit(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _setup(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    task_id = facts["task_id"]
    first_code = _run_json(facts["run_one"])["code_commit"]
    assert first_code != facts["base"]

    ok, implementer = review_changes(board, task_id, "please continue")
    assert ok is True
    assert implementer == IMPLEMENTER
    assert task_row(board, task_id)["status"] == "ready"
    assert task_row(board, task_id)["assignee"] == IMPLEMENTER

    identity_two, outcome_two = run_dev(
        board, config, facts["card"], task_id, executor_argv=facts["round_two"]
    )
    assert outcome_two.category == "success", outcome_two.reason
    assert outcome_two.terminal == "request_review", outcome_two.terminal
    run_two = dev_run_dir(config.work_root, task_id, identity_two.run_id)

    document = _run_json(run_two)
    assert document["round"] == 2
    assert document["task_base_commit"] == facts["base"]
    assert document["continuation_base"] == first_code
    assert document["base_commit"] == first_code
    assert document["code_commit"] != first_code
    state = _state_json(run_two)
    assert state["round"] == 2
    assert state["task_base_commit"] == facts["base"]
    assert state["continuation_base"] == first_code

    gate = json.loads(
        (run_two / document["gate"]["report_path"]).read_text(encoding="utf-8")
    )
    scope = next(check for check in gate["checks"] if check["name"] == "scope")
    assert scope["ok"] is True
    assert scope["changed_paths"] == ["RESULT.txt", "RESULT2.txt"]
    progress = next(check for check in gate["checks"] if check["name"] == "progress")
    assert progress["ok"] is True

    # The second round requested review again, still as the reviewer.
    assert task_row(board, task_id)["status"] == "review"
    assert task_row(board, task_id)["assignee"] == "code-reviewer"
    changes = board.changes_requested_events(task_id)
    assert [event["reason"] for event in changes] == ["please continue"]
    assert queue_release_cards(
        config.work_root, board, config, requirement_id=REQUIREMENT
    ) == []


def test_rework_cap_blocks_before_spawning_a_third_round(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _setup(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = w4_config(
        make_config,
        board_path=board.board_path,
        repo_path=facts["repo"],
        executor_argv=[facts["round_one"]],
        max_rework_rounds=2,
    )
    task_id = facts["task_id"]
    # The same card/config shape, but a cap of two rounds.
    card = load_card(
        Path(config.work_root) / "cards" / f"{task_id}.json", config
    )

    assert review_changes(board, task_id, "round one defect")[0] is True
    identity_two, outcome_two = run_dev(
        board, config, card, task_id, executor_argv=facts["round_two"]
    )
    assert outcome_two.category == "success", outcome_two.reason
    assert review_changes(board, task_id, "round two defect")[0] is True

    spawned = tmp_path / "third-round-spawned"
    third = fake_executor(
        f"Path({str(spawned)!r}).write_text('spawned', encoding='utf-8')\n"
        'print(json.dumps({"is_error": False, "result": "done"}))\n'
    )
    identity_three, outcome_three = run_dev(
        board, config, card, task_id, executor_argv=third
    )

    assert outcome_three.category == "code_failure"
    assert outcome_three.reason == "rework-cap:2"
    assert outcome_three.terminal == "block"
    assert outcome_three.terminal_ok is True
    assert not spawned.exists()
    row = task_row(board, task_id)
    assert row["status"] == "blocked"
    assert row["block_kind"] == "needs_input"
    runs = list((Path(config.work_root) / "runs" / task_id).iterdir())
    assert len(runs) == 2
    assert not any(
        path.name.startswith(f"{identity_three.run_id}-") for path in runs
    )
    assert board.child_ids(task_id) == []
    assert queue_release_cards(
        config.work_root, board, config, requirement_id=REQUIREMENT
    ) == []
    _ = identity_two
