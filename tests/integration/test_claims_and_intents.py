"""W2-03/04: intent fencing, identity change and restart replay dedupe."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_kanban_lanes.config import Executor
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.hermes_adapter.intents import validate_intent
from hermes_kanban_lanes.runtime import daemon

REPO_ROOT = Path(__file__).resolve().parents[2]


def _intent(identity: RunIdentity, kind: str, payload: dict, intent_id: str) -> dict:
    return {
        "schema_version": 1,
        "intent_id": intent_id,
        "kind": kind,
        "task_id": identity.task_id,
        "run_id": identity.run_id,
        "claim_lock": identity.claim_lock,
        "payload": payload,
    }


def _counts(board, task_id: str) -> dict[str, int]:
    comments = board.connection.execute(
        "SELECT count(*) FROM task_comments WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    children = board.connection.execute(
        "SELECT count(*) FROM tasks WHERE created_by LIKE 'hkl-intent:%'"
    ).fetchone()[0]
    links = board.connection.execute(
        "SELECT count(*) FROM task_links WHERE parent_id = ?", (task_id,)
    ).fetchone()[0]
    return {"comments": comments, "children": children, "links": links}


def test_stale_run_intents_and_terminal_writes_are_rejected(
    board, make_task, tmp_path
):
    task_id = make_task(title="parent")
    child_id = make_task(title="child")
    identity_a = board.claim(task_id)
    assert identity_a is not None
    # B takes over after A's claim is reclaimed (simulated loss).
    assert board.reclaim(identity_a, "simulated-loss")
    identity_b = board.claim(task_id)
    assert identity_b is not None and identity_b.run_id != identity_a.run_id

    stale_intents = [
        _intent(identity_a, "comment", {"body": "stale comment"}, "a-comment"),
        _intent(identity_a, "create", {"title": "stale child", "body": None}, "a-create"),
        _intent(identity_a, "link", {"child_id": child_id}, "a-link"),
    ]
    before = _counts(board, task_id)
    for document in stale_intents:
        result = board.apply_intent(identity_a, document)
        assert result["ok"] is False
        assert result["reason"] == "stale_run"
    assert board.request_review(identity_a, "stale") is False
    assert board.block(identity_a, "stale", "needs_input") is False
    assert _counts(board, task_id) == before

    fresh_intents = [
        _intent(identity_b, "comment", {"body": "fresh comment"}, "b-comment"),
        _intent(identity_b, "create", {"title": "fresh child", "body": None}, "b-create"),
        _intent(identity_b, "link", {"child_id": child_id}, "b-link"),
    ]
    for document in fresh_intents:
        result = board.apply_intent(identity_b, document)
        assert result["ok"] is True, result
    after = _counts(board, task_id)
    assert after == {
        "comments": before["comments"] + 1,
        "children": before["children"] + 1,
        "links": before["links"] + 1,
    }

    # Replaying the same intent ids is idempotent.
    for document in fresh_intents:
        replay = board.apply_intent(identity_b, document)
        assert replay["ok"] is False
        assert replay["reason"] == "duplicate_intent"
    assert _counts(board, task_id) == after
    assert board.request_review(identity_b, "fresh") is True


def test_identity_change_between_validation_and_apply_is_caught(
    board, make_task
):
    task_id = make_task(title="race-intent")
    identity_a = board.claim(task_id)
    assert identity_a is not None
    document = _intent(identity_a, "comment", {"body": "late"}, "late-comment")
    assert validate_intent(document, identity=identity_a)["intent_id"] == "late-comment"

    # The run changes after structural validation but before the transaction.
    assert board.reclaim(identity_a, "changed")
    identity_b = board.claim(task_id)
    assert identity_b is not None
    before = _counts(board, task_id)
    result = board.apply_intent(identity_a, document)
    assert result["reason"] == "stale_run"
    assert _counts(board, task_id) == before
    assert board.request_review(identity_b, "owner") is True


def test_restart_after_commit_replays_as_duplicate(
    board, make_task, tmp_path, hermes_source
):
    task_id = make_task(title="restart")
    identity = board.claim(task_id)
    assert identity is not None
    document = _intent(identity, "comment", {"body": "once"}, "restart-comment")
    intent_path = tmp_path / "intent.json"
    intent_path.write_text(json.dumps(document), encoding="utf-8")

    script = textwrap.dedent(
        """
        import json, os, sys
        sys.path.insert(0, os.environ["HKL_SRC"])
        from hermes_kanban_lanes.contracts import RunIdentity
        from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter
        adapter = BoardAdapter(sys.argv[1], sys.argv[2])
        adapter.open()
        identity = RunIdentity.from_dict(json.loads(sys.argv[3]))
        intent = json.loads(open(sys.argv[4], encoding="utf-8").read())
        result = adapter.apply_intent(identity, intent)
        print(json.dumps(result))
        sys.stdout.flush()
        os._exit(0)  # crash after COMMIT, before local bookkeeping
        """
    )
    environment = dict(os.environ)
    environment["HKL_SRC"] = str(REPO_ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            board.board_path,
            hermes_source,
            json.dumps(identity.to_dict()),
            str(intent_path),
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(tmp_path),
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    first = json.loads(completed.stdout.strip().splitlines()[-1])
    assert first["ok"] is True, first
    assert _counts(board, task_id)["comments"] == 1

    replay = board.apply_intent(identity, document)
    assert replay["ok"] is False
    assert replay["reason"] == "duplicate_intent"
    assert _counts(board, task_id)["comments"] == 1


# ---------------------------------------------------------------------------
# W8: card-body ingest (assignee mapping + lane/development synthesis)
# ---------------------------------------------------------------------------


def _legacy_body(
    *,
    project: str = "primary",
    review: bool | None = None,
    review_in_development: bool = False,
    task_type: str | None = "development",
    build_cmd: str = "",
    artifacts: str | None = None,
) -> str:
    lines = [
        "Legacy Hermes card body.",
        "",
        "```lane",
        f"project: {project}",
        "allow: [RESULT.txt]",
        f'test_cmd: "{sys.executable} -c pass"',
        f'build_cmd: "{build_cmd}"',
    ]
    if artifacts is not None:
        lines.append(f"artifacts: {artifacts}")
    if review is not None and not review_in_development:
        lines.append(f"review: {'true' if review else 'false'}")
    lines += ["```", "", "```development", "contract_version: 2"]
    if task_type is not None:
        lines.append(f"task_type: {task_type}")
    if review is not None and review_in_development:
        lines.append(f"review: {'true' if review else 'false'}")
    lines += ["risk: normal", "```", ""]
    return "\n".join(lines)


def _synthesising_config(config, executor: str, assignees=("hkl:claude", "claude-code")):
    executors = dict(config.executors)
    executors["claude"] = Executor(argv=(executor,), assignees=assignees)
    return replace(config, executors=executors)


def _successful_executor(fake_executor) -> str:
    return fake_executor(
        'Path("RESULT.txt").write_text("ok\\n", encoding="utf-8")\n'
        'print(json.dumps({"is_error": False, "result": "done"}))\n'
    )


def test_legacy_card_claimed_without_json(
    board, make_task, make_repo, make_config, fake_executor, task_status
):
    executor = _successful_executor(fake_executor)
    config = _synthesising_config(
        make_config(
            board_path=board.board_path, repo_path=make_repo(), executor_argv=[executor]
        ),
        executor,
    )
    task_id = make_task(
        title="legacy claude-code card", assignee="claude-code", body=_legacy_body()
    )
    card_path = Path(config.work_root) / "cards" / f"{task_id}.json"
    assert not card_path.exists()

    report = daemon.tick(config, adapters={"test": board}, max_workers=1)

    assert [entry["task_id"] for entry in report["claimed"]] == [task_id]
    assert report["errors"] == []
    run = report["runs"][0]
    assert run["category"] == "success"
    assert run["gate_verdict"] == "pass"
    assert run["terminal"] == "request_review"
    assert task_status(task_id)["status"] == "review"
    # Synthesis is in-memory only; the daemon writes no card ledger of its own.
    assert not card_path.exists()


def test_legacy_review_false_card_completes(
    board, make_task, make_repo, make_config, fake_executor, task_status
):
    executor = _successful_executor(fake_executor)
    config = _synthesising_config(
        make_config(
            board_path=board.board_path, repo_path=make_repo(), executor_argv=[executor]
        ),
        executor,
    )
    task_id = make_task(
        title="legacy no-review card",
        assignee="claude-code",
        body=_legacy_body(review=False, task_type="non_development"),
    )

    report = daemon.tick(config, adapters={"test": board}, max_workers=1)

    assert [entry["task_id"] for entry in report["claimed"]] == [task_id]
    run = report["runs"][0]
    assert run["category"] == "success"
    assert run["terminal"] == "complete"
    assert task_status(task_id)["status"] == "done"
    kinds = [
        row["kind"]
        for row in board.connection.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
    ]
    assert "review_requested" not in kinds
    assert "review-forced" not in kinds
    assert "completed" in kinds


@pytest.mark.parametrize("review_in_development", [False, True])
def test_development_review_false_is_forced_to_review(
    review_in_development,
    board,
    make_task,
    make_repo,
    make_config,
    fake_executor,
    task_status,
):
    executor = _successful_executor(fake_executor)
    config = _synthesising_config(
        make_config(
            board_path=board.board_path, repo_path=make_repo(), executor_argv=[executor]
        ),
        executor,
    )
    task_id = make_task(
        title="development card asking for no review",
        assignee="claude-code",
        body=_legacy_body(
            review=False,
            review_in_development=review_in_development,
            task_type="development",
        ),
    )

    report = daemon.tick(config, adapters={"test": board}, max_workers=1)

    assert [entry["task_id"] for entry in report["claimed"]] == [task_id]
    run = report["runs"][0]
    assert run["category"] == "success"
    assert run["terminal"] == "request_review"
    assert task_status(task_id)["status"] == "review"
    rows = board.connection.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'review-forced' ORDER BY id",
        (task_id,),
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["reason"] == "development-requires-review"


def test_missing_task_type_with_review_false_is_forced_to_review(
    board, make_task, make_repo, make_config, fake_executor, task_status
):
    executor = _successful_executor(fake_executor)
    config = _synthesising_config(
        make_config(
            board_path=board.board_path, repo_path=make_repo(), executor_argv=[executor]
        ),
        executor,
    )
    task_id = make_task(
        title="untyped card asking for no review",
        assignee="claude-code",
        body=_legacy_body(review=False, task_type=None),
    )

    report = daemon.tick(config, adapters={"test": board}, max_workers=1)

    run = report["runs"][0]
    assert run["category"] == "success"
    assert run["terminal"] == "request_review"
    assert task_status(task_id)["status"] == "review"
    payload = json.loads(
        board.connection.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'review-forced'",
            (task_id,),
        ).fetchone()["payload"]
    )
    assert payload["reason"] == "task-type-missing"


def test_explicit_card_json_takes_priority_over_lane_body(
    board, make_task, make_repo, make_config, fake_executor, write_card, task_status
):
    executor = _successful_executor(fake_executor)
    config = _synthesising_config(
        make_config(
            board_path=board.board_path, repo_path=make_repo(), executor_argv=[executor]
        ),
        executor,
    )
    task_id = make_task(
        title="explicit json wins",
        assignee="claude-code",
        body=_legacy_body(project="not-configured"),
    )
    card_path = write_card(config.work_root, task_id)
    original = card_path.read_text(encoding="utf-8")

    report = daemon.tick(config, adapters={"test": board}, max_workers=1)

    assert [entry["task_id"] for entry in report["claimed"]] == [task_id]
    assert all(
        entry["task_id"] != task_id for entry in report["skipped"]
    ), report["skipped"]
    assert report["runs"][0]["category"] == "success"
    assert task_status(task_id)["status"] == "review"
    # The explicit card wins and is never overwritten by synthesis.
    assert card_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            "```lane\nallow: [RESULT.txt]\n"
            f'test_cmd: "{sys.executable} -c pass"\n```\n',
            "card-project-missing",
        ),
        (
            _legacy_body(project="secondary"),
            "repository-not-configured",
        ),
        (
            "```lane\nproject: primary\nallow: [RESULT.txt]\n"
            'test_cmd: "true"\nbuild_cmd: "true && true"\n```\n',
            "card-command-unsupported",
        ),
        (
            "```lane\nproject: primary\nallow: [RESULT.txt]\n"
            "test_cmd: \"pytest 'unclosed\"\n```\n",
            "card-command-unsupported",
        ),
        (
            "```lane\nproject: primary\nallow: [RESULT.txt]\n"
            'test_cmd: "true"\nartifacts: [artifacts/w3/]\n```\n',
            "card-invalid",
        ),
        (
            "```lane\nproject: [unclosed\nallow: [RESULT.txt]\n```\n",
            "card-invalid",
        ),
    ],
)
def test_synthesis_failure_is_skipped_on_the_card(
    body,
    reason,
    board,
    make_task,
    make_repo,
    make_config,
    fake_executor,
    task_status,
):
    executor = _successful_executor(fake_executor)
    config = make_config(
        board_path=board.board_path, repo_path=make_repo(), executor_argv=[executor]
    )
    task_id = make_task(title=f"bad body: {reason}", body=body)
    before = task_status(task_id)

    report = daemon.tick(config, adapters={"test": board}, max_workers=1)

    assert any(
        entry["task_id"] == task_id and entry["reason"] == reason
        for entry in report["skipped"]
    ), report["skipped"]
    assert report["claimed"] == []
    assert task_status(task_id) == before
    rows = board.connection.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'skipped' "
        "ORDER BY id",
        (task_id,),
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["reason"] == reason
    assert payload.get("detail")

    # Polling the same bad card must not append the same refusal forever.
    daemon.tick(config, adapters={"test": board}, max_workers=1)
    assert (
        board.connection.execute(
            "SELECT count(*) FROM task_events WHERE task_id = ? AND kind = 'skipped'",
            (task_id,),
        ).fetchone()[0]
        == 1
    )


def test_explicit_assignees_discover_both_and_claim_once(
    board, make_task, make_repo, make_config, fake_executor, task_status
):
    executor = _successful_executor(fake_executor)
    config = _synthesising_config(
        make_config(
            board_path=board.board_path,
            repo_path=make_repo(),
            executor_argv=[executor],
            max_concurrent=3,
        ),
        executor,
    )
    native = make_task(title="native hkl card", assignee="hkl:claude", body=_legacy_body())
    legacy = make_task(
        title="legacy card", assignee="claude-code", body=_legacy_body()
    )
    bad = make_task(
        title="bad card",
        assignee="hkl:claude",
        body="```lane\nallow: [RESULT.txt]\n"
        f'test_cmd: "{sys.executable} -c pass"\n```\n',
    )

    report = daemon.tick(config, adapters={"test": board}, max_workers=3)

    claimed = [entry["task_id"] for entry in report["claimed"]]
    assert sorted(claimed) == sorted([native, legacy])
    assert len(claimed) == len(set(claimed))
    assert {entry["assignee"] for entry in report["discovered"]} >= {
        "hkl:claude",
        "claude-code",
    }
    # One synthesis failure does not starve the valid cards on the same board.
    assert task_status(native)["status"] == "review"
    assert task_status(legacy)["status"] == "review"
    assert task_status(bad)["status"] == "ready"
    assert any(
        entry["task_id"] == bad and entry["reason"] == "card-project-missing"
        for entry in report["skipped"]
    )
