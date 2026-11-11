"""W3 evidence regressions for zero-commit (no-op) rework rounds.

A rework round that authored no commit has no post-sync commit for ``sync_base``
to be an ancestor of.  The supervisor names that shape ``rework-no-changes`` and
blocks before the gate/evidence stages; if the verifier is ever asked about the
shape directly it must not mislabel it as evidence tampering.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.runtime.supervisor import rework_context
from hermes_kanban_lanes.verification.evidence import (
    build_manifest,
    sha256_file,
    verify_evidence,
    write_evidence_index,
    write_json,
)
from hermes_kanban_lanes.workspace.git import repo_head, run_git

TASK_ID = "t_noop_evidence"
IDENTITY = RunIdentity(
    schema_version=1,
    task_id=TASK_ID,
    run_id=2,
    claim_lock="lock-noop",
)


def _git(*args: str, cwd: Path) -> str:
    result = run_git(args, cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git(
        "-c",
        "user.name=HKL Tests",
        "-c",
        "user.email=hkl@example.invalid",
        "commit",
        "--no-verify",
        "-m",
        f"add {name}",
        cwd=repo,
    )
    return repo_head(repo)


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("__version__ = '0'\n", encoding="utf-8")
    return package


def _no_op_evidence(tmp_path: Path, repo: Path) -> dict:
    """A run branch that stayed at ``base`` while main advanced to ``sync``."""

    base = repo_head(repo)
    workspace = tmp_path / "workspace"
    _git("clone", "-q", str(repo), str(workspace), cwd=tmp_path)
    sync = _commit(repo, "MAIN.txt", "advanced\n")
    assert sync != base

    package = _package(tmp_path)
    run_dir = tmp_path / "run"
    (run_dir / "evidence").mkdir(parents=True)
    gate_path = "evidence/gate-report.json"
    gate_report = {
        "schema_version": 2,
        "requirement_id": "HKL-W3",
        "identity": IDENTITY.to_dict(),
        "base_commit": base,
        "run_commit": base,
        "sync_base": sync,
        "checks": [],
        "verdict": "pass",
    }
    write_json(run_dir / gate_path, gate_report)
    gate_sha = sha256_file(run_dir / gate_path)

    manifest = build_manifest(
        IDENTITY,
        base_commit=base,
        code_commit=base,
        gate_report_path=gate_path,
        gate_report_sha256=gate_sha,
        verdict="pass",
        package_dir=package,
    )
    write_json(run_dir / "evidence" / "manifest.json", manifest.to_dict())
    write_evidence_index(
        run_dir,
        identity=IDENTITY,
        base_commit=base,
        sync_base=sync,
        code_commit=base,
        tool_sha256=manifest.tool_sha256,
        gate_report_path=gate_path,
        gate_report_sha256=gate_sha,
        verdict="pass",
    )
    return {
        "run_dir": run_dir,
        "workspace": workspace,
        "package": package,
        "base": base,
        "sync": sync,
    }


def test_no_op_round_is_not_sync_ancestry_tampering(tmp_path, make_repo):
    repo = make_repo()
    facts = _no_op_evidence(tmp_path, repo)

    verification = verify_evidence(
        facts["run_dir"],
        IDENTITY,
        facts["package"],
        facts["workspace"],
        repo,
    )

    assert verification.ok is True, verification.reasons
    assert "sync-base-not-ancestor" not in verification.reasons
    assert verification.checks["ancestry"] is True
    assert json.loads(
        (facts["run_dir"] / "evidence" / "index.json").read_text(encoding="utf-8")
    )["code_commit"] == facts["base"]


def _write_run(
    work_root: Path,
    run_id: int,
    *,
    code_commit: str,
    task_base_commit: str,
    base_commit: str,
) -> None:
    directory = work_root / "runs" / TASK_ID / f"{run_id}-run"
    directory.mkdir(parents=True)
    document = {
        "schema_version": 1,
        "identity": {"schema_version": 1, "task_id": TASK_ID, "run_id": run_id},
        "round": 1,
        "task_base_commit": task_base_commit,
        "continuation_base": base_commit,
        "base_commit": base_commit,
        "code_commit": code_commit,
    }
    (directory / "run.json").write_text(
        json.dumps(document, sort_keys=True), encoding="utf-8"
    )


def test_rework_context_carries_the_review_request(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    first = _commit(repo, "one.txt", "one\n")
    _write_run(
        tmp_path,
        1,
        code_commit=first,
        task_base_commit=base,
        base_commit=base,
    )

    context = rework_context(
        work_root=tmp_path,
        task_id=TASK_ID,
        current_run_id=2,
        repository=str(repo),
        events=[{"reason": "fix docs/10 hard protection root"}],
        max_rounds=3,
    )

    assert context["error"] is None
    assert context["continuation_base"] == first
    assert context["changes_requested"] == ["fix docs/10 hard protection root"]
    assert (
        context["changes_requested_summary"] == "fix docs/10 hard protection root"
    )


def test_rework_without_readable_review_input_is_refused(tmp_path, make_repo):
    repo = make_repo()
    base = repo_head(repo)
    first = _commit(repo, "one.txt", "one\n")
    _write_run(
        tmp_path,
        1,
        code_commit=first,
        task_base_commit=base,
        base_commit=base,
    )

    for events in ([{}], [{"reason": "   "}], [{"reason": None}]):
        context = rework_context(
            work_root=tmp_path,
            task_id=TASK_ID,
            current_run_id=2,
            repository=str(repo),
            events=events,
            max_rounds=3,
        )
        assert context["error"] == "rework-input-missing", events
        assert context["error_kind"] == "needs_input"
