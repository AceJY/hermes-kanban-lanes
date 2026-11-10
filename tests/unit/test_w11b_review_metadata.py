"""W11b: normalise Hermes-native review metadata for observe and queue.

A native code-reviewer card records ``review_outcome`` + ``reviewed_head`` on
its run instead of HKL's D1 ``verdict``/``dev_run_id``/``evidence_index_sha256``
document.  The release layer derives the missing fields from the development
run index -- only when the facts are unambiguous -- and the binding still
re-verifies every one of them.  Nothing here may make a card queue on a guess.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from hermes_kanban_lanes.config import (
    Executor,
    LaneConfig,
    MergeConfig,
    Repository,
    ReviewConfig,
)
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.runtime import release as release_module
from hermes_kanban_lanes.runtime.release import observe_reviews, queue_release_cards
from hermes_kanban_lanes.verification.evidence import (
    INDEX_RELATIVE_PATH,
    sha256_file,
    tool_digest,
    write_evidence_index,
)
from hermes_kanban_lanes.workspace.git import repo_head, run_git

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src" / "hermes_kanban_lanes"
TASK_ID = "t_native"
MERGE_ID = f"{TASK_ID}-merge"
REQUIREMENT = "HKL-W3"
REVIEWER = "code-reviewer"


def _git(*args: str, cwd: Path) -> str:
    result = run_git(args, cwd=cwd, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _config(tmp_path: Path, repo: Path) -> LaneConfig:
    return LaneConfig(
        schema_version=1,
        hermes_source=str(tmp_path),
        hermes_commit="a" * 40,
        boards={"test": str(tmp_path / "board.db")},
        repositories={"primary": Repository(path=str(repo), trusted=True)},
        executors={"claude": Executor(argv=(sys.executable,))},
        work_root=str(tmp_path / "work"),
        max_concurrent=1,
        review=ReviewConfig(merge=MergeConfig(enabled=True)),
    )


def _make_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """A real repository at ``head``: base commit, then the reviewed commit."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "hkl@example.invalid", cwd=repo)
    _git("config", "user.name", "HKL Tests", cwd=repo)
    (repo / "README.txt").write_text("base\n", encoding="utf-8")
    _git("add", "README.txt", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    base = repo_head(repo)

    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    _git("add", "RESULT.txt", cwd=repo)
    _git("commit", "-q", "-m", "reviewed", cwd=repo)
    head = repo_head(repo)
    tree = _git("rev-parse", f"{head}^{{tree}}", cwd=repo)
    return repo, base, head, tree


def _card(task_id: str = TASK_ID) -> dict:
    return {
        "schema_version": 1,
        "card_kind": "implementation",
        "lineage": task_id,
        "closure_for": None,
        "executor": "claude",
        "repository": "primary",
        "allow": ["RESULT.txt"],
        "commands": [{"argv": [sys.executable, "-c", "pass"], "cwd": "."}],
        "risk": "normal",
        "artifacts": [],
    }


def _dev_run(
    work_root: Path,
    repo: Path,
    *,
    run_id: int = 1,
    base: str,
    code_commit: str,
    evidence: bool = True,
    suffix: str = "dev",
) -> tuple[Path, str | None]:
    """A development ``run.json`` plus (optionally) its evidence index."""

    run_dir = work_root / "runs" / TASK_ID / f"{run_id}-{suffix}"
    run_dir.mkdir(parents=True)
    (run_dir / "evidence").mkdir()
    identity = RunIdentity(
        schema_version=1, task_id=TASK_ID, run_id=run_id, claim_lock="host:1:hkl:x"
    )
    document = {
        "schema_version": 1,
        "requirement_id": REQUIREMENT,
        "identity": identity.to_dict(),
        "base_commit": base,
        "sync_base": base,
        "task_base_commit": base,
        "continuation_base": base,
        "round": 1,
        "code_commit": code_commit,
        "run_commit": code_commit,
        "workspace": str(repo),
        "gate": {"verdict": "pass"},
        "evidence": {"verified": True, "reasons": []},
        "card": _card(),
    }
    (run_dir / "run.json").write_text(
        json.dumps(document, sort_keys=True), encoding="utf-8"
    )
    if not evidence:
        return run_dir, None

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
    _document, index_sha = write_evidence_index(
        run_dir,
        identity=identity,
        base_commit=base,
        sync_base=base,
        code_commit=code_commit,
        tool_sha256=tool_digest(SRC_DIR),
        gate_report_path=gate_path,
        gate_report_sha256=sha256_file(run_dir / gate_path),
        verdict="pass",
        requirement_id=REQUIREMENT,
    )
    return run_dir, index_sha


def _native_metadata(head: str, base: str, *, outcome: str = "approved") -> dict:
    """The real shape written by a Hermes-native code-reviewer run."""

    return {
        "review_outcome": outcome,
        "reviewer": REVIEWER,
        "review_round": 1,
        "review_lens": "artifact",
        "requirement_id": REQUIREMENT,
        "reviewed_head": head,
        "reviewed_tree_frozen": f"/tmp/rev-frozen (git archive {head[:7]})",
        "task_base_commit": base,
        "main_tip_at_review": base,
        "handoff_evidence": {"gate_report_verdict": "pass"},
    }


def _explicit_metadata(
    head: str, tree: str, index_sha: str, *, dev_run_id: int = 1, review_run_id: int = 2
) -> dict:
    return {
        "schema_version": 1,
        "requirement_id": REQUIREMENT,
        "verdict": "pass",
        "reviewed_head": head,
        "reviewed_tree": tree,
        "dev_run_id": dev_run_id,
        "rework_round": 1,
        "reviewer": REVIEWER,
        "review_run_id": review_run_id,
        "evidence_index_sha256": index_sha,
        "reasons": [],
    }


def _runs(dev_run_id: int = 1) -> list[dict]:
    return [
        {
            "id": dev_run_id,
            "profile": "hkl:claude",
            "status": "completed",
            "outcome": "review_requested",
            "metadata": None,
        }
    ]


class _Adapter:
    """The slice of ``BoardAdapter`` used by observe/queue, with a real queue."""

    def __init__(self, runs: list[dict], reviews: list[dict]):
        self.runs = runs
        self.reviews = reviews
        self.children: dict[str, dict] = {}
        self.parents: dict[str, list[str]] = {}
        self.created: list[dict] = []

    def task_row(self, task_id: str):
        if task_id == TASK_ID:
            return {"id": task_id, "status": "done"}
        return self.children.get(task_id)

    def run_rows(self, task_id: str):
        return [dict(run) for run in self.runs] if task_id == TASK_ID else []

    def review_runs(self, task_id: str):
        return [dict(run) for run in self.reviews] if task_id == TASK_ID else []

    def child_ids(self, task_id: str):
        return sorted(
            child
            for child, parents in self.parents.items()
            if task_id in parents
        )

    def create_task(self, *, title, body, assignee, parents, idempotency_key):
        self.children[MERGE_ID] = {
            "id": MERGE_ID,
            "status": "queued",
            "assignee": assignee,
            "idempotency_key": idempotency_key,
        }
        self.parents[MERGE_ID] = list(parents)
        self.created.append(
            {
                "title": title,
                "body": body,
                "assignee": assignee,
                "parents": list(parents),
                "idempotency_key": idempotency_key,
            }
        )
        return MERGE_ID

    def link_tasks(self, parent: str, child: str) -> None:
        self.parents.setdefault(child, []).append(parent)

    def parent_ids(self, task_id: str):
        return list(self.parents.get(task_id, []))


def _ledger(work_root: Path) -> dict:
    return json.loads((work_root / "ledger" / f"{TASK_ID}.json").read_text())


def _native_fixture(tmp_path: Path, *, outcome: str = "approved"):
    repo, base, head, tree = _make_repo(tmp_path)
    config = _config(tmp_path, repo)
    work_root = Path(config.work_root)
    run_dir, index_sha = _dev_run(work_root, repo, base=base, code_commit=head)
    review = {"id": 2, "profile": REVIEWER, "metadata": _native_metadata(head, base, outcome=outcome)}
    adapter = _Adapter(_runs(), [review])
    return config, work_root, repo, base, head, tree, run_dir, index_sha, adapter


def test_native_review_metadata_is_normalised(tmp_path):
    """observe derives the D1 fields and queue creates the merge card."""

    (
        config,
        work_root,
        _repo,
        _base,
        head,
        _tree,
        run_dir,
        index_sha,
        adapter,
    ) = _native_fixture(tmp_path)

    observed = observe_reviews(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )
    assert len(observed) == 1
    entry = _ledger(work_root)["reviews"][0]
    assert entry["verdict"] == "pass"
    assert entry["dev_run_id"] == 1
    assert entry["evidence_index_sha256"] == index_sha
    assert entry["evidence_index_sha256"] == sha256_file(run_dir / INDEX_RELATIVE_PATH)
    assert entry["reviewed_head"] == head
    assert entry["reviewer"] == REVIEWER
    assert entry["review_outcome"] == "approved"
    assert entry["review_metadata_problem"] is None
    assert entry["review_metadata_reason"] is None

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )

    assert len(queued) == 1
    assert queued[0]["dev_task_id"] == TASK_ID
    assert queued[0]["merge_task_id"] == MERGE_ID
    assert queued[0]["reviewed_head"] == head
    assert queued[0]["evidence_index_sha256"] == index_sha
    assert "skipped" not in queued[0]
    assert adapter.children[MERGE_ID]["idempotency_key"].startswith(
        f"hkl-release:{TASK_ID}:{head}:{index_sha}"
    )


