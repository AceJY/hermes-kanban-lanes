"""W3-02..W3-05: the five fixed gate checks, positive and negative."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from hermes_kanban_lanes.card_contract import CardSpec
from hermes_kanban_lanes.config import LaneConfig
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.runtime.process import pid_alive
from hermes_kanban_lanes.verification import gate as gate_module
from hermes_kanban_lanes.verification.evidence import sha256_file
from hermes_kanban_lanes.verification.gate import _bad_relative, run_gate
from hermes_kanban_lanes.workspace.git import (
    create_run_worktree,
    repo_head,
    run_git,
)

PYTHON = sys.executable
IDENTITY = RunIdentity(
    schema_version=1,
    task_id="t_gate_checks",
    run_id=1,
    claim_lock="lock-gate",
)


def _config() -> LaneConfig:
    return LaneConfig.from_dict(
        {
            "schema_version": 1,
            "hermes_source": "/srv/hermes/source",
            "hermes_commit": "a" * 40,
            "boards": {"test": "/srv/hermes/kanban/test.db"},
            "repositories": {
                "primary": {"path": "/srv/hermes/repos/primary", "trusted": True}
            },
            "executors": {"claude": {"argv": ["claude"]}},
            "work_root": "/srv/hermes/work",
            "max_concurrent": 1,
        }
    )


def _card(**overrides) -> CardSpec:
    document = {
        "schema_version": 1,
        "card_kind": "implementation",
        "lineage": "t_gate_checks",
        "closure_for": None,
        "executor": "claude",
        "repository": "primary",
        "allow": ["RESULT.txt"],
        "commands": [{"argv": [PYTHON, "-c", "pass"], "cwd": "."}],
        "risk": "normal",
        "artifacts": [],
    }
    document.update(overrides)
    return CardSpec.from_dict(document, _config())


def _env(run_dir: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(run_dir / "home"),
        "TMPDIR": str(run_dir / "tmp"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "commands").mkdir(parents=True)
    return run_dir


def _commit(repo: Path, message: str = "run") -> str:
    added = run_git(["add", "-A"], cwd=repo)
    assert added.returncode == 0, added.stderr
    committed = run_git(
        [
            "-c",
            "user.name=HKL Tests",
            "-c",
            "user.email=hkl@example.invalid",
            "commit",
            "--no-verify",
            "-m",
            message,
        ],
        cwd=repo,
    )
    assert committed.returncode == 0, committed.stderr
    return repo_head(repo)


def _gate(
    card: CardSpec,
    repo: Path,
    run_dir: Path,
    base: str,
    run_commit: str | None,
    *,
    timeout: float = 10.0,
):
    return run_gate(
        card,
        repo,
        base,
        run_dir,
        identity=IDENTITY,
        env=_env(run_dir),
        command_timeout_seconds=timeout,
        run_commit=run_commit,
        sync_base=base,
    )


def _check(report: dict, name: str) -> dict:
    return next(entry for entry in report["checks"] if entry["name"] == name)


# ---------------------------------------------------------------------------
# _bad_relative (A5): every rejection branch has its own case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/abs/path",
        "a\\b",
        "C:/windows",
        "C:\\windows",
        "..",
        "a/../b",
        "./a",
        "a//b",
        "a/",
    ],
)
def test_bad_relative_rejects_unsafe_shapes(path):
    assert _bad_relative(path) is True


@pytest.mark.parametrize("path", ["RESULT.txt", "src/a.py", "dist/app.whl", ".hidden"])
def test_bad_relative_accepts_plain_relative_paths(path):
    assert _bad_relative(path) is False


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


def test_scope_accepts_an_allowed_change(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = _commit(repo)
    card = _card()

    result = _gate(card, repo, _run_dir(tmp_path), base, run_commit)

    assert result.verdict == "pass", result.report
    assert _check(result.report, "scope")["changed_paths"] == ["RESULT.txt"]


def test_scope_rejects_a_path_outside_allow(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "outside.txt").write_text("nope\n", encoding="utf-8")
    run_commit = _commit(repo)
    card = _card()

    result = _gate(card, repo, _run_dir(tmp_path), base, run_commit)

    assert result.verdict == "fail"
    scope = _check(result.report, "scope")
    assert scope["ok"] is False
    assert "path outside allow: outside.txt" in scope["detail"]


@pytest.mark.parametrize(
    "bad_path",
    ["../escape", "/abs/path", "C:/windows", "a\\b"],
)
def test_scope_rejects_unsafe_diff_paths(tmp_path, make_repo, monkeypatch, bad_path):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = _commit(repo)
    monkeypatch.setattr(
        gate_module, "diff_paths", lambda *args, **kwargs: [bad_path]
    )

    result = _gate(_card(), repo, _run_dir(tmp_path), base, run_commit)

    scope = _check(result.report, "scope")
    assert scope["ok"] is False
    assert scope["detail"] == f"unsafe path: {bad_path}"


def test_scope_rejects_a_symlink_escape(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (repo / "link").symlink_to(outside)
    run_commit = _commit(repo)
    card = _card(allow=["link"])

    result = _gate(card, repo, _run_dir(tmp_path), base, run_commit)

    scope = _check(result.report, "scope")
    assert scope["ok"] is False
    assert scope["detail"] == "path escapes workspace (symlink): link"


def test_scope_rejects_a_rename_whose_source_is_outside_allow(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "outside.txt").write_text("moved\n", encoding="utf-8")
    _commit(repo, "add outside")
    base = repo_head(repo)
    (repo / "allowed").mkdir()
    moved = run_git(["mv", "outside.txt", "allowed/new.txt"], cwd=repo)
    assert moved.returncode == 0, moved.stderr
    run_commit = _commit(repo)
    card = _card(allow=["allowed/"])

    result = _gate(card, repo, _run_dir(tmp_path), base, run_commit)

    scope = _check(result.report, "scope")
    assert scope["ok"] is False
    assert scope["detail"] == "path outside allow: outside.txt"


def test_scope_rejects_a_dirty_workspace(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = _commit(repo)
    (repo / "RESULT.txt").write_text("dirty\n", encoding="utf-8")

    result = _gate(_card(), repo, _run_dir(tmp_path), base, run_commit)

    scope = _check(result.report, "scope")
    assert scope["ok"] is False
    assert scope["detail"] == "workspace-dirty"


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------


def test_progress_fails_when_nothing_changed(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)

    result = _gate(_card(), repo, _run_dir(tmp_path), base, base)

    progress = _check(result.report, "progress")
    assert progress["ok"] is False
    assert progress["detail"] == "no-progress"
    assert result.verdict == "fail"


def test_progress_is_not_applicable_for_non_implementation_cards(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    card = _card(card_kind="review", commands=[])

    result = _gate(card, repo, _run_dir(tmp_path), base, base)

    progress = _check(result.report, "progress")
    assert progress["ok"] is True
    assert progress["detail"] == "not-applicable:review"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def test_commands_capture_stdout_and_stderr_separately(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = _commit(repo)
    script = (
        "import sys;"
        "sys.stdout.write('OUT-bytes');"
        "sys.stderr.write('ERR-bytes')"
    )
    card = _card(commands=[{"argv": [PYTHON, "-c", script], "cwd": "."}])
    run_dir = _run_dir(tmp_path)

    result = _gate(card, repo, run_dir, base, run_commit)

    assert result.verdict == "pass", result.report
    commands = _check(result.report, "commands")
    assert commands["ok"] is True
    (entry,) = commands["commands"]
    assert entry["exit_code"] == 0
    assert entry["timed_out"] is False
    stdout_path = run_dir / entry["stdout_log"]
    stderr_path = run_dir / entry["stderr_log"]
    assert stdout_path == run_dir / "commands" / "0.stdout.log"
    assert stderr_path == run_dir / "commands" / "0.stderr.log"
    assert stdout_path.read_bytes() == b"OUT-bytes"
    assert stderr_path.read_bytes() == b"ERR-bytes"
    assert entry["stdout_sha256"] == sha256_file(stdout_path)
    assert entry["stderr_sha256"] == sha256_file(stderr_path)
    assert entry["stdout_size"] == stdout_path.stat().st_size
    assert entry["stderr_size"] == stderr_path.stat().st_size


def test_commands_nonzero_exit_fails(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = _commit(repo)
    card = _card(
        commands=[{"argv": [PYTHON, "-c", "raise SystemExit(7)"], "cwd": "."}]
    )

    result = _gate(card, repo, _run_dir(tmp_path), base, run_commit)

    commands = _check(result.report, "commands")
    assert commands["ok"] is False
    assert commands["commands"][0]["exit_code"] == 7
    assert commands["commands"][0]["timed_out"] is False
    assert result.verdict == "fail"


def test_commands_timeout_kills_the_group_and_keeps_logs(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = _commit(repo)
    run_dir = _run_dir(tmp_path)
    pid_file = run_dir / "grandchild.pid"
    script = (
        "import subprocess, sys, time;"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid));"
        "print('started', flush=True);"
        "time.sleep(60)"
    )
    card = _card(commands=[{"argv": [PYTHON, "-c", script], "cwd": "."}])

    result = _gate(card, repo, run_dir, base, run_commit, timeout=0.4)

    commands = _check(result.report, "commands")
    (entry,) = commands["commands"]
    assert commands["ok"] is False
    assert entry["timed_out"] is True
    assert (run_dir / entry["stdout_log"]).read_text(encoding="utf-8") == "started\n"
    assert (run_dir / entry["stderr_log"]).is_file()
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    # The whole session group (command + grandchild) is gone.
    for _ in range(50):
        if not pid_alive(grandchild):
            break
        time.sleep(0.05)
    assert not pid_alive(grandchild)


def test_commands_log_write_failure_fails(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = _commit(repo)
    run_dir = _run_dir(tmp_path)
    # A directory where the stdout log should be makes ``open(..., 'wb')`` fail
    # before the command is spawned.
    (run_dir / "commands" / "0.stdout.log").mkdir()
    card = _card()

    result = _gate(card, repo, run_dir, base, run_commit)

    commands = _check(result.report, "commands")
    assert commands["ok"] is False
    (entry,) = commands["commands"]
    assert entry["error"].startswith("log-write-failed:")
    assert result.verdict == "fail"


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


def test_artifacts_positive_entry_records_digest(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = _commit(repo)
    card = _card(artifacts=["RESULT.txt"])

    result = _gate(card, repo, _run_dir(tmp_path), base, run_commit)

    artifacts = _check(result.report, "artifacts")
    assert artifacts["ok"] is True
    (entry,) = artifacts["artifacts"]
    assert entry["path"] == "RESULT.txt"
    assert entry["size"] == 3
    assert entry["sha256"] == sha256_file(repo / "RESULT.txt")


def test_artifacts_none_declared_is_explicit(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = _commit(repo)

    result = _gate(_card(), repo, _run_dir(tmp_path), base, run_commit)

    artifacts = _check(result.report, "artifacts")
    assert artifacts["ok"] is True
    assert artifacts["detail"] == "none-declared"
    assert artifacts["artifacts"] == []


def test_artifacts_missing_file_fails(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = _commit(repo)
    card = _card(allow=["RESULT.txt", "ghost.txt"], artifacts=["ghost.txt"])

    result = _gate(card, repo, _run_dir(tmp_path), base, run_commit)

    artifacts = _check(result.report, "artifacts")
    assert artifacts["ok"] is False
    assert artifacts["detail"] == "missing: ghost.txt"


def test_artifacts_empty_file_fails(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    (repo / "empty.txt").write_text("", encoding="utf-8")
    run_commit = _commit(repo)
    card = _card(allow=["RESULT.txt", "empty.txt"], artifacts=["empty.txt"])

    result = _gate(card, repo, _run_dir(tmp_path), base, run_commit)

    artifacts = _check(result.report, "artifacts")
    assert artifacts["ok"] is False
    assert artifacts["detail"] == "empty: empty.txt"


def test_artifacts_outside_workspace_fails(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    outside = tmp_path / "outside-artifact.txt"
    outside.write_text("external\n", encoding="utf-8")
    (repo / "link.txt").symlink_to(outside)
    run_commit = _commit(repo)
    card = _card(allow=["link.txt"], artifacts=["link.txt"])

    result = _gate(card, repo, _run_dir(tmp_path), base, run_commit)

    artifacts = _check(result.report, "artifacts")
    assert artifacts["ok"] is False
    assert artifacts["detail"] == "outside-workspace: link.txt"


def test_artifacts_unmodified_file_fails(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = _commit(repo)
    card = _card(allow=["RESULT.txt", "README.txt"], artifacts=["README.txt"])

    result = _gate(card, repo, _run_dir(tmp_path), base, run_commit)

    artifacts = _check(result.report, "artifacts")
    assert artifacts["ok"] is False
    assert artifacts["detail"] == "unmodified: README.txt"


# ---------------------------------------------------------------------------
# report shape / blocked
# ---------------------------------------------------------------------------


def test_report_has_fixed_schema_and_check_order(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    run_commit = _commit(repo)

    result = _gate(_card(), repo, _run_dir(tmp_path), base, run_commit)

    report = result.report
    assert report["schema_version"] == 2
    assert report["requirement_id"] == "HKL-W3"
    assert report["identity"] == IDENTITY.to_dict()
    assert report["base_commit"] == base
    assert report["run_commit"] == run_commit
    assert report["sync_base"] == base
    assert report["verdict"] == "pass"
    assert [check["name"] for check in report["checks"]] == [
        "workspace",
        "scope",
        "progress",
        "commands",
        "artifacts",
    ]
    assert (Path(result.report_path)).as_posix() == "evidence/gate-report.json"


def test_missing_workspace_blocks_with_only_the_workspace_check(tmp_path):
    run_dir = _run_dir(tmp_path)
    missing = tmp_path / "no-workspace"

    result = run_gate(
        _card(),
        missing,
        "a" * 40,
        run_dir,
        identity=IDENTITY,
        env=_env(run_dir),
        run_commit="b" * 40,
        sync_base="a" * 40,
    )

    assert result.verdict == "blocked"
    assert [check["name"] for check in result.report["checks"]] == ["workspace"]
    assert result.report["checks"][0]["ok"] is False
