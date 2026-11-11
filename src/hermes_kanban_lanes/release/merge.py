"""D7-D8: the local ``--no-ff`` release merge and post-merge regression.

Only this module merges, and only locally: no push, tag, reset, rebase or
force.  The merge command uses a fixed committer identity and ``--no-verify``;
the result is asserted against the reviewed facts before it is accepted as a
release.  A failed merge aborts when ``MERGE_HEAD`` exists; an already-created
merge commit is never silently reverted.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import RunIdentity
from ..verification.commands import CommandGroupError, run_command
from ..verification.evidence import sha256_file
from ..workspace.git import (
    GitError,
    abort_merge,
    current_branch,
    diff_paths,
    is_ancestor,
    merge_tree_preflight,
    repo_head,
    resolve_ref,
    run_git,
)
from .binding import allow_matches

MERGE_SCHEMA_VERSION = 1
POSTMERGE_SCHEMA_VERSION = 1
NOTIFY_SCHEMA_VERSION = 1
NOTIFY_DIRNAME = "notify"
NOTIFY_REPORT_FILENAME = "report.json"
# D9-I rule 4: the critical section holds the board write lock across the merge.
# The merge must therefore be bounded well below the host ``busy_timeout``
# (120s), so a slow merge aborts and releases the lock instead of freezing every
# board writer.  This is a hard ceiling, not a tuning knob.
MERGE_TIMEOUT = 60.0
MERGE_COMMITTER_NAME = "Hermes Kanban Lanes"
MERGE_COMMITTER_EMAIL = "hkl@localhost"
DEFAULT_PROTECTED_ROOTS = (
    "tools/",
    "tests/compat/",
    "artifacts/p0",
    "artifacts/p0b/",
    "artifacts/w1/",
    "artifacts/w2/",
    "artifacts/w3/",
)


def _committer_env(inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    """Pin the commit identity even when ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``
    are exported by the host environment (``git -c`` loses to them)."""

    env = dict(os.environ if inherited is None else inherited)
    env.update(
        {
            "GIT_AUTHOR_NAME": MERGE_COMMITTER_NAME,
            "GIT_AUTHOR_EMAIL": MERGE_COMMITTER_EMAIL,
            "GIT_COMMITTER_NAME": MERGE_COMMITTER_NAME,
            "GIT_COMMITTER_EMAIL": MERGE_COMMITTER_EMAIL,
        }
    )
    return env


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _write_log(path: Path, text: str) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return {
        "log": path.as_posix(),
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }


def _protected(path: str, roots: Sequence[str]) -> bool:
    return any(path == root.rstrip("/") or path.startswith(root) for root in roots)


def _tree_of(commit: str, *, repo: Path) -> str | None:
    try:
        result = run_git(["rev-parse", f"{commit}^{{tree}}"], cwd=repo, timeout=30)
    except GitError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _parents_of(commit: str, *, repo: Path) -> list[str]:
    try:
        result = run_git(["rev-list", "--parents", "-n", "1", commit], cwd=repo, timeout=30)
    except GitError:
        return []
    if result.returncode != 0:
        return []
    fields = result.stdout.split()
    return fields[1:]


def _scope_preflight(
    *,
    repo_path: Path,
    main_before: str,
    reviewed_head: str,
    allow: Sequence[str],
    protected_roots: Sequence[str],
    timeout: float,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Predict the merge tree and run every D7 scope check **before** merging.

    ``git merge-tree --write-tree`` writes objects only: it never moves a ref,
    never touches the index or the worktree and never creates ``MERGE_HEAD``.
    Its tree is the tree ``git merge`` would commit, so the allow/protected/
    reviewed-path assertions can be evaluated on the same facts *before* the
    branch is moved.  A refusal here therefore means byte-for-byte unchanged
    git state, not "merge first, refuse afterwards".

    Returns ``(facts, reasons)``; ``facts`` is ``None`` when the dry-run merge
    itself could not produce a tree.
    """

    try:
        clean, detail = merge_tree_preflight(
            main_before, reviewed_head, repo=repo_path, timeout=timeout
        )
    except GitError:
        return None, ["merge-tree-error"]
    if not clean:
        if detail == "merge-conflict":
            # A conflicting merge cannot create a commit, so there is nothing
            # to fail open: let ``git merge`` produce the conflict, abort it and
            # record ``merge-conflict`` exactly as before.  (This also keeps
            # the in-progress-merge/timeout path observable.)
            return None, []
        # An unreadable dry run cannot prove the scope; fail closed.
        return None, [detail]

    merge_tree = detail
    try:
        changed = diff_paths(main_before, merge_tree, cwd=repo_path)
        reviewed_touched = [
            path
            for path in diff_paths(main_before, reviewed_head, cwd=repo_path)
            if allow_matches(path, allow)
        ]
        after_merge = set(diff_paths(reviewed_head, merge_tree, cwd=repo_path))
    except GitError:
        return None, ["merge-tree-error"]

    outside = [path for path in changed if not allow_matches(path, allow)]
    protected = [path for path in changed if _protected(path, protected_roots)]
    reviewed_changed = [path for path in reviewed_touched if path in after_merge]

    reasons: list[str] = []
    # The protected root is the hard stop; naming it first keeps the card's
    # block reason (reasons[0]) on the protection violation even when the same
    # path also leaves the card's allow list.
    if protected:
        reasons.append(f"protected-root-touched:{len(protected)}")
    if outside:
        reasons.append(f"outside-allow:{len(outside)}")
    if reviewed_changed:
        reasons.append(f"merge-changed-reviewed-paths:{len(reviewed_changed)}")

    return {
        "merge_tree": merge_tree,
        "changed_paths": changed,
        "outside_allow_paths": outside,
        "protected_root_paths": protected,
        "reviewed_paths_changed_by_merge": reviewed_changed,
    }, reasons