def test_explicit_review_metadata_is_unchanged(tmp_path):
    """A complete HKL metadata document keeps working exactly as before."""

    repo, base, head, tree = _make_repo(tmp_path)
    config = _config(tmp_path, repo)
    work_root = Path(config.work_root)
    _run_dir, index_sha = _dev_run(work_root, repo, base=base, code_commit=head)
    review = {
        "id": 2,
        "profile": REVIEWER,
        "metadata": _explicit_metadata(head, tree, index_sha),
    }

    queued = queue_release_cards(
        work_root, _Adapter(_runs(), [review]), config, requirement_id=REQUIREMENT
    )

    assert len(queued) == 1
    assert queued[0]["merge_task_id"] == MERGE_ID
    assert queued[0]["reviewed_head"] == head
    assert queued[0]["evidence_index_sha256"] == index_sha


def test_revise_outcome_is_not_queued(tmp_path):
    (config, work_root, _repo, _base, _head, _tree, _run, _sha, adapter) = (
        _native_fixture(tmp_path, outcome="revise")
    )

    observe_reviews(work_root, adapter, config, requirement_id=REQUIREMENT)

    assert _ledger(work_root)["reviews"][0]["verdict"] == "revise"
    assert queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    ) == []


def test_ambiguous_reviewed_head_is_skipped(tmp_path):
    """Two runs on the same commit: never pick one, record the reason."""

    repo, base, head, _tree = _make_repo(tmp_path)
    config = _config(tmp_path, repo)
    work_root = Path(config.work_root)
    _dev_run(work_root, repo, run_id=1, base=base, code_commit=head, suffix="first")
    _dev_run(work_root, repo, run_id=3, base=base, code_commit=head, suffix="second")
    review = {
        "id": 4,
        "profile": REVIEWER,
        "metadata": _native_metadata(head, base),
    }
    adapter = _Adapter(_runs(dev_run_id=3), [review])

    observed = observe_reviews(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )
    entry = _ledger(work_root)["reviews"][0]
    assert entry["verdict"] == "pass"
    assert entry["dev_run_id"] is None
    assert entry["review_metadata_problem"] == "dev-run-ambiguous"
    assert "review-metadata-incomplete:dev-run-ambiguous" in entry["review_metadata_reason"]

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )

    assert len(queued) == 1
    assert queued[0]["skipped"] == "review-metadata-incomplete"
    assert "dev-run-ambiguous" in queued[0]["reason"]
    assert "merge_task_id" not in queued[0]
    assert adapter.created == []
    assert observed  # the observation is still visible to the operator


