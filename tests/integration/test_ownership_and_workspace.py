"""W2-02/05/07: claim exclusivity, per-run workspaces, takeover, landing."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from hermes_kanban_lanes.runtime.process import start_fingerprint
from hermes_kanban_lanes.runtime.supervisor import (
    allocate_run_paths,
    assess_previous_runs,
)
from hermes_kanban_lanes.verification.evidence import write_json
from hermes_kanban_lanes.workspace.git import (
    changed_paths,
    commit_run,
    create_run_worktree,
    preflight_repo,
    repo_head,
    repository_dirty,
)
from hermes_kanban_lanes.workspace.landing import check_workspace_identity


def _run_dir(work_root: Path, task_id: str, run_id: int) -> Path:
    path = work_root / "runs" / task_id / f"{run_id}-0123456789ab"
    path.mkdir(parents=True)
    return path


def _summary(pid, pgid, fingerprint) -> dict:
    return {
        "cleanup": {
            "pid": pid,
            "pgid": pgid,
            "fingerprint": fingerprint,
            "terminated": False,
        }
    }


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(int(pgid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def test_two_connections_race_for_one_ready_task(board, fresh_adapter, make_task):
    task_id = make_task(title="race")
    first = fresh_adapter(board.board_path)
    second = fresh_adapter(board.board_path)

    claims = [first.claim(task_id), second.claim(task_id)]
    assert sum(claim is not None for claim in claims) == 1
    runs = board.connection.execute(
        "SELECT count(*) FROM task_runs WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    assert runs == 1


def test_landing_identity_accepts_only_current_run(board, make_task, tmp_path):
    task_id = make_task(title="landing")
    identity = board.claim(task_id)
    assert identity is not None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    current = board.landing_witness(
        identity,
        workspace,
        workspace,
        judge=check_workspace_identity,
    )
    assert current["ok"] is True

    foreign = board.landing_witness(
        identity,
        workspace,
        other,
        judge=check_workspace_identity,
    )
    assert foreign["ok"] is False
    assert foreign["reason"] == "not-current-run-workspace"

    assert board.reclaim(identity, "lost")
    stale = board.landing_witness(
        identity,
        workspace,
        workspace,
        judge=check_workspace_identity,
    )
    assert stale["ok"] is False
    assert stale["reason"] == "stale-run-or-claim"
    assert not (tmp_path / "reclaimable").exists()


def test_run_paths_are_never_reused(tmp_path):
    root = tmp_path / "work"
    first = allocate_run_paths(root, "t_x", 1)
    second = allocate_run_paths(root, "t_x", 1)
    assert first[0] != second[0]
    assert first[1] != second[1]
    assert first[1].name.startswith("run-1-")
    assert not first[1].exists() and not second[1].exists()


def test_previous_run_assessment_three_windows(tmp_path):
    root = tmp_path / "work"
    task_id = "t_prev"

    # A: leader alive.
    alive = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        alive_fp = start_fingerprint(alive.pid)
        write_json(
            _run_dir(root, task_id, 1) / "run.json",
            _summary(alive.pid, alive.pid, alive_fp),
        )
        # B: leader dead, descendant alive in the same group.
        descendant = subprocess.Popen(
            ["sh", "-c", "sleep 60 & exit 0"], start_new_session=True
        )
        descendant_fp = start_fingerprint(descendant.pid)
        descendant.wait(timeout=10)
        time.sleep(0.2)
        write_json(
            _run_dir(root, task_id, 2) / "run.json",
            _summary(descendant.pid, descendant.pid, descendant_fp),
        )
        # C: fully terminated.
        finished = subprocess.Popen(["true"], start_new_session=True)
        finished_fp = start_fingerprint(finished.pid)
        finished.wait(timeout=10)
        write_json(
            _run_dir(root, task_id, 3) / "run.json",
            _summary(finished.pid, finished.pid, finished_fp),
        )

        assessments = {
            entry["run_id"]: entry
            for entry in assess_previous_runs(root, task_id, 99)
        }
        assert assessments[1]["terminated"] is False
        assert assessments[1]["reason"] == "owner-alive-fingerprint-match"
        assert assessments[1]["reusable"] is False
        assert assessments[2]["terminated"] is False
        assert assessments[2]["reason"] == "leader-dead-descendants-alive"
        assert assessments[2]["reusable"] is False
        assert assessments[3]["terminated"] is True
        assert assessments[3]["reusable"] is True

        reclaimable_root = root / "reclaimable"
        for run_id in (1, 2, 3):
            marker = Path(assessments[run_id]["marker"])
            assert marker.is_file()
            payload = json.loads(marker.read_text(encoding="utf-8"))
            assert payload["reusable"] is assessments[run_id]["reusable"]
            name = Path(assessments[run_id]["workspace"]).name
            reclaim_marker = reclaimable_root / f"{name}.reclaimable.json"
            unreusable_marker = reclaimable_root / f"{name}.unreusable.json"
            if assessments[run_id]["terminated"]:
                # W2-05: only a fully terminated run earns the reclaim marker.
                assert marker == reclaim_marker
                assert not unreusable_marker.exists()
            else:
                # D4: unproven termination is marked non-reusable separately.
                assert marker == unreusable_marker
                assert not reclaim_marker.exists()
            # The old path is only marked; the new run still gets a new path.
            assert name.startswith(f"run-{run_id}-")
        new_run_dir, new_workspace = allocate_run_paths(root, task_id, 100)
        assert new_workspace not in [
            Path(entry["workspace"]) for entry in assessments.values()
        ]
        assert new_run_dir not in [
            Path(entry["workspace"]) for entry in assessments.values()
        ]

        # Fail closed: unreadable process table or fingerprint means "not dead".
        unreadable = {
            "available": False,
            "pgid": None,
            "method": "ps",
            "members": [],
            "live_members": [],
            "live_pids": [],
            "zombie_pids": [],
            "error": "unreadable",
        }
        fail_closed = assess_previous_runs(
            root,
            task_id,
            101,
            members_fn=lambda pgid: unreadable,
            alive_fn=lambda pid: False,
        )
        assert all(entry["terminated"] is False for entry in fail_closed)
        assert any(
            entry["reason"] == "process-group-unreadable" for entry in fail_closed
        )

        no_fingerprint = assess_previous_runs(
            root,
            task_id,
            102,
            fingerprint_fn=lambda pid: None,
        )
        by_run = {entry["run_id"]: entry for entry in no_fingerprint}
        assert by_run[1]["terminated"] is False
        assert by_run[1]["reason"] == "current-fingerprint-unreadable"
        assert by_run[2]["terminated"] is False
        # A provably absent leader with an absent group is terminated even if
        # its historical fingerprint can no longer be read.
        assert by_run[3]["terminated"] is True
    finally:
        _kill_group(alive.pid)
        alive.wait(timeout=10)
        _kill_group(descendant.pid)


def test_git_worktrees_are_per_run_and_gate_changes(
    tmp_path, make_repo, make_config
):
    repo = make_repo()
    assert preflight_repo(repo) == (True, "ok")
    base = repo_head(repo)
    first = tmp_path / "w1"
    second = tmp_path / "w2"
    create_run_worktree(repo, base, "hkl/t/run-1", first)
    create_run_worktree(repo, base, "hkl/t/run-2", second)
    (first / "RESULT.txt").write_text("one\n", encoding="utf-8")
    (second / "RESULT.txt").write_text("two\n", encoding="utf-8")
    assert changed_paths(first) == ["RESULT.txt"]
    assert changed_paths(second) == ["RESULT.txt"]
    first_commit = commit_run(first, "run 1")
    assert first_commit != base
    assert (first / "RESULT.txt").read_text(encoding="utf-8") == "one\n"
    assert (second / "RESULT.txt").read_text(encoding="utf-8") == "two\n"

    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    # Default preflight tolerates WIP; only the write paths ask for cleanliness.
    assert preflight_repo(repo) == (True, "ok")
    assert preflight_repo(repo, require_clean=True) == (False, "repository-dirty")
    assert repository_dirty(repo) is True


def test_preflight_repo_dirty_allowed(tmp_path, make_repo):
    repo = make_repo("dirty-preflight")
    assert preflight_repo(repo) == (True, "ok")
    assert repository_dirty(repo) is False

    (repo / "WIP.txt").write_text("wip\n", encoding="utf-8")
    assert repository_dirty(repo) is True
    # HKL reads HEAD and works in its own worktree, so dirty does not block.
    assert preflight_repo(repo) == (True, "ok")
    # A checkout about to be written must stay fail-closed.
    assert preflight_repo(repo, require_clean=True) == (False, "repository-dirty")