@dataclass(frozen=True, slots=True)
class MergeRecord:
    ok: bool
    document: dict[str, Any]
    reasons: tuple[str, ...]
    merge_sha: str | None = None
    merge_tree: str | None = None
    main_before: str | None = None

    @property
    def merged(self) -> bool:
        """True when a new merge commit actually exists on ``main``.

        ``ok`` additionally requires every post-merge assertion to hold.  A
        refusal that happened after ``git merge`` must never be mistaken for a
        refusal that left ``main`` untouched: this property is the dedupe key
        recovery and the release report use.
        """

        return (
            self.merge_sha is not None
            and self.main_before is not None
            and self.merge_sha != self.main_before
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.document)


def merge_release(
    *,
    repo: str | os.PathLike[str],
    main_ref: str,
    reviewed_head: str,
    main_before: str,
    allow: Sequence[str],
    run_dir: str | os.PathLike[str],
    identity: RunIdentity,
    requirement_id: str,
    message: str | None = None,
    timeout: float = MERGE_TIMEOUT,
    protected_roots: Sequence[str] = DEFAULT_PROTECTED_ROOTS,
) -> MergeRecord:
    repo_path = Path(repo)
    run_path = Path(run_dir)
    task_id = identity.task_id
    current_main = resolve_ref(main_ref, repo=repo_path)
    reasons: list[str] = []

    command_argv = [
        "git",
        "-c",
        f"user.name={MERGE_COMMITTER_NAME}",
        "-c",
        f"user.email={MERGE_COMMITTER_EMAIL}",
        "merge",
        "--no-ff",
        "--no-verify",
        "--no-edit",
        "-m",
        message or f"hkl: release {task_id}",
        reviewed_head,
    ]

    if current_main is None:
        reasons.append("main-unresolved")

    if reasons:
        document = {
            "schema_version": MERGE_SCHEMA_VERSION,
            "requirement_id": requirement_id,
            "identity": identity.to_dict(),
            "task_id": task_id,
            "main_before": main_before,
            "reviewed_head": reviewed_head,
            "merge_sha": None,
            "merge_tree": None,
            "parents": [],
            "main_before_is_ancestor": False,
            "changed_paths": [],
            "outside_allow": 0,
            "reviewed_paths_changed_by_merge": [],
            "protected_roots_touched": 0,
            "command": {
                "argv": command_argv,
                "exit_code": None,
                "timed_out": False,
                "stdout_log": None,
                "stdout_sha256": None,
                "stdout_size": None,
                "stderr_log": None,
                "stderr_sha256": None,
                "stderr_size": None,
            },
            "verdict": "refused",
            "reasons": reasons,
        }
        _write_json(run_path / "merge.json", document)
        return MergeRecord(False, document, tuple(reasons), main_before=main_before)

    if current_main != main_before:
        reasons.append("main-moved")
        document = {
            "schema_version": MERGE_SCHEMA_VERSION,
            "requirement_id": requirement_id,
            "identity": identity.to_dict(),
            "task_id": task_id,
            "main_before": main_before,
            "reviewed_head": reviewed_head,
            "merge_sha": None,
            "merge_tree": None,
            "parents": [],
            "main_before_is_ancestor": True,
            "changed_paths": [],
            "outside_allow": 0,
            "reviewed_paths_changed_by_merge": [],
            "protected_roots_touched": 0,
            "command": {
                "argv": command_argv,
                "exit_code": None,
                "timed_out": False,
                "stdout_log": None,
                "stdout_sha256": None,
                "stdout_size": None,
                "stderr_log": None,
                "stderr_sha256": None,
                "stderr_size": None,
            },
            "verdict": "refused",
            "reasons": reasons,
        }
        _write_json(run_path / "merge.json", document)
        return MergeRecord(False, document, tuple(reasons), main_before=main_before)

    branch = current_branch(repo_path)
    if branch != main_ref.rsplit("/", 1)[-1]:
        reasons.append("branch-mismatch")
        document = {
            "schema_version": MERGE_SCHEMA_VERSION,
            "requirement_id": requirement_id,
            "identity": identity.to_dict(),
            "task_id": task_id,
            "main_before": main_before,
            "reviewed_head": reviewed_head,
            "merge_sha": None,
            "merge_tree": None,
            "parents": [],
            "main_before_is_ancestor": True,
            "changed_paths": [],
            "outside_allow": 0,
            "reviewed_paths_changed_by_merge": [],
            "protected_roots_touched": 0,
            "command": {
                "argv": command_argv,
                "exit_code": None,
                "timed_out": False,
                "stdout_log": None,
                "stdout_sha256": None,
                "stdout_size": None,
                "stderr_log": None,
                "stderr_sha256": None,
                "stderr_size": None,
            },
            "verdict": "refused",
            "reasons": reasons,
        }
        _write_json(run_path / "merge.json", document)
        return MergeRecord(False, document, tuple(reasons), main_before=main_before)

    # D7 scope assertions must be decided *before* the branch moves.  The old
    # order merged first and refused afterwards, which made every hard check
    # (protected roots in particular) fail open: the commit was already on
    # ``main`` while the card reported ``refused``.
    scope, scope_reasons = _scope_preflight(
        repo_path=repo_path,
        main_before=main_before,
        reviewed_head=reviewed_head,
        allow=allow,
        protected_roots=protected_roots,
        timeout=timeout,
    )
    if scope_reasons:
        scope = scope or {}
        outside = list(scope.get("outside_allow_paths") or [])
        protected = list(scope.get("protected_root_paths") or [])
        reviewed_changed = list(scope.get("reviewed_paths_changed_by_merge") or [])
        document = {
            "schema_version": MERGE_SCHEMA_VERSION,
            "requirement_id": requirement_id,
            "identity": identity.to_dict(),
            "task_id": task_id,
            "main_before": main_before,
            "reviewed_head": reviewed_head,
            "merge_sha": None,
            "merge_tree": None,
            "parents": [],
            "main_before_is_ancestor": False,
            "changed_paths": list(scope.get("changed_paths") or []),
            "outside_allow": len(outside),
            "outside_allow_paths": outside,
            "reviewed_paths_changed_by_merge": reviewed_changed,
            "protected_roots_touched": len(protected),
            "protected_root_paths": protected,
            "command": {
                "argv": command_argv,
                "exit_code": None,
                "timed_out": False,
                "stdout_log": None,
                "stdout_sha256": None,
                "stdout_size": None,
                "stderr_log": None,
                "stderr_sha256": None,
                "stderr_size": None,
                "preflight": "merge-tree",
                "preflight_merge_tree": scope.get("merge_tree"),
            },
            "verdict": "refused",
            "reasons": list(scope_reasons),
            "aborted": False,
            "merged": False,
            "stage": "pre-merge",
        }
        _write_json(run_path / "merge.json", document)
        return MergeRecord(False, document, tuple(scope_reasons), main_before=main_before)

    args = command_argv[1:]
    result = None
    error: str | None = None
    timed_out = False
    try:
        result = run_git(
            args, cwd=repo_path, env=_committer_env(), timeout=timeout
        )
    except GitError as exc:
        error = str(exc)
        timed_out = "timed out" in error

    stdout_text = result.stdout if result is not None else ""
    stderr_text = result.stderr if result is not None else (error or "")
    stdout_log = _write_log(run_path / "merge.stdout.log", stdout_text)
    stderr_log = _write_log(run_path / "merge.stderr.log", stderr_text)
    command_entry = {
        "argv": command_argv,
        "exit_code": result.returncode if result is not None else None,
        "timed_out": timed_out,
        "stdout_log": "merge.stdout.log",
        "stdout_sha256": stdout_log["sha256"],
        "stdout_size": stdout_log["size"],
        "stderr_log": "merge.stderr.log",
        "stderr_sha256": stderr_log["sha256"],
        "stderr_size": stderr_log["size"],
    }

    if result is None or result.returncode != 0:
        aborted = abort_merge(repo_path)
        merges_left = False
        try:
            probe = run_git(
                ["rev-parse", "-q", "--verify", "MERGE_HEAD"],
                cwd=repo_path,
                timeout=30,
            )
            merges_left = probe.returncode == 0
        except GitError:
            merges_left = True
        if merges_left:
            reasons.append("merge-abort-failed")
        else:
            # git prints conflict diagnostics on stdout for some strategies and
            # on stderr for others; both streams are part of the record.
            reasons.append(
                "merge-conflict"
                if _conflicted(stdout_text) or _conflicted(stderr_text)
                else "merge-failed"
            )

        # ``git merge`` may have committed before exiting non-zero (for
        # example a failing clean-up step).  If the new HEAD really is the
        # two-parent merge we asked for, the honest record is "merged"; it must
        # never be written as a pre-merge ``refused`` with ``merge_sha: null``.
        try:
            head_after = repo_head(repo_path)
        except GitError:
            head_after = None
        if (
            head_after is not None
            and head_after != main_before
            and set(_parents_of(head_after, repo=repo_path))
            == {main_before, reviewed_head}
        ):
            command_entry["merge_commit_observed_after_failure"] = True
            return _record_merge_assertions(
                repo_path=repo_path,
                run_path=run_path,
                identity=identity,
                requirement_id=requirement_id,
                task_id=task_id,
                main_before=main_before,
                reviewed_head=reviewed_head,
                allow=allow,
                protected_roots=protected_roots,
                command_entry=command_entry,
            )
        document = {
            "schema_version": MERGE_SCHEMA_VERSION,
            "requirement_id": requirement_id,
            "identity": identity.to_dict(),
            "task_id": task_id,
            "main_before": main_before,
            "reviewed_head": reviewed_head,
            "merge_sha": None,
            "merge_tree": None,
            "parents": [],
            "main_before_is_ancestor": True,
            "changed_paths": [],
            "outside_allow": 0,
            "outside_allow_paths": [],
            "reviewed_paths_changed_by_merge": [],
            "protected_roots_touched": 0,
            "protected_root_paths": [],
            "command": command_entry,
            "verdict": "refused",
            "reasons": reasons,
            "aborted": aborted,
            "merged": False,
            "stage": "merge-attempt",
        }
        _write_json(run_path / "merge.json", document)
        return MergeRecord(False, document, tuple(reasons), main_before=main_before)

    return _record_merge_assertions(
        repo_path=repo_path,
        run_path=run_path,
        identity=identity,
        requirement_id=requirement_id,
        task_id=task_id,
        main_before=main_before,
        reviewed_head=reviewed_head,
        allow=allow,
        protected_roots=protected_roots,
        command_entry=command_entry,
    )


