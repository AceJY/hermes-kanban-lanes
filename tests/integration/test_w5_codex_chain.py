"""W5-07/W5-08: the same lifecycle with a fake Codex executor.

The development card really runs through ``run_once`` (workspace, gate,
evidence, landing plan); the review input is synthetic test metadata; the
release gate, ``--no-ff`` merge and post-merge regression run the product code
unchanged.  No real Codex binary and no real board is touched.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.config import Executor, MergeConfig, ReviewConfig
from hermes_kanban_lanes.executors.codex import CodexAdapter
from hermes_kanban_lanes.runtime.release import queue_release_cards, release_once
from hermes_kanban_lanes.runtime.supervisor import RunSettings, run_once
from hermes_kanban_lanes.verification.evidence import INDEX_RELATIVE_PATH, sha256_file
from hermes_kanban_lanes.workspace.git import repo_head, run_git

REQUIREMENT = "HKL-W5"
REVIEWER = "code-reviewer"
SRC_DIR = Path(__file__).resolve().parents[2] / "src" / "hermes_kanban_lanes"
THREAD_ID = "thread-w5-chain"

SETTINGS = RunSettings(
    max_runtime_seconds=60,
    heartbeat_seconds=30,
    poll_seconds=0.05,
    grace_seconds=1,
    kill_grace_seconds=1,
)


def _assert_protocol_argv() -> str:
    """Fake-executor preamble: fail the run if D3 argv is wrong."""

    return f"""
argv = sys.argv[1:]
assert "exec" in argv, argv
exec_index = argv.index("exec")
assert argv[exec_index + 1] == "--json", argv
assert argv[exec_index + 2] == "-o", argv
assert argv[exec_index + 3].endswith("codex-last-message.txt"), argv
fixed = ["--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
         "--sandbox", "workspace-write"]
for offset, value in enumerate(fixed):
    assert argv[exec_index + 4 + offset] == value, (offset, argv)
prompt_index = exec_index + 4 + len(fixed)
assert prompt_index == len(argv) - 1, argv
assert argv[-1] and not argv[-1].startswith("--"), argv
assert "--max-budget-usd" not in argv, argv
assert argv.count("--sandbox") == 1, argv
"""


def _success_body() -> str:
    return _assert_protocol_argv() + f"""
Path("RESULT.txt").write_text("ok\\n", encoding="utf-8")
print(json.dumps({{"type": "thread.started", "thread_id": {THREAD_ID!r}}}))
print(json.dumps({{"type": "turn.started"}}))
print(json.dumps({{"type": "item.completed",
                  "item": {{"id": "item_0", "type": "agent_message",
                            "text": "created RESULT.txt"}}}}))
print(json.dumps({{"type": "turn.completed",
                  "usage": {{"input_tokens": 120, "cached_input_tokens": 0,
                            "cache_write_input_tokens": 0, "output_tokens": 7,
                            "reasoning_output_tokens": 0}}}}))
