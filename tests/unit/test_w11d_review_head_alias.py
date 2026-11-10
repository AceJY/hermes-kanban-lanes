"""W11d: accept ``reviewed_commit`` as an alias for ``reviewed_head``.

Reviewer cards drifted between two spellings of the same fact.  A card that
only records ``reviewed_commit`` must still be observed and queued, while a
card whose two keys disagree is a contradiction and must fail closed -- never
guess, never queue.
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
TASK_ID = "t_alias"
MERGE_ID = f"{TASK_ID}-merge"
REQUIREMENT = "HKL-W3"
REVIEWER = "code-reviewer"
DEV_PROFILE = "hkl:claude"


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
    """A real repository: a base commit, then the reviewed commit."""

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
) -> tuple[Path, str]:
    """A development ``run.json`` plus its evidence index."""

    run_dir = work_root / "runs" / TASK_ID / f"{run_id}-dev"
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


def _native_metadata(head: str, base: str, *, key: str = "reviewed_head") -> dict:
    """Native reviewer metadata, with either spelling of the reviewed commit."""

    return {
        "review_outcome": "approved",
        "reviewer": REVIEWER,
        "review_round": 1,
        "review_lens": "artifact",
        "requirement_id": REQUIREMENT,
        key: head,
        "reviewed_tree_frozen": f"/tmp/rev-frozen (git archive {head[:7]})",
        "task_base_commit": base,
        "main_tip_at_review": base,
        "handoff_evidence": {"gate_report_verdict": "pass"},
    }


def _runs(dev_run_id: int = 1) -> list[dict]:
    return [
        {
            "id": dev_run_id,
            "profile": DEV_PROFILE,
            "status": "completed",
            "outcome": "review_requested",
            "metadata": None,
        }
    ]


class _Adapter:
    """The observe/queue slice of ``BoardAdapter`` with a real merge queue."""

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
            child for child, parents in self.parents.items() if task_id in parents
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


def _fixture(tmp_path: Path, *, key: str, head_override: str | None = None):
    repo, base, head, tree = _make_repo(tmp_path)
    config = _config(tmp_path, repo)
    work_root = Path(config.work_root)
    run_dir, index_sha = _dev_run(work_root, repo, base=base, code_commit=head)
    metadata = _native_metadata(head, base, key=key)
    if head_override is not None:
        metadata[key] = head_override
    review = {"id": 2, "profile": REVIEWER, "metadata": metadata}
    adapter = _Adapter(_runs(), [review])
    return config, work_root, repo, base, head, tree, run_dir, index_sha, adapter


# ---------------------------------------------------------------------------
# BoardAdapter.review_runs: both keys count and normalise
# ---------------------------------------------------------------------------


def _insert_run(board, task_id: str, run_id: int, metadata: dict) -> None:
    board.connection.execute(
        "INSERT INTO task_runs "
        "(id, task_id, profile, status, started_at, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, task_id, REVIEWER, "completed", 1, json.dumps(metadata)),
    )
    board.connection.commit()


def test_review_runs_accepts_reviewed_commit_alias(board, make_task):
    task_id = make_task(title="alias card")
    _insert_run(board, task_id, 2, {"reviewed_commit": "b" * 40})

    runs = board.review_runs(task_id)

    assert len(runs) == 1
    assert runs[0]["reviewed_head"] == "b" * 40
    # The canonical key is filled and the original one is kept for audit.
    assert runs[0]["metadata"]["reviewed_head"] == "b" * 40
    assert runs[0]["metadata"]["reviewed_commit"] == "b" * 40


def test_review_runs_reviewed_head_only_is_unchanged(board, make_task):
    task_id = make_task(title="canonical card")
    _insert_run(board, task_id, 2, {"reviewed_head": "c" * 40})

    runs = board.review_runs(task_id)

    assert len(runs) == 1
    assert runs[0]["reviewed_head"] == "c" * 40
    # No alias key is invented for a card that never had one.
    assert "reviewed_commit" not in runs[0]["metadata"]


def test_review_runs_conflicting_keys_are_flagged(board, make_task):
    task_id = make_task(title="conflict card")
    _insert_run(
        board, task_id, 2, {"reviewed_head": "d" * 40, "reviewed_commit": "e" * 40}
    )

    runs = board.review_runs(task_id)

    assert len(runs) == 1
    assert runs[0]["reviewed_head"] is None
    assert runs[0]["reviewed_head_conflict"] == {
        "reviewed_head": "d" * 40,
        "reviewed_commit": "e" * 40,
    }


# ---------------------------------------------------------------------------
# observe/queue: the alias queues; a contradiction never does
# ---------------------------------------------------------------------------


def test_reviewed_commit_alias_is_accepted(tmp_path):
    """Only ``reviewed_commit``: observe normalises, queue creates the card."""

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
    ) = _fixture(tmp_path, key="reviewed_commit")

    observed = observe_reviews(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )
    assert len(observed) == 1
    entry = _ledger(work_root)["reviews"][0]
    assert entry["verdict"] == "pass"
    assert entry["dev_run_id"] == 1
    assert entry["reviewed_head"] == head
    assert entry["evidence_index_sha256"] == index_sha
    assert entry["evidence_index_sha256"] == sha256_file(
        run_dir / INDEX_RELATIVE_PATH
    )
    assert entry["review_metadata_problem"] is None
    assert entry["review_metadata_reason"] is None

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )

    assert len(queued) == 1
    assert queued[0]["merge_task_id"] == MERGE_ID
    assert queued[0]["reviewed_head"] == head
    assert queued[0]["evidence_index_sha256"] == index_sha
    assert adapter.children[MERGE_ID]["idempotency_key"].startswith(
        f"hkl-release:{TASK_ID}:{head}:{index_sha}"
    )


def test_reviewed_head_only_is_unchanged(tmp_path):
    """The canonical spelling keeps behaving exactly as before."""

    (
        config,
        work_root,
        _repo,
        _base,
        head,
        _tree,
        _run,
        index_sha,
        adapter,
    ) = _fixture(tmp_path, key="reviewed_head")

    observed = observe_reviews(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )
    entry = _ledger(work_root)["reviews"][0]
    assert entry["reviewed_head"] == head
    assert entry["review_metadata_problem"] is None

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )
    assert len(queued) == 1
    assert queued[0]["merge_task_id"] == MERGE_ID
    assert queued[0]["reviewed_head"] == head
    assert len(observed) == 1


def test_conflicting_head_keys_are_skipped(tmp_path):
    """Two different heads: record the reason, create no merge card."""

    repo, base, head, _tree = _make_repo(tmp_path)
    config = _config(tmp_path, repo)
    work_root = Path(config.work_root)
    _dev_run(work_root, repo, base=base, code_commit=head)
    metadata = {
        **_native_metadata(head, base),
        "reviewed_commit": base,
    }
    review = {"id": 2, "profile": REVIEWER, "metadata": metadata}
    adapter = _Adapter(_runs(), [review])

    observed = observe_reviews(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )
    entry = _ledger(work_root)["reviews"][0]
    assert entry["verdict"] == "pass"
    assert entry["reviewed_head"] is None
    assert entry["review_metadata_problem"] == "reviewed-head-conflict"
    assert "review-metadata-incomplete:reviewed-head-conflict" == (
        entry["review_metadata_reason"]
    )

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )

    assert len(queued) == 1
    assert queued[0]["skipped"] == "review-metadata-incomplete"
    assert "reviewed-head-conflict" in queued[0]["reason"]
    assert "merge_task_id" not in queued[0]
    assert adapter.created == []
    assert observed  # the contradiction stays visible to the operator
