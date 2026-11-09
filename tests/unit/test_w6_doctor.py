"""W6 D5: doctor positive/negative, read-only and credential-safe."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_kanban_lanes import __version__
from hermes_kanban_lanes import cli
from hermes_kanban_lanes.config import LaneConfig


def _write_config(config: LaneConfig, path: Path) -> str:
    path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
    return str(path)


def _fake_hermes_source(tmp_path: Path) -> tuple[str, str]:
    """A git checkout whose host import chain needs a missing package."""

    source = tmp_path / "fake-hermes"
    package = source / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "kanban_db.py").write_text(
        "import hkl_missing_host_dependency_xyz\n", encoding="utf-8"
    )
    (package / "kanban_db_connect.py").write_text("", encoding="utf-8")
    (package / "kanban_db_dispatch.py").write_text("", encoding="utf-8")
    git = ["git", "-C", str(source)]
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run([*git, "config", "user.email", "hkl@example.invalid"], check=True)
    subprocess.run([*git, "config", "user.name", "HKL Tests"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "fake host"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return str(source), head


def _write_run_state(run_dir: Path, *, stage: str) -> None:
    state = {
        "schema_version": 1,
        "requirement_id": "HKL-W3",
        "identity": {
            "schema_version": 1,
            "task_id": "task",
            "run_id": 1,
            "claim_lock": "claim",
        },
        "board": "test",
        "task_id": "task",
        "run_id": 1,
        "claim_lock": "claim",
        "run_dir": str(run_dir),
        "workspace": "",
        "stage": stage,
        "child": None,
        "round": 1,
        "updated_at": 0,
    }
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


@pytest.fixture
def doctor_env(tmp_path, monkeypatch):
    """A hermetic HOME with one Claude credential and no Codex state."""

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_API_KEY": "sk-ant-unit-test-secret-123456",
                    "ANTHROPIC_BASE_URL": "https://example.invalid",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_distribution_version", lambda: __version__)
    return home


def _config(tmp_path, make_config, board, repo, fake_executor, **overrides):
    return make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[fake_executor("")],
        **overrides,
    )


def _checks(report):
    return {check["name"]: check for check in report["checks"]}


def test_doctor_all_ok(tmp_path, doctor_env, make_config, board, make_repo, fake_executor, capsys):
    repo = make_repo()
    config = _config(tmp_path, make_config, board, repo, fake_executor)
    config_path = _write_config(config, tmp_path / "config.json")

    report = cli.doctor_report(config, config_path)
    assert report["ok"] is True
    assert [check["name"] for check in report["checks"]] == [
        "package",
        "platform",
        "host-hermes",
        "board:test",
        "workspace:primary",
        "executor:claude",
        "credential:claude",
        "lock",
        "service",
        "logs",
    ]
    assert report["version"] == __version__
    assert report["platform"]["sys_platform"]
    assert report["lock"]["state"] == "free"
    assert report["next_steps"] == []
    assert all(check["ok"] for check in report["checks"])

    assert main_doctor(config_path, capsys) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["checks"] == report["checks"]


def main_doctor(config_path, capsys) -> int:
    return cli.main(["doctor", "--config", config_path, "--json"])


def test_workspace_dirty_is_reported_not_failed(
    tmp_path, doctor_env, make_config, board, make_repo, fake_executor
):
    """W9: daily WIP is reported as ``dirty=true``, not a failed check."""

    repo = make_repo()
    (repo / "WIP.txt").write_text("wip\n", encoding="utf-8")
    config = _config(tmp_path, make_config, board, repo, fake_executor)

    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    check = _checks(report)["workspace:primary"]
    assert check["ok"] is True
    assert "dirty=true" in check["detail"]
    assert report["ok"] is True

    # A non-git directory is still a hard failure.
    not_git = tmp_path / "not-git"
    not_git.mkdir()
    bad_config = make_config(
        board_path=board.board_path,
        repo_path=not_git,
        executor_argv=[fake_executor("")],
    )
    bad_report = cli.doctor_report(bad_config, str(tmp_path / "bad.json"))
    bad_check = _checks(bad_report)["workspace:primary"]
    assert bad_check["ok"] is False
    assert "repository-not-a-worktree" in bad_check["detail"]


def test_doctor_failure_exits_two_with_independent_checks(
    tmp_path, doctor_env, make_config, board, make_repo, fake_executor, capsys
):
    repo = make_repo()
    config = _config(
        tmp_path,
        make_config,
        board,
        repo,
        fake_executor,
        hermes_commit_override="b" * 40,
    )
    config_path = _write_config(config, tmp_path / "config.json")

    assert main_doctor(config_path, capsys) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    failing = [check for check in payload["checks"] if not check["ok"]]
    assert [check["name"] for check in failing] == ["host-hermes"]
    assert "hermes-commit-mismatch" in failing[0]["detail"]
    assert "git -C" in failing[0]["detail"]
    assert payload["next_steps"] == [failing[0]["detail"]]
    assert payload["reason"] == "hermes-commit-mismatch"
    assert payload["impact"]


def test_doctor_missing_host_dependency_fails_with_actionable_diagnostic(
    tmp_path, doctor_env, make_config, board, make_repo, fake_executor, capsys
):
    source, head = _fake_hermes_source(tmp_path)
    repo = make_repo()
    config = _config(
        tmp_path,
        make_config,
        board,
        repo,
        fake_executor,
        hermes_source_override=source,
        hermes_commit_override=head,
    )
    config_path = _write_config(config, tmp_path / "config.json")

    assert main_doctor(config_path, capsys) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    checks = {check["name"]: check for check in payload["checks"]}
    detail = checks["host-hermes"]["detail"]
    assert checks["host-hermes"]["ok"] is False
    assert (
        "host-dependency-missing:hkl_missing_host_dependency_xyz" in detail
    )
    assert "same Python environment" in detail
    assert payload["reason"].startswith("host-dependency-missing")
    assert payload["impact"]
    assert any(
        "same Python environment" in step for step in payload["next_steps"]
    )


@pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS systemd promise boundary"
)
def test_doctor_systemd_on_macos_reports_render_only(
    tmp_path, doctor_env, make_config, board, make_repo, fake_executor
):
    repo = make_repo()
    base = _config(tmp_path, make_config, board, repo, fake_executor)
    document = base.to_dict()
    document["service"] = {"platform": "systemd"}
    config = LaneConfig.from_dict(document)

    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    checks = _checks(report)
    detail = checks["service"]["detail"]
    assert checks["service"]["ok"] is False
    assert "service-platform-render-only:systemd" in detail
    assert "service render" in detail
    # The doctor must never recommend installing a systemd service here.
    assert "service install --platform systemd" not in detail
    assert report["reason"] == "service-platform-render-only:systemd"
    assert report["impact"]
    assert any("service render" in step for step in report["next_steps"])


def test_doctor_bad_board_path(tmp_path, doctor_env, make_config, board, make_repo, fake_executor):
    repo = make_repo()
    config = _config(
        tmp_path,
        make_config,
        board,
        repo,
        fake_executor,
        board_path_override=tmp_path / "missing.db",
    )
    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    checks = _checks(report)
    assert checks["board:test"]["ok"] is False
    assert "board-missing" in checks["board:test"]["detail"]
    assert all(
        check["ok"] for name, check in checks.items() if name != "board:test"
    )


def test_doctor_missing_executor_binary(tmp_path, doctor_env, make_config, board, make_repo, fake_executor):
    repo = make_repo()
    config = _config(
        tmp_path,
        make_config,
        board,
        repo,
        fake_executor,
        executor_argv_override=[str(tmp_path / "missing-executor")],
    )
    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    checks = _checks(report)
    assert checks["executor:claude"]["ok"] is False
    assert "executor-not-resolvable" in checks["executor:claude"]["detail"]


def test_doctor_version_unavailable(tmp_path, doctor_env, make_config, board, make_repo):
    repo = make_repo()
    binary = tmp_path / "silent-exec"
    binary.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    binary.chmod(0o755)
    config = _config(
        tmp_path,
        make_config,
        board,
        repo,
        lambda body, name=None: str(binary),
        executor_argv_override=[str(binary)],
    )
    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    checks = _checks(report)
    assert checks["executor:claude"]["ok"] is False
    assert "executor-version-unavailable" in checks["executor:claude"]["detail"]


def test_doctor_missing_credentials(tmp_path, doctor_env, make_config, board, make_repo, fake_executor):
    (doctor_env / ".claude" / "settings.json").unlink()
    repo = make_repo()
    config = _config(tmp_path, make_config, board, repo, fake_executor)
    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    checks = _checks(report)
    assert checks["credential:claude"]["ok"] is False
    assert "claude-settings-missing" in checks["credential:claude"]["detail"]
    assert "hermes-kanban-lanes doctor" in checks["credential:claude"]["detail"]


def test_doctor_package_version_mismatch(monkeypatch, tmp_path, doctor_env, make_config, board, make_repo, fake_executor):
    monkeypatch.setattr(cli, "_distribution_version", lambda: "9.9.9")
    repo = make_repo()
    config = _config(tmp_path, make_config, board, repo, fake_executor)
    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    checks = _checks(report)
    assert checks["package"]["ok"] is False
    assert "package-version-mismatch" in checks["package"]["detail"]


def test_doctor_package_metadata_missing(monkeypatch, tmp_path, doctor_env, make_config, board, make_repo, fake_executor):
    monkeypatch.setattr(cli, "_distribution_version", lambda: None)
    repo = make_repo()
    config = _config(tmp_path, make_config, board, repo, fake_executor)
    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    checks = _checks(report)
    assert checks["package"]["ok"] is False
    assert "package-metadata-missing" in checks["package"]["detail"]


def test_doctor_lock_states(monkeypatch, tmp_path, doctor_env, make_config, board, make_repo, fake_executor):
    from hermes_kanban_lanes.runtime import daemon

    repo = make_repo()
    config = _config(tmp_path, make_config, board, repo, fake_executor)

    monkeypatch.setattr(
        daemon,
        "probe_daemon_lock",
        lambda work_root: {"state": "held", "holder": {"pid": 321, "version": "0.1.0"}},
    )
    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    assert _checks(report)["lock"]["ok"] is True
    assert "held pid=321" in _checks(report)["lock"]["detail"]
    assert report["lock"]["holder"]["pid"] == 321

    monkeypatch.setattr(
        daemon,
        "probe_daemon_lock",
        lambda work_root: {"state": "unsupported", "holder": None},
    )
    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    assert _checks(report)["lock"]["ok"] is False
    assert "daemon-lock-unsupported" in _checks(report)["lock"]["detail"]


def test_doctor_service_not_installed(tmp_path, doctor_env, make_config, board, make_repo, fake_executor, monkeypatch):
    from hermes_kanban_lanes.runtime import service

    monkeypatch.setattr(
        service,
        "default_runner",
        lambda argv, timeout=30.0: service.RunnerResult(tuple(argv), 1),
    )
    repo = make_repo()
    base = _config(tmp_path, make_config, board, repo, fake_executor)
    document = base.to_dict()
    document["service"] = {"platform": "launchd", "label": "com.example.hkl.test"}
    config = LaneConfig.from_dict(document)

    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    checks = _checks(report)
    assert checks["service"]["ok"] is False
    assert "service-not-installed" in checks["service"]["detail"]
    assert "service install --platform launchd" in checks["service"]["detail"]


def test_doctor_service_running(tmp_path, doctor_env, make_config, board, make_repo, fake_executor, monkeypatch):
    from hermes_kanban_lanes.runtime import service

    repo = make_repo()
    base = _config(tmp_path, make_config, board, repo, fake_executor)
    document = base.to_dict()
    document["service"] = {"platform": "launchd", "label": "com.example.hkl.test"}
    config = LaneConfig.from_dict(document)

    monkeypatch.setattr(
        service,
        "default_runner",
        lambda argv, timeout=30.0: service.RunnerResult(tuple(argv), 0),
    )
    unit_dir = doctor_env / "Library" / "LaunchAgents"
    unit_dir.mkdir(parents=True)
    (unit_dir / "com.example.hkl.test.plist").write_text("x", encoding="utf-8")

    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    checks = _checks(report)
    assert checks["service"]["ok"] is True
    assert checks["service"]["detail"] == "installed=true running=true"


def test_doctor_service_not_running(tmp_path, doctor_env, make_config, board, make_repo, fake_executor, monkeypatch):
    from hermes_kanban_lanes.runtime import service

    repo = make_repo()
    base = _config(tmp_path, make_config, board, repo, fake_executor)
    document = base.to_dict()
    document["service"] = {"platform": "launchd", "label": "com.example.hkl.test"}
    config = LaneConfig.from_dict(document)

    monkeypatch.setattr(
        service,
        "default_runner",
        lambda argv, timeout=30.0: service.RunnerResult(tuple(argv), 1),
    )
    unit_dir = doctor_env / "Library" / "LaunchAgents"
    unit_dir.mkdir(parents=True)
    (unit_dir / "com.example.hkl.test.plist").write_text("x", encoding="utf-8")

    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    checks = _checks(report)
    assert checks["service"]["ok"] is False
    assert "service-not-running" in checks["service"]["detail"]


def test_doctor_service_runtime_check(
    tmp_path, doctor_env, make_config, board, make_repo, fake_executor, monkeypatch
):
    """D5/D6: the service-runtime check probes the resolved launch interpreter."""

    from hermes_kanban_lanes.runtime import service

    repo = make_repo()
    base = _config(tmp_path, make_config, board, repo, fake_executor)
    config_path = str(tmp_path / "config.json")

    # service.platform null: the check is absent.
    report = cli.doctor_report(base, config_path)
    assert "service-runtime" not in _checks(report)

    document = base.to_dict()
    document["service"] = {
        "platform": "launchd",
        "label": "com.example.hkl.runtime",
    }
    config = LaneConfig.from_dict(document)

    def fake_runner(argv):
        args = [str(part) for part in argv]
        if "--print-runtime-command" in args:
            tail = args[args.index("--") + 1:]
            payload = [sys.executable, "-I", "-c", "bootstrap", *tail]
            return service.RunnerResult(
                tuple(args), 0, json.dumps(payload)
            )
        if args and args[0] == sys.executable:
            return service.RunnerResult(
                tuple(args), 0, 'HKL-HOST-PROBE:{"ok": true}\n'
            )
        return service.RunnerResult(tuple(args), 1)

    monkeypatch.setattr(service, "default_runner", fake_runner)
    report = cli.doctor_report(config, config_path)
    check = _checks(report)["service-runtime"]
    assert check["ok"] is True
    assert "host imports ok" in check["detail"]

    # Launcher missing: false, and the detail names the reason and rerun path.
    source, head = _fake_hermes_source(tmp_path)
    missing_document = base.to_dict()
    missing_document["hermes_source"] = source
    missing_document["hermes_commit"] = head
    missing_document["service"] = {"platform": "launchd"}
    missing = LaneConfig.from_dict(missing_document)
    report = cli.doctor_report(missing, config_path)
    check = _checks(report)["service-runtime"]
    assert check["ok"] is False
    assert "service-launcher-missing" in check["detail"]
    assert "hermes-kanban-lanes doctor --config" in check["detail"]


def test_doctor_logs_reports_size_rotation_and_last_error(tmp_path, doctor_env, make_config, board, make_repo, fake_executor):
    repo = make_repo()
    config = _config(tmp_path, make_config, board, repo, fake_executor)
    logs = Path(config.work_root) / "logs"
    logs.mkdir(parents=True)
    (logs / "daemon.log").write_text(
        json.dumps({"tick": 1, "errors": [], "note": "x" * 32}) + "\n"
        + json.dumps({"tick": 2, "errors": ["board:test:boom"]}) + "\n",
        encoding="utf-8",
    )
    (logs / "daemon.log.1").write_text("{}\n", encoding="utf-8")

    old_run = Path(config.work_root) / "runs" / "t1" / "r1"
    old_run.mkdir(parents=True)
    _write_run_state(old_run, stage="terminal")
    (old_run / "run.json").write_text(
        json.dumps(
            {
                "finished_at": int(time.time()) - 40 * 86400,
                "category": "success",
            }
        ),
        encoding="utf-8",
    )
    live_run = Path(config.work_root) / "runs" / "t2" / "r1"
    live_run.mkdir(parents=True)
    _write_run_state(live_run, stage="claimed")

    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    detail = json.loads(_checks(report)["logs"]["detail"])
    assert detail["exists"] is True
    assert detail["size_bytes"] > 0
    assert detail["rotated"] == 1
    assert detail["last_error"]["errors"] == ["board:test:boom"]
    assert detail["retention_days"] == 14
    assert detail["prunable_runs"] == 1
    assert detail["skipped_runs"] == 1
    # doctor reports the plan; it must not delete the eligible run.
    assert old_run.is_dir() and live_run.is_dir()


def _read_only_snapshot(board_path, config_path, work_root):
    """Everything the ``doctor`` command must leave byte/mtime/entry identical."""

    board = Path(board_path)
    config_file = Path(config_path)
    root = Path(work_root)
    return {
        "board_bytes": board.read_bytes(),
        "board_mtime_ns": board.stat().st_mtime_ns,
        "config_bytes": config_file.read_bytes(),
        "config_mtime_ns": config_file.stat().st_mtime_ns,
        "work_root_entries": sorted(
            str(path.relative_to(root)) for path in root.rglob("*")
        ),
        "daemon_lock_exists": (root / "daemon.lock").exists(),
        "logs_exists": (root / "logs").exists(),
    }


def _assert_read_only_snapshot_unchanged(snapshot, board_path, config_path, work_root):
    after = _read_only_snapshot(board_path, config_path, work_root)
    assert after["board_bytes"] == snapshot["board_bytes"]
    assert after["board_mtime_ns"] == snapshot["board_mtime_ns"]
    assert after["config_bytes"] == snapshot["config_bytes"]
    assert after["config_mtime_ns"] == snapshot["config_mtime_ns"]
    assert after["work_root_entries"] == snapshot["work_root_entries"]
    assert after["daemon_lock_exists"] == snapshot["daemon_lock_exists"]
    assert after["logs_exists"] == snapshot["logs_exists"]
    assert after["daemon_lock_exists"] is False
    assert after["logs_exists"] is False


def test_doctor_command_is_read_only(
    tmp_path, doctor_env, make_config, board, make_repo, fake_executor, capsys
):
    """D-1: the ``main`` → ``_doctor`` command entry is genuinely read-only.

    Deliberately goes through ``cli.main`` (not ``doctor_report``): a write
    injected into ``_doctor`` must turn this test red.
    """

    repo = make_repo()
    config = _config(tmp_path, make_config, board, repo, fake_executor)
    config_path = _write_config(config, tmp_path / "config.json")
    work_root = Path(config.work_root)
    before = _read_only_snapshot(board.board_path, config_path, work_root)

    assert cli.main(["doctor", "--config", config_path, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True

    _assert_read_only_snapshot_unchanged(
        before, board.board_path, config_path, work_root
    )
    status = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""


def test_doctor_command_failure_path_is_read_only(
    tmp_path, doctor_env, make_config, board, make_repo, fake_executor, capsys
):
    """D-1: a failing ``main doctor`` (bad hermes commit) writes nothing either."""

    repo = make_repo()
    config = _config(
        tmp_path,
        make_config,
        board,
        repo,
        fake_executor,
        hermes_commit_override="b" * 40,
    )
    config_path = _write_config(config, tmp_path / "config.json")
    work_root = Path(config.work_root)
    before = _read_only_snapshot(board.board_path, config_path, work_root)

    assert cli.main(["doctor", "--config", config_path, "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    failing = [check for check in payload["checks"] if not check["ok"]]
    assert [check["name"] for check in failing] == ["host-hermes"]

    _assert_read_only_snapshot_unchanged(
        before, board.board_path, config_path, work_root
    )
    status = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""


def test_doctor_never_prints_credential_values(
    tmp_path, doctor_env, make_config, board, make_repo, fake_executor
):
    repo = make_repo()
    config = _config(tmp_path, make_config, board, repo, fake_executor)
    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    serialized = json.dumps(report)
    assert "sk-ant-unit-test-secret-123456" not in serialized
    assert "https://example.invalid" not in serialized
    # Key names are allowed; values are not.
    assert "ANTHROPIC_API_KEY" in _checks(report)["credential:claude"]["detail"]


def test_doctor_codex_credential_reports_key_name_not_value(
    tmp_path, doctor_env, make_config, board, make_repo, monkeypatch
):
    monkeypatch_value = "sk-openai-unit-test-secret-987654"
    monkeypatch.setenv("OPENAI_API_KEY", monkeypatch_value)
    repo = make_repo()
    python = sys.executable
    config = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[python],
    )
    document = config.to_dict()
    document["executors"] = {"codex": {"argv": [python]}}
    config = LaneConfig.from_dict(document)
    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    checks = _checks(report)
    assert checks["credential:codex"]["ok"] is True
    assert checks["credential:codex"]["detail"] == "OPENAI_API_KEY"
    assert monkeypatch_value not in json.dumps(report)


def test_leak_assertion_raises():
    with pytest.raises(cli.CredentialLeakError):
        cli._assert_no_leak('{"detail": "sk-secret-value"}', ["sk-secret-value"])
    cli._assert_no_leak('{"detail": "ok"}', ["sk-secret-value", "short"])


def test_doctor_leak_maps_to_internal_error(tmp_path, monkeypatch, capsys):
    from hermes_kanban_lanes.contracts import ContractError

    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    def fake_load(path):
        raise ContractError("bad config")

    monkeypatch.setattr(cli, "_load", fake_load)
    assert cli.main(["doctor", "--config", str(config_path), "--json"]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False

    monkeypatch.setattr(cli, "_load", lambda path: object())

    def leak(*args, **kwargs):
        raise cli.CredentialLeakError("doctor-credential-leak")

    monkeypatch.setattr(cli, "doctor_report", leak)
    assert cli.main(["doctor", "--config", str(config_path), "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == "doctor-credential-leak"