"""


def _codex_config(
    make_config,
    *,
    board_path,
    repo_path,
    executor: str,
    merge_enabled: bool = True,
    post_merge_commands=None,
):
    base = make_config(
        board_path=board_path, repo_path=repo_path, executor_argv=["claude"]
    )
    executors = dict(base.executors)
    executors["codex"] = Executor(argv=(executor,))
    review = ReviewConfig(
        reviewer=REVIEWER,
        max_rework_rounds=3,
        merge=MergeConfig(
            enabled=merge_enabled,
            main_ref="refs/heads/main",
            post_merge_commands=list(post_merge_commands or []),
            post_merge_timeout_seconds=120.0,
        ),
    )
    return replace(base, executors=executors, review=review)


def _claim_and_run(board, config, card, task_id, *, settings=SETTINGS):
    identity = board.claim(task_id)
    assert identity is not None, "claim unexpectedly lost"
    row = board.connection.execute(
        "SELECT id, title, body FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row is not None
    outcome = run_once(
        config=config,
        adapter=board,
        board_alias="test",
        task=dict(row),
        card=card,
        identity=identity,
        executor=CodexAdapter(),
        settings=settings,
        package_dir=SRC_DIR,
        requirement_id=REQUIREMENT,
    )
    return identity, outcome


def _run_dir(work_root, task_id: str, run_id: int) -> Path:
    runs = sorted((Path(work_root) / "runs" / task_id).iterdir())
    matches = [path for path in runs if path.name.startswith(f"{run_id}-")]
    assert len(matches) == 1, matches
    return matches[0]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _task_row(board, task_id: str) -> dict:
    return board.task_row(task_id) or {}


def _review_metadata(run_dir: Path, *, dev_run_id: int, review_run_id: int) -> dict:
    document = _load(run_dir / "run.json")
    head = document["code_commit"]
    tree = run_git(
        ["rev-parse", f"{head}^{{tree}}"], cwd=Path(document["workspace"]), timeout=30
    ).stdout.strip()
    return {
        "schema_version": 1,
        "requirement_id": REQUIREMENT,
        "verdict": "pass",
        "reviewed_head": head,
        "reviewed_tree": tree,
        "dev_run_id": dev_run_id,
        "rework_round": 1,
        "reviewer": REVIEWER,
        "review_run_id": review_run_id,
        "evidence_index_sha256": sha256_file(run_dir / INDEX_RELATIVE_PATH),
        "reasons": [],
    }


def _write_authorization(work_root, task_id: str, run_dir: Path, metadata: dict) -> Path:
    document = _load(run_dir / "run.json")
    landing = _load(run_dir / "landing-plan.json")
    now = int(time.time())
    payload = {
        "schema_version": 1,
        "requirement_id": REQUIREMENT,
        "task_id": task_id,
        "review_run_id": metadata["review_run_id"],
        "dev_run_id": metadata["dev_run_id"],
        "rework_round": metadata["rework_round"],
        "task_base_commit": document["task_base_commit"],
        "reviewed_head": metadata["reviewed_head"],
        "reviewed_tree": metadata["reviewed_tree"],
        "evidence_index_sha256": metadata["evidence_index_sha256"],
        "main_ref": "refs/heads/main",
        "main_commit": landing["main_commit"],
        "approver": "operator",
        "approved_at": now - 10,
        "expires_at": now + 3600,
    }
    target = Path(work_root) / "authorizations" / f"{task_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


# ---------------------------------------------------------------------------
# W5-07: full development -> gate -> evidence -> review -> release -> merge
# ---------------------------------------------------------------------------


def test_w5_codex_full_chain(
    board, make_task, make_repo, make_config, write_card, fake_executor, load_run_json
):
    repo = make_repo()
    base = repo_head(repo)
    executor = fake_executor(_success_body())
    config = _codex_config(
        make_config,
        board_path=board.board_path,
        repo_path=repo,
        executor=executor,
        post_merge_commands=[
            [
                sys.executable,
                "-c",
                "from pathlib import Path; "
                "raise SystemExit(0 if Path('RESULT.txt').read_text() == 'ok\\n' else 1)",
            ]
        ],
    )
    task_id = make_task(title="w5 codex chain", assignee="hkl:codex")
    card = load_card(
        write_card(
            config.work_root,
            task_id,
            executor="codex",
            allow=("RESULT.txt",),
            commands=[
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; "
                        "raise SystemExit(0 if Path('RESULT.txt').read_text() == 'ok\\n' else 1)",
                    ],
                    "cwd": ".",
                }
            ],
        ),
        config,
    )

    identity, outcome = _claim_and_run(board, config, card, task_id)
    assert outcome.category == "success", outcome.reason
    assert outcome.gate_verdict == "pass"
    assert outcome.terminal == "request_review", outcome.terminal
    assert outcome.evidence["verified"] is True, outcome.evidence
    assert _task_row(board, task_id)["status"] == "review"
    assert _task_row(board, task_id)["assignee"] == REVIEWER
    run_dir = _run_dir(config.work_root, task_id, identity.run_id)

    document = load_run_json(run_dir)
    assert document["requirement_id"] == REQUIREMENT
    assert document["category"] == "success"
    assert document["executor"]["name"] == "codex"
    assert document["executor"]["executor_session_id"] == THREAD_ID
    assert document["usage"] == {
        "input_tokens": 120,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 7,
        "reasoning_output_tokens": 0,
        "cost_usd": None,
    }
    assert (run_dir / "codex-settings.json").is_file()
    assert not (run_dir / "claude-settings.json").exists()
    code_commit = document["code_commit"]
    assert code_commit and document["gate"]["verdict"] == "pass"

    # -- synthetic independent-review input (not an independent review) -----
    review_identity = board.claim_review_task(task_id)
    assert review_identity is not None
    metadata = _review_metadata(
        run_dir, dev_run_id=identity.run_id, review_run_id=review_identity.run_id
    )
    assert board.complete_task(
        review_identity,
        result="review pass",
        summary="synthetic review pass",
        metadata=metadata,
    )
    assert _task_row(board, task_id)["status"] == "done"
    _write_authorization(config.work_root, task_id, run_dir, metadata)

    queued = queue_release_cards(
        config.work_root, board, config, requirement_id=REQUIREMENT
    )
    matches = [entry for entry in queued if entry["dev_task_id"] == task_id]
    assert len(matches) == 1, queued
    merge_task_id = matches[0]["merge_task_id"]
    merge_card = load_card(
        Path(config.work_root) / "cards" / f"{merge_task_id}.json", config
    )
    assert merge_card.card_kind == "merge"
    assert merge_card.executor == "codex"
    assert merge_card.closure_for == task_id

    merge_identity = board.claim(merge_task_id)
    assert merge_identity is not None
    release = release_once(
        config=config,
        adapter=board,
        card=merge_card,
        identity=merge_identity,
        work_root=config.work_root,
        package_dir=SRC_DIR,
        requirement_id=REQUIREMENT,
    )
    assert release["action"] == "merged", release
    assert release["terminal_ok"] is True
    merge_sha = release["merge_sha"]
    assert merge_sha
    release_dir = Path(release["run_dir"])

    report = _load(release_dir / "release-report.json")
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
    assert all(check["ok"] for check in report["checks"])

    merge_document = _load(release_dir / "merge.json")
    assert merge_document["verdict"] == "merged"
    assert merge_document["parents"] == [base, code_commit]
    assert merge_document["outside_allow"] == 0
    assert merge_document["protected_roots_touched"] == 0

    postmerge = _load(release_dir / "postmerge" / "report.json")
    assert postmerge["verdict"] == "pass", postmerge
    assert postmerge["merge_sha"] == merge_sha
    assert postmerge["worktree"] == "postmerge/worktree"
    assert [entry["exit_code"] for entry in postmerge["commands"]] == [0]

    # The local main moved to the merge commit and nowhere else.
    assert repo_head(repo) == merge_sha
    parents = run_git(
        ["rev-list", "--parents", "-n", "1", merge_sha], cwd=repo, timeout=30
    ).stdout.split()
    assert parents[0] == merge_sha
    assert set(parents[1:]) == {base, code_commit}

    merge_row = _task_row(board, merge_task_id)
    assert merge_row["status"] == "done", merge_row
    handoff = board.run_rows(merge_task_id)[0]["metadata"]["hkl"]
    assert handoff["merge_sha"] == merge_sha
    assert handoff["reviewed_head"] == code_commit
    assert handoff["evidence_index_sha256"] == metadata["evidence_index_sha256"]


# ---------------------------------------------------------------------------
# W5-08: failure classes block without a merge card or a moved main
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected_category", "expected_kind"),
    [
        (
            'print(json.dumps({"type": "turn.failed", '
            '"error": {"message": "401 unauthorized: login required"}}))\n'
            "raise SystemExit(1)\n",
            "environment_failure",
            "capability",
        ),
        (
            'print(json.dumps({"type": "turn.failed", '
            '"error": {"message": "429 rate limit exceeded"}}))\n'
            "raise SystemExit(1)\n",
            "environment_failure",
            "transient",
        ),
        (
            'print(json.dumps({"type": "turn.failed", '
            '"error": {"message": "The model is not supported"}}))\n'
            "raise SystemExit(1)\n",
            "code_failure",
            "needs_input",
        ),
    ],
)
def test_w5_codex_failure_classes_block_without_merge(
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    task_status,
    body,
    expected_category,
    expected_kind,
):
    repo = make_repo()
    base = repo_head(repo)
    executor = fake_executor(body)
    config = _codex_config(
        make_config,
        board_path=board.board_path,
        repo_path=repo,
        executor=executor,
    )
    task_id = make_task(title="w5 failure", assignee="hkl:codex")
    card = load_card(
        write_card(config.work_root, task_id, executor="codex"), config
    )

    _, outcome = _claim_and_run(board, config, card, task_id)
    assert outcome.category == expected_category, outcome
    assert outcome.terminal == "block"
    status = task_status(task_id)
    assert status["status"] == "blocked"
    assert status["block_kind"] == expected_kind
    # No merge card was queued and the repo main never moved.
    assert board.child_ids(task_id) == []
    assert repo_head(repo) == base


def test_w5_codex_timeout_blocks_transient(
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    task_status,
):
    repo = make_repo()
    base = repo_head(repo)
    executor = fake_executor("time.sleep(30)\n")
    config = _codex_config(
        make_config,
        board_path=board.board_path,
        repo_path=repo,
        executor=executor,
    )
    task_id = make_task(title="w5 timeout", assignee="hkl:codex")
    card = load_card(
        write_card(config.work_root, task_id, executor="codex"), config
    )
    settings = RunSettings(
        max_runtime_seconds=0.5,
        heartbeat_seconds=30,
        poll_seconds=0.05,
        grace_seconds=0.5,
        kill_grace_seconds=1,
    )

    _, outcome = _claim_and_run(board, config, card, task_id, settings=settings)
    assert outcome.category == "timeout", outcome
    assert outcome.terminal == "block"
    status = task_status(task_id)
    assert status["status"] == "blocked"
    assert status["block_kind"] == "transient"
    assert board.child_ids(task_id) == []
    assert repo_head(repo) == base
