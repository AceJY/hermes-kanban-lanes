"""Git plumbing for per-run worktrees.

Every git process goes through the one permitted execution path,
``verification.commands.run_command``: it owns the argv-only spawn, the
``start_new_session`` process group, the timeout and the group kill.  This
module only adds git semantics (executable lookup, captured output, exit-code
checking) and never starts a process itself.

Only the supervisor mutates git state.  ``preflight_repo`` never writes; the
``create_run_worktree`` / ``commit_run`` calls run inside a freshly allocated
per-run path.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Sequence


HKL_COMMITTER_NAME = "Hermes Kanban Lanes"
HKL_COMMITTER_EMAIL = "hkl@localhost"


class GitError(RuntimeError):
    """A git command failed or could not be started."""


def _fixed_committer_env() -> dict[str, str]:
    """Pin the run committer even when the host exports ``GIT_AUTHOR_*`` /
    ``GIT_COMMITTER_*``; ``git -c`` alone loses to those variables."""

    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": HKL_COMMITTER_NAME,
            "GIT_AUTHOR_EMAIL": HKL_COMMITTER_EMAIL,
            "GIT_COMMITTER_NAME": HKL_COMMITTER_NAME,
            "GIT_COMMITTER_EMAIL": HKL_COMMITTER_EMAIL,
        }
    )
    return env


@dataclass(frozen=True, slots=True)
class GitResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def _git_executable() -> str:
    path = shutil.which("git")
    if not path:
        raise GitError("git executable not found on PATH")
    return path


def run_git(
    args: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> GitResult:
    """Run one git command in ``cwd`` with captured output and a hard timeout."""

    if not args:
        raise GitError("git command is empty")
    # Imported lazily: ``verification.gate`` imports this module, so a
    # module-level import here would close an import cycle through the
    # ``verification`` package ``__init__``.  By call time both modules exist.
    from ..verification.commands import CommandGroupError, run_command

    directory = os.path.abspath(os.fspath(cwd))
    command = [_git_executable(), *args]
    environment = dict(env if env is not None else os.environ)
    try:
        result = run_command(
            command,
            cwd=directory,
            env=environment,
            timeout=timeout,
            capture=True,
        )
    except CommandGroupError as exc:
        # A git helper that survived its own group is an explicit GitError, not
        # a command whose exit code may accidentally look successful.
        raise GitError(str(exc)) from exc
    if result.timed_out:
        raise GitError(f"git command timed out: {' '.join(args[:2])}")
    return GitResult(
        argv=tuple(result.argv),
        returncode=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _require_ok(result: GitResult) -> GitResult:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise GitError(f"git {' '.join(result.argv[1:3])} failed{suffix}")
    return result


def repo_head(repo: str | os.PathLike[str]) -> str:
    result = _require_ok(
        run_git(["rev-parse", "HEAD"], cwd=repo, timeout=30)
    )
    return result.stdout.strip()


def repository_dirty(repo: str | os.PathLike[str]) -> bool:
    """True when the checkout has staged, unstaged or untracked changes.

    Read-only: ``git status`` may refresh the index stat cache but never
    changes the working tree.  Raises :class:`GitError` when status cannot be
    read, so callers that require a definite answer stay fail-closed.
    """

    result = _require_ok(
        run_git(["status", "--porcelain", "-uall"], cwd=repo, timeout=60)
    )
    return bool(result.stdout.strip())


def preflight_repo(
    repo: str | os.PathLike[str], *, require_clean: bool = False
) -> tuple[bool, str]:
    """A repository rooted exactly at ``repo``; never writes.

    By default only the git root and a readable ``HEAD`` are required: a team
    checkout with uncommitted WIP must still start and claim work, because HKL
    reads ``HEAD`` and runs in its own per-run worktree.  ``require_clean=True``
    adds the strict ``repository-dirty`` refusal for callers that are about to
    write the checkout (merge/release).
    """

    path = os.path.realpath(os.fspath(repo))
    if not os.path.isdir(path):
        return False, "repository-missing"
    try:
        inside = run_git(["rev-parse", "--is-inside-work-tree"], cwd=path, timeout=30)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return False, "repository-not-a-worktree"
        toplevel = run_git(["rev-parse", "--show-toplevel"], cwd=path, timeout=30)
        if toplevel.returncode != 0:
            return False, "repository-unreadable"
        if os.path.realpath(toplevel.stdout.strip()) != path:
            return False, "repository-not-root"
        head = run_git(["rev-parse", "--verify", "HEAD"], cwd=path, timeout=30)
        if head.returncode != 0:
            return False, "repository-no-head"
        if require_clean:
            status = run_git(
                ["status", "--porcelain", "-uall"], cwd=path, timeout=60
            )
            if status.returncode != 0:
                return False, "repository-unreadable"
            if status.stdout.strip():
                return False, "repository-dirty"
    except GitError:
        return False, "repository-git-failed"
    return True, "ok"


def create_run_worktree(
    repo: str | os.PathLike[str],
    base_commit: str,
    branch: str,
    workspace: str | os.PathLike[str],
) -> None:
    """``git worktree add -b <branch> <workspace> <base>``; path must be new."""

    path = os.path.abspath(os.fspath(workspace))
    if os.path.exists(path):
        raise GitError("workspace path already exists")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    result = run_git(
        ["worktree", "add", "-b", branch, path, base_commit],
        cwd=repo,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise GitError(f"git worktree add failed: {detail[-1] if detail else 'unknown'}")


def _parse_porcelain_z(raw: str) -> list[str]:
    parts = raw.split("\x00")
    paths: list[str] = []
    index = 0
    while index < len(parts):
        text = parts[index]
        if not text:
            index += 1
            continue
        if len(text) < 4:
            index += 1
            continue
        state = text[:2]
        path = text[3:]
        paths.append(path)
        if state[0] in "RC" or state[1] in "RC":
            # Rename/copy: the following NUL-separated field is the source.
            if index + 1 < len(parts):
                source = parts[index + 1]
                if source:
                    paths.append(source)
            index += 2
        else:
            index += 1
    return paths


def changed_paths(workspace: str | os.PathLike[str]) -> list[str]:
    """Working-tree changes (including untracked), sorted and de-duplicated."""

    result = _require_ok(
        run_git(
            ["status", "--porcelain", "-z", "-uall"],
            cwd=workspace,
            timeout=120,
        )
    )
    return sorted({path for path in _parse_porcelain_z(result.stdout) if path})


def commit_run(workspace: str | os.PathLike[str], message: str) -> str:
    """Commit the run branch; returns ``HEAD`` (unchanged when no diff)."""

    status = _require_ok(
        run_git(["status", "--porcelain", "-uall"], cwd=workspace, timeout=60)
    )
    head = repo_head(workspace)
    if not status.stdout.strip():
        return head
    _require_ok(run_git(["add", "-A"], cwd=workspace, timeout=120))
    result = run_git(
        [
            "-c",
            f"user.name={HKL_COMMITTER_NAME}",
            "-c",
            f"user.email={HKL_COMMITTER_EMAIL}",
            "commit",
            "--no-verify",
            "-m",
            message,
        ],
        cwd=workspace,
        env=_fixed_committer_env(),
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise GitError(f"git commit failed: {detail[-1] if detail else 'unknown'}")
    return repo_head(workspace)


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Outcome of one baseline ``git merge --no-ff`` attempt."""

    ok: bool
    commit: str | None
    conflict: bool
    conflict_paths: tuple[str, ...] = ()
    error: str | None = None


