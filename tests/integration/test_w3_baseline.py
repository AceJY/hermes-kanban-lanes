"""W3-07/08: baseline sync when the repository advances during a run."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.runtime.supervisor import RunSettings
from hermes_kanban_lanes.verification.evidence import (
    INDEX_RELATIVE_PATH,
    verify_evidence,
)
from hermes_kanban_lanes.workspace.git import (
    is_ancestor,
    repo_head,
    run_git,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src" / "hermes_kanban_lanes"

SETTINGS = dict(
    max_runtime_seconds=30,
    heartbeat_seconds=30,
    poll_seconds=0.05,
    grace_seconds=1,
    kill_grace_seconds=1,
)

ADVANCE_TEMPLATE = """
import subprocess
repo = {repo!r}
Path(repo, {name!r}).write_text({content!r}, encoding="utf-8")
subprocess.run(
    ["git", "-C", repo, "add", {name!r}], check=True, capture_output=True
)
subprocess.run(
    [
        "git", "-C", repo,
        "-c", "user.name=HKL Tests",
        "-c", "user.email=hkl@example.invalid",
        "commit", "--no-verify", "-m", "advance main",
    ],
    check=True,
    capture_output=True,
)
Path("RESULT.txt").write_text("ok\\n", encoding="utf-8")
print(json.dumps({{"is_error": False, "result": "done"}}))
"""


CONFLICT_TEMPLATE = """
import subprocess
repo = {repo!r}
Path("README.txt").write_text("run-side\\n", encoding="utf-8")
Path(repo, "README.txt").write_text("main-side\\n", encoding="utf-8")
subprocess.run(
    ["git", "-C", repo, "add", "README.txt"], check=True, capture_output=True
)
subprocess.run(
    [
        "git", "-C", repo,
        "-c", "user.name=HKL Tests",
        "-c", "user.email=hkl@example.invalid",
        "commit", "--no-verify", "-m", "advance main",
    ],
    check=True,
    capture_output=True,
)
Path("RESULT.txt").write_text("ok\\n", encoding="utf-8")
print(json.dumps({{"is_error": False, "result": "done"}}))
"""


def _advance_body(repo: Path, name: str, content: str) -> str:
    return ADVANCE_TEMPLATE.format(
        repo=str(repo), name=name, content=content
    )


def test_advanced_base_is_merged_into_the_run_branch(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    claim_and_run,
    fake_executor,
    task_status,
    load_run_json,
):
    repo = make_repo()
    base = repo_head(repo)
    executor = fake_executor(_advance_body(repo, "MAIN.txt", "main advance\n"))
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="w3 baseline advance")
    card = load_card(
        write_card(config.work_root, task_id, allow=("RESULT.txt",)), config
    )

    identity, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=RunSettings(**SETTINGS)
    )

    assert outcome.category == "success", outcome.reason
    assert outcome.terminal == "request_review"
    assert task_status(task_id)["status"] == "review"

    run_dir = Path(outcome.run_dir)
    workspace = Path(outcome.workspace)
    document = load_run_json(run_dir)
    baseline = document["baseline"]
    assert baseline["original_base"] == base
    assert baseline["updated"] is True
    assert baseline["conflict"] is False
    assert baseline["conflict_paths"] == []
    assert baseline["sync_base"] != base
    assert document["sync_base"] == baseline["sync_base"]
    assert document["run_commit"] != document["code_commit"]
    assert baseline["merge_commit"] == document["code_commit"]
    assert repo_head(repo) == baseline["sync_base"]

    # The merge commit combines the authored run commit and the new base.
    assert is_ancestor(document["run_commit"], document["code_commit"], repo=repo)
    assert is_ancestor(baseline["sync_base"], document["code_commit"], repo=repo)
    parents = run_git(
        ["rev-list", "--parents", "-n", "1", document["code_commit"]],
        cwd=workspace,
    ).stdout.split()
    assert len(parents) == 3

    # The gate judged the authored diff, not the baseline merge.
    gate_report = json.loads(
        (run_dir / document["gate"]["report_path"]).read_text(encoding="utf-8")
    )
    assert gate_report["verdict"] == "pass"
    assert gate_report["base_commit"] == base
    assert gate_report["run_commit"] == document["run_commit"]
    assert gate_report["sync_base"] == baseline["sync_base"]
    checks = {check["name"]: check for check in gate_report["checks"]}
    assert checks["scope"]["changed_paths"] == ["RESULT.txt"]
    assert checks["progress"]["ok"] is True

    index = json.loads(
        (run_dir / INDEX_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert index["base_commit"] == base
    assert index["sync_base"] == baseline["sync_base"]
    assert index["code_commit"] == document["code_commit"]
    verification = verify_evidence(run_dir, identity, SRC_DIR, workspace, repo)
    assert verification.ok is True, verification.reasons

    landing = json.loads(
        (run_dir / "landing-plan.json").read_text(encoding="utf-8")
    )
    assert landing["verdict"] == "ready"
    assert landing["main_commit"] == baseline["sync_base"]
    assert landing["code_commit"] == document["code_commit"]

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["stage"] == "terminal"
    assert state["sync_base"] == baseline["sync_base"]
    assert state["code_commit"] == document["code_commit"]


def test_baseline_conflict_aborts_and_keeps_both_sides(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    claim_and_run,
    fake_executor,
    task_status,
    load_run_json,
):
    repo = make_repo()  # README.txt contains "base\n"
    base = repo_head(repo)
    executor = fake_executor(CONFLICT_TEMPLATE.format(repo=str(repo)))
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="w3 baseline conflict")
    card = load_card(
        write_card(
            config.work_root,
            task_id,
            allow=("RESULT.txt", "README.txt"),
        ),
        config,
    )

    _, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=RunSettings(**SETTINGS)
    )

    assert outcome.category == "baseline_conflict", outcome.reason
    assert outcome.terminal == "block"
    assert outcome.reason == "baseline-conflict:1 paths"
    status = task_status(task_id)
    assert status["status"] == "blocked"
    assert status["block_kind"] == "needs_input"

    run_dir = Path(outcome.run_dir)
    workspace = Path(outcome.workspace)
    document = load_run_json(run_dir)
    baseline = document["baseline"]
    assert baseline["original_base"] == base
    assert baseline["updated"] is True
    assert baseline["conflict"] is True
    assert baseline["conflict_paths"] == ["README.txt"]
    assert baseline["merge_commit"] is None
    assert document["code_commit"] == document["run_commit"]
    assert document["gate"]["verdict"] is None
    assert document["gate"]["report_path"] is None

    # No gate, no manifest, no evidence index: the run stopped at the sync.
    assert not (run_dir / "evidence" / "gate-report.json").exists()
    assert not (run_dir / "evidence" / "manifest.json").exists()
    assert not (run_dir / INDEX_RELATIVE_PATH).exists()
    assert not (run_dir / "landing-plan.json").exists()

    # Both sides survive: the run branch keeps its edit, main keeps its own.
    assert (workspace / "README.txt").read_text(encoding="utf-8") == "run-side\n"
    assert (repo / "README.txt").read_text(encoding="utf-8") == "main-side\n"
    assert run_git(
        ["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=workspace
    ).returncode != 0
    assert run_git(
        ["cat-file", "-e", document["run_commit"]], cwd=workspace
    ).returncode == 0
    assert run_git(["status", "--porcelain"], cwd=workspace).stdout.strip() == ""
    assert repo_head(repo) == baseline["sync_base"]

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["stage"] == "terminal"
    assert state["run_commit"] == document["run_commit"]
    assert state["sync_base"] is None