def _record_merge_assertions(
    *,
    repo_path: Path,
    run_path: Path,
    identity: RunIdentity,
    requirement_id: str,
    task_id: str,
    main_before: str,
    reviewed_head: str,
    allow: Sequence[str],
    protected_roots: Sequence[str],
    command_entry: Mapping[str, Any],
) -> MergeRecord:
    """Assert the D7 facts about the current HEAD and write ``merge.json``.

    Shared by the live merge path and by crash adoption (where the merge
    commit already exists and must be recorded, never re-created).
    """

    reasons: list[str] = []
    merge_sha = repo_head(repo_path)
    merge_tree = _tree_of(merge_sha, repo=repo_path)
    parents = _parents_of(merge_sha, repo=repo_path)
    ancestry = is_ancestor(main_before, merge_sha, repo=repo_path) and is_ancestor(
        reviewed_head, merge_sha, repo=repo_path
    )

    try:
        changed = diff_paths(main_before, merge_sha, cwd=repo_path)
    except GitError:
        changed = []
    try:
        reviewed_touched = [
            path
            for path in diff_paths(main_before, reviewed_head, cwd=repo_path)
            if allow_matches(path, allow)
        ]
    except GitError:
        reviewed_touched = []
    try:
        after_merge = set(diff_paths(reviewed_head, merge_sha, cwd=repo_path))
    except GitError:
        after_merge = set()

    outside = [path for path in changed if not allow_matches(path, allow)]
    protected = [path for path in changed if _protected(path, protected_roots)]
    reviewed_changed = [path for path in reviewed_touched if path in after_merge]

    if len(parents) != 2 or set(parents) != {main_before, reviewed_head}:
        reasons.append("parents-mismatch")
    if not ancestry:
        reasons.append("merge-not-ancestor")
    if merge_sha == main_before:
        reasons.append("merge-not-created")
    if outside:
        reasons.append(f"outside-allow:{len(outside)}")
    if protected:
        reasons.append(f"protected-root-touched:{len(protected)}")
    if reviewed_changed:
        reasons.append(f"merge-changed-reviewed-paths:{len(reviewed_changed)}")

    merged = merge_sha is not None and merge_sha != main_before
    # A refusal *after* ``git merge`` succeeded must never be written as
    # ``refused``: the commit exists on ``main`` and recovery/reporting need
    # the merge_sha as the dedupe key.  Only a pre-merge refusal is "refused".
    if not reasons:
        verdict = "merged"
    elif merged:
        verdict = "merged-with-failures"
    else:
        verdict = "refused"
    document = {
        "schema_version": MERGE_SCHEMA_VERSION,
        "requirement_id": requirement_id,
        "identity": identity.to_dict(),
        "task_id": task_id,
        "main_before": main_before,
        "reviewed_head": reviewed_head,
        "merge_sha": merge_sha,
        "merge_tree": merge_tree,
        "parents": parents,
        "main_before_is_ancestor": ancestry,
        "changed_paths": changed,
        "outside_allow": len(outside),
        "outside_allow_paths": outside,
        "reviewed_paths_changed_by_merge": reviewed_changed,
        "protected_roots_touched": len(protected),
        "protected_root_paths": protected,
        "command": dict(command_entry),
        "verdict": verdict,
        "reasons": reasons,
        "merged": merged,
        "stage": "post-merge" if merged else "merge-attempt",
    }
    _write_json(run_path / "merge.json", document)
    return MergeRecord(
        not reasons,
        document,
        tuple(reasons),
        merge_sha=merge_sha,
        merge_tree=merge_tree,
        main_before=main_before,
    )


