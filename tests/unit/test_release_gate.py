"""W4-01/W4-04/W4-05: the fixed-order, fail-closed release gate.

Every test here drives the real gate against a temporary repository and a
temporary development run directory, then inspects ``release-report.json``.
The report must exist whatever the verdict: the operator needs the record even
when the gate refuses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import hermes_kanban_lanes.release.gate as gate_module
from hermes_kanban_lanes.config import (
    Executor,
    LaneConfig,
    MergeConfig,
    Repository,
    ReviewConfig,
)
from hermes_kanban_lanes.contracts import ContractError, RunIdentity
from hermes_kanban_lanes.runtime.release import _dev_cards, queue_release_cards
from hermes_kanban_lanes.release.gate import (
    RELEASE_CHECKS,
    RELEASE_REPORT_FILENAME,
    release_preflight,
)
from hermes_kanban_lanes.verification.evidence import (
    build_manifest,
    sha256_file,
    tool_digest,
    write_evidence_index,
)
from hermes_kanban_lanes.workspace.git import repo_head, run_git

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src" / "hermes_kanban_lanes"
TASK_ID = "t_w4gate"
MERGE_TASK_ID = "t_w4gate_merge"
REQUIREMENT = "HKL-W4"
MAIN_REF = "refs/heads/main"


def _git(*args: str, cwd: Path) -> str:
    result = run_git(args, cwd=cwd, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _authorization(
    *,
    base: str,
    head: str,
    tree: str,
    index_sha: str,
    review_run_id: int = 2,
    dev_run_id: int = 1,
    rework_round: int = 1,
    **overrides,
):
    document = {
        "schema_version": 1,
        "requirement_id": REQUIREMENT,
        "task_id": TASK_ID,
        "review_run_id": review_run_id,
        "dev_run_id": dev_run_id,
        "rework_round": rework_round,
        "task_base_commit": base,
        "reviewed_head": head,
        "reviewed_tree": tree,
        "evidence_index_sha256": index_sha,
        "main_ref": MAIN_REF,
        "main_commit": base,
        "approver": "operator",
        "approved_at": 1_000,
        "expires_at": 2_000,
    }
    document.update(overrides)
    return document


@pytest.fixture
def gate_facts(tmp_path):
    """A main repo at ``base`` and a separate run worktree at ``head``."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "hkl@example.invalid", cwd=repo)
    _git("config", "user.name", "HKL Tests", cwd=repo)
    (repo / "README.txt").write_text("base\n", encoding="utf-8")
    _git("add", "README.txt", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    base = repo_head(repo)

    workspace = tmp_path / "workspace"
    _git("worktree", "add", "-q", "-b", "task", str(workspace), base, cwd=repo)
    (workspace / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    _git("add", "RESULT.txt", cwd=workspace)
    _git("commit", "-q", "-m", "run 1", cwd=workspace)
    head = repo_head(workspace)
    tree = _git("rev-parse", f"{head}^{{tree}}", cwd=workspace)

    identity = RunIdentity(
        schema_version=1, task_id=TASK_ID, run_id=1, claim_lock="host:1:hkl:x"
    )
    run_dir = tmp_path / "runs" / TASK_ID / "1-abc"
    run_dir.mkdir(parents=True)
    (run_dir / "evidence").mkdir()
    (run_dir / "commands").mkdir()

    _write_json(
        run_dir / "run.json",
        {
            "schema_version": 1,
            "requirement_id": REQUIREMENT,
            "identity": identity.to_dict(),
            "board": "test",
            "category": "success",
            "base_commit": base,
            "sync_base": base,
            "task_base_commit": base,
            "continuation_base": base,
            "round": 1,
            "code_commit": head,
            "run_commit": head,
            "workspace": str(workspace),
            "gate": {"verdict": "pass"},
            "evidence": {"verified": True, "reasons": []},
            "review": None,
        },
    )
    gate_path = "evidence/gate-report.json"
    _write_json(
        run_dir / gate_path,
        {
            "schema_version": 2,
            "requirement_id": REQUIREMENT,
            "identity": identity.to_dict(),
            "base_commit": base,
            "sync_base": base,
            "run_commit": head,
            "verdict": "pass",
        },
    )
    _write_json(
        run_dir / "landing-plan.json",
        {"schema_version": 1, "main_commit": base, "verdict": "ready"},
    )
    manifest = build_manifest(
        identity,
        base_commit=base,
        code_commit=head,
        gate_report_path=gate_path,
        gate_report_sha256=sha256_file(run_dir / gate_path),
        verdict="pass",
        package_dir=SRC_DIR,
    )
    _write_json(run_dir / "evidence" / "manifest.json", manifest.to_dict())
    _document, index_sha = write_evidence_index(
        run_dir,
        identity=identity,
        base_commit=base,
        sync_base=base,
        code_commit=head,
        tool_sha256=tool_digest(SRC_DIR),
        gate_report_path=gate_path,
        gate_report_sha256=sha256_file(run_dir / gate_path),
        verdict="pass",
        requirement_id=REQUIREMENT,
    )

    metadata = {
        "schema_version": 1,
        "requirement_id": REQUIREMENT,
        "verdict": "pass",
        "reviewed_head": head,
        "reviewed_tree": tree,
        "dev_run_id": 1,
        "rework_round": 1,
        "reviewer": "code-reviewer",
        "review_run_id": 2,
        "evidence_index_sha256": index_sha,
        "reasons": [],
    }
    dev_run = {
        "id": 1,
        "profile": "hkl:claude",
        "status": "completed",
        "outcome": "review_requested",
        "metadata": None,
    }
    review_run = {
        "id": 2,
        "profile": "code-reviewer",
        "status": "completed",
        "outcome": "completed",
        "metadata": metadata,
    }

    authorization = tmp_path / "authorizations" / f"{TASK_ID}.json"
    _write_json(
        authorization,
        _authorization(base=base, head=head, tree=tree, index_sha=index_sha),
    )

    return {
        "repo": repo,
        "workspace": workspace,
        "run_dir": run_dir,
        "release_dir": tmp_path / "release",
        "authorization": authorization,
        "identity": RunIdentity(
            schema_version=1,
            task_id=MERGE_TASK_ID,
            run_id=9,
            claim_lock="host:9:hkl:merge",
        ),
        "task_row": {"id": TASK_ID, "status": "done"},
        "runs": [dev_run, review_run],
        "base": base,
        "head": head,
        "tree": tree,
        "index_sha": index_sha,
        "metadata": metadata,
    }


def _gate(facts, **overrides):
    arguments = {
        "identity": facts["identity"],
        "task_row": facts["task_row"],
        "runs": facts["runs"],
        "dev_run_dir": facts["run_dir"],
        "repo": facts["repo"],
        "package_dir": SRC_DIR,
        "main_ref": MAIN_REF,
        "authorization_path": facts["authorization"],
        "merge_enabled": True,
        "post_merge_commands": [["true"]],
        "max_rework_rounds": 3,
        "release_run_dir": facts["release_dir"],
        "requirement_id": REQUIREMENT,
        "executor_profile": "hkl:claude",
        "implementer_profile": "hkl:claude",
        "changes_events": [],
        "previous_code_commit": None,
        "now": 1_500,
    }
    arguments.update(overrides)
    return release_preflight(**arguments)


def _report(facts) -> dict:
    path = Path(facts["release_dir"]) / RELEASE_REPORT_FILENAME
    assert path.is_file(), "the gate must always write release-report.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_release_ready_runs_all_checks_in_fixed_order(gate_facts):
    decision = _gate(gate_facts)
    assert decision.verdict == "release-ready"
    assert decision.reasons == ()
    report = _report(gate_facts)
    assert [check["name"] for check in report["checks"]] == list(RELEASE_CHECKS)
    assert all(check["ok"] for check in report["checks"])
    assert report["verdict"] == "release-ready"
    assert report["reasons"] == []
    assert report["schema_version"] == 1
    assert report["requirement_id"] == REQUIREMENT
    assert report["identity"] == gate_facts["identity"].to_dict()
    assert report["task_id"] == TASK_ID
    assert report["dev_run_id"] == 1
    assert report["rework_round"] == 1
    assert report["task_base_commit"] == gate_facts["base"]
    assert report["continuation_base"] == gate_facts["base"]
    assert report["reviewed_head"] == gate_facts["head"]
    assert report["reviewed_tree"] == gate_facts["tree"]
    assert report["evidence_index_sha256"] == gate_facts["index_sha"]
    assert report["tool_sha256"] == tool_digest(SRC_DIR)
    assert report["main_ref"] == MAIN_REF
    assert report["main_commit"] == gate_facts["base"]
    assert report["authorization"]["approver"] == "operator"
    assert report["authorization"]["sha256"] == sha256_file(gate_facts["authorization"])
    assert isinstance(report["generated_at"], int)
    assert decision.report == report
    assert decision.report_path == str(
        Path(gate_facts["release_dir"]) / RELEASE_REPORT_FILENAME
    )
    assert decision.binding is not None
    assert decision.merge_tree is not None


def test_gate_never_merges_or_moves_main(gate_facts):
    before = repo_head(gate_facts["repo"])
    _gate(gate_facts)
    assert repo_head(gate_facts["repo"]) == before
    assert not (Path(gate_facts["release_dir"]) / "merge.json").exists()


def test_missing_repository_is_blocked_but_reported(gate_facts, tmp_path):
    decision = _gate(gate_facts, repo=tmp_path / "absent")
    assert decision.verdict == "blocked"
    report = _report(gate_facts)
    assert report["verdict"] == "blocked"
    workspace = next(c for c in report["checks"] if c["name"] == "workspace")
    assert workspace["detail"] == "workspace-missing"
    assert "workspace-missing" in report["reasons"]


def test_non_repository_is_blocked_but_reported(gate_facts, tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    decision = _gate(gate_facts, repo=not_a_repo)
    assert decision.verdict == "blocked"
    workspace = next(c for c in _report(gate_facts)["checks"] if c["name"] == "workspace")
    assert workspace["detail"] == "workspace-unreadable"


def test_main_must_be_the_checked_out_branch(gate_facts):
    _git("checkout", "-q", "-b", "other", cwd=gate_facts["repo"])
    decision = _gate(gate_facts)
    assert decision.verdict == "refused"
    assert "workspace-not-main" in decision.reasons


def test_dirty_workspace_is_refused(gate_facts):
    (gate_facts["repo"] / "UNTRACKED.txt").write_text("x\n", encoding="utf-8")
    decision = _gate(gate_facts)
    assert "workspace-dirty" in decision.reasons
    assert _report(gate_facts)["verdict"] == "refused"


def test_merge_disabled_refuses_even_with_a_valid_authorization(gate_facts):
    decision = _gate(gate_facts, merge_enabled=False)
    assert decision.verdict == "refused"
    assert "merge-disabled" in decision.reasons
    check = next(c for c in _report(gate_facts)["checks"] if c["name"] == "merge_enabled")
    assert check["ok"] is False


def test_empty_post_merge_commands_refuse(gate_facts):
    decision = _gate(gate_facts, post_merge_commands=[])
    assert "postmerge-commands-missing" in decision.reasons


def test_missing_authorization_refuses(gate_facts, tmp_path):
    decision = _gate(gate_facts, authorization_path=tmp_path / "absent.json")
    assert decision.verdict == "refused"
    assert "authorization-missing" in decision.reasons
    check = next(
        c for c in _report(gate_facts)["checks"] if c["name"] == "authorization"
    )
    assert check["ok"] is False


def test_expired_authorization_refuses(gate_facts):
    document = _authorization(
        base=gate_facts["base"],
        head=gate_facts["head"],
        tree=gate_facts["tree"],
        index_sha=gate_facts["index_sha"],
        expires_at=1_500,
    )
    _write_json(gate_facts["authorization"], document)
    decision = _gate(gate_facts)
    assert "authorization-expired" in decision.reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewed_head", "a" * 40),
        ("reviewed_tree", "b" * 40),
        ("evidence_index_sha256", "c" * 64),
        ("main_commit", "d" * 40),
        ("requirement_id", "HKL-W3"),
        ("task_id", "t_other"),
    ],
)
def test_authorization_must_match_every_bound_field(gate_facts, field, value):
    document = _authorization(
        base=gate_facts["base"],
        head=gate_facts["head"],
        tree=gate_facts["tree"],
        index_sha=gate_facts["index_sha"],
        **{field: value},
    )
    _write_json(gate_facts["authorization"], document)
    decision = _gate(gate_facts)
    assert f"authorization-mismatch:{field}" in decision.reasons, decision.reasons
    assert decision.verdict == "refused"


def test_review_tampering_refuses_with_named_reason(gate_facts):
    tampered = dict(gate_facts["metadata"])
    tampered["verdict"] = "revise"
    runs = [gate_facts["runs"][0], dict(gate_facts["runs"][1], metadata=tampered)]
    decision = _gate(gate_facts, runs=runs)
    assert decision.verdict == "refused"
    assert "review-verdict-not-pass" in decision.reasons
    check = next(c for c in _report(gate_facts)["checks"] if c["name"] == "review")
    assert check["ok"] is False
    assert "review-verdict-not-pass" in check["reasons"]


def test_main_moved_after_landing_is_refused(gate_facts):
    repo = gate_facts["repo"]
    (repo / "MAIN.txt").write_text("moved\n", encoding="utf-8")
    _git("add", "MAIN.txt", cwd=repo)
    _git("commit", "-q", "-m", "main moved", cwd=repo)
    decision = _gate(gate_facts)
    assert "main-moved" in decision.reasons
    check = next(
        c for c in _report(gate_facts)["checks"] if c["name"] == "main_unchanged"
    )
    assert check["ok"] is False


def test_merge_tree_conflict_is_named(gate_facts):
    repo = gate_facts["repo"]
    workspace = gate_facts["workspace"]
    (repo / "RESULT.txt").write_text("main-side\n", encoding="utf-8")
    _git("add", "RESULT.txt", cwd=repo)
    _git("commit", "-q", "-m", "main adds result", cwd=repo)
    decision = _gate(gate_facts)
    assert "merge-conflict" in decision.reasons
    check = next(c for c in _report(gate_facts)["checks"] if c["name"] == "merge_tree")
    assert check["ok"] is False
    assert "merge-conflict" in check["detail"]
    # The gate itself never touches the worktree or the index.
    assert (workspace / "RESULT.txt").read_text(encoding="utf-8") == "ok\n"


def _switch_to_round_two(gate_facts):
    """Rebuild the run facts as a valid C2 review after one changes request."""

    run_dir = Path(gate_facts["run_dir"])
    identity = RunIdentity(
        schema_version=1, task_id=TASK_ID, run_id=4, claim_lock="host:4:hkl:x"
    )
    run_document = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_document["identity"] = identity.to_dict()
    run_document["round"] = 2
    _write_json(run_dir / "run.json", run_document)

    gate_path = "evidence/gate-report.json"
    gate_document = json.loads((run_dir / gate_path).read_text(encoding="utf-8"))
    gate_document["identity"] = identity.to_dict()
    _write_json(run_dir / gate_path, gate_document)

    manifest = build_manifest(
        identity,
        base_commit=gate_facts["base"],
        code_commit=gate_facts["head"],
        gate_report_path=gate_path,
        gate_report_sha256=sha256_file(run_dir / gate_path),
        verdict="pass",
        package_dir=SRC_DIR,
    )
    _write_json(run_dir / "evidence" / "manifest.json", manifest.to_dict())
    _document, index_sha = write_evidence_index(
        run_dir,
        identity=identity,
        base_commit=gate_facts["base"],
        sync_base=gate_facts["base"],
        code_commit=gate_facts["head"],
        tool_sha256=tool_digest(SRC_DIR),
        gate_report_path=gate_path,
        gate_report_sha256=sha256_file(run_dir / gate_path),
        verdict="pass",
        requirement_id=REQUIREMENT,
    )

    metadata = dict(gate_facts["metadata"])
    metadata.update(
        {
            "dev_run_id": 4,
            "rework_round": 2,
            "review_run_id": 5,
            "evidence_index_sha256": index_sha,
        }
    )
    changes = {
        "id": 3,
        "profile": "code-reviewer",
        "status": "completed",
        "outcome": "changes_requested",
        "metadata": None,
    }
    dev_two = {
        "id": 4,
        "profile": "hkl:claude",
        "status": "completed",
        "outcome": "review_requested",
        "metadata": None,
    }
    review_two = {
        "id": 5,
        "profile": "code-reviewer",
        "status": "completed",
        "outcome": "completed",
        "metadata": metadata,
    }
    runs = [*gate_facts["runs"], changes, dev_two, review_two]
    _write_json(
        gate_facts["authorization"],
        _authorization(
            base=gate_facts["base"],
            head=gate_facts["head"],
            tree=gate_facts["tree"],
            index_sha=index_sha,
            review_run_id=5,
            dev_run_id=4,
            rework_round=2,
        ),
    )
    return runs, index_sha


def test_round_two_binding_is_release_ready(gate_facts):
    runs, index_sha = _switch_to_round_two(gate_facts)
    decision = _gate(gate_facts, runs=runs)
    assert decision.verdict == "release-ready", decision.reasons
    assert decision.binding.rework_round == 2
    assert decision.binding.evidence_index_sha256 == index_sha
    assert "release-ready" == _report(gate_facts)["verdict"]


def test_round_mismatch_surfaces_through_the_binding(gate_facts):
    changes = {
        "id": 3,
        "profile": "code-reviewer",
        "status": "completed",
        "outcome": "changes_requested",
        "metadata": None,
    }
    decision = _gate(gate_facts, runs=[*gate_facts["runs"], changes])
    # The C1 metadata claims round 1 while two rounds now exist.
    assert "review-round-mismatch" in decision.reasons
    assert "rounds-unavailable" in decision.reasons
    assert decision.verdict == "refused"


def test_same_reason_stall_is_refused(gate_facts):
    runs, _ = _switch_to_round_two(gate_facts)
    # Two identical reasons, and the previous run's code commit is the
    # reviewed head: no progress was made.
    decision = _gate(
        gate_facts,
        runs=runs,
        changes_events=[
            {"reason": "same defect"},
            {"reason": "same defect"},
        ],
        previous_code_commit=gate_facts["head"],
    )
    assert "rework-stalled-same-reason" in decision.reasons, decision.reasons
    check = next(c for c in _report(gate_facts)["checks"] if c["name"] == "rounds")
    assert check["ok"] is False
    assert check["round"] == 2


def test_different_reasons_are_not_a_stall(gate_facts):
    runs, _ = _switch_to_round_two(gate_facts)
    decision = _gate(
        gate_facts,
        runs=runs,
        changes_events=[{"reason": "defect a"}, {"reason": "defect b"}],
        previous_code_commit=gate_facts["head"],
    )
    assert decision.verdict == "release-ready", decision.reasons


def test_rework_cap_is_refused(gate_facts):
    decision = _gate(gate_facts, max_rework_rounds=0)
    assert "rework-cap:0" in decision.reasons


def test_evaluation_exception_still_writes_a_report(gate_facts, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("broken")

    monkeypatch.setattr(gate_module, "evaluate_binding", boom)
    decision = _gate(gate_facts)
    assert decision.verdict == "refused"
    assert "review-evaluation-error:RuntimeError" in decision.reasons
    assert _report(gate_facts)["verdict"] == "refused"


def test_verifier_exception_still_writes_a_report(gate_facts, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("broken")

    monkeypatch.setattr(gate_module, "verify_evidence", boom)
    decision = _gate(gate_facts)
    assert decision.verdict == "refused"
    assert "evidence:verifier-error:ValueError" in decision.reasons
    assert _report(gate_facts)["verdict"] == "refused"


# ---------------------------------------------------------------------------
# D2b: _dev_cards() also reads run.json card snapshots
# ---------------------------------------------------------------------------


def _d2b_config(tmp_path: Path) -> LaneConfig:
    return LaneConfig(
        schema_version=1,
        hermes_source=str(tmp_path),
        hermes_commit="a" * 40,
        boards={"test": str(tmp_path / "board.db")},
        repositories={
            "primary": Repository(path=str(tmp_path / "repo"), trusted=True)
        },
        executors={"claude": Executor(argv=(sys.executable,))},
        work_root=str(tmp_path / "work"),
        max_concurrent=1,
        review=ReviewConfig(merge=MergeConfig(enabled=True)),
    )


def _d2b_card(task_id: str, *, allow=("RESULT.txt",)) -> dict:
    return {
        "schema_version": 1,
        "card_kind": "implementation",
        "lineage": task_id,
        "closure_for": None,
        "executor": "claude",
        "repository": "primary",
        "allow": list(allow),
        "commands": [{"argv": [sys.executable, "-c", "pass"], "cwd": "."}],
        "risk": "normal",
        "artifacts": [],
    }


def _write_run_snapshot(
    work_root: Path, task_id: str, run_id: int, card
) -> Path:
    run_dir = Path(work_root) / "runs" / task_id / f"{run_id}-snapshot"
    run_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "identity": {
            "schema_version": 1,
            "task_id": task_id,
            "run_id": run_id,
            "claim_lock": "test",
        },
        "card": card,
    }
    (run_dir / "run.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    return run_dir


def test_dev_cards_reads_run_card_snapshot_without_explicit_json(tmp_path):
    config = _d2b_config(tmp_path)
    _write_run_snapshot(config.work_root, "t_synth", 7, _d2b_card("t_synth"))

    cards = _dev_cards(config.work_root, config)

    assert [task_id for task_id, _, _ in cards] == ["t_synth"]
    _, document, card = cards[0]
    assert document["lineage"] == "t_synth"
    assert card.card_kind == "implementation"


def test_dev_cards_prefers_explicit_card_over_run_snapshot(tmp_path):
    config = _d2b_config(tmp_path)
    _write_run_snapshot(config.work_root, "t_both", 7, _d2b_card("t_both"))
    cards_dir = Path(config.work_root) / "cards"
    cards_dir.mkdir(parents=True)
    (cards_dir / "t_both.json").write_text(
        json.dumps(_d2b_card("t_both", allow=("EXPLICIT.txt",))),
        encoding="utf-8",
    )

    cards = _dev_cards(config.work_root, config)

    assert len(cards) == 1
    assert cards[0][1]["allow"] == ["EXPLICIT.txt"]
    assert cards[0][2].allow == ("EXPLICIT.txt",)


def test_dev_cards_skips_invalid_or_non_implementation_snapshots(tmp_path):
    config = _d2b_config(tmp_path)
    _write_run_snapshot(config.work_root, "t_bad", 1, {"card_kind": "nope"})
    non_implementation = {
        **_d2b_card("t_merge"),
        "card_kind": "merge",
        "closure_for": "t_merge",
    }
    _write_run_snapshot(config.work_root, "t_merge", 1, non_implementation)

    assert _dev_cards(config.work_root, config) == []


class _QueueAdapter:
    """The slice of BoardAdapter that ``queue_release_cards`` reads."""

    def __init__(self, task_id: str):
        self._task_id = task_id

    def task_row(self, task_id: str):
        if task_id != self._task_id:
            return None
        return {"id": task_id, "status": "done"}

    def run_rows(self, task_id: str):
        return [
            {
                "id": 1,
                "profile": "hkl:claude",
                "status": "completed",
                "outcome": "review_requested",
                "metadata": None,
            }
        ]

    def review_runs(self, task_id: str):
        return [{"id": 2, "metadata": {"dev_run_id": 1}}]

    def child_ids(self, task_id: str):
        return []


# ---------------------------------------------------------------------------
# Policy authorization (owner decision 2026-11-11)
# ---------------------------------------------------------------------------


def test_file_mode_rejects_a_forged_policy_approver(gate_facts):
    _write_json(
        gate_facts["authorization"],
        _authorization(
            base=gate_facts["base"],
            head=gate_facts["head"],
            tree=gate_facts["tree"],
            index_sha=gate_facts["index_sha"],
            approver="policy:auto",
        ),
    )
    decision = _gate(gate_facts)
    assert decision.verdict == "refused"
    assert "authorization-policy-approver-forbidden" in decision.reasons
    check = next(
        c for c in _report(gate_facts)["checks"] if c["name"] == "authorization"
    )
    assert check["source"] == "file"


def test_policy_mode_normal_card_generates_an_auditable_record(gate_facts, tmp_path):
    absent = tmp_path / "authorizations" / "absent.json"
    decision = _gate(
        gate_facts,
        authorization_path=absent,
        authorization_mode="policy",
        card_risk="normal",
        authorization_window_seconds=900,
    )
    assert decision.verdict == "release-ready", decision.reasons
    assert decision.reasons == ()

    report = _report(gate_facts)
    authorization = report["authorization"]
    assert authorization["source"] == "policy"
    assert authorization["approver"] == "policy:auto"
    assert authorization["path"] == "authorization.json"
    assert authorization["policy"]["generated"] is True
    assert authorization["policy"]["high_risk"] is False

    generated = Path(gate_facts["release_dir"]) / "authorization.json"
    assert generated.is_file()
    document = json.loads(generated.read_text(encoding="utf-8"))
    assert document["source"] == "policy"
    assert document["approver"] == "policy:auto"
    assert document["expires_at"] - document["approved_at"] == 900
    assert document["approved_at"] == 1_500
    assert document["reviewed_head"] == gate_facts["head"]
    assert document["reviewed_tree"] == gate_facts["tree"]
    assert document["evidence_index_sha256"] == gate_facts["index_sha"]
    assert document["main_commit"] == gate_facts["base"]
    assert authorization["sha256"] == sha256_file(generated)


def test_policy_mode_high_risk_requires_the_operator_authorization(gate_facts):
    decision = _gate(
        gate_facts,
        authorization_path=gate_facts["authorization"].with_name("absent.json"),
        authorization_mode="policy",
        card_risk="high",
    )
    assert decision.verdict == "refused"
    assert "authorization-required:high-risk" in decision.reasons
    assert not (Path(gate_facts["release_dir"]) / "authorization.json").exists()

    # The operator (main controller) writes the approval; the change releases.
    _write_json(
        gate_facts["authorization"],
        _authorization(
            base=gate_facts["base"],
            head=gate_facts["head"],
            tree=gate_facts["tree"],
            index_sha=gate_facts["index_sha"],
        ),
    )
    decision = _gate(
        gate_facts,
        authorization_mode="policy",
        card_risk="high",
    )
    assert decision.verdict == "release-ready", decision.reasons
    assert _report(gate_facts)["authorization"]["source"] == "file"


def test_policy_mode_required_paths_force_the_operator_authorization(gate_facts):
    decision = _gate(
        gate_facts,
        authorization_path=gate_facts["authorization"].with_name("absent.json"),
        authorization_mode="policy",
        require_authorization_paths=("RESULT.txt",),
    )
    assert decision.verdict == "refused"
    assert "authorization-required:high-risk" in decision.reasons
    assert "paths:RESULT.txt" in _report(gate_facts)["authorization"]["policy"][
        "high_risk_reasons"
    ]

    # A prefix that does not cover any changed path stays on policy release.
    decision = _gate(
        gate_facts,
        authorization_path=gate_facts["authorization"].with_name("absent.json"),
        authorization_mode="policy",
        require_authorization_paths=("docs/",),
    )
    assert decision.verdict == "release-ready", decision.reasons


def test_policy_mode_does_not_bypass_binding_checks(gate_facts, tmp_path):
    tampered = dict(gate_facts["metadata"])
    tampered["verdict"] = "revise"
    runs = [gate_facts["runs"][0], dict(gate_facts["runs"][1], metadata=tampered)]
    decision = _gate(
        gate_facts,
        runs=runs,
        authorization_path=tmp_path / "absent.json",
        authorization_mode="policy",
    )
    assert decision.verdict == "refused"
    assert "review-verdict-not-pass" in decision.reasons
    assert "authorization-required:binding-unavailable" in decision.reasons
    assert not (Path(gate_facts["release_dir"]) / "authorization.json").exists()


def test_policy_mode_does_not_bypass_evidence_checks(gate_facts, tmp_path):
    (Path(gate_facts["run_dir"]) / "evidence" / "manifest.json").write_text(
        "{}\n", encoding="utf-8"
    )
    decision = _gate(
        gate_facts,
        authorization_path=tmp_path / "absent.json",
        authorization_mode="policy",
    )
    assert decision.verdict == "refused"
    assert any(
        reason.startswith("evidence:") for reason in decision.reasons
    ), decision.reasons
    # A policy record may exist, but it never turns a failed check green.
    assert _report(gate_facts)["verdict"] == "refused"


def test_policy_mode_does_not_bypass_main_unchanged(gate_facts, tmp_path):
    repo = gate_facts["repo"]
    (repo / "MAIN.txt").write_text("moved\n", encoding="utf-8")
    _git("add", "MAIN.txt", cwd=repo)
    _git("commit", "-q", "-m", "main moved", cwd=repo)
    decision = _gate(
        gate_facts,
        authorization_path=tmp_path / "absent.json",
        authorization_mode="policy",
    )
    assert decision.verdict == "refused"
    assert "main-moved" in decision.reasons


# ---------------------------------------------------------------------------
# Policy configuration (D11 additive keys)
# ---------------------------------------------------------------------------


def _policy_config_document(**merge_overrides):
    merge = {
        "enabled": True,
        "main_ref": MAIN_REF,
        "post_merge_commands": [["true"]],
        "post_merge_timeout_seconds": 60,
    }
    merge.update(merge_overrides)
    return {
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
        "review": {
            "reviewer": "code-reviewer",
            "max_rework_rounds": 3,
            "merge": merge,
        },
    }


def test_policy_config_defaults_are_file_mode():
    config = LaneConfig.from_dict(_policy_config_document())
    assert config.review is not None
    merge = config.review.merge
    assert merge.authorization == "file"
    assert merge.require_authorization_paths == ()
    assert merge.authorization_window_seconds == 900
    # Defaults stay omitted so an existing document round-trips unchanged.
    assert config.to_dict() == _policy_config_document()


def test_policy_config_parses_and_round_trips_its_keys():
    document = _policy_config_document(
        authorization="policy",
        require_authorization_paths=["src/hermes_kanban_lanes/release/"],
        authorization_window_seconds=42,
    )
    config = LaneConfig.from_dict(document)
    assert config.review is not None
    merge = config.review.merge
    assert merge.authorization == "policy"
    assert merge.require_authorization_paths == (
        "src/hermes_kanban_lanes/release/",
    )
    assert merge.authorization_window_seconds == 42
    assert config.to_dict() == document


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authorization", "auto"),
        ("authorization", "FILE"),
        ("authorization", None),
        ("require_authorization_paths", "src/"),
        ("require_authorization_paths", [""]),
        ("require_authorization_paths", ["/absolute/path"]),
        ("require_authorization_paths", ["src/../escape"]),
        ("authorization_window_seconds", 0),
        ("authorization_window_seconds", True),
        ("authorization_window_seconds", "900"),
    ],
)
def test_policy_config_invalid_values_are_rejected(field, value):
    with pytest.raises(ContractError):
        LaneConfig.from_dict(_policy_config_document(**{field: value}))


def test_policy_config_unknown_key_is_still_rejected():
    with pytest.raises(ContractError) as excinfo:
        LaneConfig.from_dict(_policy_config_document(push=True))
    assert "push" in str(excinfo.value)


def test_release_snapshot_missing_skips(tmp_path):
    config = _d2b_config(tmp_path)
    task_id = "t_snapshotless"
    run_dir = Path(config.work_root) / "runs" / task_id / "1-snapshot"
    run_dir.mkdir(parents=True)
    # A run.json from a supervisor that never wrote the card snapshot.
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "identity": {
                    "schema_version": 1,
                    "task_id": task_id,
                    "run_id": 1,
                    "claim_lock": "test",
                }
            }
        ),
        encoding="utf-8",
    )

    queued = queue_release_cards(
        config.work_root, _QueueAdapter(task_id), config, requirement_id=REQUIREMENT
    )

    assert len(queued) == 1
    assert queued[0]["dev_task_id"] == task_id
    assert queued[0]["skipped"] == "card-snapshot-missing"
    assert "card-snapshot-missing" in queued[0]["reason"]
    assert "merge_task_id" not in queued[0]
