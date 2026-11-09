"""W2-01/08(fake)/09/10/11: supervisor and daemon lifecycle."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.config import LaneConfig
from hermes_kanban_lanes.executors.claude import ClaudeAdapter
from hermes_kanban_lanes.runtime import daemon
from hermes_kanban_lanes.runtime.supervisor import RunSettings
from hermes_kanban_lanes.verification.evidence import (
    sha256_file,
    tool_digest,
    verify_gate_report,
)
from hermes_kanban_lanes.workspace.git import repo_head, run_git

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src" / "hermes_kanban_lanes"
PYTHON = sys.executable

DEFAULT_SETTINGS = dict(
    max_runtime_seconds=30,
    heartbeat_seconds=30,
    poll_seconds=0.05,
    grace_seconds=1,
    kill_grace_seconds=1,
)


def _config_file(tmp_path: Path, config: LaneConfig) -> Path:
    path = tmp_path / "lane-config.json"
    path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
    return path


def _success_script(fake_executor, board_path: str) -> str:
    body = f"""
BOARD_PATH = {board_path!r}
run_dir = Path(os.environ["HOME"]).parent
identity = json.loads((run_dir / "identity.json").read_text(encoding="utf-8"))
obs = {{
    "hermes_env": sorted(k for k in os.environ if k.startswith("HERMES_")),
    "board_path_in_env": any(BOARD_PATH in str(v) for v in os.environ.values()),
    "hermes_cli_importable": False,
    "env_keys": sorted(k for k in os.environ if not k.startswith("ANTHROPIC_")),
    "cwd": os.getcwd(),
}}
try:
    import hermes_cli  # noqa: F401
    obs["hermes_cli_importable"] = True
except Exception:
    pass
Path("obs.json").write_text(json.dumps(obs), encoding="utf-8")
Path("RESULT.txt").write_text("ok\\n", encoding="utf-8")
intent = {{
    "schema_version": 1,
    "intent_id": "fake-comment-1",
    "kind": "comment",
    "task_id": identity["task_id"],
    "run_id": identity["run_id"],
    "claim_lock": identity["claim_lock"],
    "payload": {{"body": "comment from executor"}},
}}
intents = run_dir / "intents"
temporary = intents / ".comment.json.tmp"
temporary.write_text(json.dumps(intent), encoding="utf-8")
os.replace(temporary, intents / "comment.json")
print(json.dumps({{
    "is_error": False,
    "result": "done",
    "terminal_reason": "completed",
    "usage": {{"input_tokens": 10, "output_tokens": 5}},
    "total_cost_usd": 0.01,
    "permission_denials": [{{"tool_name": "Bash"}}],
}}))
"""
    return fake_executor(body)


def _obs_command() -> dict:
    code = (
        "import json,pathlib,sys;"
        "o=json.loads(pathlib.Path('obs.json').read_text());"
        "sys.exit(0 if not o['hermes_env'] and not o['board_path_in_env'] "
        "and not o['hermes_cli_importable'] else 1)"
    )
    return {"argv": [PYTHON, "-c", code], "cwd": "."}


def test_success_path_minimal_env_intent_and_evidence(
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
    executor = _success_script(fake_executor, board.board_path)
    config = make_config(
        board_path=board.board_path, repo_path=repo, executor_argv=[executor]
    )
    task_id = make_task(title="success")
    base_commit = repo_head(repo)
    card = load_card(
        write_card(
            config.work_root,
            task_id,
            allow=("RESULT.txt", "obs.json"),
            commands=[_obs_command()],
        ),
        config,
    )

    identity, outcome = claim_and_run(
        config=config,
        card=card,
        task_id=task_id,
        settings=RunSettings(**DEFAULT_SETTINGS),
    )

    assert outcome.category == "success", outcome.reason
    assert outcome.gate_verdict == "pass"
    assert outcome.terminal == "request_review"
    assert task_status(task_id)["status"] == "review"

    run_dir = Path(outcome.run_dir)
    observation = json.loads((Path(outcome.workspace) / "obs.json").read_text("utf-8"))
    assert observation["hermes_env"] == []
    assert observation["board_path_in_env"] is False
    assert observation["hermes_cli_importable"] is False
    expected_env = {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
        "TMPDIR",
    }
    observed = set(observation["env_keys"])
    assert expected_env <= observed
    # macOS may add its own text-encoding variable; nothing else is allowed.
    assert observed - expected_env <= {"__CF_USER_TEXT_ENCODING"}
    assert Path(observation["cwd"]).resolve() == Path(outcome.workspace).resolve()

    comment = board.connection.execute(
        "SELECT author, body FROM task_comments WHERE task_id = ?", (task_id,)
    ).fetchone()
    assert (comment["author"], comment["body"]) == ("hkl-executor", "comment from executor")

    run_json_path = run_dir / "run.json"
    document = load_run_json(run_dir)
    assert document["requirement_id"] == "HKL-W3"
    assert document["identity"] == identity.to_dict()
    assert document["category"] == "success"
    assert document["gate"]["verdict"] == "pass"
    assert document["cleanup"]["terminated"] is True
    assert document["cleanup"]["residual_pids"] == []
    assert document["intents"][0]["ok"] is True
    assert not any(
        key.startswith("HERMES_") for key in document["executor"]["env_keys"]
    )
    assert all(
        key.startswith("ANTHROPIC_")
        for key in document["executor"]["credential_keys"]
    )
    # ``removed_env_keys`` is a redacted audit of what the host exported; it
    # records names only, never values.
    removed = document["executor"]["removed_env_keys"]
    assert isinstance(removed, list)
    assert all(isinstance(key, str) for key in removed)

    # Credential values never leak into any evidence file.
    credentials, _ = ClaudeAdapter().credential_env()
    run_bytes = run_json_path.read_text(encoding="utf-8")
    for value in credentials.values():
        assert value not in run_bytes

    manifest_path = run_dir / "evidence" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gate_report_path = run_dir / manifest["gate_report"]["path"]
    assert manifest["gate_report"]["sha256"] == sha256_file(gate_report_path)
    assert manifest["tool_sha256"] == tool_digest(SRC_DIR)
    assert manifest["identity"] == identity.to_dict()
    assert manifest["base_commit"] == base_commit
    assert manifest["code_commit"] == document["code_commit"]
    assert verify_gate_report(
        run_dir, manifest["gate_report"]["path"], manifest["gate_report"]["sha256"]
    ) == (True, "ok")

    changed = run_git(["cat-file", "-e", manifest["code_commit"]], cwd=outcome.workspace)
    assert changed.returncode == 0
    assert document["changed_paths"] == ["RESULT.txt", "obs.json"]


def test_gate_scope_failure_blocks_without_request_review(
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
    executor = fake_executor(
        """
