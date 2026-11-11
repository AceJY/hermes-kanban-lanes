"""W11e: the review run identity supplies ``reviewer``; skips stay visible.

A Hermes-native review run records the reviewed commit and outcome but not the
D1 ``reviewer`` key.  The profile recorded on that run is a mechanical fact,
so the release normaliser uses it -- ambiguity is never resolved from prose.
A card that still cannot bind must appear in ``queue_release_cards()`` output
with a named reason instead of disappearing silently.
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
TASK_ID = "t_queue"
MERGE_ID = f"{TASK_ID}-merge"
REQUIREMENT = "HKL-W4"
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


def _dev_run(work_root: Path, repo: Path, *, base: str, code_commit: str) -> tuple[Path, str]:
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


def _native_metadata(head: str, tree: str) -> dict:
    """The actual native shape: no ``reviewer``, no ``dev_run_id``."""

    return {
        "verdict": "pass",
        "review_outcome": "approved",
        "reviewed_commit": head,
        "reviewed_tree": tree,
        "review_round": 1,
    }


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


def _fixture(
    tmp_path: Path, *, review_profile: str | None = REVIEWER
) -> tuple[LaneConfig, Path, dict, _Adapter]:
    repo, base, head, tree = _make_repo(tmp_path)
    config = _config(tmp_path, repo)
    work_root = Path(config.work_root)
    run_dir, index_sha = _dev_run(work_root, repo, base=base, code_commit=head)
    metadata = _native_metadata(head, tree)
    review = {"id": 2, "profile": review_profile, "metadata": metadata}
    return config, run_dir, metadata, _Adapter(_runs(), [review])


def test_reviewer_is_derived_from_the_review_run_profile(tmp_path):
    config, run_dir, metadata, _adapter = _fixture(tmp_path)

    normalised = _review_release_metadata(
        Path(config.work_root),
        TASK_ID,
        {"id": 2, "profile": REVIEWER, "metadata": metadata},
        config=config,
        requirement_id=REQUIREMENT,
    )

    assert normalised["problem"] is None
    assert normalised["reviewer"] == REVIEWER
    assert normalised["dev_run_id"] == 1
    assert normalised["reviewed_head"] == metadata["reviewed_commit"]
    assert normalised["metadata"]["reviewer"] == REVIEWER


def test_missing_reviewer_and_profile_is_a_named_problem(tmp_path):
    config, run_dir, metadata, _adapter = _fixture(tmp_path)

    normalised = _review_release_metadata(
        Path(config.work_root),
        TASK_ID,
        {"id": 2, "profile": None, "metadata": metadata},
        config=config,
        requirement_id=REQUIREMENT,
    )

    assert normalised["problem"] == "reviewer"
    assert normalised["reviewer"] is None
    assert normalised["reason"] == "review-metadata-incomplete:reviewer"


def test_binding_failure_is_visible_with_a_named_reason(tmp_path):
    config, run_dir, metadata, adapter = _fixture(
        tmp_path, review_profile=DEV_PROFILE
    )

    queued = queue_release_cards(
        Path(config.work_root), adapter, config, requirement_id=REQUIREMENT
    )

    matches = [entry for entry in queued if entry.get("dev_task_id") == TASK_ID]
    assert len(matches) == 1, queued
    assert matches[0]["skipped"] == "review-metadata-incomplete"
    assert matches[0]["reason"] == (
        "review-metadata-incomplete:reviewer-is-implementer"
    )
    assert adapter.created == []


def test_missing_profile_is_reported_not_silently_skipped(tmp_path):
    config, run_dir, metadata, adapter = _fixture(tmp_path, review_profile=None)

    queued = queue_release_cards(
        Path(config.work_root), adapter, config, requirement_id=REQUIREMENT
    )

    matches = [entry for entry in queued if entry.get("dev_task_id") == TASK_ID]
    assert len(matches) == 1, queued
    assert matches[0]["skipped"] == "review-metadata-incomplete"
    assert matches[0]["reason"] == "review-metadata-incomplete:reviewer"
    assert adapter.created == []


def test_complete_native_shape_still_queues_one_card(tmp_path):
    config, run_dir, metadata, adapter = _fixture(tmp_path)

    queued = queue_release_cards(
        Path(config.work_root), adapter, config, requirement_id=REQUIREMENT
    )

    matches = [entry for entry in queued if entry.get("dev_task_id") == TASK_ID]
    assert len(matches) == 1, queued
    assert "skipped" not in matches[0]
    assert matches[0]["merge_task_id"] == MERGE_ID
    assert adapter.created == [
        {"assignee": "hkl:merge", "parents": [TASK_ID]}
    ]
    assert sha256_file(run_dir / INDEX_RELATIVE_PATH)
