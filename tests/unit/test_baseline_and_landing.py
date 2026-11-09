"""W3-07..W3-09: baseline git plumbing and the landing preflight."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.verification.evidence import (
    build_manifest,
    sha256_file,
    verify_evidence,
    write_evidence_index,
    write_json,
)
from hermes_kanban_lanes.workspace.git import (
    HKL_COMMITTER_EMAIL,
    HKL_COMMITTER_NAME,
    abort_merge,
    branch_tip,
    commit_run,
    create_run_worktree,
    diff_paths,
    is_ancestor,
    merge_commit,
    merge_tree_preflight,
    repo_head,
    run_git,
)
from hermes_kanban_lanes.workspace.landing import (
    LANDING_PLAN_RELATIVE_PATH,
    landing_preflight,
)

IDENTITY = RunIdentity(
    schema_version=1,
    task_id="t_landing",
    run_id=5,
    claim_lock="lock-landing",
)


def _commit(repo: Path, message: str = "commit") -> str:
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


def _row(identity: RunIdentity = IDENTITY) -> dict:
    return {
        "status": "running",
        "current_run_id": identity.run_id,
        "claim_lock": identity.claim_lock,
    }


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 0\n", encoding="utf-8")
    return package


def _evidence(
    tmp_path: Path,
    *,
    repo: Path,
    workspace: Path,
    base: str,
    sync_base: str,
    code: str,
    package: Path,
    identity: RunIdentity = IDENTITY,
) -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "evidence").mkdir(parents=True, exist_ok=True)
    gate_path = "evidence/gate-report.json"
    write_json(
        run_dir / gate_path,
        {
            "schema_version": 2,
            "requirement_id": "HKL-W3",
            "identity": identity.to_dict(),
            "base_commit": base,
            "run_commit": code,
            "sync_base": sync_base,
            "checks": [],
            "verdict": "pass",
        },
    )
    gate_sha = sha256_file(run_dir / gate_path)
    manifest = build_manifest(
        identity,
        base_commit=base,
        code_commit=code,
        gate_report_path=gate_path,
        gate_report_sha256=gate_sha,
        verdict="pass",
        package_dir=package,
    )
    write_json(run_dir / "evidence" / "manifest.json", manifest.to_dict())
    write_evidence_index(
        run_dir,
        identity=identity,
        base_commit=base,
        sync_base=sync_base,
        code_commit=code,
        tool_sha256=manifest.tool_sha256,
        gate_report_path=gate_path,
        gate_report_sha256=gate_sha,
        verdict="pass",
    )
    return run_dir


@pytest.fixture
def landing_case(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    workspace = tmp_path / "worktree"
    create_run_worktree(repo, base, "hkl/t_landing/run-5-aaaa", workspace)
    (workspace / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    code = _commit(workspace, "run")
    package = _package(tmp_path)
    run_dir = _evidence(
        tmp_path,
        repo=repo,
        workspace=workspace,
        base=base,
        sync_base=base,
        code=code,
        package=package,
    )
    return {
        "repo": repo,
        "base": base,
        "workspace": workspace,
        "code": code,
        "package": package,
        "run_dir": run_dir,
    }


def _preflight(case, **overrides):
    values = {
        "run_dir": case["run_dir"],
        "identity": IDENTITY,
        "workspace": case["workspace"],
        "current_workspace": case["workspace"],
        "row": _row(),
        "repo": case["repo"],
        "main_ref": "refs/heads/main",
        "package_dir": case["package"],
        "base_commit": case["base"],
        "sync_base": case["base"],
        "code_commit": case["code"],
    }
    values.update(overrides)
    return landing_preflight(**values)


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def test_diff_paths_include_both_rename_sides(make_repo):
    repo = make_repo()
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(repo)
    base = repo_head(repo)
    (repo / "README.txt").write_text("changed\n", encoding="utf-8")
    moved = run_git(["mv", "a.txt", "b.txt"], cwd=repo)
    assert moved.returncode == 0, moved.stderr
    head = _commit(repo)

    assert diff_paths(base, head, cwd=repo) == ["README.txt", "a.txt", "b.txt"]


def test_diff_paths_is_empty_without_changes(make_repo):
    repo = make_repo()
    head = repo_head(repo)
    assert diff_paths(head, head, cwd=repo) == []


def test_commit_run_uses_the_fixed_identity_when_git_env_is_exported(
    tmp_path, make_repo, monkeypatch
):
    """R1: a host-exported ``GIT_*`` identity must not leak into run commits."""

    repo = make_repo()
    workspace = tmp_path / "worktree"
    create_run_worktree(repo, repo_head(repo), "hkl/run", workspace)
    (workspace / "run.txt").write_text("run\n", encoding="utf-8")
    for name, value in {
        "GIT_AUTHOR_NAME": "code-reviewer",
        "GIT_AUTHOR_EMAIL": "code-reviewer@example.invalid",
        "GIT_COMMITTER_NAME": "code-reviewer",
        "GIT_COMMITTER_EMAIL": "code-reviewer@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)

    commit = commit_run(workspace, "run commit")
    fields = run_git(
        ["log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce", commit],
        cwd=workspace,
    ).stdout.strip()
    author_name, author_email, committer_name, committer_email = fields.split("\x00")
    assert (author_name, author_email) == (HKL_COMMITTER_NAME, HKL_COMMITTER_EMAIL)
    assert (committer_name, committer_email) == (
        HKL_COMMITTER_NAME,
        HKL_COMMITTER_EMAIL,
    )


def test_merge_commit_clean_creates_a_merge_commit(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    workspace = tmp_path / "worktree"
    create_run_worktree(repo, base, "hkl/run", workspace)
    (workspace / "run.txt").write_text("run\n", encoding="utf-8")
    run_commit = _commit(workspace, "run")
    (repo / "main.txt").write_text("main\n", encoding="utf-8")
    sync_base = _commit(repo, "main")

    result = merge_commit(workspace, sync_base)

    assert result.ok is True
    assert result.conflict is False
    assert result.commit == branch_tip(workspace)
    assert is_ancestor(run_commit, result.commit, repo=repo)
    assert is_ancestor(sync_base, result.commit, repo=repo)
    parents = run_git(
        ["rev-list", "--parents", "-n", "1", result.commit], cwd=workspace
    ).stdout.split()
    assert len(parents) == 3  # commit + two parents


def test_merge_conflict_aborts_and_preserves_both_sides(tmp_path, make_repo):
    repo = make_repo()
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    _commit(repo, "shared base")
    base = repo_head(repo)
    workspace = tmp_path / "worktree"
    create_run_worktree(repo, base, "hkl/run", workspace)
    (workspace / "shared.txt").write_text("run\n", encoding="utf-8")
    run_commit = _commit(workspace, "run side")
    (repo / "shared.txt").write_text("main\n", encoding="utf-8")
    side_base = _commit(repo, "main side")

    result = merge_commit(workspace, side_base)

    assert result.ok is False
    assert result.conflict is True
    assert result.conflict_paths == ("shared.txt",)
    assert abort_merge(workspace) is True
    # Both sides survive the abort untouched.
    assert (workspace / "shared.txt").read_text(encoding="utf-8") == "run\n"
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "main\n"
    assert (workspace / ".git").exists()
    assert run_git(
        ["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=workspace
    ).returncode != 0
    assert run_git(["cat-file", "-e", run_commit], cwd=workspace).returncode == 0


def test_merge_tree_preflight_reports_clean_and_conflict(tmp_path, make_repo):
    repo = make_repo()
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    _commit(repo, "shared base")
    base = repo_head(repo)
    workspace = tmp_path / "worktree"
    create_run_worktree(repo, base, "hkl/run", workspace)
    (workspace / "shared.txt").write_text("run\n", encoding="utf-8")
    run_commit = _commit(workspace, "run side")

    clean, tree = merge_tree_preflight("refs/heads/main", run_commit, repo=repo)
    assert clean is True
    assert tree and tree == run_git(
        ["rev-parse", f"{tree}^{{tree}}"], cwd=repo
    ).stdout.strip()

    # Advance main into a conflicting edit after the run commit exists.
    (repo / "shared.txt").write_text("main\n", encoding="utf-8")
    _commit(repo, "main side")
    clean, reason = merge_tree_preflight("refs/heads/main", run_commit, repo=repo)
    assert clean is False
    assert reason == "merge-conflict"

    clean, reason = merge_tree_preflight("refs/heads/missing", run_commit, repo=repo)
    assert clean is False
    assert reason.startswith("merge-tree-error:")


# ---------------------------------------------------------------------------
# landing preflight
# ---------------------------------------------------------------------------


def test_landing_ready_writes_a_plan_and_never_merges(landing_case):
    repo = landing_case["repo"]
    main_before = repo_head(repo)

    plan = _preflight(landing_case)

    assert plan.ok is True
    assert plan.verdict == "ready"
    assert plan.merge_tree
    assert plan.plan_path == LANDING_PLAN_RELATIVE_PATH
    document = json.loads(
        (landing_case["run_dir"] / LANDING_PLAN_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
    )
    assert document["requirement_id"] == "HKL-W3"
    assert document["identity"] == IDENTITY.to_dict()
    assert document["main_ref"] == "refs/heads/main"
    assert document["code_commit"] == landing_case["code"]
    assert document["base_commit"] == landing_case["base"]
    assert document["sync_base"] == landing_case["base"]
    assert document["merge_tree"] == plan.merge_tree
    assert document["verdict"] == "ready"
    assert document["reasons"] == []
    assert document["checks"] == {
        "identity": True,
        "workspace": True,
        "evidence": True,
        "tip": True,
        "ancestry": True,
        "merge_tree": True,
    }
    # The preflight is read-only for ``main``.
    assert repo_head(repo) == main_before


def test_landing_rejects_a_stale_identity_without_a_plan(landing_case):
    plan = _preflight(landing_case, row=_row(RunIdentity(
        schema_version=1,
        task_id="t_landing",
        run_id=IDENTITY.run_id + 1,
        claim_lock=IDENTITY.claim_lock,
    )))

    assert plan.ok is False
    assert plan.verdict == "rejected"
    assert "stale-run-or-claim" in plan.reasons
    assert not (landing_case["run_dir"] / LANDING_PLAN_RELATIVE_PATH).exists()


def test_landing_rejects_a_missing_row(landing_case):
    plan = _preflight(landing_case, row=None)
    assert plan.ok is False
    assert "task-not-found" in plan.reasons
    assert not (landing_case["run_dir"] / LANDING_PLAN_RELATIVE_PATH).exists()


def test_landing_rejects_a_different_workspace(landing_case, tmp_path):
    plan = _preflight(
        landing_case, current_workspace=tmp_path / "other-workspace"
    )
    assert plan.ok is False
    assert "not-current-run-workspace" in plan.reasons
    assert not (landing_case["run_dir"] / LANDING_PLAN_RELATIVE_PATH).exists()


def test_landing_rejects_an_advanced_tip(landing_case):
    (landing_case["workspace"] / "LATER.txt").write_text("later\n", encoding="utf-8")
    _commit(landing_case["workspace"], "later")

    plan = _preflight(landing_case)

    assert plan.ok is False
    assert "branch-tip-advanced" in plan.reasons
    assert not (landing_case["run_dir"] / LANDING_PLAN_RELATIVE_PATH).exists()


def test_landing_rejects_wrong_index_values(landing_case):
    # The package itself verifies, but the caller's explicit sync base differs
    # from the value the index commits to.
    plan = _preflight(landing_case, sync_base=landing_case["code"])

    assert plan.ok is False
    assert "evidence:index-values-mismatch" in plan.reasons
    assert not (landing_case["run_dir"] / LANDING_PLAN_RELATIVE_PATH).exists()


def test_landing_rejects_an_unresolvable_main_ref(landing_case):
    plan = _preflight(landing_case, main_ref="refs/heads/missing")

    assert plan.ok is False
    assert "main-ref-unresolved" in plan.reasons
    assert not (landing_case["run_dir"] / LANDING_PLAN_RELATIVE_PATH).exists()


def test_landing_merge_conflict_is_not_ready_but_plans(landing_case):
    repo = landing_case["repo"]
    # ``main`` moves after the evidence package was built, into a conflicting
    # edit.  ``base``/``sync_base`` remain ancestors of the run commit.
    (repo / "RESULT.txt").write_text("main version\n", encoding="utf-8")
    main_commit = _commit(repo, "main conflicting edit")

    plan = _preflight(landing_case)

    assert plan.ok is False
    assert plan.verdict == "not-ready"
    assert plan.reasons == ("merge-conflict",)
    assert plan.merge_tree is None
    assert plan.checks["merge_tree"] is False
    document = json.loads(
        (landing_case["run_dir"] / LANDING_PLAN_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
    )
    assert document["verdict"] == "not-ready"
    assert document["merge_tree"] is None
    assert document["reasons"] == ["merge-conflict"]
    assert repo_head(repo) == main_commit


def test_landing_requires_green_evidence(landing_case):
    (landing_case["run_dir"] / "evidence" / "gate-report.json").unlink()

    plan = _preflight(landing_case)

    assert plan.ok is False
    assert plan.verdict == "rejected"
    assert any(reason.startswith("evidence:") for reason in plan.reasons)
    assert verify_evidence(
        landing_case["run_dir"],
        IDENTITY,
        landing_case["package"],
        landing_case["workspace"],
        landing_case["repo"],
    ).ok is False
    assert not (landing_case["run_dir"] / LANDING_PLAN_RELATIVE_PATH).exists()
