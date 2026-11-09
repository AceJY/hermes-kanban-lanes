"""D1-D3: the review verdict binding and cross-round invalidation.

A reviewer records one metadata document on its own review run.  The release
gate never trusts that document on its own: it recomputes every field from the
development run's ``run.json``, its evidence index and the repository, and it
invalidates a binding when the reviewed facts have moved since the review.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..verification.evidence import INDEX_RELATIVE_PATH, sha256_file, tool_digest
from ..workspace.git import GitError, repo_head, run_git

REVIEW_SCHEMA_VERSION = 1
REVIEW_METADATA_KEYS = (
    "schema_version",
    "requirement_id",
    "verdict",
    "reviewed_head",
    "reviewed_tree",
    "dev_run_id",
    "rework_round",
    "reviewer",
    "review_run_id",
    "evidence_index_sha256",
    "reasons",
)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_hex(value: Any, lengths: tuple[int, ...] = (40, 64)) -> bool:
    if not isinstance(value, str) or len(value) not in lengths:
        return False
    return all(character in "0123456789abcdef" for character in value)


def allow_matches(path: str, allow: Sequence[str]) -> bool:
    """The scope-check allow matcher, reused for release assertions."""

    for entry in allow:
        if entry.endswith("/"):
            directory = entry[:-1]
            if path == directory or path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


@dataclass(frozen=True, slots=True)
class ReviewBinding:
    task_id: str
    dev_run_id: int
    rework_round: int
    task_base_commit: str
    reviewed_head: str
    reviewed_tree: str
    evidence_index_sha256: str
    tool_sha256: str
    reviewer: str
    review_run_id: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "dev_run_id": self.dev_run_id,
            "rework_round": self.rework_round,
            "task_base_commit": self.task_base_commit,
            "reviewed_head": self.reviewed_head,
            "reviewed_tree": self.reviewed_tree,
            "evidence_index_sha256": self.evidence_index_sha256,
            "tool_sha256": self.tool_sha256,
            "reviewer": self.reviewer,
            "review_run_id": self.review_run_id,
        }

    def tuple_fields(self) -> dict[str, Any]:
        """The D2 equality tuple (same keys as ``to_dict``)."""

        return self.to_dict()


@dataclass(frozen=True, slots=True)
class BindingEvaluation:
    binding: ReviewBinding | None
    problems: tuple[str, ...]
    metadata: dict[str, Any] | None = None
    review_run: dict[str, Any] | None = None
    dev_run: dict[str, Any] | None = None
    dev_run_dir: str | None = None
    run_document: dict[str, Any] | None = None
    index: dict[str, Any] | None = None
    reviewed_tree: str | None = None
    main_commit: str | None = None
    reviewer: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _review_metadata(run: Mapping[str, Any]) -> dict[str, Any] | None:
    metadata = run.get("metadata")
    if not isinstance(metadata, dict):
        return None
    if "reviewed_head" not in metadata and "verdict" not in metadata:
        return None
    return dict(metadata)


def _latest_review_run(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    candidates = []
    for run in runs:
        metadata = _review_metadata(run)
        if metadata is not None:
            candidates.append((int(run.get("id") or 0), dict(run), metadata))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    run_id, run, metadata = candidates[-1]
    run["metadata"] = metadata
    return run


def _expected_dev_run(runs: Sequence[Mapping[str, Any]], review_run_id: int) -> dict[str, Any] | None:
    candidates = [
        run
        for run in runs
        if int(run.get("id") or 0) < int(review_run_id)
        and run.get("outcome") == "review_requested"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda run: int(run.get("id") or 0))


def _tree_of(commit: str, *, cwd: Path) -> str | None:
    try:
        result = run_git(["rev-parse", f"{commit}^{{tree}}"], cwd=cwd, timeout=30)
    except GitError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _tip_of(workspace: Path, repo: Path) -> str | None:
    target = workspace if workspace.is_dir() else repo
    try:
        return repo_head(target)
    except GitError:
        return None


def evaluate_binding(
    *,
    task_row: Mapping[str, Any] | None,
    runs: Sequence[Mapping[str, Any]],
    dev_run_dir: str | Path,
    repo: str | Path,
    requirement_id: str,
    package_dir: str | Path | None = None,
    executor_profile: str | None = None,
) -> BindingEvaluation:
    """Recompute the D1 facts and the D3 staleness reasons."""

    problems: list[str] = []
    if task_row is None:
        return BindingEvaluation(None, ("task-missing",))

    root = Path(dev_run_dir)
    repo_path = Path(repo)
    review_run = _latest_review_run(runs)
    if review_run is None:
        return BindingEvaluation(None, ("review-missing",), dev_run_dir=str(root))

    metadata = dict(review_run.get("metadata") or {})
    review_run_id = int(review_run.get("id") or 0)

    missing = [key for key in REVIEW_METADATA_KEYS if key not in metadata]
    if missing:
        problems.append("review-metadata-missing:" + ",".join(sorted(missing)))

    if metadata.get("schema_version") != REVIEW_SCHEMA_VERSION:
        problems.append("review-metadata-schema")
    if metadata.get("requirement_id") != requirement_id:
        problems.append("review-requirement-mismatch")
    if metadata.get("verdict") != "pass":
        problems.append("review-verdict-not-pass")
    if metadata.get("review_run_id") != review_run_id:
        problems.append("review-run-not-latest")
    if "reasons" in metadata and not isinstance(metadata.get("reasons"), list):
        problems.append("review-reasons-invalid")

    reviewed_head = metadata.get("reviewed_head")
    reviewed_tree = metadata.get("reviewed_tree")
    reviewer = metadata.get("reviewer")
    dev_run_id = metadata.get("dev_run_id")
    rework_round = metadata.get("rework_round")
    if not _is_hex(reviewed_head):
        problems.append("review-head-invalid")
    if not _is_hex(reviewed_tree):
        problems.append("review-tree-invalid")
    if not isinstance(dev_run_id, int) or isinstance(dev_run_id, bool) or dev_run_id <= 0:
        problems.append("review-dev-run-invalid")
    if not isinstance(rework_round, int) or isinstance(rework_round, bool) or rework_round <= 0:
        problems.append("review-round-invalid")
    if not isinstance(reviewer, str) or not reviewer:
        problems.append("review-reviewer-invalid")

    dev_run = _expected_dev_run(runs, review_run_id)
    if dev_run is None:
        problems.append("review-dev-run-missing")
    elif dev_run_id != int(dev_run.get("id") or 0):
        problems.append("review-dev-run-mismatch")

    if isinstance(reviewer, str) and reviewer:
        if review_run.get("profile") != reviewer:
            problems.append("review-reviewer-mismatch")
        dev_profile = dev_run.get("profile") if dev_run is not None else None
        if dev_profile is not None and reviewer == dev_profile:
            problems.append("reviewer-is-implementer")
        if executor_profile is not None and reviewer == executor_profile:
            problems.append("reviewer-is-executor")

    changes_requested = [
        run for run in runs if run.get("outcome") == "changes_requested"
    ]
    expected_round = len(changes_requested) + 1
    if isinstance(rework_round, int) and not isinstance(rework_round, bool):
        if rework_round != expected_round:
            problems.append("review-round-mismatch")

    run_document = _read_json(root / "run.json")
    if not isinstance(run_document, dict):
        problems.append("review-dev-run-missing")
        run_document = None
    task_base = None
    if run_document is not None:
        identity = run_document.get("identity")
        if not isinstance(identity, dict) or identity.get("run_id") != dev_run_id:
            problems.append("review-dev-run-mismatch")
        task_base = run_document.get("task_base_commit") or run_document.get("base_commit")
        if not isinstance(task_base, str) or not task_base:
            problems.append("review-base-missing")
        if run_document.get("code_commit") != reviewed_head:
            problems.append("review-head-not-run")
        if (
            isinstance(rework_round, int)
            and run_document.get("round") is not None
            and int(run_document.get("round")) != rework_round
        ):
            problems.append("review-round-mismatch")

    workspace = root / "workspace"
    if run_document is not None:
        workspace = Path(str(run_document.get("workspace") or workspace))
    actual_tree = _tree_of(reviewed_head, cwd=workspace if workspace.is_dir() else repo_path)
    if actual_tree is None:
        problems.append("review-head-unresolvable")
    elif reviewed_tree != actual_tree:
        problems.append("review-tree-mismatch")

    tip = _tip_of(workspace, repo_path)
    if tip != reviewed_head:
        problems.append("review-head-not-tip")

    index = _read_json(root / INDEX_RELATIVE_PATH)
    index_sha = None
    declared_index_sha = metadata.get("evidence_index_sha256")
    if not _is_hex(declared_index_sha, (64,)) and "evidence_index_sha256" in metadata:
        problems.append("review-index-mismatch")
    if not isinstance(index, dict):
        problems.append("review-index-missing")
        index = None
    else:
        try:
            index_sha = sha256_file(root / INDEX_RELATIVE_PATH)
        except OSError:
            index_sha = None
            problems.append("review-index-missing")
        if index_sha is not None and declared_index_sha != index_sha:
            problems.append("review-index-mismatch")
        if index.get("code_commit") != reviewed_head:
            problems.append("review-index-code-mismatch")
        identity = index.get("identity")
        if not isinstance(identity, dict) or identity.get("run_id") != dev_run_id:
            problems.append("review-index-identity-mismatch")
        if index.get("requirement_id") != requirement_id:
            problems.append("review-requirement-mismatch")

    tool_sha = index.get("tool_sha256") if isinstance(index, dict) else None
    if not _is_hex(tool_sha):
        problems.append("review-tool-sha-missing")

    landing = _read_json(root / "landing-plan.json")
    main_commit = None
    if isinstance(landing, dict):
        candidate = landing.get("main_commit")
        if isinstance(candidate, str) and candidate:
            main_commit = candidate
    if main_commit is None and run_document is not None:
        candidate = run_document.get("sync_base")
        if isinstance(candidate, str) and candidate:
            main_commit = candidate

    binding = None
    if (
        isinstance(dev_run_id, int)
        and not isinstance(dev_run_id, bool)
        and isinstance(rework_round, int)
        and not isinstance(rework_round, bool)
        and isinstance(reviewed_head, str)
        and isinstance(reviewed_tree, str)
        and _is_hex(declared_index_sha, (64,))
        and isinstance(tool_sha, str)
        and isinstance(reviewer, str)
        and isinstance(task_base, str)
    ):
        binding = ReviewBinding(
            task_id=str(task_row.get("id") or ""),
            dev_run_id=dev_run_id,
            rework_round=rework_round,
            task_base_commit=task_base,
            reviewed_head=reviewed_head,
            reviewed_tree=reviewed_tree,
            evidence_index_sha256=declared_index_sha,
            tool_sha256=tool_sha,
            reviewer=reviewer,
            review_run_id=review_run_id,
        )

    # -- D3: the reviewed facts must still be the current facts -------------
    if binding is not None:
        if tip is not None and tip != binding.reviewed_head:
            problems.append("review-stale:reviewed_head")
        if index_sha is not None and index_sha != binding.evidence_index_sha256:
            problems.append("review-stale:evidence_index_sha256")
        if package_dir is not None:
            try:
                current_tool = tool_digest(package_dir)
            except OSError:
                current_tool = None
            if current_tool is not None and current_tool != binding.tool_sha256:
                problems.append("review-stale:tool_sha256")
        # D3: any newer run of the task (not only one that requested another
        # review) invalidates the earlier binding.
        later_dev = [
            run for run in runs if int(run.get("id") or 0) > review_run_id
        ]
        if later_dev:
            problems.append("review-stale:dev_run_id")
        if run_document is not None:
            current_base = run_document.get("task_base_commit")
            if current_base is not None and current_base != binding.task_base_commit:
                problems.append("review-stale:task_base_commit")

    deduped = tuple(dict.fromkeys(problems))
    return BindingEvaluation(
        binding=binding if not deduped else binding,
        problems=deduped,
        metadata=metadata,
        review_run=review_run,
        dev_run=dev_run,
        dev_run_dir=str(root),
        run_document=run_document,
        index=index,
        reviewed_tree=actual_tree,
        main_commit=main_commit,
        reviewer=reviewer if isinstance(reviewer, str) else None,
    )


def build_binding(
    *,
    task_row: Mapping[str, Any] | None,
    runs: Sequence[Mapping[str, Any]],
    dev_run_dir: str | Path,
    repo: str | Path,
    requirement_id: str,
    package_dir: str | Path | None = None,
    executor_profile: str | None = None,
) -> ReviewBinding | None:
    evaluation = evaluate_binding(
        task_row=task_row,
        runs=runs,
        dev_run_dir=dev_run_dir,
        repo=repo,
        requirement_id=requirement_id,
        package_dir=package_dir,
        executor_profile=executor_profile,
    )
    if evaluation.problems:
        return None
    return evaluation.binding


def binding_problems(
    *,
    task_row: Mapping[str, Any] | None,
    runs: Sequence[Mapping[str, Any]],
    dev_run_dir: str | Path,
    repo: str | Path,
    requirement_id: str,
    package_dir: str | Path | None = None,
    executor_profile: str | None = None,
) -> list[str]:
    return list(
        evaluate_binding(
            task_row=task_row,
            runs=runs,
            dev_run_dir=dev_run_dir,
            repo=repo,
            requirement_id=requirement_id,
            package_dir=package_dir,
            executor_profile=executor_profile,
        ).problems
    )


__all__ = [
    "BindingEvaluation",
    "REVIEW_METADATA_KEYS",
    "REVIEW_SCHEMA_VERSION",
    "ReviewBinding",
    "allow_matches",
    "binding_problems",
    "build_binding",
    "evaluate_binding",
]
