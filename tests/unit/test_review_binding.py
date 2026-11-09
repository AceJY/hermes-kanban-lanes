"""W4-01/W4-02: review binding recomputation and cross-round invalidation.

The reviewer's metadata is only a declaration.  Every test here starts from a
complete, independently recomputable set of facts and then breaks exactly one
of them, asserting the named ``review-*`` reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.release.binding import (
    allow_matches,
    build_binding,
    evaluate_binding,
)
from hermes_kanban_lanes.verification.evidence import (
    INDEX_RELATIVE_PATH,
    sha256_file,
    tool_digest,
    write_evidence_index,
)
from hermes_kanban_lanes.workspace.git import repo_head, run_git

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src" / "hermes_kanban_lanes"
TASK_ID = "t_w4binding"
REQUIREMENT = "HKL-W4"


def _git(*args: str, cwd: Path) -> str:
    result = run_git(args, cwd=cwd, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def dev_facts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "hkl@example.invalid", cwd=repo)
    _git("config", "user.name", "HKL Tests", cwd=repo)
    (repo / "README.txt").write_text("base\n", encoding="utf-8")
    _git("add", "README.txt", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    base = repo_head(repo)

    run_dir = tmp_path / "runs" / TASK_ID / "1-abc"
    run_dir.mkdir(parents=True)
    (run_dir / "evidence").mkdir()
    (run_dir / "commands").mkdir()

    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    _git("add", "RESULT.txt", cwd=repo)
    _git("commit", "-q", "-m", "run 1", cwd=repo)
    head = repo_head(repo)
    tree = _git("rev-parse", f"{head}^{{tree}}", cwd=repo)

    identity = RunIdentity(
        schema_version=1, task_id=TASK_ID, run_id=1, claim_lock="host:1:hkl:x"
    )
    run_document = {
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
        "workspace": str(repo),
        "gate": {"verdict": "pass"},
        "evidence": {"verified": True, "reasons": []},
        "review": None,
    }
    (run_dir / "run.json").write_text(
        json.dumps(run_document, sort_keys=True), encoding="utf-8"
    )
    gate_path = "evidence/gate-report.json"
    (run_dir / gate_path).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "requirement_id": REQUIREMENT,
                "identity": identity.to_dict(),
                "base_commit": base,
                "sync_base": base,
                "verdict": "pass",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (run_dir / "landing-plan.json").write_text(
        json.dumps(
            {"schema_version": 1, "main_commit": base, "verdict": "ready"},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
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
    return {
        "repo": repo,
        "run_dir": run_dir,
        "identity": identity,
        "base": base,
        "head": head,
        "tree": tree,
        "index_sha": index_sha,
        "metadata": metadata,
        "dev_run": dev_run,
        "review_run": review_run,
        "runs": [dev_run, review_run],
        "task_row": {"id": TASK_ID},
        "task_id": TASK_ID,
        "write_index": write_evidence_index,
    }


def _evaluate(facts, *, metadata=None, runs=None, package_dir=None, executor=None):
    review_run = dict(facts["review_run"])
    review_run["metadata"] = dict(facts["metadata"] if metadata is None else metadata)
    use_runs = list(facts["runs"] if runs is None else runs)
    if runs is None:
        use_runs[-1] = review_run
    return evaluate_binding(
        task_row=facts["task_row"],
        runs=use_runs,
        dev_run_dir=facts["run_dir"],
        repo=facts["repo"],
        requirement_id=REQUIREMENT,
        package_dir=package_dir,
        executor_profile=executor,
    )


def test_complete_binding_recomputes_green(dev_facts):
    evaluation = _evaluate(dev_facts, package_dir=SRC_DIR)
    assert evaluation.problems == ()
    binding = evaluation.binding
    assert binding is not None
    assert binding.dev_run_id == 1
    assert binding.rework_round == 1
    assert binding.task_base_commit == dev_facts["base"]
    assert binding.reviewed_head == dev_facts["head"]
    assert binding.reviewed_tree == dev_facts["tree"]
    assert binding.evidence_index_sha256 == dev_facts["index_sha"]
    assert binding.tool_sha256 == tool_digest(SRC_DIR)
    assert binding.reviewer == "code-reviewer"
    assert binding.review_run_id == 2
    assert set(binding.tuple_fields()) == {
        "task_id",
        "dev_run_id",
        "rework_round",
        "task_base_commit",
        "reviewed_head",
        "reviewed_tree",
        "evidence_index_sha256",
        "tool_sha256",
        "reviewer",
        "review_run_id",
    }
    assert build_binding(
        task_row=dev_facts["task_row"],
        runs=dev_facts["runs"],
        dev_run_dir=dev_facts["run_dir"],
        repo=dev_facts["repo"],
        requirement_id=REQUIREMENT,
        package_dir=SRC_DIR,
    ) == binding


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("verdict", "revise", "review-verdict-not-pass"),
        ("schema_version", 2, "review-metadata-schema"),
        ("requirement_id", "HKL-W3", "review-requirement-mismatch"),
        ("reviewed_head", "zz", "review-head-invalid"),
        ("reviewed_tree", "not-hex", "review-tree-invalid"),
        ("dev_run_id", 0, "review-dev-run-invalid"),
        ("dev_run_id", 9, "review-dev-run-mismatch"),
        ("rework_round", 0, "review-round-invalid"),
        ("rework_round", 2, "review-round-mismatch"),
        ("reviewer", "", "review-reviewer-invalid"),
        ("reviewer", "hkl:claude", "reviewer-is-implementer"),
        ("review_run_id", 1, "review-run-not-latest"),
    ],
)
def test_metadata_field_tampering_is_refused(dev_facts, field, value, reason):
    metadata = dict(dev_facts["metadata"])
    metadata[field] = value
    evaluation = _evaluate(dev_facts, metadata=metadata)
    assert reason in evaluation.problems, evaluation.problems
    assert build_binding(
        task_row=dev_facts["task_row"],
        runs=[
            dev_facts["dev_run"],
            {**dev_facts["review_run"], "metadata": metadata},
        ],
        dev_run_dir=dev_facts["run_dir"],
        repo=dev_facts["repo"],
        requirement_id=REQUIREMENT,
    ) is None


def test_missing_metadata_keys_are_named(dev_facts):
    metadata = dict(dev_facts["metadata"])
    del metadata["reviewed_tree"]
    del metadata["evidence_index_sha256"]
    del metadata["reasons"]
    evaluation = _evaluate(dev_facts, metadata=metadata)
    assert (
        "review-metadata-missing:evidence_index_sha256,reasons,reviewed_tree"
        in evaluation.problems
    )
    assert evaluation.binding is None


@pytest.mark.parametrize("value", ["pass", 0, None, {}])
def test_review_reasons_must_be_an_array(dev_facts, value):
    """D1 lists ``"reasons":[]``; the declaration is invalid without an array."""

    metadata = dict(dev_facts["metadata"])
    metadata["reasons"] = value
    evaluation = _evaluate(dev_facts, metadata=metadata)
    assert "review-reasons-invalid" in evaluation.problems, evaluation.problems
    assert build_binding(
        task_row=dev_facts["task_row"],
        runs=[
            dev_facts["dev_run"],
            {**dev_facts["review_run"], "metadata": metadata},
        ],
        dev_run_dir=dev_facts["run_dir"],
        repo=dev_facts["repo"],
        requirement_id=REQUIREMENT,
    ) is None


def test_missing_review_is_refused(dev_facts):
    evaluation = evaluate_binding(
        task_row=dev_facts["task_row"],
        runs=[dev_facts["dev_run"]],
        dev_run_dir=dev_facts["run_dir"],
        repo=dev_facts["repo"],
        requirement_id=REQUIREMENT,
    )
    assert evaluation.problems == ("review-missing",)
    assert evaluation.binding is None


def test_missing_task_is_refused(dev_facts):
    evaluation = evaluate_binding(
        task_row=None,
        runs=dev_facts["runs"],
        dev_run_dir=dev_facts["run_dir"],
        repo=dev_facts["repo"],
        requirement_id=REQUIREMENT,
    )
    assert evaluation.problems == ("task-missing",)


def test_reviewed_head_no_longer_tip_is_stale(dev_facts):
    repo = dev_facts["repo"]
    (repo / "AFTER.txt").write_text("moved\n", encoding="utf-8")
    _git("add", "AFTER.txt", cwd=repo)
    _git("commit", "-q", "-m", "after review", cwd=repo)
    evaluation = _evaluate(dev_facts)
    assert "review-head-not-tip" in evaluation.problems
    assert "review-stale:reviewed_head" in evaluation.problems


def test_reviewed_tree_must_match_the_commit(dev_facts):
    metadata = dict(dev_facts["metadata"])
    metadata["reviewed_tree"] = "b" * 40
    evaluation = _evaluate(dev_facts, metadata=metadata)
    assert "review-tree-mismatch" in evaluation.problems


def test_evidence_index_change_invalidates_c1_binding(dev_facts):
    index_path = dev_facts["run_dir"] / INDEX_RELATIVE_PATH
    document = json.loads(index_path.read_text(encoding="utf-8"))
    document["files"] = list(document["files"])
    index_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    evaluation = _evaluate(dev_facts)
    assert "review-index-mismatch" in evaluation.problems
    assert "review-stale:evidence_index_sha256" in evaluation.problems
    assert evaluation.binding is not None
    assert build_binding(
        task_row=dev_facts["task_row"],
        runs=dev_facts["runs"],
        dev_run_dir=dev_facts["run_dir"],
        repo=dev_facts["repo"],
        requirement_id=REQUIREMENT,
    ) is None


def test_tool_digest_change_is_stale(dev_facts, tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "module.py").write_text("one\n", encoding="utf-8")
    metadata = dict(dev_facts["metadata"])
    metadata["evidence_index_sha256"] = dev_facts["index_sha"]
    (package / "module.py").write_text("two\n", encoding="utf-8")
    evaluation = _evaluate(dev_facts, metadata=metadata, package_dir=package)
    assert evaluation.binding is not None
    assert "review-stale:tool_sha256" in evaluation.problems


def test_later_dev_run_invalidates_previous_review(dev_facts):
    later = {
        "id": 3,
        "profile": "hkl:claude",
        "status": "completed",
        "outcome": "review_requested",
        "metadata": None,
    }
    runs = [*dev_facts["runs"], later]
    evaluation = _evaluate(dev_facts, runs=runs)
    assert "review-stale:dev_run_id" in evaluation.problems
    assert evaluation.binding is not None


def test_any_later_run_invalidates_the_binding(dev_facts):
    """D3 says any newer development run, not only another review request."""

    later_failed = {
        "id": 3,
        "profile": "hkl:claude",
        "status": "failed",
        "outcome": "completed",
        "metadata": None,
    }
    evaluation = _evaluate(dev_facts, runs=[*dev_facts["runs"], later_failed])
    assert "review-stale:dev_run_id" in evaluation.problems
    assert build_binding(
        task_row=dev_facts["task_row"],
        runs=[*dev_facts["runs"], later_failed],
        dev_run_dir=dev_facts["run_dir"],
        repo=dev_facts["repo"],
        requirement_id=REQUIREMENT,
    ) is None


def test_cross_round_c1_binding_is_not_reusable_for_c2(dev_facts):
    """After a changes_requested round, the C1 tuple stays stale by design."""

    review_run = dict(dev_facts["review_run"])
    review_run["metadata"] = dict(dev_facts["metadata"])
    changes = {
        "id": 3,
        "profile": "code-reviewer",
        "status": "completed",
        "outcome": "changes_requested",
        "metadata": None,
    }
    c2_dev = {
        "id": 4,
        "profile": "hkl:claude",
        "status": "completed",
        "outcome": "review_requested",
        "metadata": None,
    }
    evaluation = _evaluate(
        dev_facts, runs=[dev_facts["dev_run"], review_run, changes, c2_dev]
    )
    assert "review-stale:dev_run_id" in evaluation.problems
    assert "review-round-mismatch" in evaluation.problems
    assert build_binding(
        task_row=dev_facts["task_row"],
        runs=[dev_facts["dev_run"], review_run, changes, c2_dev],
        dev_run_dir=dev_facts["run_dir"],
        repo=dev_facts["repo"],
        requirement_id=REQUIREMENT,
    ) is None


def test_review_run_profile_must_be_the_declared_reviewer(dev_facts):
    review_run = dict(dev_facts["review_run"])
    review_run["profile"] = "someone-else"
    review_run["metadata"] = dict(dev_facts["metadata"])
    evaluation = _evaluate(dev_facts, runs=[dev_facts["dev_run"], review_run])
    assert "review-reviewer-mismatch" in evaluation.problems


def test_executor_cannot_review_own_work(dev_facts):
    metadata = dict(dev_facts["metadata"])
    metadata["reviewer"] = "claude"
    review_run = dict(dev_facts["review_run"])
    review_run["profile"] = "claude"
    review_run["metadata"] = metadata
    evaluation = evaluate_binding(
        task_row=dev_facts["task_row"],
        runs=[dev_facts["dev_run"], review_run],
        dev_run_dir=dev_facts["run_dir"],
        repo=dev_facts["repo"],
        requirement_id=REQUIREMENT,
        executor_profile="claude",
    )
    assert "reviewer-is-executor" in evaluation.problems


def test_index_code_and_identity_must_match_the_reviewed_facts(dev_facts):
    index_path = dev_facts["run_dir"] / INDEX_RELATIVE_PATH
    document = json.loads(index_path.read_text(encoding="utf-8"))
    document["code_commit"] = "c" * 40
    document["identity"] = {**document["identity"], "run_id": 99}
    index_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    evaluation = _evaluate(dev_facts)
    assert "review-index-code-mismatch" in evaluation.problems
    assert "review-index-identity-mismatch" in evaluation.problems


def test_dev_run_document_identity_and_head_are_recomputed(dev_facts):
    run_path = dev_facts["run_dir"] / "run.json"
    document = json.loads(run_path.read_text(encoding="utf-8"))
    document["identity"] = {**document["identity"], "run_id": 77}
    document["code_commit"] = "d" * 40
    run_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    evaluation = _evaluate(dev_facts)
    assert "review-dev-run-mismatch" in evaluation.problems
    assert "review-head-not-run" in evaluation.problems


@pytest.mark.parametrize(
    ("path", "allow", "expected"),
    [
        ("RESULT.txt", ["RESULT.txt"], True),
        ("RESULT.txt", ["src/"], False),
        ("src/a.py", ["src/"], True),
        ("src", ["src/"], True),
        ("srcx/a.py", ["src/"], False),
        ("a/b.py", ["a/b.py"], True),
    ],
)
def test_allow_matcher_matches_the_gate_rule(path, allow, expected):
    assert allow_matches(path, allow) is expected
