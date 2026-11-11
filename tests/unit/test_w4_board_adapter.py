"""W4: every reviewer receipt shape either queues or is named.

Production run 625 proved the old ``review_runs()`` filter dropped a PASSing
review because it only looked for ``reviewed_head`` / ``reviewed_commit``.
The four shapes below are the receipts observed on one machine:

===  ====================================================
run  keys actually written
===  ====================================================
612  reviewed_head, reviewed_tree_commit, round
616  reviewed_commit, frozen_tree_sha256, base_commit
620  frozen_tree, changed_files (no head key at all)
625  frozen_commit, frozen_tree (no reviewed_head/commit)
===  ====================================================

The chosen policy is the explicit-alias one: ``frozen_commit`` is the only new
head synonym and the queue report records ``review-receipt-alias:<key>``.
``frozen_tree`` is **not** a tree synonym -- the observed values are a dict
(run 620) and a temporary path (run 625) -- so the tree is derived from the
reviewed commit instead.  Every receipt without a mappable head is reported as
``review-receipt-unknown-keys:<card>`` -- never dropped, never a crash.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from hermes_kanban_lanes.config import (
    Executor,
    LaneConfig,
    MergeConfig,
    Repository,
    ReviewConfig,
)
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.runtime.release import (
    _review_release_metadata,
    queue_release_cards,
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
TASK_ID = "t_receipt"
MERGE_ID = f"{TASK_ID}-merge"
REQUIREMENT = "HKL-W4"
REVIEWER = "code-reviewer"
DEV_PROFILE = "hkl:claude"

# Real values from production reviewer run 625.  The fixture must not replace
# them with synthetic 40-hex strings: a synthetic tree is exactly what hid the
# ``frozen_tree`` mapping bug.
REAL_625_HEAD = "3835b693e04c9a296fbfaa56f61a8cfe4d0ca1cb"
REAL_625_TREE = "959c0672dd40343b126a73c2abadafc754d50d44"
REAL_625_FROZEN_TREE = "/tmp/cr_t_d6dde27e_r2"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


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


def _card() -> dict:
    return {
        "schema_version": 1,
        "card_kind": "implementation",
        "lineage": TASK_ID,
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
    base: str,
    code_commit: str,
    round_number: int = 1,
) -> tuple[Path, str]:
    run_dir = work_root / "runs" / TASK_ID / "1-dev"
    run_dir.mkdir(parents=True)
    (run_dir / "evidence").mkdir()
    identity = RunIdentity(
        schema_version=1, task_id=TASK_ID, run_id=1, claim_lock="host:1:hkl:x"
    )
    document = {
        "schema_version": 1,
        "requirement_id": REQUIREMENT,
        "identity": identity.to_dict(),
        "base_commit": base,
        "sync_base": base,
        "task_base_commit": base,
        "continuation_base": base,
        "round": round_number,
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
        # The real adapter's ``review_runs()`` is a filter over these same
        # rows, so the test slice must expose both sets here.
        if task_id != TASK_ID:
            return []
        merged: dict[int, dict] = {}
        for run in [*self.runs, *self.reviews]:
            merged[int(run.get("id") or 0)] = dict(run)
        return [merged[run_id] for run_id in sorted(merged)]

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
        self.created.append({"assignee": assignee, "parents": list(parents)})
        return MERGE_ID

    def link_tasks(self, parent: str, child: str) -> None:
        self.parents.setdefault(child, []).append(parent)

    def parent_ids(self, task_id: str):
        return list(self.parents.get(task_id, []))


def _runs() -> list[dict]:
    return [
        {
            "id": 1,
            "profile": DEV_PROFILE,
            "status": "completed",
            "outcome": "review_requested",
            "metadata": None,
        }
    ]


def _insert_run(board, task_id: str, run_id: int, metadata: dict) -> None:
    board.connection.execute(
        "INSERT INTO task_runs "
        "(id, task_id, profile, status, started_at, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, task_id, REVIEWER, "completed", 1, json.dumps(metadata)),
    )
    board.connection.commit()


# The four receipt shapes.  Head/tree arguments are supplied by each test so
# the queue tests can use a real commit.
def _shape_612(head: str) -> dict:
    return {
        "reviewed_head": head,
        "reviewed_tree_commit": "b" * 40,
        "round": 1,
        "review_outcome": "approved",
    }


def _shape_616(head: str, base: str) -> dict:
    return {
        "reviewed_commit": head,
        # The real receipt stores a per-file digest map here, not a 64-hex
        # string.  It must be ignored, not treated as a tree.
        "frozen_tree_sha256": {"src/example.py": "c" * 64},
        "base_commit": base,
        "verdict": "pass",
        "review_outcome": "approved",
        "review_round": 1,
    }


def _shape_620() -> dict:
    # Run 620 really wrote a dict here; the OLD conflict set comprehension
    # hashed it and raised TypeError, killing the whole queue pass.
    return {
        "frozen_tree": {
            "clean": True,
            "commit": "63d734de4f0c7fdc8b24cb146239ff414cf937b0",
            "files": {"src/example.py": "d" * 64},
        },
        "changed_files": ["src/example.py"],
        "review_outcome": "approved",
        "review_round": 1,
    }


def _shape_625(
    head: str = REAL_625_HEAD,
    frozen_tree: str = REAL_625_FROZEN_TREE,
) -> dict:
    # Run 625 really wrote a temporary *path* here, not a git tree id.
    return {
        "frozen_commit": head,
        "frozen_tree": frozen_tree,
        "review_outcome": "approved",
        "review_round": 2,
    }


def _board_review(
    board, make_task, metadata: dict, *, run_id: int = 2
) -> dict:
    """Run one receipt through the real ``BoardAdapter.review_runs``."""

    task_id = make_task(title="receipt shape")
    _insert_run(board, task_id, run_id, metadata)
    reviews = board.review_runs(task_id)
    assert len(reviews) == 1, reviews
    return reviews[0]


# ---------------------------------------------------------------------------
# BoardAdapter.review_runs: recognise, normalise, never silently drop
# ---------------------------------------------------------------------------


def test_shape_612_is_recognised(board, make_task):
    head = "a" * 40
    run = _board_review(board, make_task, _shape_612(head))

    assert run["reviewed_head"] == head
    # ``reviewed_tree_commit`` is a frozen-archive note, not the git tree.
    assert "reviewed_tree" not in run["metadata"]
    assert "review_receipt_aliases" not in run


def test_shape_616_reviewed_commit_is_normalised(board, make_task):
    head = "b" * 40
    run = _board_review(board, make_task, _shape_616(head, "e" * 40))

    assert run["reviewed_head"] == head
    assert run["metadata"]["reviewed_head"] == head
    assert run["metadata"]["reviewed_commit"] == head
    assert run["review_receipt_aliases"] == ["reviewed_commit"]


def test_shape_625_frozen_commit_is_mapped_tree_is_not(board, make_task):
    run = _board_review(board, make_task, _shape_625())

    assert run["reviewed_head"] == REAL_625_HEAD
    assert run["metadata"]["reviewed_head"] == REAL_625_HEAD
    # ``frozen_tree`` is a /tmp path, not a git tree id: never mapped.
    assert "reviewed_tree" not in run["metadata"]
    # Only the confirmed head synonym is recorded, nothing fuzzy.
    assert run["review_receipt_aliases"] == ["frozen_commit"]


def test_shape_620_is_returned_with_a_named_problem(board, make_task):
    # A dict under ``frozen_tree`` must neither be hashed nor mapped.
    run = _board_review(board, make_task, _shape_620())

    assert run["reviewed_head"] is None
    assert run["review_receipt_problem"] == "reviewed-head-missing"
    assert "frozen_tree" in run["review_receipt_keys"]
    assert "changed_files" in run["review_receipt_keys"]


def test_non_string_head_is_a_named_problem_not_a_crash(board, make_task):
    metadata = {
        "frozen_commit": {"commit": "63d734de4f0c7fdc8b24cb146239ff414cf937b0"},
        "review_outcome": "approved",
    }
    run = _board_review(board, make_task, metadata)

    assert run["reviewed_head"] is None
    assert run["review_receipt_problem"] == "reviewed-head-invalid"
    assert run["review_receipt_invalid_keys"] == ["frozen_commit"]


def test_conflicting_head_spellings_still_fail_closed(board, make_task):
    metadata = {"reviewed_head": "a" * 40, "frozen_commit": "b" * 40}
    run = _board_review(board, make_task, metadata)

    assert run["reviewed_head"] is None
    assert run["reviewed_head_conflict"] == {
        "reviewed_head": "a" * 40,
        "frozen_commit": "b" * 40,
    }


# ---------------------------------------------------------------------------
# queue_release_cards: the four real shapes end-to-end
# ---------------------------------------------------------------------------


def _queue_fixture(tmp_path: Path):
    repo, base, head, tree = _make_repo(tmp_path)
    config = _config(tmp_path, repo)
    work_root = Path(config.work_root)
    run_dir, index_sha = _dev_run(work_root, repo, base=base, code_commit=head)
    return config, work_root, repo, base, head, tree, run_dir, index_sha


@pytest.mark.parametrize("shape", ["612", "616"])
def test_shapes_with_a_head_key_still_queue(tmp_path, board, make_task, shape):
    config, work_root, repo, base, head, tree, _run_dir, index_sha = (
        _queue_fixture(tmp_path)
    )
    metadata = _shape_612(head) if shape == "612" else _shape_616(head, base)
    review = _board_review(board, make_task, metadata)
    adapter = _Adapter(_runs(), [review])

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )

    assert len(queued) == 1, queued
    assert queued[0]["merge_task_id"] == MERGE_ID
    assert queued[0]["reviewed_head"] == head
    assert queued[0]["evidence_index_sha256"] == index_sha


@pytest.fixture
def real_625_worktree(tmp_path):
    """A checkout at the real reviewed commit 3835b69 (from this repo)."""

    check = run_git(["cat-file", "-t", REAL_625_HEAD], cwd=REPO_ROOT, timeout=30)
    if check.returncode != 0:
        pytest.skip(f"real reviewed commit {REAL_625_HEAD} not in {REPO_ROOT}")
    worktree = tmp_path / "wt-625"
    added = run_git(
        ["worktree", "add", "--detach", "--force", str(worktree), REAL_625_HEAD],
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert added.returncode == 0, added.stderr
    try:
        yield worktree
    finally:
        run_git(
            ["worktree", "remove", "--force", str(worktree)],
            cwd=REPO_ROOT,
            timeout=60,
        )
        run_git(["worktree", "prune"], cwd=REPO_ROOT, timeout=60)


def test_shape_625_queues_via_the_explicit_alias(
    tmp_path, board, make_task, real_625_worktree
):
    """The real run-625 receipt: dict-free head alias + /tmp frozen_tree."""

    repo = real_625_worktree
    config = _config(tmp_path, repo)
    work_root = Path(config.work_root)
    _run_dir, index_sha = _dev_run(
        work_root,
        repo,
        base=REAL_625_HEAD,
        code_commit=REAL_625_HEAD,
        round_number=2,
    )
    review = _board_review(board, make_task, _shape_625(), run_id=3)
    prior_round = {
        "id": 2,
        "profile": REVIEWER,
        "status": "done",
        "outcome": "changes_requested",
        "metadata": None,
    }
    adapter = _Adapter(_runs() + [prior_round], [review])

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )

    assert len(queued) == 1, queued
    # The real production head, not a guessed "sha-like" value.
    assert queued[0]["reviewed_head"] == REAL_625_HEAD
    assert queued[0]["evidence_index_sha256"] == index_sha
    assert queued[0]["review_receipt_aliases"] == [
        "review-receipt-alias:frozen_commit"
    ]
    assert adapter.created == [{"assignee": "hkl:merge", "parents": [TASK_ID]}]


def test_shape_620_is_named_not_silent(tmp_path, board, make_task):
    config, work_root, repo, base, head, _tree, _run_dir, _index = _queue_fixture(
        tmp_path
    )
    review = _board_review(board, make_task, _shape_620())
    adapter = _Adapter(_runs(), [review])

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )

    assert len(queued) == 1, queued
    assert queued[0]["skipped"] == f"review-receipt-unknown-keys:{TASK_ID}"
    assert f"review-receipt-unknown-keys:{TASK_ID}" in queued[0]["reason"]
    assert "keys=" in queued[0]["reason"]
    assert "frozen_tree" in queued[0]["reason"]
    assert "missing=reviewed_head" in queued[0]["reason"]
    assert adapter.created == []


def test_profile_only_receipt_is_named_not_silent(tmp_path):
    config, work_root, repo, base, head, _tree, _run_dir, _index = _queue_fixture(
        tmp_path
    )
    # No key HKL maps, but the run's profile *is* the reviewer: still visible.
    dropped = {
        "id": 3,
        "profile": REVIEWER,
        "status": "completed",
        "metadata": {"changed_files": ["src/example.py"]},
    }
    adapter = _Adapter(_runs() + [dropped], [])

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )

    assert len(queued) == 1, queued
    assert queued[0]["skipped"] == f"review-receipt-unknown-keys:{TASK_ID}"
    assert "keys=changed_files" in queued[0]["reason"]
    assert adapter.created == []


def test_release_normaliser_maps_frozen_head_without_the_board(tmp_path):
    """The release path is alias-aware even if the adapter returns raw rows."""

    config, work_root, repo, base, head, tree, _run_dir, _index = _queue_fixture(
        tmp_path
    )
    metadata = {
        "frozen_commit": head,
        # A frozen-worktree path must never become ``reviewed_tree``.
        "frozen_tree": REAL_625_FROZEN_TREE,
        "review_outcome": "approved",
    }
    normalised = _review_release_metadata(
        work_root,
        TASK_ID,
        {"id": 2, "profile": REVIEWER, "metadata": metadata},
        config=config,
        requirement_id=REQUIREMENT,
    )

    assert normalised["problem"] is None
    assert normalised["reviewed_head"] == head
    # Derived from the reviewed head, not copied from ``frozen_tree``.
    assert normalised["reviewed_tree"] == tree
    assert normalised["aliases"] == ["review-receipt-alias:frozen_commit"]


def test_normaliser_names_a_non_string_tree_instead_of_crashing(tmp_path):
    config, work_root, repo, base, head, _tree, _run_dir, _index = _queue_fixture(
        tmp_path
    )
    normalised = _review_release_metadata(
        work_root,
        TASK_ID,
        {
            "id": 2,
            "profile": REVIEWER,
            "metadata": {
                "frozen_commit": head,
                "reviewed_tree": {"commit": "deadbeef"},
                "review_outcome": "approved",
            },
        },
        config=config,
        requirement_id=REQUIREMENT,
    )

    assert normalised["problem"] == "reviewed-tree-invalid"
    assert normalised["reason"] == "review-metadata-incomplete:reviewed-tree-invalid"
    assert normalised["reviewed_tree"] is None


def test_one_unreadable_card_does_not_kill_the_queue(tmp_path, board, make_task):
    """A card whose review rows cannot be read yields a named skip only."""

    config, work_root, repo, base, head, _tree, _run_dir, _index = _queue_fixture(
        tmp_path
    )

    class _BrokenAdapter(_Adapter):
        def review_runs(self, task_id: str):
            raise TypeError("unhashable type: 'dict'")

    adapter = _BrokenAdapter(_runs(), [])

    queued = queue_release_cards(
        work_root, adapter, config, requirement_id=REQUIREMENT
    )

    assert len(queued) == 1, queued
    assert queued[0]["skipped"] == f"review-receipt-unreadable:{TASK_ID}"
    assert "TypeError" in queued[0]["reason"]
    assert adapter.created == []
