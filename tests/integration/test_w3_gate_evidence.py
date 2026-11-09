"""W3-01/04/06: full-chain gate evidence and command process-group handling."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.runtime.process import pid_alive
from hermes_kanban_lanes.runtime.supervisor import RunSettings
from hermes_kanban_lanes.verification.evidence import (
    INDEX_RELATIVE_PATH,
    EvidenceVerification,
    sha256_file,
    verify_evidence,
)
from hermes_kanban_lanes.workspace.git import repo_head

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src" / "hermes_kanban_lanes"
PYTHON = sys.executable

SETTINGS = dict(
    max_runtime_seconds=30,
    heartbeat_seconds=30,
    poll_seconds=0.05,
    grace_seconds=1,
    kill_grace_seconds=1,
)

SUCCESS_BODY = """
Path("RESULT.txt").write_text("ok\\n", encoding="utf-8")
print(json.dumps({"is_error": False, "result": "done"}))
"""

ARTIFACT_COMMAND = (
    "import pathlib,sys;"
    "sys.exit(0 if pathlib.Path('RESULT.txt').read_text() == 'ok\\n' else 1)"
)


def _report_checks(report: dict) -> dict[str, dict]:
    return {check["name"]: check for check in report["checks"]}


def test_full_chain_writes_a_green_w3_evidence_package(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    claim_and_run,
    task_status,
    fake_executor,
    load_run_json,
):
    repo = make_repo()
    executor = fake_executor(SUCCESS_BODY)
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="w3 success")
    base = repo_head(repo)
    card_path = write_card(
        config.work_root,
        task_id,
        allow=("RESULT.txt",),
        commands=[{"argv": [PYTHON, "-c", ARTIFACT_COMMAND], "cwd": "."}],
    )
    # D8 additive key; declaring it test-locally keeps the shared fixture
    # (tests/conftest.py) untouched and outside this lane's scope.
    document = json.loads(card_path.read_text(encoding="utf-8"))
    document["artifacts"] = ["RESULT.txt"]
    card_path.write_text(json.dumps(document), encoding="utf-8")
    card = load_card(card_path, config)

    identity, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=RunSettings(**SETTINGS)
    )

    assert outcome.category == "success", outcome.reason
    assert outcome.gate_verdict == "pass"
    assert outcome.terminal == "request_review"
    assert task_status(task_id)["status"] == "review"

    run_dir = Path(outcome.run_dir)
    workspace = Path(outcome.workspace)
    document = load_run_json(run_dir)
    assert document["requirement_id"] == "HKL-W3"
    assert document["category"] == "success"
    assert document["recovery"] == {
        "recovered": False,
        "stage_before": None,
        "finalized": False,
    }
    assert document["baseline"] == {
        "original_base": base,
        "sync_base": base,
        "updated": False,
        "merge_commit": None,
        "conflict": False,
        "conflict_paths": [],
    }
    assert document["run_commit"] == document["code_commit"]
    assert document["evidence"]["verified"] is True
    assert document["evidence"]["reasons"] == []

    # -- gate report schema 2 with five passing checks ---------------------
    gate_report = json.loads(
        (run_dir / document["gate"]["report_path"]).read_text(encoding="utf-8")
    )
    assert gate_report["schema_version"] == 2
    assert gate_report["requirement_id"] == "HKL-W3"
    assert gate_report["verdict"] == "pass"
    checks = _report_checks(gate_report)
    assert list(checks) == [
        "workspace",
        "scope",
        "progress",
        "commands",
        "artifacts",
    ]
    assert all(check["ok"] for check in checks.values())
    assert checks["scope"]["changed_paths"] == ["RESULT.txt"]
    assert checks["progress"]["detail"] == "ok"
    assert checks["artifacts"]["artifacts"][0]["path"] == "RESULT.txt"

    (entry,) = checks["commands"]["commands"]
    assert entry["exit_code"] == 0
    assert entry["timed_out"] is False
    for stream in ("stdout", "stderr"):
        log_path = run_dir / entry[f"{stream}_log"]
        assert log_path.is_file()
        assert entry[f"{stream}_sha256"] == sha256_file(log_path)
        assert entry[f"{stream}_size"] == log_path.stat().st_size
    assert (run_dir / entry["stdout_log"]).is_file()
    assert (run_dir / entry["stderr_log"]).is_file()

    # -- index verifies and registers every stream ------------------------
    index = json.loads(
        (run_dir / INDEX_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert index["requirement_id"] == "HKL-W3"
    assert index["verdict"] == "pass"
    indexed = {item["path"] for item in index["files"]}
    assert {
        "evidence/gate-report.json",
        "evidence/manifest.json",
        "commands/0.stdout.log",
        "commands/0.stderr.log",
        "landing-plan.json",
    } <= indexed
    verification = verify_evidence(
        run_dir, identity, SRC_DIR, workspace, repo
    )
    assert verification.ok is True, verification.reasons

    # -- landing plan is a plan, never a merge ----------------------------
    landing = json.loads(
        (run_dir / "landing-plan.json").read_text(encoding="utf-8")
    )
    assert landing["verdict"] == "ready"
    assert landing["code_commit"] == document["code_commit"]
    assert landing["main_commit"] == base
    assert repo_head(repo) == base

    # -- journal reaches terminal and still records the child -------------
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["requirement_id"] == "HKL-W3"
    assert state["stage"] == "terminal"
    assert state["child"]["pid"]
    assert state["run_commit"] == document["run_commit"]


def test_invalid_evidence_never_requests_review(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    claim_and_run,
    task_status,
    fake_executor,
    load_run_json,
    monkeypatch,
):
    repo = make_repo()
    executor = fake_executor(SUCCESS_BODY)
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="w3 evidence-invalid")
    card = load_card(write_card(config.work_root, task_id), config)

    import hermes_kanban_lanes.runtime.supervisor as supervisor

    monkeypatch.setattr(
        supervisor,
        "verify_evidence",
        lambda *args, **kwargs: EvidenceVerification(
            False, ("tool-digest-mismatch",), {"tool_sha256": False}
        ),
    )
    _, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=RunSettings(**SETTINGS)
    )

    assert outcome.category == "environment_failure"
    assert outcome.terminal == "block"
    assert outcome.reason == "evidence:tool-digest-mismatch"
    status = task_status(task_id)
    assert status["status"] == "blocked"
    assert status["block_kind"] == "capability"
    document = load_run_json(outcome.run_dir)
    assert document["evidence"]["verified"] is False
    assert document["evidence"]["reasons"] == ["tool-digest-mismatch"]
    assert document["gate"]["verdict"] == "pass"
    # The manifest was written before verification; the plan was not.
    assert (Path(outcome.run_dir) / "evidence" / "manifest.json").is_file()
    assert not (Path(outcome.run_dir) / "landing-plan.json").exists()


def test_gate_timeout_kills_the_group_and_keeps_logs(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    claim_and_run,
    task_status,
    fake_executor,
    load_run_json,
):
    repo = make_repo()
    executor = fake_executor(SUCCESS_BODY)
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="w3 timeout")
    command = (
        "import os,subprocess,sys,time,pathlib;"
        "run=pathlib.Path(os.environ['HOME']).parent;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "run.joinpath('grandchild.pid').write_text(str(child.pid));"
        "print('started', flush=True);"
        "time.sleep(60)"
    )
    card = load_card(
        write_card(
            config.work_root,
            task_id,
            commands=[{"argv": [PYTHON, "-c", command], "cwd": "."}],
        ),
        config,
    )
    settings = RunSettings(**{**SETTINGS, "command_timeout_seconds": 0.4})

    _, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=settings
    )

    assert outcome.category == "code_failure"
    assert outcome.gate_verdict == "fail"
    assert outcome.terminal == "block"
    status = task_status(task_id)
    assert status["status"] == "blocked"
    assert status["block_kind"] == "needs_input"

    run_dir = Path(outcome.run_dir)
    document = load_run_json(run_dir)
    assert document["gate"]["failure_check"] == "commands"
    assert not (run_dir / "evidence" / "manifest.json").exists()
    assert not (run_dir / INDEX_RELATIVE_PATH).exists()

    gate_report = json.loads(
        (run_dir / document["gate"]["report_path"]).read_text(encoding="utf-8")
    )
    (entry,) = _report_checks(gate_report)["commands"]["commands"]
    assert entry["timed_out"] is True
    assert entry["exit_code"] != 0
    # Logs survive the timeout, byte for byte.
    stdout_log = run_dir / entry["stdout_log"]
    assert stdout_log.read_text(encoding="utf-8") == "started\n"
    assert entry["stdout_sha256"] == sha256_file(stdout_log)
    assert entry["stderr_sha256"] == sha256_file(run_dir / entry["stderr_log"])

    pid_file = run_dir / "grandchild.pid"
    assert pid_file.is_file()
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    # No post-hoc wait: ``run_command`` proves the group empty before it
    # returns, so the survivor is already gone here (D10).
    assert not pid_alive(grandchild), "gate command left a live group member"
    assert Path(outcome.workspace).is_dir()


def test_gate_command_group_is_empty_at_return(tmp_path, make_repo):
    """A leader that exits while a descendant lives must not escape the gate."""

    from hermes_kanban_lanes.card_contract import CardCommand, CardSpec
    from hermes_kanban_lanes.verification.gate import run_gate
    from hermes_kanban_lanes.workspace.git import (
        commit_run,
        create_run_worktree,
    )

    repo = make_repo()
    base = repo_head(repo)
    workspace = tmp_path / "worktree"
    create_run_worktree(repo, base, "hkl/group", workspace)
    (workspace / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = commit_run(workspace, "run")
    run_dir = tmp_path / "run_dir"

    command = (
        "import os,pathlib,subprocess,sys;"
        "run=pathlib.Path(os.environ['HOME']).parent;"
        "child=subprocess.Popen("
        "[sys.executable,'-c','import time;time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL);"
        "run.joinpath('grandchild.pid').write_text(str(child.pid), encoding='utf-8');"
        "raise SystemExit(0)"
    )
    card = CardSpec(
        schema_version=1,
        card_kind="implementation",
        lineage="t_gate_group",
        closure_for=None,
        executor="claude",
        repository="primary",
        allow=("RESULT.txt",),
        commands=(CardCommand(argv=(PYTHON, "-c", command), cwd="."),),
        risk="normal",
        artifacts=(),
    )

    identity = RunIdentity(
        schema_version=1, task_id="t_gate_group", run_id=1, claim_lock="lock"
    )
    gate = run_gate(
        card,
        workspace,
        base,
        run_dir,
        identity=identity,
        run_commit=run_commit,
        sync_base=base,
    )

    assert gate.verdict == "pass", gate.report
    (entry,) = _report_checks(gate.report)["commands"]["commands"]
    assert entry["exit_code"] == 0
    grandchild = int((run_dir / "grandchild.pid").read_text(encoding="utf-8"))
    # No post-hoc wait: the group is already empty when ``run_gate`` returns.
    assert not pid_alive(grandchild), "run_gate returned with a live group member"
    try:
        os.killpg(grandchild, 0)
    except ProcessLookupError:
        pass
    else:  # pragma: no cover - the assertion above is the real witness
        raise AssertionError("process group still exists at return")