Path("RESULT.txt").write_text("ok\\n")
Path("outside.txt").write_text("not allowed\\n")
print(json.dumps({"is_error": False, "result": "done"}))
"""
    )
    config = make_config(board_path=board.board_path, repo_path=repo, executor_argv=[executor])
    task_id = make_task(title="scope")
    card = load_card(write_card(config.work_root, task_id, allow=("RESULT.txt",)), config)
    _, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=RunSettings(**DEFAULT_SETTINGS)
    )
    assert outcome.category == "code_failure"
    assert outcome.gate_verdict == "fail"
    assert outcome.terminal == "block"
    assert task_status(task_id)["status"] == "blocked"
    assert task_status(task_id)["block_kind"] == "needs_input"
    report = load_run_json(outcome.run_dir)
    assert report["gate"]["verdict"] == "fail"
    assert report["gate"]["failure_check"] == "scope"
    assert not (Path(outcome.run_dir) / "evidence" / "manifest.json").exists()
    assert Path(outcome.workspace).is_dir()


def test_gate_command_failure_blocks(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    claim_and_run,
    fake_executor,
    task_status,
):
    repo = make_repo()
    executor = fake_executor(
        """
Path("RESULT.txt").write_text("ok\\n")
print(json.dumps({"is_error": False, "result": "done"}))
"""
    )
    config = make_config(board_path=board.board_path, repo_path=repo, executor_argv=[executor])
    task_id = make_task(title="command")
    card = load_card(
        write_card(
            config.work_root,
            task_id,
            commands=[{"argv": [PYTHON, "-c", "raise SystemExit(7)"], "cwd": "."}],
        ),
        config,
    )
    _, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=RunSettings(**DEFAULT_SETTINGS)
    )
    assert outcome.category == "code_failure"
    assert outcome.gate_verdict == "fail"
    assert task_status(task_id)["status"] == "blocked"
    assert Path(outcome.workspace).is_dir()


@pytest.mark.parametrize(
    ("body", "expected_category", "expected_kind"),
    [
        (
            'print(json.dumps({"is_error": True, "result": "Not logged in", '
            '"terminal_reason": "api_error"}))',
            "environment_failure",
            "capability",
        ),
        (
            'print(json.dumps({"is_error": True, "result": "rate limit exceeded"}))',
            "environment_failure",
            "transient",
        ),
        ("raise SystemExit(2)", "code_failure", "needs_input"),
    ],
)
def test_failure_classification_matrix(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    claim_and_run,
    fake_executor,
    task_status,
    body,
    expected_category,
    expected_kind,
):
    repo = make_repo()
    executor = fake_executor(body + "\n")
    config = make_config(board_path=board.board_path, repo_path=repo, executor_argv=[executor])
    task_id = make_task(title="classification")
    card = load_card(write_card(config.work_root, task_id), config)
    _, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=RunSettings(**DEFAULT_SETTINGS)
    )
    assert outcome.category == expected_category
    assert outcome.terminal == "block"
    status = task_status(task_id)
    assert status["status"] == "blocked"
    assert status["block_kind"] == expected_kind
    assert Path(outcome.workspace).is_dir()


def test_heartbeat_loss_writes_nothing(
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    claim_and_run,
    fake_executor,
    monkeypatch,
):
    repo = make_repo()
    executor = fake_executor("time.sleep(60)\n")
    config = make_config(board_path=board.board_path, repo_path=repo, executor_argv=[executor])
    task_id = make_task(title="lost")
    card = load_card(write_card(config.work_root, task_id), config)

    def lost_heartbeat(identity):
        return False

    monkeypatch.setattr(board, "heartbeat", lost_heartbeat)
    identity, outcome = claim_and_run(
        config=config,
        card=card,
        task_id=task_id,
        settings=RunSettings(
            max_runtime_seconds=30,
            heartbeat_seconds=0.2,
            poll_seconds=0.05,
            grace_seconds=0.3,
            kill_grace_seconds=1,
        ),
    )
    assert outcome.category == "cancelled"
    assert outcome.terminal is None
    assert outcome.reason == "heartbeat_lost"
    row = board.connection.execute(
        "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["status"] == "running"
    assert int(row["current_run_id"]) == identity.run_id
    assert row["claim_lock"] == identity.claim_lock
    assert (
        board.connection.execute(
            "SELECT count(*) FROM task_comments WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == 0
    )
    assert outcome.cleanup["terminated"] is True
    board.reclaim(identity, "test-cleanup")


def test_evidence_integrity_helpers_are_explicit(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert verify_gate_report(run_dir, "evidence/gate-report.json", "0" * 64) == (
        False,
        "gate-report-missing",
    )
    (run_dir / "evidence").mkdir()
    (run_dir / "evidence" / "gate-report.json").write_text("", encoding="utf-8")
    assert verify_gate_report(run_dir, "evidence/gate-report.json", "0" * 64) == (
        False,
        "gate-report-empty",
    )
    (run_dir / "evidence" / "gate-report.json").write_text("{}\n", encoding="utf-8")
    assert verify_gate_report(run_dir, "evidence/gate-report.json", "0" * 64) == (
        False,
        "gate-report-digest-mismatch",
    )
    digest = sha256_file(run_dir / "evidence" / "gate-report.json")
    assert verify_gate_report(run_dir, "evidence/gate-report.json", digest) == (True, "ok")


def test_replaced_gate_report_never_requests_review(
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
    monkeypatch,
):
    repo = make_repo()
    executor = fake_executor(
        'Path("RESULT.txt").write_text("ok\\n")\n'
        'print(json.dumps({"is_error": False, "result": "done"}))\n'
    )
    config = make_config(board_path=board.board_path, repo_path=repo, executor_argv=[executor])
    task_id = make_task(title="tampered")
    card = load_card(write_card(config.work_root, task_id), config)

    import hermes_kanban_lanes.runtime.supervisor as supervisor

    monkeypatch.setattr(
        supervisor,
        "verify_gate_report",
        lambda run_dir, path, expected: (False, "gate-report-digest-mismatch"),
    )
    _, outcome = claim_and_run(
        config=config, card=card, task_id=task_id, settings=RunSettings(**DEFAULT_SETTINGS)
    )
    assert outcome.category == "environment_failure"
    assert outcome.terminal == "block"
    assert outcome.reason == "evidence:gate-report-digest-mismatch"
    assert task_status(task_id)["status"] == "blocked"
    report = load_run_json(outcome.run_dir)
    assert report["gate"]["verdict"] == "blocked"


def test_preflight_failures_are_explicit(
    tmp_path, board, make_repo, make_config, hermes_commit
):
    repo = make_repo()
    executor = str(tmp_path / "does-not-exist")
    valid = make_config(board_path=board.board_path, repo_path=repo, executor_argv=[executor])
    assert "executor:claude:executor-not-resolvable" in daemon.preflight(valid)

    mismatch = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[PYTHON],
        hermes_commit_override="0" * 40,
    )
    assert "hermes-commit-mismatch" in daemon.preflight(mismatch)

    missing_root = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[PYTHON],
        work_root=tmp_path / "missing-work",
        create_work_root=False,
    )
    errors = daemon.preflight(missing_root)
    assert "work-root-missing" in errors
    assert not (tmp_path / "missing-work").exists()

    dirty_repo = make_repo("dirty-repo")
    (dirty_repo / "extra.txt").write_text("x", encoding="utf-8")
    dirty = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[PYTHON],
        repo_path_override=dirty_repo,
    )
    # Uncommitted WIP no longer blocks startup/claiming; the merge gate keeps
    # its own cleanliness check (release:workspace-dirty).
    assert "repository:primary:repository-dirty" not in daemon.preflight(dirty)

    missing_board = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[PYTHON],
        board_path_override=tmp_path / "missing.db",
    )
    assert "board:test:board-missing" in daemon.preflight(missing_board)
    assert not (tmp_path / "missing.db").exists()


def test_preflight_allows_dirty_repository(tmp_path, board, make_repo, make_config):
    """W9: a team checkout with WIP must still start and claim.

    HKL reads ``HEAD`` and runs in its own per-run worktree, so uncommitted
    files are not a startup error.  Missing/non-git/no-HEAD stay fatal (the
    existing failure test covers those).
    """

    repo = make_repo("dirty-start")
    (repo / "WIP.txt").write_text("wip\n", encoding="utf-8")
    config = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[PYTHON],
    )

    assert daemon.preflight(config) == []


def test_main_returns_two_on_preflight_failure(
    tmp_path, board, make_repo, make_config, capsys
):
    repo = make_repo()
    config = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[str(tmp_path / "missing-executor")],
    )
    path = _config_file(tmp_path, config)
    assert daemon.main(["--config", str(path), "--once"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["ok"] is False


def test_tick_skips_missing_and_invalid_cards_without_claiming(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    task_status,
):
    repo = make_repo()
    executor = fake_executor('Path("RESULT.txt").write_text("ok\\n")\n')
    config = make_config(board_path=board.board_path, repo_path=repo, executor_argv=[executor])
    missing_task = make_task(title="missing card")
    report = daemon.tick(config, adapters={"test": board})
    assert any(
        entry["task_id"] == missing_task and entry["reason"] == "card-missing"
        for entry in report["skipped"]
    )
    assert task_status(missing_task)["status"] == "ready"

    invalid_task = make_task(title="bad lineage")
    write_card(config.work_root, invalid_task, lineage="some-other-task")
    report = daemon.tick(config, adapters={"test": board})
    assert any(
        entry["task_id"] == invalid_task and entry["reason"] == "card-lineage-mismatch"
        for entry in report["skipped"]
    )
    assert task_status(invalid_task)["status"] == "ready"

    bad_repo_task = make_task(title="bad repo")
    write_card(config.work_root, bad_repo_task, repository="secondary")
    report = daemon.tick(config, adapters={"test": board})
    assert any(
        entry["task_id"] == bad_repo_task and entry["reason"] == "card-invalid"
        for entry in report["skipped"]
    )
    assert task_status(bad_repo_task)["status"] == "ready"


def test_tick_once_runs_to_review(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    task_status,
    capsys,
):
    repo = make_repo()
    executor = fake_executor(
        'Path("RESULT.txt").write_text("ok\\n")\n'
        'print(json.dumps({"is_error": False, "result": "done"}))\n'
    )
    config = make_config(board_path=board.board_path, repo_path=repo, executor_argv=[executor])
    task_id = make_task(title="tick")
    write_card(config.work_root, task_id)
    path = _config_file(tmp_path, config)

    assert daemon.main(["--config", str(path), "--once"]) == 0
    output = capsys.readouterr().out.strip().splitlines()[-1]
    report = json.loads(output)
    assert len(report["claimed"]) == 1
    assert report["runs"][0]["category"] == "success"
    assert task_status(task_id)["status"] == "review"
    log = Path(config.work_root) / "logs" / "daemon.log"
    assert log.is_file()
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["tick"] == 0
