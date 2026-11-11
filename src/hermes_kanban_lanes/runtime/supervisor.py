"""Single-run lifecycle (W3 spec section 6.2).

The supervisor is the **only** writer: it claims, allocates a per-run working
tree, spawns the executor with a minimal environment, heartbeats, terminates
the whole process group, applies executor intents atomically, commits the run
branch, syncs the baseline, runs the five-check gate, writes reproducible
evidence and performs the landing preflight.  W3 never merges into ``main``
and never re-runs an executor after a crash.

Ordering note: intents are applied *before* the terminal transition.  The
intent fence requires ``tasks.status == 'running'``; after
``request_review``/``block``/``reclaim`` the card is no longer running, so
applying intents afterwards would reject every one of them.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..card_contract import CardSpec
from ..config import LaneConfig
from ..contracts import ExecutorResult, RunIdentity, Usage
from ..hermes_adapter.board import BoardAdapter
from ..verification.evidence import (
    INDEX_RELATIVE_PATH,
    build_manifest,
    sha256_file,
    verify_evidence,
    verify_gate_report,
    write_evidence_index,
    write_json,
)
from ..verification.gate import run_gate
from ..workspace.git import (
    GitError,
    abort_merge,
    changed_paths,
    commit_run,
    create_run_worktree,
    current_branch,
    diff_paths,
    is_ancestor,
    merge_commit,
    repo_head,
    run_git,
)
from ..workspace.landing import (
    landing_preflight,
    write_reclaim_marker,
    write_unreusable_marker,
)
from .process import (
    ManagedChild,
    pid_alive,
    process_group_members,
    prove_terminated,
    spawn,
    start_fingerprint,
    terminate,
)
from .retention import remove_run_tmp
from .state import RunState, new_state, read_state, write_state

REQUIREMENT_ID = "HKL-W3"
# No config field is added by W3 (D12); the landing preflight uses the
# repository's conventional main ref.
MAIN_REF = "refs/heads/main"


@dataclass(frozen=True, slots=True)
class RunSettings:
    max_runtime_seconds: float = 3600.0
    command_timeout_seconds: float = 1800.0
    heartbeat_seconds: float = 60.0
    poll_seconds: float = 0.25
    grace_seconds: float = 5.0
    kill_grace_seconds: float = 3.0


@dataclass
class RunOutcome:
    task_id: str
    run_id: int
    board: str
    category: str
    gate_verdict: str | None = None
    code_commit: str | None = None
    workspace: str | None = None
    run_dir: str | None = None
    terminal: str | None = None
    terminal_ok: bool | None = None
    reason: str | None = None
    intents: list[dict[str, Any]] = field(default_factory=list)
    cleanup: dict[str, Any] = field(default_factory=dict)
    exit_code: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    permission_denials: list[Any] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)
    sync_base: str | None = None
    run_commit: str | None = None
    baseline: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)
    stage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "board": self.board,
            "category": self.category,
            "gate_verdict": self.gate_verdict,
            "code_commit": self.code_commit,
            "workspace": self.workspace,
            "run_dir": self.run_dir,
            "terminal": self.terminal,
            "terminal_ok": self.terminal_ok,
            "reason": self.reason,
            "intents": self.intents,
            "cleanup": self.cleanup,
            "exit_code": self.exit_code,
            "usage": self.usage,
            "permission_denials": self.permission_denials,
            "changed_paths": self.changed_paths,
            "sync_base": self.sync_base,
            "run_commit": self.run_commit,
            "baseline": self.baseline,
            "evidence": self.evidence,
            "recovery": self.recovery,
            "stage": self.stage,
        }


def allocate_run_paths(
    work_root: str | os.PathLike[str], task_id: str, run_id: int
) -> tuple[Path, Path]:
    """Per-run directory and workspace; a fresh suffix on every call."""

    token = uuid.uuid4().hex[:12]
    root = Path(work_root)
    run_dir = root / "runs" / task_id / f"{int(run_id)}-{token}"
    workspace = root / "workspaces" / task_id / f"run-{int(run_id)}-{token}"
    return run_dir, workspace


def build_minimal_env(
    run_dir: str | os.PathLike[str],
    *,
    inherited: Mapping[str, str] | None = None,
    credentials: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Explicit allowlist; no inherited ``HERMES_*``/credential variables."""

    inherited = dict(os.environ if inherited is None else inherited)
    run = Path(run_dir)
    for name in ("home", "tmp", "nosite"):
        (run / name).mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": inherited.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(run / "home"),
        "TMPDIR": str(run / "tmp"),
        "PYTHONPATH": str(run / "nosite"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if credentials:
        env.update({str(key): str(value) for key, value in credentials.items()})
    return env


def _removed_env_keys(inherited: Mapping[str, str]) -> list[str]:
    return sorted(
        key
        for key in inherited
        if key.startswith("HERMES_")
        or key.startswith("ANTHROPIC_")
        or key.upper().endswith(("_TOKEN", "_API_KEY", "_SECRET"))
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _source_info(hermes_source: str) -> dict[str, Any]:
    try:
        head = run_git(["rev-parse", "HEAD"], cwd=hermes_source, timeout=15)
        dirty_result = run_git(
            ["status", "--porcelain", "-uall"], cwd=hermes_source, timeout=30
        )
        return {
            "hermes_commit": head.stdout.strip() if head.returncode == 0 else None,
            "dirty": bool(dirty_result.stdout.strip())
            if dirty_result.returncode == 0
            else None,
        }
    except GitError:
        return {"hermes_commit": None, "dirty": None}


def _board_row(adapter: BoardAdapter, task_id: str) -> dict[str, Any] | None:
    """Re-read the run/claim row on the adapter's own connection."""

    row = adapter.connection.execute(
        "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _supervisor_alive(state: RunState) -> bool:
    """Only a live supervisor with a matching (or unreadable) fingerprint wins."""

    pid = state.supervisor_pid
    if not pid or not pid_alive(int(pid)):
        return False
    current = start_fingerprint(int(pid))
    recorded = state.supervisor_fingerprint
    if recorded is None or current is None:
        # Cannot prove the recorded supervisor is gone: never take over.
        return True
    return current == recorded


def assess_previous_runs(
    work_root: str | os.PathLike[str],
    task_id: str,
    current_run_id: int,
    *,
    alive_fn: Callable[[int | None], bool] | None = None,
    fingerprint_fn: Callable[[int | None], str | None] | None = None,
    members_fn: Callable[[int | None], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Prove old runs terminated, then mark their old workspaces.

    ``state.json`` is read as a fallback when ``run.json`` is missing (or does
    not carry the child identity): the child is only ever recorded, never
    removed, so an orphan window after ``spawn`` remains provable.
    """

    kwargs: dict[str, Any] = {}
    if alive_fn is not None:
        kwargs["alive_fn"] = alive_fn
    if fingerprint_fn is not None:
        kwargs["fingerprint_fn"] = fingerprint_fn
    if members_fn is not None:
        kwargs["members_fn"] = members_fn

    root = Path(work_root)
    runs_root = root / "runs" / task_id
    reclaimable_root = root / "reclaimable"
    assessments: list[dict[str, Any]] = []
    if not runs_root.is_dir():
        return assessments
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            previous_run_id = int(entry.name.split("-", 1)[0])
        except (ValueError, IndexError):
            continue
        if previous_run_id == int(current_run_id):
            continue
        summary = _read_json(entry / "run.json")
        cleanup = summary.get("cleanup") if isinstance(summary, dict) else {}
        cleanup = cleanup if isinstance(cleanup, dict) else {}
        pid = cleanup.get("pid")
        fingerprint = cleanup.get("fingerprint")
        pgid = cleanup.get("pgid")
        state = read_state(entry)
        source = "run.json"
        if (pid is None or pgid is None) and state is not None and state.child:
            pid = state.child.get("pid")
            pgid = state.child.get("pgid")
            fingerprint = state.child.get("fingerprint")
            source = "state.json"
        terminated, witness = prove_terminated(
            pid, fingerprint, pgid, **kwargs
        )
        workspace = (
            Path(state.workspace)
            if state is not None and state.workspace
            else root / "workspaces" / task_id / f"run-{entry.name}"
        )
        reason = witness.get("reason") or "unknown"
        if terminated:
            # W2-05: only a fully terminated old run earns the reclaim marker.
            marker = write_reclaim_marker(
                reclaimable_root,
                workspace,
                reusable=True,
                reason=reason,
                witness=witness,
                run_id=previous_run_id,
            )
        else:
            # D4: unproven termination is explicitly marked non-reusable.
            marker = write_unreusable_marker(
                reclaimable_root,
                workspace,
                reason=reason,
                witness=witness,
                run_id=previous_run_id,
            )
        assessments.append(
            {
                "run_id": previous_run_id,
                "workspace": str(workspace),
                "terminated": terminated,
                "reusable": terminated,
                "reason": witness.get("reason"),
                "marker": str(marker),
                "witness": witness,
                "witness_source": source,
            }
        )
    return assessments


def _write_identity(run_dir: Path, identity: RunIdentity) -> None:
    write_json(run_dir / "identity.json", identity.to_dict())


def _read_intent_files(run_dir: Path) -> list[tuple[str, Any]]:
    intents_dir = run_dir / "intents"
    if not intents_dir.is_dir():
        return []
    documents: list[tuple[str, Any]] = []
    for path in sorted(intents_dir.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            documents.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            documents.append((path.name, None))
    return documents


def _gate_failure_check(report: Mapping[str, Any]) -> str:
    for check in report.get("checks", []):
        if not check.get("ok"):
            return str(check.get("name"))
    return "unknown"


def _default_package_dir(package_dir: str | os.PathLike[str] | None) -> Path:
    if package_dir is not None:
        return Path(package_dir)
    return Path(__file__).resolve().parents[1]


def previous_run_summaries(
    work_root: str | os.PathLike[str], task_id: str, current_run_id: int
) -> list[dict[str, Any]]:
    """Finished runs for ``task_id`` older than ``current_run_id``, in order."""

    runs_root = Path(work_root) / "runs" / task_id
    summaries: list[tuple[int, dict[str, Any]]] = []
    if not runs_root.is_dir():
        return []
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            run_id = int(entry.name.split("-", 1)[0])
        except (ValueError, IndexError):
            continue
        if run_id >= int(current_run_id):
            continue
        document = _read_json(entry / "run.json")
        if isinstance(document, dict):
            summaries.append((run_id, document))
    summaries.sort(key=lambda item: item[0])
    return [document for _, document in summaries]


def rework_context(
    *,
    work_root: str | os.PathLike[str],
    task_id: str,
    current_run_id: int,
    repository: str,
    events: list[Mapping[str, Any]],
    max_rounds: int,
) -> dict[str, Any]:
    """Compute D4 round/continuation/base and the pre-claim rework failures.

    The round is ``changes_requested`` event count + 1.  Round 1 starts at the
    current main head; a rework round starts at the previous run's
    ``code_commit`` and keeps the round-1 base for the cumulative scope check.

    A rework round also carries the reviewer's ``changes_requested`` text: the
    round must be able to name what it is fixing (it is copied into ``run.json``
    and the executor prompt).  A rework with no readable review input is refused
    before any workspace or executor is created, so an empty round cannot spin
    twice and then fail for an unrelated ancestry reason.
    """

    round_number = len(events) + 1
    result: dict[str, Any] = {
        "round": round_number,
        "task_base_commit": None,
        "continuation_base": None,
        "error": None,
        "error_kind": None,
    }

    if round_number > max_rounds:
        result["error"] = f"rework-cap:{max_rounds}"
        result["error_kind"] = "needs_input"
        return result

    previous = previous_run_summaries(work_root, task_id, current_run_id)
    if round_number == 1:
        try:
            head = repo_head(repository)
        except (GitError, OSError):
            result["error"] = "rework-base-missing"
            result["error_kind"] = "capability"
            return result
        result["task_base_commit"] = head
        result["continuation_base"] = head
        return result

    if len(previous) < round_number - 1:
        result["error"] = "rework-base-missing"
        result["error_kind"] = "capability"
        return result

    latest = previous[-1]
    continuation = latest.get("code_commit")
    first_round = next(
        (
            document
            for document in previous
            if int(document.get("round") or 1) == 1
        ),
        previous[0],
    )
    task_base = first_round.get("task_base_commit") or first_round.get("base_commit")
    if not isinstance(continuation, str) or not continuation:
        result["error"] = "rework-base-missing"
        result["error_kind"] = "capability"
        return result
    if not isinstance(task_base, str) or not task_base:
        result["error"] = "rework-base-missing"
        result["error_kind"] = "capability"
        return result

    # The latest review hand-off is the input this round must act on.  It is
    # deliberately read from the event payload rather than inferred from the
    # card body: a rework without a readable request cannot be executed.
    latest_event = events[-1] if events else {}
    latest_reason = (
        latest_event.get("reason") if isinstance(latest_event, Mapping) else None
    )
    if not isinstance(latest_reason, str) or not latest_reason.strip():
        result["error"] = "rework-input-missing"
        result["error_kind"] = "needs_input"
        return result
    requested = [
        str(event.get("reason")).strip()
        for event in events
        if isinstance(event, Mapping)
        and isinstance(event.get("reason"), str)
        and str(event.get("reason")).strip()
    ]

    if len(events) >= 2 and len(previous) >= 2:
        last_reason = events[-1].get("reason") if isinstance(events[-1], dict) else None
        prior_reason = events[-2].get("reason") if isinstance(events[-2], dict) else None
        same_reason = (
            isinstance(last_reason, str)
            and last_reason != ""
            and last_reason == prior_reason
        )
        same_head = (
            previous[-1].get("code_commit")
            and previous[-1].get("code_commit") == previous[-2].get("code_commit")
        )
        if same_reason and same_head:
            result["error"] = "rework-stalled-same-reason"
            result["error_kind"] = "needs_input"
            return result

    result["task_base_commit"] = task_base
    result["continuation_base"] = continuation
    result["changes_requested"] = requested
    result["changes_requested_summary"] = latest_reason.strip()
    return result


def _rework_no_changes_reason(rework: Mapping[str, Any]) -> str:
    """Name a rework round that authored no commit, with the requested fixes."""

    summary = rework.get("changes_requested_summary")
    if isinstance(summary, str) and summary.strip():
        return f"rework-no-changes: {summary.strip()}"
    return "rework-no-changes"


def run_once(
    *,
    config: LaneConfig,
    adapter: BoardAdapter,
    board_alias: str,
    task: Mapping[str, Any],
    card: CardSpec,
    identity: RunIdentity,
    executor: Any,
    settings: RunSettings | None = None,
    stop_event: Any = None,
    work_root: str | os.PathLike[str] | None = None,
    package_dir: str | os.PathLike[str] | None = None,
    inherited_env: Mapping[str, str] | None = None,
    members_fn: Callable[[int | None], dict[str, Any]] = process_group_members,
    requirement_id: str = REQUIREMENT_ID,
) -> RunOutcome:
    settings = settings or RunSettings()
    root = Path(work_root or config.work_root)
    outcome = RunOutcome(
        task_id=identity.task_id,
        run_id=identity.run_id,
        board=board_alias,
        category="environment_failure",
    )
    started_at = int(time.time())

    # -- D4 rework preflight: no workspace and no executor when it fails -----
    review_config = config.review_options()
    try:
        events = adapter.changes_requested_events(identity.task_id)
    except Exception:
        events = []
    rework = rework_context(
        work_root=root,
        task_id=identity.task_id,
        current_run_id=identity.run_id,
        repository=config.repositories[card.repository].path,
        events=events,
        max_rounds=review_config.max_rework_rounds,
    )
    if rework["error"] is not None:
        outcome.category = "code_failure"
        outcome.reason = rework["error"]
        outcome.terminal = "block"
        try:
            if adapter.identity(identity.task_id) == identity:
                outcome.terminal_ok = adapter.block(
                    identity, rework["error"], rework["error_kind"]
                )
        except Exception:  # pragma: no cover - board unavailable
            pass
        return outcome

    round_number = int(rework["round"])
    task_base_commit = str(rework["task_base_commit"])
    continuation_base = str(rework["continuation_base"])
    rework_changes = list(rework.get("changes_requested") or [])
    rework_summary = rework.get("changes_requested_summary")

    run_dir, workspace = allocate_run_paths(root, identity.task_id, identity.run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("intents", "commands", "evidence", "home", "tmp", "nosite"):
        (run_dir / name).mkdir(exist_ok=True)
    outcome.run_dir = str(run_dir)
    outcome.workspace = str(workspace)
    _write_identity(run_dir, identity)

    supervisor_pid = os.getpid()
    supervisor_fingerprint = start_fingerprint(supervisor_pid)
    state = new_state(
        identity=identity,
        board=board_alias,
        run_dir=run_dir,
        workspace=workspace,
        supervisor_pid=supervisor_pid,
        supervisor_fingerprint=supervisor_fingerprint,
        requirement_id=requirement_id,
        round=round_number,
        task_base_commit=task_base_commit,
        continuation_base=continuation_base,
    )
    write_state(run_dir, state)

    assessments = assess_previous_runs(
        root, identity.task_id, identity.run_id, members_fn=members_fn
    )

    repository = config.repositories[card.repository].path
    executor_cfg = config.executors[card.executor]
    session_id = str(uuid.uuid4())
    source_info = _source_info(config.hermes_source)
    child: ManagedChild | None = None
    cleanup: dict[str, Any] = {
        "terminated": False,
        "residual_pids": [],
        "pid": None,
        "pgid": None,
        "fingerprint": None,
        "witness": None,
    }
    intents: list[dict[str, Any]] = []
    gate_verdict: str | None = None
    gate_report: dict[str, Any] | None = None
    gate_report_path: str | None = None
    gate_report_sha: str | None = None
    original_base: str | None = None
    run_commit: str | None = None
    sync_base: str | None = None
    code_commit: str | None = None
    baseline: dict[str, Any] = {
        "original_base": None,
        "sync_base": None,
        "updated": False,
        "merge_commit": None,
        "conflict": False,
        "conflict_paths": [],
    }
    evidence_document: dict[str, Any] = {
        "index_path": None,
        "index_sha256": None,
        "verified": False,
        "reasons": [],
    }
    changed: list[str] = []
    category = "environment_failure"
    failure_reason: str | None = None
    classification: Any = None
    stdout_text = ""
    parsed: dict[str, Any] = {}
    lost = False
    timed_out = False
    cancelled = False
    raw_exit_code = -1
    credentials: dict[str, str] = {}
    credential_reason: str | None = None
    env: dict[str, str] = {}
    argv: list[str] = []
    version: str | None = None
    terminal: str | None = None
    terminal_ok: bool | None = None
    reason: str | None = None

    try:
        # 1-2. base + per-run worktree ------------------------------------
        original_base = repo_head(repository)
        baseline["original_base"] = original_base
        create_run_worktree(
            repository,
            continuation_base,
            f"hkl/{identity.task_id}/run-{identity.run_id}-{uuid.uuid4().hex[:8]}",
            workspace,
        )
        state.advance(
            "workspace",
            base_commit=continuation_base,
            task_base_commit=task_base_commit,
            continuation_base=continuation_base,
            round=round_number,
        )
        write_state(run_dir, state)

        # 3. executor spawn; journal the child *before* anything else ----
        credentials, credential_reason = executor.credential_env()
        env = build_minimal_env(
            run_dir,
            inherited=inherited_env,
            credentials=credentials,
        )
        # W5 D7: the executor names its own run-local settings file; Claude
        # keeps ``claude-settings.json``, Codex uses ``codex-settings.json``.
        settings_filename = getattr(
            executor, "settings_filename", "executor-settings.json"
        )
        write_json(run_dir / settings_filename, executor.settings_document())
        prompt = str(task.get("body") or task.get("title") or "")
        if round_number > 1 and isinstance(rework_summary, str) and rework_summary:
            # D4: the review hand-off is part of the executor input, not just
            # board metadata.  Without it a rework round runs blind.
            prompt = (
                f"{prompt}\n\n"
                f"Rework round {round_number}. The reviewer requested:\n"
                f"{rework_summary}"
            )
        argv = executor.build_argv(
            executor_cfg.argv,
            run_dir=str(run_dir),
            session_id=session_id,
            prompt=prompt,
        )
        version = executor.version(executor_cfg.argv)

        child = spawn(
            argv,
            env=env,
            cwd=str(workspace),
            stdout_path=str(run_dir / "stdout.log"),
            stderr_path=str(run_dir / "stderr.log"),
            name=f"{card.executor}:{identity.task_id}",
        )
        state.advance("executor").set_child(
            pid=child.pid, pgid=child.pgid, fingerprint=child.fingerprint
        )
        write_state(run_dir, state)
        adapter.record_worker(identity, child.pid or 0)

        # 4. wait / heartbeat / timeout ----------------------------------
        deadline = time.monotonic() + settings.max_runtime_seconds
        next_heartbeat = time.monotonic() + max(0.01, settings.heartbeat_seconds)
        while True:
            if child.poll() is not None:
                break
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break
            if stop_event is not None and stop_event.is_set():
                cancelled = True
                break
            if now >= next_heartbeat:
                if not adapter.heartbeat(identity):
                    lost = True
                    break
                next_heartbeat = time.monotonic() + max(
                    0.01, settings.heartbeat_seconds
                )
            child.wait(
                min(
                    settings.poll_seconds,
                    max(0.01, deadline - time.monotonic()),
                )
            )

        raw_exit_code = child.returncode if child.returncode is not None else -1
        cleanup_result = terminate(
            child,
            grace_seconds=settings.grace_seconds,
            kill_grace_seconds=settings.kill_grace_seconds,
            members_fn=members_fn,
        )
        cleanup = {
            **cleanup_result,
            "pid": child.pid,
            "pgid": child.pgid,
            "fingerprint": child.fingerprint,
        }
        outcome.cleanup = cleanup

        try:
            stdout_text = (run_dir / "stdout.log").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            stdout_text = ""
        parsed = executor.parse(stdout_text)
        stderr_tail = child.stderr_tail()

        if lost:
            category = "cancelled"
        elif timed_out:
            category = "timeout"
        elif cancelled:
            category = "cancelled"
        else:
            classification = executor.classify(
                raw_exit_code, parsed, stderr=stderr_tail
            )
            category = classification.category

        # D5: an unproven process-tree termination dominates every other
        # category; the workspace must not be reused.
        cleanup_failed = not cleanup.get("terminated", False)
        if cleanup_failed:
            category = "environment_failure"
            failure_reason = "process-group-cleanup-incomplete"
            write_unreusable_marker(
                root / "reclaimable",
                workspace,
                reason="process-group-cleanup-incomplete",
                witness=cleanup.get("witness"),
                run_id=identity.run_id,
            )
        cancel_reason = (
            (getattr(stop_event, "reason", None) or "cancelled")
            if cancelled
            else None
        )

        # W13: the executor has exited (successfully or not), so its pid is
        # dead.  Leaving it published would let the host's crash sweep reclaim
        # this still-running card while gate/review finishing is in flight.
        # Clear only this run, fenced by its run id and claim lock, and do it
        # before any intents/commit/gate work.  After losing the claim we write
        # nothing (D2/D6), so the doomed pid stays for the host's own takeover.
        if not lost:
            try:
                adapter.clear_worker(identity)
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                # Never continue silently into the gate with a dead pid still
                # published: record the explicit cause and take the ordinary
                # environment-failure/block path.
                category = "environment_failure"
                failure_reason = f"worker-pid-clear:{type(exc).__name__}"

        state.advance("executor_exited")
        write_state(run_dir, state)

        # 5. intents (while still running) -------------------------------
        if not lost:
            for name, document in _read_intent_files(run_dir):
                if document is None:
                    intents.append(
                        {
                            "file": name,
                            "ok": False,
                            "reason": "invalid_intent",
                            "intent_id": None,
                            "kind": None,
                        }
                    )
                    continue
                result = adapter.apply_intent(identity, document)
                result["file"] = name
                intents.append(result)
        outcome.intents = intents

        if classification is not None:
            outcome.usage = classification.usage
            outcome.permission_denials = classification.permission_denials

        # 6. commit the run branch (failure classes keep their work) -----
        commit_error: str | None = None
        try:
            run_commit = commit_run(
                workspace,
                f"hkl: {identity.task_id} run {identity.run_id}",
            )
        except GitError as exc:
            commit_error = str(exc)
            run_commit = None
        no_op_round = bool(
            category == "success"
            and run_commit is not None
            and run_commit == continuation_base
        )
        if run_commit is not None:
            try:
                # ``run_commit == continuation_base`` means this round authored
                # nothing; do not report the previous round's diff as its own.
                changed = (
                    []
                    if run_commit == continuation_base
                    else diff_paths(task_base_commit, run_commit, cwd=workspace)
                )
            except GitError:
                changed = []
        else:
            try:
                changed = changed_paths(workspace)
            except GitError:
                changed = []
        state.advance("committed", run_commit=run_commit)
        write_state(run_dir, state)
        if category == "success" and run_commit is None:
            category = "environment_failure"
            failure_reason = "commit-failed"
        elif no_op_round and round_number > 1:
            # D4: a rework round that authored no commit is named for what it
            # is.  It must never fall through to the evidence ancestry check,
            # where the advancing main tip would mislabel it as tampering.
            category = "code_failure"
            failure_reason = _rework_no_changes_reason(rework)

        # 7. baseline sync (D5) ------------------------------------------
        try:
            sync_base = repo_head(repository)
        except GitError:
            sync_base = original_base
        baseline["sync_base"] = sync_base
        code_commit = run_commit
        merge_error: str | None = None
        needs_sync = False
        if category == "success" and run_commit is not None and sync_base is not None:
            # Merge the baseline when main moved during the run *or* when the
            # rework branch was cut from an older commit.  After the merge
            # ``sync_base`` is an ancestor of ``code_commit`` by construction.
            needs_sync = bool(
                sync_base != original_base
                or not is_ancestor(sync_base, run_commit, repo=workspace)
            )
        baseline["updated"] = needs_sync
        if category == "success" and run_commit is not None and needs_sync:
            merge = merge_commit(
                workspace,
                sync_base,
                message=f"hkl: sync base for {identity.task_id} run {identity.run_id}",
            )
            if merge.ok:
                code_commit = merge.commit
                baseline["merge_commit"] = merge.commit
            elif merge.conflict:
                abort_merge(workspace)
                baseline["conflict"] = True
                baseline["conflict_paths"] = list(merge.conflict_paths)
                category = "baseline_conflict"
            else:
                abort_merge(workspace)
                merge_error = merge.error or "merge-failed"
                category = "environment_failure"
                failure_reason = f"baseline-sync:{merge_error}"
        if category not in ("baseline_conflict",):
            state.advance(
                "synced",
                sync_base=sync_base,
                code_commit=code_commit,
                run_commit=run_commit,
            )
            write_state(run_dir, state)

        # 8. gate (D1) ----------------------------------------------------
        if category == "success":
            gate_env = build_minimal_env(run_dir, inherited=inherited_env)
            gate = run_gate(
                card,
                workspace,
                continuation_base,
                run_dir,
                identity=identity,
                env=gate_env,
                command_timeout_seconds=settings.command_timeout_seconds,
                run_commit=run_commit,
                sync_base=sync_base or original_base,
                scope_base=task_base_commit,
                requirement_id=requirement_id,
            )
            gate_verdict = gate.verdict
            gate_report = gate.report
            gate_report_path = gate.report_path
            gate_report_sha = sha256_file(run_dir / gate_report_path)
            verified, evidence_reason = verify_gate_report(
                run_dir, gate_report_path, gate_report_sha
            )
            if not verified:
                # A missing/empty/substituted report is an explicit error and
                # can never produce a request_review.
                failure_reason = f"evidence:{evidence_reason}"
                gate_verdict = "blocked"
                category = "environment_failure"
            elif gate.verdict == "blocked":
                failure_reason = "gate-workspace-unavailable"
                category = "environment_failure"
            elif not gate.passed:
                category = "code_failure"
            state.advance("gated", gate_verdict=gate_verdict)
            write_state(run_dir, state)

        # 9. manifest -> evidence index -> landing preflight -------------
        if category == "success" and code_commit and gate_report_path and gate_report_sha:
            package_root = _default_package_dir(package_dir)
            manifest = build_manifest(
                identity,
                base_commit=continuation_base,
                code_commit=code_commit,
                gate_report_path=gate_report_path,
                gate_report_sha256=gate_report_sha,
                verdict="pass",
                package_dir=package_root,
            )
            write_json(run_dir / "evidence" / "manifest.json", manifest.to_dict())
            index_document, index_sha = write_evidence_index(
                run_dir,
                identity=identity,
                base_commit=continuation_base,
                sync_base=sync_base or original_base or "",
                code_commit=code_commit,
                tool_sha256=manifest.tool_sha256,
                gate_report_path=gate_report_path,
                gate_report_sha256=gate_report_sha,
                verdict="pass",
                requirement_id=requirement_id,
            )
            evidence_document = {
                "index_path": INDEX_RELATIVE_PATH,
                "index_sha256": index_sha,
                "verified": False,
                "reasons": [],
            }
            verification = verify_evidence(
                run_dir,
                identity,
                package_root,
                workspace,
                repository,
                MAIN_REF,
                requirement_id=requirement_id,
            )
            if not verification.ok:
                evidence_document["reasons"] = list(verification.reasons)
                failure_reason = "evidence:" + ";".join(verification.reasons)
                category = "environment_failure"
            else:
                row = _board_row(adapter, identity.task_id)
                landing = landing_preflight(
                    run_dir=run_dir,
                    identity=identity,
                    workspace=workspace,
                    current_workspace=outcome.workspace or workspace,
                    row=row,
                    repo=repository,
                    main_ref=MAIN_REF,
                    package_dir=package_root,
                    base_commit=continuation_base,
                    sync_base=sync_base or original_base or "",
                    code_commit=code_commit,
                    branch=current_branch(workspace),
                    requirement_id=requirement_id,
                )
                if landing.verdict in ("ready", "not-ready") and landing.plan_path:
                    # Re-register the plan (D3) and re-verify the package.
                    _, index_sha = write_evidence_index(
                        run_dir,
                        identity=identity,
                        base_commit=continuation_base,
                        sync_base=sync_base or original_base or "",
                        code_commit=code_commit,
                        tool_sha256=manifest.tool_sha256,
                        gate_report_path=gate_report_path,
                        gate_report_sha256=gate_report_sha,
                        verdict="pass",
                        requirement_id=requirement_id,
                    )
                    evidence_document["index_sha256"] = index_sha
                    verification = verify_evidence(
                        run_dir,
                        identity,
                        package_root,
                        workspace,
                        repository,
                        MAIN_REF,
                        requirement_id=requirement_id,
                    )
                    evidence_document["verified"] = verification.ok
                    evidence_document["reasons"] = list(verification.reasons)
                if landing.verdict == "ready" and verification.ok:
                    state.advance("evidence")
                    write_state(run_dir, state)
                    summary = _summary(
                        identity, changed, code_commit, requirement_id
                    )
                    metadata = {
                        "hkl": {
                            "requirement_id": requirement_id,
                            "run_id": identity.run_id,
                            "round": round_number,
                            "task_base_commit": task_base_commit,
                            "continuation_base": continuation_base,
                            "code_commit": code_commit,
                            "gate_verdict": "pass",
                            "changed_paths": changed,
                            "evidence_index": INDEX_RELATIVE_PATH,
                            "evidence_index_sha256": evidence_document[
                                "index_sha256"
                            ],
                            "landing_plan": landing.plan_path,
                        }
                    }
                    # W8 D3: the card body declares whether a passing gate is
                    # enough.  Both terminals write through BoardAdapter.
                    if card.review:
                        ok = adapter.request_review(
                            identity,
                            summary=summary,
                            reviewer=review_config.reviewer,
                            metadata=metadata,
                        )
                        terminal = "request_review"
                    else:
                        ok = adapter.complete_task(
                            identity,
                            result=summary,
                            summary=summary,
                            metadata=metadata,
                        )
                        terminal = "complete"
                    terminal_ok = ok
                    if not ok:
                        reason = "stale_run"
                elif landing.verdict == "not-ready":
                    if not verification.ok:
                        failure_reason = "evidence:" + ";".join(
                            verification.reasons
                        )
                        category = "environment_failure"
                    else:
                        category = "code_failure"
                        failure_reason = "landing:merge-conflict"
                else:
                    if not landing.checks.get("evidence", False):
                        detail = [
                            item
                            for item in landing.reasons
                            if item.startswith("evidence:")
                        ]
                        failure_reason = (
                            detail[0]
                            if detail
                            else "evidence:landing-verification-failed"
                        )
                        category = "environment_failure"
                    elif not landing.checks.get("identity", False) or not landing.checks.get(
                        "workspace", False
                    ):
                        category = "cancelled"
                        reason = "stale_run"
                    else:
                        category = "environment_failure"
                        failure_reason = "landing:" + (
                            landing.reasons[0] if landing.reasons else "rejected"
                        )

        outcome.gate_verdict = gate_verdict
        outcome.code_commit = code_commit
        outcome.sync_base = sync_base
        outcome.run_commit = run_commit
        outcome.baseline = dict(baseline)
        outcome.changed_paths = changed
        outcome.exit_code = raw_exit_code
        outcome.category = category

        # 10. terminal transition ----------------------------------------
        if lost:
            # D2/D6: after losing the claim the supervisor writes nothing.
            terminal, reason = None, "heartbeat_lost"
            terminal_ok = None
        elif category == "success" and terminal in ("request_review", "complete"):
            pass
        elif category == "baseline_conflict":
            reason = f"baseline-conflict:{len(baseline['conflict_paths'])} paths"
            terminal, terminal_ok = "block", adapter.block(
                identity, reason, "needs_input"
            )
        elif category == "cancelled" and not cancelled:
            # Landing identity/path failure: stale run, never write the board.
            terminal, terminal_ok = None, None
            reason = reason or "stale_run"
        elif category == "cancelled" and cancelled:
            terminal, terminal_ok = "reclaim", adapter.reclaim(
                identity, cancel_reason or "cancelled"
            )
        elif category == "timeout":
            terminal, terminal_ok = "block", adapter.block(
                identity, "timeout", "transient"
            )
        elif category == "environment_failure":
            reason = failure_reason or reason or "environment_failure"
            block_kind = (
                "capability"
                if failure_reason is not None or cleanup_failed
                else (
                    classification.block_kind
                    if classification is not None
                    else "capability"
                )
            ) or "capability"
            terminal, terminal_ok = "block", adapter.block(
                identity, reason, block_kind
            )
        else:  # code_failure (including gate failure and landing conflict)
            block_reason = reason or failure_reason or (
                f"gate:{_gate_failure_check(gate_report)}"
                if gate_verdict is not None and gate_verdict != "pass"
                else "code_failure"
            )
            # Mirror the environment_failure branch: the outcome names the same
            # reason the board was given, so a named refusal is never ``None``
            # only in run metadata.
            reason = block_reason
            terminal, terminal_ok = "block", adapter.block(
                identity, block_reason, "needs_input"
            )

        if terminal_ok is False:
            # Never blindly retry a terminal write; re-read and record loss.
            current = adapter.identity(identity.task_id)
            if current != identity:
                reason = "stale_run"

        # -- ExecutorResult (validated against the W1 contract) ----------
        contract_exit = raw_exit_code
        if category in ("code_failure", "environment_failure") and contract_exit == 0:
            contract_exit = 1
        usage_dict = outcome.usage
        ExecutorResult(
            schema_version=1,
            identity=identity,
            category=category,
            exit_code=int(contract_exit),
            changed_paths=tuple(changed),
            artifact_paths=tuple(card.artifacts) if category == "success" else (),
            usage=Usage(
                input_tokens=usage_dict.get("input_tokens"),
                output_tokens=usage_dict.get("output_tokens"),
                cost_usd=usage_dict.get("cost_usd"),
            ),
        )

        outcome.terminal = terminal
        outcome.terminal_ok = terminal_ok
        outcome.reason = reason
        outcome.category = category
        outcome.evidence = evidence_document
        state.advance("terminal")
        write_state(run_dir, state)
        outcome.stage = state.stage
    except Exception as exc:  # noqa: BLE001 - record, never crash the daemon
        category = "environment_failure"
        outcome.category = category
        outcome.reason = f"{type(exc).__name__}: {exc}"
        if child is not None and not cleanup.get("terminated", False):
            try:
                cleanup_result = terminate(
                    child,
                    grace_seconds=settings.grace_seconds,
                    kill_grace_seconds=settings.kill_grace_seconds,
                    members_fn=members_fn,
                )
                cleanup = {
                    **cleanup_result,
                    "pid": child.pid,
                    "pgid": child.pgid,
                    "fingerprint": child.fingerprint,
                }
            except Exception:  # pragma: no cover - best effort cleanup
                pass
        if not cleanup.get("terminated", False):
            try:
                write_unreusable_marker(
                    root / "reclaimable",
                    workspace,
                    reason="process-group-cleanup-incomplete",
                    witness=cleanup.get("witness"),
                    run_id=identity.run_id,
                )
            except OSError:  # pragma: no cover - marker is best effort here
                pass
        outcome.cleanup = cleanup
        # W13: an exception after the executor exited must not leave its dead
        # pid published either.  Only clear once termination is proven and the
        # claim still belongs to this run; a failed clear is recorded here and
        # the ordinary block path below still runs.
        if child is not None and cleanup.get("terminated", False) and not lost:
            try:
                adapter.clear_worker(identity)
            except Exception as clear_exc:  # noqa: BLE001 - adapter boundary
                outcome.reason = (
                    f"{outcome.reason}; worker-pid-clear:"
                    f"{type(clear_exc).__name__}"
                )
        try:
            if adapter.identity(identity.task_id) == identity:
                outcome.terminal_ok = adapter.block(
                    identity, "environment_failure", "capability"
                )
                outcome.terminal = "block"
        except Exception:  # pragma: no cover - board unavailable
            pass
        try:
            state.advance("terminal")
            write_state(run_dir, state)
        except Exception:  # pragma: no cover - journal is best effort here
            pass
        outcome.stage = state.stage

    # -- run.json ------------------------------------------------------------
    # W11c: the run is terminal in every path that reaches here (success,
    # failure, timeout, cancellation or a recorded exception).  Disposable
    # ``tmp/`` is no longer needed, but run.json/evidence/state stay put.
    tmp_cleanup_bytes = remove_run_tmp(run_dir)
    finished_at = int(time.time())
    outcome.baseline = dict(baseline)
    outcome.evidence = evidence_document
    # D2b: the run carries the declaration it actually used.  A synthesised
    # card has no ``cards/<id>.json``; its pointer must read ``null`` instead of
    # naming a file that does not (and must not) exist.
    card_path = Path(config.work_root) / "cards" / f"{identity.task_id}.json"
    run_document = {
        "schema_version": 1,
        "requirement_id": requirement_id,
        "identity": identity.to_dict(),
        "board": board_alias,
        "card_path": str(card_path) if card_path.is_file() else None,
        "card": card.to_dict(),
        "workspace": str(workspace),
        "run_dir": str(run_dir),
        "base_commit": continuation_base,
        "sync_base": sync_base,
        "run_commit": run_commit,
        "code_commit": code_commit,
        "round": round_number,
        "task_base_commit": task_base_commit,
        "continuation_base": continuation_base,
        "rework": {
            "round": round_number,
            "changes_requested": rework_changes,
            "summary": rework_summary,
        },
        "review": None,
        "baseline": baseline,
        "previous_runs": assessments,
        "executor": {
            "name": card.executor,
            "version": version,
            "argv_sha256": child.argv_sha256 if child is not None else None,
            "session_id": session_id,
            # W5 D7: Codex reports its ephemeral thread id in JSONL; Claude has
            # no equivalent and keeps ``None``.
            "executor_session_id": (
                parsed.get("thread_id")
                if isinstance(parsed.get("thread_id"), str)
                and parsed.get("thread_id")
                else None
            ),
            "env_keys": sorted(env),
            "removed_env_keys": _removed_env_keys(
                inherited_env if inherited_env is not None else os.environ
            ),
            "credential_keys": sorted(credentials),
            "credential_reason": credential_reason,
        },
        "category": outcome.category,
        "exit_code": raw_exit_code,
        "changed_paths": changed,
        "gate": {
            "verdict": gate_verdict,
            "report_path": gate_report_path,
            "sha256": gate_report_sha,
            "failure_check": _gate_failure_check(gate_report)
            if gate_verdict not in (None, "pass")
            else None,
        },
        "evidence": evidence_document,
        "recovery": {"recovered": False, "stage_before": None, "finalized": False},
        "cleanup": cleanup,
        "tmp_cleanup": {
            "path": "tmp",
            "removed_bytes": tmp_cleanup_bytes,
        },
        "intents": intents,
        "terminal": {
            "action": outcome.terminal,
            "ok": outcome.terminal_ok,
            "reason": outcome.reason,
        },
        "usage": outcome.usage,
        "permission_denials": outcome.permission_denials,
        "source": source_info,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    write_json(run_dir / "run.json", run_document)
    outcome.category = run_document["category"]
    return outcome


def _summary(
    identity: RunIdentity,
    changed: list[str],
    code_commit: str | None,
    requirement_id: str = REQUIREMENT_ID,
) -> str:
    rendered = ", ".join(changed[:20]) if changed else "no file changes"
    return (
        f"{requirement_id} run {identity.run_id}: {rendered}; "
        f"gate pass; code_commit={code_commit}"
    )


def recover_run(
    *,
    adapter: BoardAdapter,
    board_alias: str,
    run_dir: str | os.PathLike[str],
    package_dir: str | os.PathLike[str] | None = None,
    requirement_id: str = REQUIREMENT_ID,
    reviewer: str | None = None,
) -> dict[str, Any]:
    """Finish a run whose supervisor died at ``stage=evidence``; nothing else.

    Only the evidence stage may be completed idempotently.  Every other stage
    is left to claim TTL / crash recovery, and the executor is never re-run.
    """

    run_path = Path(run_dir)
    state = read_state(run_path)
    if state is None:
        return {
            "run_dir": str(run_path),
            "task_id": None,
            "run_id": None,
            "stage": None,
            "action": "no-state",
        }
    base = {
        "run_dir": str(run_path),
        "task_id": state.task_id,
        "run_id": state.run_id,
        "stage": state.stage,
    }
    if state.board and board_alias and state.board != board_alias:
        # A shared work root may hold runs from several boards; never finish
        # someone else's run on this board connection.
        return {**base, "action": "foreign-board"}
    if _supervisor_alive(state):
        return {**base, "action": "supervisor-alive"}
    if state.stage == "terminal":
        return {**base, "action": "noop"}
    if state.stage != "evidence":
        return {
            **base,
            "action": "left-for-reclaim",
            "child": state.child,
        }

    row = _board_row(adapter, state.task_id)
    status = row.get("status") if row else None
    if status == "review":
        return {**base, "action": "already-review"}
    if row is None or status != "running":
        return {**base, "action": "stale_run"}
    if (
        row.get("current_run_id") is None
        or int(row["current_run_id"]) != state.run_id
        or row.get("claim_lock") != state.claim_lock
    ):
        return {**base, "action": "stale_run"}

    package_root = _default_package_dir(package_dir)
    verification = verify_evidence(
        run_path,
        state.identity,
        package_root,
        state.workspace,
        state.workspace,
        requirement_id=requirement_id,
    )
    if not verification.ok:
        return {
            **base,
            "action": "evidence-invalid",
            "reasons": list(verification.reasons),
        }
    ok = adapter.request_review(
        state.identity,
        summary=f"{requirement_id} run {state.run_id}: recovered evidence; "
        f"code_commit={state.code_commit}",
        reviewer=reviewer,
        metadata={
            "hkl": {
                "run_id": state.run_id,
                "code_commit": state.code_commit,
                "gate_verdict": state.gate_verdict,
                "recovered": True,
            }
        },
    )
    if not ok:
        return {**base, "action": "request-review-not-applied"}
    state.advance("terminal")
    write_state(run_path, state)
    # The recovered run is terminal too; retention would do this on its next
    # pass, but the supervisor owns the immediate cleanup.
    remove_run_tmp(run_path)
    return {**base, "action": "request_review"}


def recover_runs(
    work_root: str | os.PathLike[str],
    adapter: BoardAdapter,
    package_dir: str | os.PathLike[str] | None = None,
    requirement_id: str = REQUIREMENT_ID,
    reviewer: str | None = None,
) -> list[dict[str, Any]]:
    """Scan ``<work_root>/runs/*/*`` and recover supervisor-dead evidence runs."""

    runs_root = Path(work_root) / "runs"
    recovered: list[dict[str, Any]] = []
    if not runs_root.is_dir():
        return recovered
    board_alias = getattr(adapter, "name", None)
    for task_dir in sorted(runs_root.iterdir()):
        if not task_dir.is_dir():
            continue
        for run_path in sorted(task_dir.iterdir()):
            if not run_path.is_dir():
                continue
            state = read_state(run_path)
            if state is None:
                continue
            if board_alias and state.board and state.board != board_alias:
                continue
            entry = recover_run(
                adapter=adapter,
                board_alias=board_alias or state.board,
                run_dir=run_path,
                package_dir=package_dir,
                requirement_id=requirement_id,
                reviewer=reviewer,
            )
            recovered.append(entry)
    return recovered


__all__ = [
    "MAIN_REF",
    "RunOutcome",
    "RunSettings",
    "allocate_run_paths",
    "assess_previous_runs",
    "build_minimal_env",
    "recover_run",
    "recover_runs",
    "run_once",
]