def record_existing_merge(
    *,
    repo: str | os.PathLike[str],
    main_ref: str,
    reviewed_head: str,
    main_before: str,
    allow: Sequence[str],
    run_dir: str | os.PathLike[str],
    identity: RunIdentity,
    requirement_id: str,
    protected_roots: Sequence[str] = DEFAULT_PROTECTED_ROOTS,
) -> MergeRecord:
    """D9-I rule 5: adopt an already-created merge commit without re-merging.

    Recovery calls this only after :func:`classify_merge_intent` returned
    ``adopt``.  It rebuilds ``merge.json`` from the observed facts; it never
    runs ``git merge`` and therefore cannot create a second merge commit.
    """

    repo_path = Path(repo)
    run_path = Path(run_dir)
    command_entry = {
        "argv": [
            "git",
            "-c",
            f"user.name={MERGE_COMMITTER_NAME}",
            "-c",
            f"user.email={MERGE_COMMITTER_EMAIL}",
            "merge",
            "--no-ff",
            "--no-verify",
            "--no-edit",
            "-m",
            f"hkl: release {identity.task_id}",
            reviewed_head,
        ],
        "exit_code": 0,
        "timed_out": False,
        "stdout_log": None,
        "stdout_sha256": None,
        "stdout_size": None,
        "stderr_log": None,
        "stderr_sha256": None,
        "stderr_size": None,
        "recovered": True,
        "main_ref": main_ref,
    }
    return _record_merge_assertions(
        repo_path=repo_path,
        run_path=run_path,
        identity=identity,
        requirement_id=requirement_id,
        task_id=identity.task_id,
        main_before=main_before,
        reviewed_head=reviewed_head,
        allow=allow,
        protected_roots=protected_roots,
        command_entry=command_entry,
    )


