"""W4-01/W4-04/W4-05: the local ``--no-ff`` merge and post-merge regression.

The merge is allowed to move only the local main ref.  Every test asserts the
recorded facts, not merely the returned boolean: parents, ancestry, allow
scope, protected roots, abort behaviour and the fixed committer identity.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.release.gate import release_preflight
from hermes_kanban_lanes.release.merge import (
    DEFAULT_PROTECTED_ROOTS,
    MERGE_COMMITTER_EMAIL,
    MERGE_COMMITTER_NAME,
    merge_release,
    run_post_merge,
)
from hermes_kanban_lanes.runtime.release import _finalise_release_report
from hermes_kanban_lanes.verification.evidence import sha256_file
from hermes_kanban_lanes.workspace.git import repo_head, run_git

REQUIREMENT = "HKL-W4"
MAIN_REF = "refs/heads/main"


def _git(*args: str, cwd: Path) -> str:
    result = run_git(args, cwd=cwd, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _git_code(*args: str, cwd: Path) -> int:
    return run_git(args, cwd=cwd, timeout=120).returncode


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "hkl@example.invalid", cwd=repo)
    _git("config", "user.name", "HKL Tests", cwd=repo)
    (repo / "README.txt").write_text("base\n", encoding="utf-8")
    _git("add", "README.txt", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    return repo, repo_head(repo)


def _branch_commit(
    repo: Path,
    base: str,
    branch: str,
    files: dict[str, str],
    message: str,
    *,
    tmp_path: Path,
) -> str:
    workspace = tmp_path / f"wt-{branch}"
    _git("worktree", "add", "-q", "-b", branch, str(workspace), base, cwd=repo)
    for name, content in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git("add", "-A", cwd=workspace)
    _git("commit", "-q", "-m", message, cwd=workspace)
    return repo_head(workspace)


def _identity(task_id: str = "t_w4merge") -> RunIdentity:
    return RunIdentity(
        schema_version=1,
        task_id=task_id,
        run_id=3,
        claim_lock="host:3:hkl:merge",
    )


def _refs(repo: Path) -> dict[str, str]:
    result = run_git(
        ["for-each-ref", "--format=%(refname) %(objectname)", "refs/"],
        cwd=repo,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split(" ", 1) for line in result.stdout.splitlines() if line)


def _merge(repo: Path, run_dir: Path, head: str, base: str, *, allow, **kwargs):
    run_dir.mkdir(parents=True, exist_ok=True)
    return merge_release(
        repo=repo,
        main_ref=kwargs.pop("main_ref", MAIN_REF),
        reviewed_head=head,
        main_before=base,
        allow=allow,
        run_dir=run_dir,
        identity=kwargs.pop("identity", _identity()),
        requirement_id=REQUIREMENT,
        **kwargs,
    )


def _document(run_dir: Path) -> dict:
    path = run_dir / "merge.json"
    assert path.is_file()
    return json.loads(path.read_text(encoding="utf-8"))


def test_no_ff_merge_records_and_asserts_the_reviewed_facts(tmp_path):
    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo, base, "task", {"RESULT.txt": "ok\n"}, "run 1", tmp_path=tmp_path
    )
    refs_before = _refs(repo)
    run_dir = tmp_path / "release"
    record = _merge(repo, run_dir, head, base, allow=["RESULT.txt"])

    assert record.ok
    document = _document(run_dir)
    assert document["verdict"] == "merged"
    assert document["reasons"] == []
    assert document["schema_version"] == 1
    assert document["requirement_id"] == REQUIREMENT
    assert document["identity"] == _identity().to_dict()
    assert document["task_id"] == "t_w4merge"
    assert document["main_before"] == base
    assert document["reviewed_head"] == head
    assert document["merge_sha"] == record.merge_sha
    assert document["parents"] == [base, head]
    assert document["main_before_is_ancestor"] is True
    assert document["changed_paths"] == ["RESULT.txt"]
    assert document["outside_allow"] == 0
    assert document["reviewed_paths_changed_by_merge"] == []
    assert document["protected_roots_touched"] == 0
    assert "--no-ff" in document["command"]["argv"]
    assert "--no-verify" in document["command"]["argv"]
    assert document["command"]["exit_code"] == 0
    assert document["command"]["timed_out"] is False
    assert sha256_file(run_dir / "merge.stdout.log") == document["command"][
        "stdout_sha256"
    ]

    assert repo_head(repo) == record.merge_sha
    assert record.merge_tree == _git(
        "rev-parse", f"{record.merge_sha}^{{tree}}", cwd=repo
    )
    assert _git("rev-list", "--parents", "-n", "1", record.merge_sha, cwd=repo) == (
        f"{record.merge_sha} {base} {head}"
    )

    refs_after = _refs(repo)
    assert refs_after["refs/heads/main"] == record.merge_sha
    assert refs_after["refs/heads/task"] == refs_before["refs/heads/task"]
    changed_refs = {
        name
        for name in set(refs_before) | set(refs_after)
        if refs_before.get(name) != refs_after.get(name)
    }
    assert changed_refs == {"refs/heads/main"}


def test_merge_uses_the_fixed_committer_identity_and_message(tmp_path):
    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo, base, "task", {"RESULT.txt": "ok\n"}, "run 1", tmp_path=tmp_path
    )
    record = _merge(
        repo,
        tmp_path / "release",
        head,
        base,
        allow=["RESULT.txt"],
        message="hkl: release t_w4merge\r\n",
    )
    assert record.ok
    fields = _git(
        "log", "-1", "--format=%cn%x00%ce%x00%s", record.merge_sha, cwd=repo
    )
    name, email, subject = fields.split("\x00")
    assert (name, email) == (MERGE_COMMITTER_NAME, MERGE_COMMITTER_EMAIL)
    assert subject == "hkl: release t_w4merge"


def test_merge_uses_the_fixed_identity_when_git_env_is_exported(
    tmp_path, monkeypatch
):
    """R1: ``GIT_*`` exported by the host must not override the fixed identity."""

    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo, base, "task", {"RESULT.txt": "ok\n"}, "run 1", tmp_path=tmp_path
    )
    for name, value in {
        "GIT_AUTHOR_NAME": "code-reviewer",
        "GIT_AUTHOR_EMAIL": "code-reviewer@example.invalid",
        "GIT_COMMITTER_NAME": "code-reviewer",
        "GIT_COMMITTER_EMAIL": "code-reviewer@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)

    record = _merge(repo, tmp_path / "release", head, base, allow=["RESULT.txt"])
    assert record.ok
    fields = _git(
        "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce", record.merge_sha, cwd=repo
    )
    author_name, author_email, committer_name, committer_email = fields.split("\x00")
    assert (committer_name, committer_email) == (
        MERGE_COMMITTER_NAME,
        MERGE_COMMITTER_EMAIL,
    )
    assert (author_name, author_email) == (
        MERGE_COMMITTER_NAME,
        MERGE_COMMITTER_EMAIL,
    )


def test_parent_set_mismatch_is_recorded_as_merged_with_failures(tmp_path, monkeypatch):
    """D7: the merge commit must have exactly ``main_before`` and ``reviewed``.

    The commit already exists, so the honest record is "merged + follow-up
    failure", not ``refused`` (which would imply main never moved).
    """

    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo, base, "task", {"RESULT.txt": "ok\n"}, "run 1", tmp_path=tmp_path
    )
    wrong = "f" * 40
    monkeypatch.setattr(
        "hermes_kanban_lanes.release.merge._parents_of",
        lambda commit, *, repo: [base, wrong],
    )
    run_dir = tmp_path / "release"
    record = _merge(repo, run_dir, head, base, allow=["RESULT.txt"])
    assert record.ok is False
    assert record.merged is True
    assert record.reasons == ("parents-mismatch",), record.reasons
    document = _document(run_dir)
    assert document["verdict"] == "merged-with-failures"
    assert document["merged"] is True
    assert document["merge_sha"] == repo_head(repo)
    assert document["parents"] == [base, wrong]


def test_merge_that_does_not_create_a_commit_is_refused(tmp_path, monkeypatch):
    """D7: ``merge_sha`` must differ from ``main_before``."""

    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo, base, "task", {"RESULT.txt": "ok\n"}, "run 1", tmp_path=tmp_path
    )
    monkeypatch.setattr(
        "hermes_kanban_lanes.release.merge.repo_head",
        lambda path: base,
    )
    run_dir = tmp_path / "release"
    record = _merge(repo, run_dir, head, base, allow=["RESULT.txt"])
    assert record.ok is False
    assert "merge-not-created" in record.reasons, record.reasons
    document = _document(run_dir)
    assert document["verdict"] == "refused"
    assert document["merge_sha"] == base


def test_merge_refuses_when_the_checkout_is_not_the_main_ref(tmp_path):
    """A merge into another checked-out branch must never move ``main``."""

    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo, base, "task", {"RESULT.txt": "ok\n"}, "run 1", tmp_path=tmp_path
    )
    _git("checkout", "-q", "-b", "side", cwd=repo)
    run_dir = tmp_path / "release"
    record = _merge(repo, run_dir, head, base, allow=["RESULT.txt"])
    assert record.ok is False
    assert record.reasons == ("branch-mismatch",), record.reasons
    assert repo_head(repo) == base
    assert _document(run_dir)["merge_sha"] is None


def test_main_moved_refuses_before_running_merge(tmp_path):
    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo, base, "task", {"RESULT.txt": "ok\n"}, "run 1", tmp_path=tmp_path
    )
    (repo / "MAIN.txt").write_text("moved\n", encoding="utf-8")
    _git("add", "MAIN.txt", cwd=repo)
    _git("commit", "-q", "-m", "main moved", cwd=repo)
    moved = repo_head(repo)

    record = _merge(repo, tmp_path / "release", head, base, allow=["RESULT.txt"])
    assert not record.ok
    assert record.reasons == ("main-moved",)
    document = _document(tmp_path / "release")
    assert document["verdict"] == "refused"
    assert document["command"]["exit_code"] is None
    assert document["merge_sha"] is None
    assert repo_head(repo) == moved


def test_merge_refuses_dirty_main(tmp_path):
    """W9 regression: a dirty main is refused before any ``git merge``.

    Startup/claiming tolerate WIP, but the release gate still owns the
    ``workspace-dirty`` refusal so a half-dirty tree never reaches ``git merge``.
    """

    repo, base = _init_repo(tmp_path)
    _branch_commit(
        repo, base, "task", {"RESULT.txt": "ok\n"}, "run 1", tmp_path=tmp_path
    )
    (repo / "WIP.txt").write_text("wip\n", encoding="utf-8")
    status_before = _git("status", "--porcelain", "-uall", cwd=repo)

    run_dir = tmp_path / "release"
    run_dir.mkdir()
    dev_dir = tmp_path / "dev-run"
    dev_dir.mkdir()
    decision = release_preflight(
        identity=_identity(),
        task_row=None,
        runs=[],
        dev_run_dir=dev_dir,
        repo=repo,
        package_dir=Path(__file__).resolve().parents[2]
        / "src"
        / "hermes_kanban_lanes",
        main_ref=MAIN_REF,
        authorization_path=tmp_path / "authorization.json",
        merge_enabled=True,
        post_merge_commands=[["true"]],
        max_rework_rounds=3,
        release_run_dir=run_dir,
        requirement_id=REQUIREMENT,
    )

    assert decision.ready is False
    assert "workspace-dirty" in decision.reasons
    workspace = next(
        check for check in decision.report["checks"] if check["name"] == "workspace"
    )
    assert workspace["detail"] == "workspace-dirty"
    # No merge ran: main and the working tree are byte-for-byte untouched.
    assert repo_head(repo) == base
    assert _git("status", "--porcelain", "-uall", cwd=repo) == status_before


def test_unresolved_main_ref_refuses(tmp_path):
    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo, base, "task", {"RESULT.txt": "ok\n"}, "run 1", tmp_path=tmp_path
    )
    record = _merge(
        repo,
        tmp_path / "release",
        head,
        base,
        allow=["RESULT.txt"],
        main_ref="refs/heads/absent",
    )
    assert not record.ok
    assert record.reasons == ("main-unresolved",)
    assert _document(tmp_path / "release")["command"]["exit_code"] is None
    assert repo_head(repo) == base


def test_conflict_aborts_and_leaves_main_untouched(tmp_path):
    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo,
        base,
        "task",
        {"README.txt": "task-side\n"},
        "task edit",
        tmp_path=tmp_path,
    )
    (repo / "README.txt").write_text("main-side\n", encoding="utf-8")
    _git("add", "README.txt", cwd=repo)
    _git("commit", "-q", "-m", "main edit", cwd=repo)
    main_before = repo_head(repo)

    record = _merge(repo, tmp_path / "release", head, main_before, allow=["README.txt"])
    assert not record.ok
    assert "merge-conflict" in record.reasons, record.reasons
    document = _document(tmp_path / "release")
    assert document["verdict"] == "refused"
    assert document["merged"] is False
    # A conflicting merge cannot create a commit, so it is attempted and
    # aborted; the metadata never advances.
    assert document["aborted"] is True
    assert document["command"]["exit_code"] != 0
    assert "preflight" not in document["command"]
    assert document["merge_sha"] is None
    assert repo_head(repo) == main_before
    assert _git("status", "--porcelain", cwd=repo) == ""
    assert _git_code("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=repo) != 0
    assert (tmp_path / "release" / "merge.stdout.log").exists()


def test_failed_abort_is_named_and_keeps_the_scene(tmp_path, monkeypatch):
    """W4-09: an abort that does not restore the checkout is its own refusal.

    ``git merge`` fails because a merge state is already present; the abort is
    forced to fail.  The record must name ``merge-abort-failed`` rather than a
    conflict, keep the merge state for an operator, and never move ``main``.
    """

    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo, base, "task", {"RESULT.txt": "ok\n"}, "run 1", tmp_path=tmp_path
    )
    (repo / ".git" / "MERGE_HEAD").write_text(head + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_kanban_lanes.release.merge.abort_merge",
        lambda repo_path: False,
    )

    run_dir = tmp_path / "release"
    record = _merge(repo, run_dir, head, base, allow=["RESULT.txt"])
    assert not record.ok
    assert record.reasons == ("merge-abort-failed",)
    document = _document(run_dir)
    assert document["aborted"] is False
    assert document["merge_sha"] is None
    assert repo_head(repo) == base
    assert _git_code("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=repo) == 0


def test_failed_abort_maps_to_a_capability_block():
    """W4-09: a preserved abort scene is an environment problem, not input."""

    from hermes_kanban_lanes.runtime.release import _release_reason_kind

    assert _release_reason_kind(("merge-abort-failed",)) == "capability"


def test_outside_allow_is_refused_before_the_branch_moves(tmp_path):
    """A scope violation must be decided before ``git merge``.

    A refusal that only happens after the merge commit exists is fail-open:
    the code is already on ``main``.  Assert the git state is byte-for-byte
    unchanged, not merely that ``record.ok`` is False.
    """

    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo, base, "task", {"outside.txt": "x\n"}, "run 1", tmp_path=tmp_path
    )
    refs_before = _refs(repo)
    status_before = _git("status", "--porcelain", "-uall", cwd=repo)
    record = _merge(repo, tmp_path / "release", head, base, allow=["src/"])
    assert not record.ok
    assert record.merged is False
    assert "outside-allow:1" in record.reasons, record.reasons
    document = _document(tmp_path / "release")
    assert document["verdict"] == "refused"
    assert document["outside_allow"] == 1
    assert document["outside_allow_paths"] == ["outside.txt"]
    assert document["command"]["preflight"] == "merge-tree"
    # Nothing moved: same main, same refs, clean tree, no merge in progress.
    assert repo_head(repo) == base
    assert _refs(repo) == refs_before
    assert _git("log", "-1", "--format=%H", cwd=repo) == base
    assert _git("status", "--porcelain", "-uall", cwd=repo) == status_before
    assert _git_code("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=repo) != 0


def test_protected_root_is_refused_before_the_branch_moves(tmp_path):
    """D7/protection: a hard protected root is a pre-merge refusal."""

    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo, base, "task", {"tools/tool.sh": "#!/bin/sh\n"}, "run 1", tmp_path=tmp_path
    )
    refs_before = _refs(repo)
    record = _merge(repo, tmp_path / "release", head, base, allow=["tools/tool.sh"])
    assert not record.ok
    assert record.merged is False
    assert record.reasons == ("protected-root-touched:1",), record.reasons
    document = _document(tmp_path / "release")
    assert document["verdict"] == "refused"
    assert document["protected_roots_touched"] == 1
    assert document["protected_root_paths"] == ["tools/tool.sh"]
    assert document["merge_sha"] is None
    assert "tools/" in DEFAULT_PROTECTED_ROOTS
    # Byte-for-byte unchanged main/worktree.
    assert repo_head(repo) == base
    assert _refs(repo) == refs_before
    assert _git("status", "--porcelain", "-uall", cwd=repo) == ""
    assert _git_code("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=repo) != 0


def test_protected_root_outranks_outside_allow_in_the_named_reason(tmp_path):
    """Shape A: allow only ``docs/x.md`` but the reviewed branch changed
    ``tools/y.sh``.  The card's block reason is ``reasons[0]``, so the hard
    protected-root violation must be the first named reason even though the
    same path is also outside the allow list."""

    repo, base = _init_repo(tmp_path)
    head = _branch_commit(
        repo,
        base,
        "task",
        {"docs/x.md": "docs\n", "tools/y.sh": "#!/bin/sh\n"},
        "run 1",
        tmp_path=tmp_path,
    )
    refs_before = _refs(repo)
    record = _merge(repo, tmp_path / "release", head, base, allow=["docs/x.md"])
    assert not record.ok
    assert record.merged is False
    assert record.reasons[0] == "protected-root-touched:1", record.reasons
    assert "outside-allow:1" in record.reasons, record.reasons
    document = _document(tmp_path / "release")
    assert document["verdict"] == "refused"
    assert document["protected_root_paths"] == ["tools/y.sh"]
    assert document["outside_allow_paths"] == ["tools/y.sh"]
    assert document["merge_sha"] is None
    assert repo_head(repo) == base
    assert _refs(repo) == refs_before
    assert _git("status", "--porcelain", "-uall", cwd=repo) == ""
    assert _git_code("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=repo) != 0


def test_merge_that_changes_reviewed_paths_is_named(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "hkl@example.invalid", cwd=repo)
    _git("config", "user.name", "HKL Tests", cwd=repo)
    # A merge driver that keeps "ours" lets main and the reviewed branch both
    # change the same reviewed path without a textual conflict.
    _git("config", "merge.ours.driver", "true", cwd=repo)
    (repo / "RESULT.txt").write_text("base\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("RESULT.txt merge=ours\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    base = repo_head(repo)

    head = _branch_commit(
        repo, base, "task", {"RESULT.txt": "reviewed\n"}, "run 1", tmp_path=tmp_path
    )
    (repo / "RESULT.txt").write_text("main-side\n", encoding="utf-8")
    _git("add", "RESULT.txt", cwd=repo)
    _git("commit", "-q", "-m", "main edit", cwd=repo)
    main_before = repo_head(repo)

    record = _merge(repo, tmp_path / "release", head, main_before, allow=["RESULT.txt"])
    assert not record.ok
    assert record.merged is False
    assert "merge-changed-reviewed-paths:1" in record.reasons, record.reasons
    document = _document(tmp_path / "release")
    assert document["reviewed_paths_changed_by_merge"] == ["RESULT.txt"]
    assert document["changed_paths"] == []
    # The dry-run tree already shows the merge driver changing a reviewed
    # path, so the refusal happens before the branch moves.
    assert document["merge_sha"] is None
    assert repo_head(repo) == main_before


# ---------------------------------------------------------------------------
# D8: post-merge regression on a temporary detached worktree
# ---------------------------------------------------------------------------


def _postmerge_repo(tmp_path):
    repo, base = _init_repo(tmp_path)
    tree = _git("rev-parse", f"{base}^{{tree}}", cwd=repo)
    return repo, base, tree


def _postmerge(repo, run_dir, sha, tree, commands, *, timeout=60.0):
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_post_merge(
        repo=repo,
        merge_sha=sha,
        merge_tree=tree,
        run_dir=run_dir,
        commands=commands,
        timeout_seconds=timeout,
        identity=_identity(),
        requirement_id=REQUIREMENT,
    )


def _report(run_dir: Path) -> dict:
    path = run_dir / "postmerge" / "report.json"
    assert path.is_file(), "postmerge report must always be written"
    return json.loads(path.read_text(encoding="utf-8"))


def test_postmerge_pass_runs_at_the_merge_sha(tmp_path):
    repo, sha, tree = _postmerge_repo(tmp_path)
    run_dir = tmp_path / "release"
    document = _postmerge(
        repo, run_dir, sha, tree, [["sh", "-c", "git rev-parse HEAD"]]
    )
    assert document["verdict"] == "pass"
    assert document["reasons"] == []
    assert document["schema_version"] == 1
    assert document["requirement_id"] == REQUIREMENT
    assert document["identity"] == _identity().to_dict()
    assert document["merge_sha"] == sha
    assert document["merge_tree"] == tree
    # The temporary worktree is named run-relatively so the report is portable.
    assert document["worktree"] == "postmerge/worktree"
    entry = document["commands"][0]
    assert entry["argv"] == ["sh", "-c", "git rev-parse HEAD"]
    assert entry["exit_code"] == 0
    assert entry["timed_out"] is False
    stdout_log = run_dir / entry["stdout_log"]
    assert stdout_log.read_text(encoding="utf-8").strip() == sha
    assert sha256_file(stdout_log) == entry["stdout_sha256"]
    assert entry["stdout_size"] == stdout_log.stat().st_size
    assert entry["stderr_sha256"] == sha256_file(run_dir / entry["stderr_log"])
    assert not (run_dir / "postmerge" / "worktree").exists()
    assert _report(run_dir) == document
    assert repo_head(repo) == sha


def test_postmerge_failure_blocks_without_rollback(tmp_path):
    repo, sha, tree = _postmerge_repo(tmp_path)
    run_dir = tmp_path / "release"
    document = _postmerge(repo, run_dir, sha, tree, [["sh", "-c", "exit 3"]])
    assert document["verdict"] == "fail"
    assert document["reasons"] == ["postmerge-failed:0"]
    assert document["commands"][0]["exit_code"] == 3
    assert repo_head(repo) == sha
    assert not (run_dir / "postmerge" / "worktree").exists()


def test_postmerge_runs_each_command_and_names_the_failing_index(tmp_path):
    repo, sha, tree = _postmerge_repo(tmp_path)
    document = _postmerge(
        repo,
        tmp_path / "release",
        sha,
        tree,
        [["true"], ["sh", "-c", "exit 1"], ["true"]],
    )
    assert document["reasons"] == ["postmerge-failed:1"]
    # Every configured command still runs and is kept in the record.
    assert len(document["commands"]) == 3
    assert document["commands"][0]["exit_code"] == 0
    assert document["commands"][1]["exit_code"] == 1
    assert document["commands"][2]["exit_code"] == 0


def test_postmerge_timeout_is_named(tmp_path):
    repo, sha, tree = _postmerge_repo(tmp_path)
    document = _postmerge(
        repo, tmp_path / "release", sha, tree, [["sleep", "5"]], timeout=0.3
    )
    assert document["verdict"] == "fail"
    assert document["reasons"] == ["postmerge-timeout:0"]
    assert document["commands"][0]["timed_out"] is True


def test_postmerge_missing_executable_is_a_command_error(tmp_path):
    repo, sha, tree = _postmerge_repo(tmp_path)
    document = _postmerge(
        repo, tmp_path / "release", sha, tree, [["/nonexistent/hkl-command"]]
    )
    assert document["verdict"] == "fail"
    assert document["reasons"] == ["postmerge-command-error:0"]
    assert document["commands"][0]["exit_code"] is None


def test_postmerge_worktree_failure_is_reported(tmp_path):
    repo, _sha, _tree = _postmerge_repo(tmp_path)
    run_dir = tmp_path / "release"
    document = _postmerge(repo, run_dir, "0" * 40, None, [["true"]])
    assert document["verdict"] == "fail"
    assert document["reasons"][0].startswith("postmerge-worktree-failed:")
    assert document["commands"] == []
    assert _report(run_dir) == document


# ---------------------------------------------------------------------------
# W4-01/F1: one release, one final verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "merge_reasons",
    [
        ("main-moved",),
        ("merge-conflict",),
        ("merge-tree-error:128",),
        ("merge-abort-failed",),
        ("protected-root-touched:1",),
        ("outside-allow:2", "merge-changed-reviewed-paths:1"),
    ],
)
def test_finalise_keeps_the_gate_verdict_and_one_release_verdict(
    tmp_path, merge_reasons
):
    """F1: ``verdict`` is the gate verdict for *every* pre-merge refusal.

    The old code rewrote it to ``refused`` only for the scope family, so the
    same field meant two different things and ``release-ready`` coexisted with
    ``refused`` for ``main-moved``/``merge-conflict``/... .  The final outcome
    lives only in ``release_verdict`` (backed by reasons/merged/merge_sha), so
    ``verdict`` must stay the gate's ``release-ready`` in all these cases.

    Mutation guard: restoring the scope-only overwrite fails the
    ``protected-root-touched:1`` case; rewriting every refusal (option 乙)
    fails every case.
    """

    run_dir = tmp_path / "release"
    run_dir.mkdir()
    (run_dir / "release-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "requirement_id": REQUIREMENT,
                "verdict": "release-ready",
                "reasons": [],
                "checks": [{"name": "merge_enabled", "ok": True}],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "merge.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "verdict": "refused",
                "reasons": list(merge_reasons),
                "merge_sha": None,
                "changed_paths": [],
            }
        ),
        encoding="utf-8",
    )

    digest = _finalise_release_report(
        run_dir,
        main_before="a" * 40,
        merge_sha=None,
        merge_tree=None,
        merge_verdict="refused",
        merge_reasons=merge_reasons,
        merged=False,
        postmerge=None,
    )

    report_path = run_dir / "release-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["verdict"] == "release-ready", report
    assert report["release_verdict"] == "refused", report
    assert report["reasons"] == list(merge_reasons), report
    assert report["merged"] is False, report
    assert report["merge_sha"] is None, report
    assert report["postmerge"] is None, report
    assert report["merge"]["verdict"] == "refused", report["merge"]
    assert report["merge"]["reasons"] == list(merge_reasons), report["merge"]
    assert digest == sha256_file(report_path)


def test_finalise_keeps_the_gate_verdict_after_a_merged_release(tmp_path):
    """The same field keeps the same meaning on the success path."""

    run_dir = tmp_path / "release"
    run_dir.mkdir()
    (run_dir / "release-report.json").write_text(
        json.dumps({"schema_version": 1, "verdict": "release-ready", "reasons": []}),
        encoding="utf-8",
    )
    postmerge = {"verdict": "pass", "reasons": [], "commands": []}
    _finalise_release_report(
        run_dir,
        main_before="a" * 40,
        merge_sha="b" * 40,
        merge_tree="c" * 40,
        merge_verdict="merged",
        merge_reasons=(),
        merged=True,
        postmerge=postmerge,
    )
    report = json.loads((run_dir / "release-report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "release-ready", report
    assert report["release_verdict"] == "merged", report
    assert report["reasons"] == [], report
    assert report["merged"] is True, report
    assert report["merge_sha"] == "b" * 40, report
    assert report["postmerge"] == postmerge, report