def diff_paths(
    base: str,
    head: str,
    *,
    cwd: str | os.PathLike[str],
    timeout: float = 120.0,
) -> list[str]:
    """Changed paths between two commits, including both sides of renames."""

    result = _require_ok(
        run_git(
            ["diff", "--name-status", "-z", "--find-renames", base, head],
            cwd=cwd,
            timeout=timeout,
        )
    )
    tokens = result.stdout.split("\x00")
    paths: set[str] = set()
    index = 0
    while index < len(tokens):
        status = tokens[index]
        if not status:
            index += 1
            continue
        kind = status[:1]
        if kind in ("R", "C"):
            if index + 2 < len(tokens):
                source, destination = tokens[index + 1], tokens[index + 2]
                if source:
                    paths.add(source)
                if destination:
                    paths.add(destination)
            index += 3
        else:
            if index + 1 < len(tokens):
                path = tokens[index + 1]
                if path:
                    paths.add(path)
            index += 2
    return sorted(paths)


def _merge_in_progress(workspace: str | os.PathLike[str]) -> bool:
    result = run_git(
        ["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=workspace, timeout=30
    )
    return result.returncode == 0


def _merge_conflict_paths(workspace: str | os.PathLike[str]) -> list[str]:
    result = run_git(
        ["diff", "--name-only", "--diff-filter=U", "-z"],
        cwd=workspace,
        timeout=30,
    )
    if result.returncode != 0:
        return []
    return sorted({path for path in result.stdout.split("\x00") if path})


def merge_commit(
    workspace: str | os.PathLike[str],
    sync_base: str,
    *,
    message: str = "hkl: sync base",
    timeout: float = 120.0,
) -> MergeResult:
    """Merge ``sync_base`` into the run branch; never resolves conflicts.

    A failed merge that left conflict entries is reported with ``conflict=True``
    and the unmerged paths; the caller must decide whether to ``abort_merge``.
    """

    result = run_git(
        [
            "-c",
            f"user.name={HKL_COMMITTER_NAME}",
            "-c",
            f"user.email={HKL_COMMITTER_EMAIL}",
            "merge",
            "--no-ff",
            "--no-edit",
            "--no-verify",
            "-m",
            message,
            sync_base,
        ],
        cwd=workspace,
        env=_fixed_committer_env(),
        timeout=timeout,
    )
    if result.returncode == 0:
        return MergeResult(True, repo_head(workspace), False)
    conflicts = _merge_conflict_paths(workspace)
    in_progress = _merge_in_progress(workspace)
    detail = (result.stderr or result.stdout).strip().splitlines()
    return MergeResult(
        False,
        None,
        bool(in_progress or conflicts),
        tuple(conflicts),
        detail[-1] if detail else f"git merge exited {result.returncode}",
    )


def abort_merge(workspace: str | os.PathLike[str], *, timeout: float = 60.0) -> bool:
    """``git merge --abort``; returns False when there was nothing to abort."""

    result = run_git(["merge", "--abort"], cwd=workspace, timeout=timeout)
    return result.returncode == 0


def merge_tree_preflight(
    main_ref: str,
    code_commit: str,
    *,
    repo: str | os.PathLike[str],
    timeout: float = 120.0,
) -> tuple[bool, str]:
    """Dry-run merge tree; writes objects only, never moves refs or the worktree.

    Returns ``(True, tree_sha)`` on a clean merge, ``(False, "merge-conflict")``
    for a conflict, and ``(False, "merge-tree-error:<rc>")`` otherwise.
    """

    result = run_git(
        ["merge-tree", "--write-tree", main_ref, code_commit],
        cwd=repo,
        timeout=timeout,
    )
    if result.returncode == 0:
        lines = result.stdout.strip().splitlines()
        return True, lines[0] if lines else ""
    if result.returncode == 1 and result.stdout.strip():
        # A real conflict prints CONFLICT lines; an unresolvable argument exits
        # 1 with empty stdout and only a diagnostic on stderr.
        return False, "merge-conflict"
    return False, f"merge-tree-error:{result.returncode}"


def is_ancestor(
    ancestor: str,
    descendant: str,
    *,
    repo: str | os.PathLike[str],
    timeout: float = 30.0,
) -> bool:
    result = run_git(
        ["merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo,
        timeout=timeout,
    )
    return result.returncode == 0


def branch_tip(repo: str | os.PathLike[str]) -> str:
    """Current ``HEAD`` of a repository or worktree (the branch tip)."""

    return repo_head(repo)


def current_branch(repo: str | os.PathLike[str]) -> str | None:
    result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, timeout=30)
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def resolve_ref(ref: str, *, repo: str | os.PathLike[str]) -> str | None:
    result = run_git(
        ["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo, timeout=30
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


__all__ = [
    "GitError",
    "GitResult",
    "MergeResult",
    "abort_merge",
    "branch_tip",
    "changed_paths",
    "commit_run",
    "create_run_worktree",
    "current_branch",
    "diff_paths",
    "is_ancestor",
    "merge_commit",
    "merge_tree_preflight",
    "preflight_repo",
    "repo_head",
    "repository_dirty",
    "resolve_ref",
    "run_git",
]