def _conflicted(stderr_text: str) -> bool:
    lowered = stderr_text.lower()
    return "conflict" in lowered or "automatic merge failed" in lowered


def _command_env(run_dir: Path, inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    inherited = dict(os.environ if inherited is None else inherited)
    base = run_dir / "postmerge" / "env"
    for name in ("home", "tmp", "nosite"):
        (base / name).mkdir(parents=True, exist_ok=True)
    return {
        "PATH": inherited.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(base / "home"),
        "TMPDIR": str(base / "tmp"),
        "PYTHONPATH": str(base / "nosite"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _prepare_worktree(
    *, repo: Path, merge_sha: str, worktree: Path
) -> tuple[bool, str]:
    if worktree.exists():
        run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo, timeout=120)
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    result = run_git(
        ["worktree", "add", "--detach", str(worktree), merge_sha],
        cwd=repo,
        timeout=180,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return False, detail[-1] if detail else f"git worktree add exited {result.returncode}"
    return True, "ok"


def _remove_worktree(*, repo: Path, worktree: Path) -> None:
    try:
        run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo, timeout=120)
    except GitError:
        pass
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)


def run_post_merge(
    *,
    repo: str | os.PathLike[str],
    merge_sha: str,
    merge_tree: str | None,
    run_dir: str | os.PathLike[str],
    commands: Sequence[Sequence[str]],
    timeout_seconds: float,
    identity: RunIdentity,
    requirement_id: str,
    inherited_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run the configured regression commands on a temporary detached worktree."""

    repo_path = Path(repo)
    run_path = Path(run_dir)
    worktree = run_path / "postmerge" / "worktree"
    report_path = run_path / "postmerge" / "report.json"
    reasons: list[str] = []
    entries: list[dict[str, Any]] = []

    prepared, detail = _prepare_worktree(
        repo=repo_path, merge_sha=merge_sha, worktree=worktree
    )
    if not prepared:
        reasons.append(f"postmerge-worktree-failed:{detail}")
        document = {
            "schema_version": POSTMERGE_SCHEMA_VERSION,
            "requirement_id": requirement_id,
            "identity": identity.to_dict(),
            "merge_sha": merge_sha,
            "merge_tree": merge_tree,
            "worktree": "postmerge/worktree",
            "commands": entries,
            "verdict": "fail",
            "reasons": reasons,
        }
        _write_json(report_path, document)
        return document

    env = _command_env(run_path, inherited_env)
    try:
        for index, argv in enumerate(commands):
            log_dir = run_path / "postmerge"
            stdout_relative = f"postmerge/{index}.stdout.log"
            stderr_relative = f"postmerge/{index}.stderr.log"
            started = time.monotonic()
            exit_code: int | None = None
            timed_out = False
            error: str | None = None
            try:
                result = run_command(
                    list(argv),
                    cwd=worktree,
                    env=env,
                    timeout=float(timeout_seconds),
                    capture=True,
                    stdout_log_path=log_dir / f"{index}.stdout.log",
                    stderr_log_path=log_dir / f"{index}.stderr.log",
                )
                exit_code = result.exit_code
                timed_out = result.timed_out
            except CommandGroupError as exc:
                exit_code = exc.result.exit_code
                timed_out = exc.result.timed_out
                error = f"process-group:{exc.reason}"
            except (OSError, ValueError) as exc:
                error = f"command-error:{type(exc).__name__}"
            duration = time.monotonic() - started
            stdout_path = run_path / stdout_relative
            stderr_path = run_path / stderr_relative
            stdout_sha = sha256_file(stdout_path) if stdout_path.is_file() else None
            stderr_sha = sha256_file(stderr_path) if stderr_path.is_file() else None
            entry: dict[str, Any] = {
                "argv": [str(part) for part in argv],
                "cwd": ".",
                "exit_code": exit_code,
                "timed_out": timed_out,
                "stdout_log": stdout_relative,
                "stdout_sha256": stdout_sha,
                "stdout_size": stdout_path.stat().st_size
                if stdout_path.is_file()
                else None,
                "stderr_log": stderr_relative,
                "stderr_sha256": stderr_sha,
                "stderr_size": stderr_path.stat().st_size
                if stderr_path.is_file()
                else None,
                "duration_seconds": round(duration, 3),
            }
            if error is not None:
                entry["error"] = error
                reasons.append(f"postmerge-command-error:{index}")
            elif timed_out:
                reasons.append(f"postmerge-timeout:{index}")
            elif exit_code != 0:
                reasons.append(f"postmerge-failed:{index}")
            entries.append(entry)
    finally:
        _remove_worktree(repo=repo_path, worktree=worktree)

    document = {
        "schema_version": POSTMERGE_SCHEMA_VERSION,
        "requirement_id": requirement_id,
        "identity": identity.to_dict(),
        "merge_sha": merge_sha,
        "merge_tree": merge_tree,
        "worktree": "postmerge/worktree",
        "commands": entries,
        "verdict": "fail" if reasons else "pass",
        "reasons": reasons,
    }
    _write_json(report_path, document)
    return document


# ---------------------------------------------------------------------------
# merge notification hook (optional, ops-owned argv)
# ---------------------------------------------------------------------------


def notify_report_path(run_dir: str | os.PathLike[str]) -> Path:
    return Path(run_dir) / NOTIFY_DIRNAME / NOTIFY_REPORT_FILENAME


def _read_notify_report(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def merge_notification_sent(
    run_dir: str | os.PathLike[str], merge_sha: str
) -> bool:
    """True when ``merge_sha`` was already handed to the notify commands.

    The report is keyed by ``merge_sha``: a resumed release must never notify
    the same merge twice, whether the earlier attempt passed or failed.
    """

    document = _read_notify_report(notify_report_path(run_dir))
    return document is not None and document.get("merge_sha") == merge_sha


def _notify_env(
    *,
    requirement_id: str,
    board: str,
    merge_task_id: str,
    dev_task_id: str,
    merge_sha: str,
    merge_tree: str | None,
    reviewed_head: str,
    reviewed_tree: str | None,
    main_before: str,
    evidence_index_sha256: str | None,
    authorization_source: str,
    repo: Path,
    run_dir: Path,
) -> dict[str, str]:
    """The daemon's own environment plus the merge facts, all as strings."""

    env = dict(os.environ)
    env.update(
        {
            "HKL_REQUIREMENT_ID": str(requirement_id),
            "HKL_BOARD": str(board),
            "HKL_MERGE_TASK_ID": str(merge_task_id),
            "HKL_DEV_TASK_ID": str(dev_task_id),
            "HKL_MERGE_SHA": str(merge_sha),
            "HKL_MERGE_TREE": "" if merge_tree is None else str(merge_tree),
            "HKL_REVIEWED_HEAD": str(reviewed_head),
            "HKL_REVIEWED_TREE": (
                "" if reviewed_tree is None else str(reviewed_tree)
            ),
            "HKL_MAIN_BEFORE": str(main_before),
            "HKL_EVIDENCE_INDEX_SHA256": (
                "" if evidence_index_sha256 is None else str(evidence_index_sha256)
            ),
            "HKL_AUTHORIZATION_SOURCE": str(authorization_source),
            "HKL_REPO": str(repo),
            "HKL_RUN_DIR": str(run_dir),
        }
    )
    return env


def run_merge_notify(
    *,
    repo: str | os.PathLike[str],
    run_dir: str | os.PathLike[str],
    merge_sha: str,
    merge_tree: str | None,
    commands: Sequence[Sequence[str]],
    timeout_seconds: float,
    requirement_id: str,
    board: str,
    merge_task_id: str,
    dev_task_id: str,
    reviewed_head: str,
    reviewed_tree: str | None,
    main_before: str,
    evidence_index_sha256: str | None,
    authorization_source: str,
) -> dict[str, Any]:
    """Run the configured notify argv after a release merge is complete.

    This is an ops hook, not a sandboxed card command: it runs with the
    daemon's own ``os.environ`` (so external CLIs can find ``HOME`` and their
    credentials) and with ``cwd`` set to the merged repository root.  The
    ``HKL_*`` variables carry the merge facts.  A call is idempotent per
    ``merge_sha``: an existing ``notify/report.json`` for the same merge is
    returned untouched and no command runs again.

    Without configured ``commands`` nothing is created and nothing is run.
    """

    repo_path = Path(repo)
    run_path = Path(run_dir)
    report_path = notify_report_path(run_path)

    if not commands:
        return {
            "schema_version": NOTIFY_SCHEMA_VERSION,
            "requirement_id": requirement_id,
            "merge_sha": merge_sha,
            "merge_tree": merge_tree,
            "commands": [],
            "verdict": "skipped",
            "reasons": ["notify-disabled"],
            "notified": False,
        }

    existing = _read_notify_report(report_path)
    if existing is not None and existing.get("merge_sha") == merge_sha:
        existing = dict(existing)
        existing["skipped"] = True
        return existing

    notify_dir = run_path / NOTIFY_DIRNAME
    env = _notify_env(
        requirement_id=requirement_id,
        board=board,
        merge_task_id=merge_task_id,
        dev_task_id=dev_task_id,
        merge_sha=merge_sha,
        merge_tree=merge_tree,
        reviewed_head=reviewed_head,
        reviewed_tree=reviewed_tree,
        main_before=main_before,
        evidence_index_sha256=evidence_index_sha256,
        authorization_source=authorization_source,
        repo=repo_path,
        run_dir=run_path,
    )
    reasons: list[str] = []
    entries: list[dict[str, Any]] = []
    for index, argv in enumerate(commands):
        stdout_relative = f"{NOTIFY_DIRNAME}/{index}.stdout.log"
        stderr_relative = f"{NOTIFY_DIRNAME}/{index}.stderr.log"
        started = time.monotonic()
        exit_code: int | None = None
        timed_out = False
        error: str | None = None
        try:
            result = run_command(
                list(argv),
                cwd=repo_path,
                env=env,
                timeout=float(timeout_seconds),
                capture=True,
                stdout_log_path=notify_dir / f"{index}.stdout.log",
                stderr_log_path=notify_dir / f"{index}.stderr.log",
            )
            exit_code = result.exit_code
            timed_out = result.timed_out
        except CommandGroupError as exc:
            exit_code = exc.result.exit_code
            timed_out = exc.result.timed_out
            error = f"process-group:{exc.reason}"
        except (OSError, ValueError) as exc:
            error = f"command-error:{type(exc).__name__}"
        duration = time.monotonic() - started
        stdout_path = run_path / stdout_relative
        stderr_path = run_path / stderr_relative
        entry: dict[str, Any] = {
            "argv": [str(part) for part in argv],
            "cwd": str(repo_path),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout_log": stdout_relative,
            "stdout_sha256": (
                sha256_file(stdout_path) if stdout_path.is_file() else None
            ),
            "stdout_size": (
                stdout_path.stat().st_size if stdout_path.is_file() else None
            ),
            "stderr_log": stderr_relative,
            "stderr_sha256": (
                sha256_file(stderr_path) if stderr_path.is_file() else None
            ),
            "stderr_size": (
                stderr_path.stat().st_size if stderr_path.is_file() else None
            ),
            "duration_seconds": round(duration, 3),
        }
        if error is not None:
            entry["error"] = error
            reasons.append(f"notify-command-error:{index}")
        elif timed_out:
            reasons.append(f"notify-timeout:{index}")
        elif exit_code != 0:
            reasons.append(f"notify-failed:{index}")
        entries.append(entry)

    document = {
        "schema_version": NOTIFY_SCHEMA_VERSION,
        "requirement_id": requirement_id,
        "merge_task_id": merge_task_id,
        "dev_task_id": dev_task_id,
        "merge_sha": merge_sha,
        "merge_tree": merge_tree,
        "reviewed_head": reviewed_head,
        "reviewed_tree": reviewed_tree,
        "main_before": main_before,
        "evidence_index_sha256": evidence_index_sha256,
        "authorization_source": authorization_source,
        "repo": str(repo_path),
        "run_dir": str(run_path),
        "commands": entries,
        "verdict": "fail" if reasons else "pass",
        "reasons": reasons,
        "notified": True,
    }
    _write_json(report_path, document)
    return document


__all__ = [
    "DEFAULT_PROTECTED_ROOTS",
    "MERGE_COMMITTER_EMAIL",
    "MERGE_COMMITTER_NAME",
    "MERGE_SCHEMA_VERSION",
    "MERGE_TIMEOUT",
    "MergeRecord",
    "NOTIFY_DIRNAME",
    "NOTIFY_REPORT_FILENAME",
    "NOTIFY_SCHEMA_VERSION",
    "POSTMERGE_SCHEMA_VERSION",
    "merge_notification_sent",
    "merge_release",
    "notify_report_path",
    "record_existing_merge",
    "run_merge_notify",
    "run_post_merge",
]
