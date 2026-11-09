"""Per-run git workspaces and landing identity checks."""

from __future__ import annotations

from .git import (
    GitError,
    MergeResult,
    abort_merge,
    branch_tip,
    changed_paths,
    commit_run,
    create_run_worktree,
    current_branch,
    diff_paths,
    is_ancestor,
    merge_commit,
    merge_tree_preflight,
    preflight_repo,
    repo_head,
    resolve_ref,
)
from .landing import (
    LandingPlan,
    check_workspace_identity,
    landing_preflight,
    reclaim_marker_path,
    unreusable_marker_path,
    write_reclaim_marker,
    write_unreusable_marker,
)

__all__ = [
    "GitError",
    "LandingPlan",
    "MergeResult",
    "abort_merge",
    "branch_tip",
    "changed_paths",
    "check_workspace_identity",
    "commit_run",
    "create_run_worktree",
    "current_branch",
    "diff_paths",
    "is_ancestor",
    "landing_preflight",
    "merge_commit",
    "merge_tree_preflight",
    "preflight_repo",
    "reclaim_marker_path",
    "repo_head",
    "resolve_ref",
    "unreusable_marker_path",
    "write_reclaim_marker",
    "write_unreusable_marker",
]