def test_missing_reviewed_head_run_is_skipped(tmp_path):
    """No run matches ``reviewed_head``: fail closed with a named reason."""

    repo, base, head, _tree = _make_repo(tmp_path)
    config = _config(tmp_path, repo)
    work_root = Path(config.work_root)
    # The only run ended on a different commit.
    _dev_run(work_root, repo, base=base, code_commit=base)
    review = {
        "id": 2,
        "profile": REVIEWER,
        "metadata": _native_metadata(head, base),
    }
    adapter = _Adapter(_runs(), [review])

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )

    assert len(queued) == 1
    assert queued[0]["skipped"] == "review-metadata-incomplete"
    assert "dev-run-not-found" in queued[0]["reason"]
    assert adapter.created == []


def test_missing_evidence_index_is_skipped(tmp_path):
    """The unique run is found but its evidence index is absent: skip."""

    repo, base, head, _tree = _make_repo(tmp_path)
    config = _config(tmp_path, repo)
    work_root = Path(config.work_root)
    _dev_run(work_root, repo, base=base, code_commit=head, evidence=False)
    review = {
        "id": 2,
        "profile": REVIEWER,
        "metadata": _native_metadata(head, base),
    }
    adapter = _Adapter(_runs(), [review])

    observed = observe_reviews(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )
    entry = _ledger(work_root)["reviews"][0]
    assert entry["dev_run_id"] == 1
    assert entry["verdict"] == "pass"
    assert entry["evidence_index_sha256"] is None
    assert entry["review_metadata_problem"] == "evidence-index-missing"

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )
    assert len(queued) == 1
    assert queued[0]["skipped"] == "review-metadata-incomplete"
    assert "evidence-index-missing" in queued[0]["reason"]
    assert adapter.created == []
    assert observed


def test_outcome_mapping_is_required(tmp_path, monkeypatch):
    """Mutation guard: without the outcome map the native card cannot queue."""

    (config, work_root, _repo, _base, _head, _tree, _run, _sha, adapter) = (
        _native_fixture(tmp_path)
    )
    monkeypatch.setattr(release_module, "_REVIEW_OUTCOME_VERDICTS", {})

    observe_reviews(work_root, adapter, config, requirement_id=REQUIREMENT)
    entry = _ledger(work_root)["reviews"][0]
    assert entry["verdict"] is None
    assert entry["review_metadata_problem"] == "verdict"

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )
    assert len(queued) == 1
    assert queued[0]["skipped"] == "review-metadata-incomplete"
    assert "verdict" in queued[0]["reason"]
    assert adapter.created == []
