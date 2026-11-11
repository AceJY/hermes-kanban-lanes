"""Independent W3 gate: workspace, scope, progress, commands, artifacts.

The gate is run by the supervisor, never by the executor.  A passing verdict
requires every one of the five checks to pass; a missing or unreadable
workspace produces ``blocked`` rather than a silent pass.

Scope and progress judge only what the run authored, never the paths a
baseline merge brought in: the merge only brings the repository forward and
must not widen what the run is allowed to change.  ``run_gate`` never commits,
merges or writes ``main``.

The scope diff is three-dot from the baseline ``sync_base`` to the run-authored
commit (``run_commit``).  That is ``git diff <merge-base> <run_commit>``: it
contains every change the card itself authored across its rounds, and none of
the paths that advanced ``main`` and were merged in by the baseline sync.  This
matters for a rework round, whose continuation base already contains the trunk
advance merged by the previous round: a two-dot diff from ``task_base`` would
grow with main and flag compliant cards whenever the trunk moved during the
card's lifetime.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..card_contract import CardSpec
from ..contracts import RunIdentity
from ..workspace.git import GitError, changed_paths, diff_paths, run_git
from ..workspace.landing import is_within_workspace
from .commands import CommandGroupError, run_command

REQUIREMENT_ID = "HKL-W3"
REPORT_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class GateResult:
    verdict: str
    report: dict[str, Any]
    report_path: str  # relative to the run directory

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


def _bad_relative(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return True
    if len(path) > 1 and path[1] == ":":
        return True
    return any(segment in ("", ".", "..") for segment in path.split("/"))


def _allow_matches(path: str, allow: tuple[str, ...]) -> bool:
    for entry in allow:
        if entry.endswith("/"):
            directory = entry[:-1]
            if path == directory or path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


def _gate_env(
    run_dir: str | os.PathLike[str], inherited: Mapping[str, str]
) -> dict[str, str]:
    run = Path(run_dir)
    nosite = run / "nosite"
    for name in ("home", "tmp", "nosite"):
        (run / name).mkdir(parents=True, exist_ok=True)
    return {
        "PATH": inherited.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(run / "home"),
        "TMPDIR": str(run / "tmp"),
        "PYTHONPATH": str(nosite),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _blocked_report(
    *,
    identity: RunIdentity,
    base_commit: str,
    run_commit: str | None,
    sync_base: str | None,
    detail: str,
    requirement_id: str = REQUIREMENT_ID,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "requirement_id": requirement_id,
        "identity": identity.to_dict(),
        "base_commit": base_commit,
        "run_commit": run_commit,
        "sync_base": sync_base,
        "checks": [{"name": "workspace", "ok": False, "detail": detail}],
        "verdict": "blocked",
    }


def _prepare_workspace(workspace: str) -> tuple[bool, str]:
    if not os.path.isdir(workspace):
        return False, "workspace directory is missing"
    try:
        probe = run_git(
            ["rev-parse", "--is-inside-work-tree"], cwd=workspace, timeout=30
        )
    except GitError as exc:
        return False, f"workspace git unavailable: {exc}"
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return False, "workspace is not a git working tree"
    return True, "ok"


def _scope_merge_base(sync_base: str, run_commit: str, *, workspace: str) -> str:
    """Return ``git merge-base sync_base run_commit`` for a three-dot diff.

    Diffing from this commit to ``run_commit`` sees only the card's own
    history: anything already reachable from ``sync_base`` (the trunk the
    baseline sync merged) is excluded.
    """

    probe = run_git(["merge-base", sync_base, run_commit], cwd=workspace, timeout=120)
    if probe.returncode != 0:
        error = (probe.stderr or "").strip()
        raise GitError(f"git merge-base failed: {error or 'unknown error'}")
    merge_base = probe.stdout.strip()
    if not merge_base:
        raise GitError("git merge-base produced no commit")
    return merge_base


def _scope_check(
    *,
    card: CardSpec,
    workspace: str,
    base_commit: str,
    run_commit: str | None,
    w3_mode: bool,
    scope_base: str | None = None,
    sync_base: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    ok = True
    detail = "ok"
    changed: list[str] = []
    try:
        if w3_mode:
            dirty = changed_paths(workspace)
            if dirty:
                ok = False
                detail = "workspace-dirty"
            if sync_base:
                # Three-dot semantics: exclude the trunk the baseline sync
                # merged in, keep everything the card authored (all rounds).
                scope_from = _scope_merge_base(
                    sync_base, run_commit or "", workspace=workspace
                )
            else:
                scope_from = scope_base or base_commit
            changed = diff_paths(scope_from, run_commit or "", cwd=workspace)
        else:
            # Legacy direct callers (W2 gate test) judge the working tree.
            changed = changed_paths(workspace)
    except GitError as exc:
        ok = False
        detail = f"git failed: {exc}"
        changed = []

    if ok:
        for path in changed:
            if _bad_relative(path):
                ok = False
                detail = f"unsafe path: {path}"
                break
            if not _allow_matches(path, card.allow):
                ok = False
                detail = f"path outside allow: {path}"
                break
            if not is_within_workspace(os.path.join(workspace, path), workspace):
                ok = False
                detail = f"path escapes workspace (symlink): {path}"
                break
    return (
        {"name": "scope", "ok": ok, "detail": detail, "changed_paths": changed},
        changed,
    )


def _progress_check(
    *, card: CardSpec, base_commit: str, run_commit: str | None, changed: list[str]
) -> dict[str, Any]:
    if card.card_kind != "implementation":
        return {
            "name": "progress",
            "ok": True,
            "detail": f"not-applicable:{card.card_kind}",
            "changed_paths": changed,
        }
    if run_commit is None:
        # Legacy mode: no commit exists yet; a non-empty working-tree change is
        # the only observable progress signal.
        ok = bool(changed)
    else:
        ok = run_commit != base_commit and bool(changed)
    return {
        "name": "progress",
        "ok": ok,
        "detail": "ok" if ok else "no-progress",
        "changed_paths": changed,
    }


def _command_entry(
    *,
    argv: tuple[str, ...] | list[str],
    cwd: str,
    exit_code: int,
    timed_out: bool,
    stdout_log: str | None,
    stdout_sha256: str | None,
    stdout_size: int | None,
    stderr_log: str | None,
    stderr_sha256: str | None,
    stderr_size: int | None,
    duration_seconds: float,
    error: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "argv": list(argv),
        "cwd": cwd,
        "exit_code": int(exit_code),
        "timed_out": bool(timed_out),
        "stdout_log": stdout_log,
        "stdout_sha256": stdout_sha256,
        "stdout_size": stdout_size,
        "stderr_log": stderr_log,
        "stderr_sha256": stderr_sha256,
        "stderr_size": stderr_size,
        "duration_seconds": round(float(duration_seconds), 3),
    }
    if error is not None:
        entry["error"] = error
    return entry


def _digest(path: Path) -> tuple[str, int]:
    from .evidence import sha256_file

    return sha256_file(path), path.stat().st_size


def _commands_check(
    *,
    card: CardSpec,
    workspace: str,
    run_dir: Path,
    command_env: Mapping[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    ok = True
    entries: list[dict[str, Any]] = []
    for index, command in enumerate(card.commands):
        relative_cwd = command.cwd
        cwd = (
            workspace
            if relative_cwd == "."
            else os.path.join(workspace, relative_cwd)
        )
        if not is_within_workspace(cwd, workspace) or not os.path.isdir(cwd):
            ok = False
            entries.append(
                _command_entry(
                    argv=command.argv,
                    cwd=relative_cwd,
                    exit_code=-1,
                    timed_out=False,
                    stdout_log=None,
                    stdout_sha256=None,
                    stdout_size=None,
                    stderr_log=None,
                    stderr_sha256=None,
                    stderr_size=None,
                    duration_seconds=0.0,
                    error="cwd outside workspace or missing",
                )
            )
            continue
        stdout_relative = f"commands/{index}.stdout.log"
        stderr_relative = f"commands/{index}.stderr.log"
        stdout_path = run_dir / stdout_relative
        stderr_path = run_dir / stderr_relative
        try:
            result = run_command(
                command.argv,
                cwd=cwd,
                env=command_env,
                stdout_log_path=stdout_path,
                stderr_log_path=stderr_path,
                timeout=timeout_seconds,
                capture=True,
            )
        except OSError as exc:
            # The log could not be opened or written; the command may not have
            # run, so this is always a failure.
            ok = False
            entries.append(
                _command_entry(
                    argv=command.argv,
                    cwd=relative_cwd,
                    exit_code=-1,
                    timed_out=False,
                    stdout_log=stdout_relative,
                    stdout_sha256=None,
                    stdout_size=None,
                    stderr_log=stderr_relative,
                    stderr_sha256=None,
                    stderr_size=None,
                    duration_seconds=0.0,
                    error=f"log-write-failed: {type(exc).__name__}",
                )
            )
            continue
        except CommandGroupError as exc:
            # D10: the group could not be proven empty.  The logs were written
            # before the exception, so they stay as evidence; the command is an
            # explicit failure and forces ``commands.ok=false``.
            ok = False
            result = exc.result
            try:
                stdout_sha, stdout_size = _digest(stdout_path)
                stderr_sha, stderr_size = _digest(stderr_path)
            except OSError as digest_exc:
                stdout_sha = stderr_sha = None
                stdout_size = stderr_size = None
                detail = f"; log-unreadable: {type(digest_exc).__name__}"
            else:
                detail = ""
            entries.append(
                _command_entry(
                    argv=result.argv,
                    cwd=relative_cwd,
                    exit_code=result.exit_code,
                    timed_out=result.timed_out,
                    stdout_log=stdout_relative,
                    stdout_sha256=stdout_sha,
                    stdout_size=stdout_size,
                    stderr_log=stderr_relative,
                    stderr_sha256=stderr_sha,
                    stderr_size=stderr_size,
                    duration_seconds=result.duration_seconds,
                    error=f"process-group-not-empty:{exc.reason}{detail}",
                )
            )
            continue
        try:
            stdout_sha, stdout_size = _digest(stdout_path)
            stderr_sha, stderr_size = _digest(stderr_path)
        except OSError as exc:
            ok = False
            entries.append(
                _command_entry(
                    argv=result.argv,
                    cwd=relative_cwd,
                    exit_code=result.exit_code,
                    timed_out=result.timed_out,
                    stdout_log=stdout_relative,
                    stdout_sha256=None,
                    stdout_size=None,
                    stderr_log=stderr_relative,
                    stderr_sha256=None,
                    stderr_size=None,
                    duration_seconds=result.duration_seconds,
                    error=f"log-unreadable: {type(exc).__name__}",
                )
            )
            continue
        if result.exit_code != 0 or result.timed_out:
            ok = False
        entries.append(
            _command_entry(
                argv=result.argv,
                cwd=relative_cwd,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                stdout_log=stdout_relative,
                stdout_sha256=stdout_sha,
                stdout_size=stdout_size,
                stderr_log=stderr_relative,
                stderr_sha256=stderr_sha,
                stderr_size=stderr_size,
                duration_seconds=result.duration_seconds,
            )
        )
    return {"name": "commands", "ok": ok, "commands": entries}


def _artifacts_check(
    *,
    card: CardSpec,
    workspace: str,
    changed: list[str],
) -> dict[str, Any]:
    declared = card.artifacts
    if not declared:
        return {
            "name": "artifacts",
            "ok": True,
            "detail": "none-declared",
            "artifacts": [],
        }
    changed_set = set(changed)
    ok = True
    detail = "ok"
    verified: list[dict[str, Any]] = []
    for relative in declared:
        if _bad_relative(relative):
            ok = False
            detail = f"outside-workspace: {relative}"
            continue
        target = os.path.join(workspace, relative)
        if not is_within_workspace(target, workspace):
            ok = False
            detail = f"outside-workspace: {relative}"
            continue
        if not os.path.isfile(target):
            ok = False
            detail = f"missing: {relative}"
            continue
        size = os.path.getsize(target)
        if size <= 0:
            ok = False
            detail = f"empty: {relative}"
            continue
        if relative not in changed_set:
            ok = False
            detail = f"unmodified: {relative}"
            continue
        from .evidence import sha256_file

        verified.append(
            {"path": relative, "size": size, "sha256": sha256_file(target)}
        )
    return {
        "name": "artifacts",
        "ok": ok,
        "detail": detail,
        "artifacts": verified,
    }


def run_gate(
    card: CardSpec,
    workspace: str | os.PathLike[str],
    base_commit: str,
    run_dir: str | os.PathLike[str],
    *,
    identity: RunIdentity,
    env: Mapping[str, str] | None = None,
    command_timeout_seconds: float = 1800.0,
    inherited: Mapping[str, str] | None = None,
    run_commit: str | None = None,
    sync_base: str | None = None,
    scope_base: str | None = None,
    requirement_id: str = REQUIREMENT_ID,
) -> GateResult:
    """Run the five fixed checks in order and write ``evidence/gate-report.json``.

    ``run_commit`` selects W3 semantics (committed run branch, strict clean
    workspace, three-dot scope diff from ``sync_base``).  ``None`` keeps the
    legacy working-tree behaviour used by direct callers of the W2 API.
    """

    workspace_path = os.path.abspath(os.fspath(workspace))
    run_path = Path(run_dir)
    report_path = Path("evidence") / "gate-report.json"
    w3_mode = run_commit is not None

    available, workspace_detail = _prepare_workspace(workspace_path)
    if not available:
        report = _blocked_report(
            identity=identity,
            base_commit=base_commit,
            run_commit=run_commit,
            sync_base=sync_base,
            detail=workspace_detail,
            requirement_id=requirement_id,
        )
        _write_json(run_path / report_path, report)
        return GateResult("blocked", report, report_path.as_posix())

    checks: list[dict[str, Any]] = [
        {"name": "workspace", "ok": True, "detail": "ok"}
    ]

    scope, changed = _scope_check(
        card=card,
        workspace=workspace_path,
        base_commit=base_commit,
        run_commit=run_commit,
        w3_mode=w3_mode,
        scope_base=scope_base,
        sync_base=sync_base,
    )
    checks.append(scope)
    round_changed = changed
    if w3_mode and scope_base is not None and scope_base != base_commit:
        try:
            round_changed = diff_paths(base_commit, run_commit or "", cwd=workspace_path)
        except GitError:
            round_changed = []
    checks.append(
        _progress_check(
            card=card,
            base_commit=base_commit,
            run_commit=run_commit,
            changed=round_changed,
        )
    )

    command_env = (
        dict(env)
        if env is not None
        else _gate_env(run_path, inherited if inherited is not None else os.environ)
    )
    checks.append(
        _commands_check(
            card=card,
            workspace=workspace_path,
            run_dir=run_path,
            command_env=command_env,
            timeout_seconds=command_timeout_seconds,
        )
    )
    checks.append(
        _artifacts_check(card=card, workspace=workspace_path, changed=round_changed)
    )

    verdict = "pass" if all(check["ok"] for check in checks) else "fail"
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "requirement_id": requirement_id,
        "identity": identity.to_dict(),
        "base_commit": base_commit,
        "run_commit": run_commit,
        "sync_base": sync_base if sync_base is not None else base_commit,
        "checks": checks,
        "verdict": verdict,
    }
    _write_json(run_path / report_path, report)
    return GateResult(verdict, report, report_path.as_posix())


__all__ = ["GateResult", "REQUIREMENT_ID", "REPORT_SCHEMA_VERSION", "run_gate"]
