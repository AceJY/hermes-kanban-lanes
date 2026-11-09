"""W3-12 live smoke: one real Claude chain on temporary board/repo/work-root.

Opt-in only.  ``HKL_LIVE_CLAUDE=1`` is required; without it the module is
skipped, so the default suite never spawns Claude, spends money or touches the
network.  When the switch *is* set but the prerequisites are unavailable the
test fails explicitly instead of skipping as if it had passed.

The test never prints ``ANTHROPIC_*`` values.  Credential values are read into
memory, injected into the child environment by the supervisor, and screened
out of every artifact written under ``artifacts/w3/raw/``.

The executor's own stdout/stderr can echo an ``ANTHROPIC_*`` value (the model
identifier comes back from the gateway inside the CLI's JSON result).  Per
docs/09 section 8 the archived copies of exactly those two files are scrubbed;
``evidence/index.json`` in the archive is then recomputed against the *archive
bytes*, so only the affected ``size``/``sha256`` entries change.  The original
run directory is treated as a read-only source (snapshotted before archiving
and asserted byte-identical afterwards).  The redaction record keeps only key
names and the original/archived digests and sizes -- never a value -- and the
archived package is verified green with the product ``verify_evidence``.

Gate command output is never rewritten.  If an ``ANTHROPIC_*`` value appears
in any archived file other than the executor's two logs, archiving stops with
an explicit error instead of producing a doctored package.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.executors.claude import ClaudeAdapter
from hermes_kanban_lanes.runtime import daemon
from hermes_kanban_lanes.runtime.supervisor import RunSettings
from hermes_kanban_lanes.verification.evidence import (
    INDEX_RELATIVE_PATH,
    sha256_file,
    tool_digest,
    verify_evidence,
    verify_gate_report,
)
from hermes_kanban_lanes.workspace.git import run_git

pytestmark = pytest.mark.skipif(
    os.environ.get("HKL_LIVE_CLAUDE") != "1",
    reason="live Claude smoke is opt-in (set HKL_LIVE_CLAUDE=1)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src" / "hermes_kanban_lanes"
ARTIFACT_DIR = REPO_ROOT / "artifacts" / "w3" / "raw"
# Verbatim run evidence, archived before any success assertion so a reviewer
# can recompute the gate digest, the manifest identity and the evidence index.
LIVE_DIR = ARTIFACT_DIR / "live"
LIVE_ARTIFACTS = (
    "run.json",
    "state.json",
    "evidence/gate-report.json",
    "evidence/manifest.json",
    "evidence/index.json",
    "landing-plan.json",
    "stdout.log",
    "stderr.log",
)
PYTHON = sys.executable
BUDGET_USD = 2.0
PROMPT = (
    "Create a file named RESULT.txt in the current working directory containing "
    "exactly the text hkl-live-ok followed by a newline. Do not create or modify "
    "any other file. Do not run git commands."
)
EXPECTED_TEXT = "hkl-live-ok\n"


def _require_prerequisites() -> tuple[str, dict[str, str]]:
    binary = shutil.which("claude")
    if not binary:
        pytest.fail(
            "HKL_LIVE_CLAUDE=1 but no 'claude' executable is on PATH",
            pytrace=False,
        )
    credentials, reason = ClaudeAdapter().credential_env()
    if not credentials:
        pytest.fail(
            f"HKL_LIVE_CLAUDE=1 but Claude credentials are unavailable: {reason}",
            pytrace=False,
        )
    return binary, credentials


def _write_artifact(
    name: str,
    document,
    credentials: dict[str, str],
    *,
    artifact_dir: Path = ARTIFACT_DIR,
) -> Path:
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    for value in credentials.values():
        # Never let a credential value reach the artifact tree.
        assert value not in text, "credential value would leak into artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / name
    path.write_text(text, encoding="utf-8")
    return path


EXECUTOR_LOGS = frozenset({"stdout.log", "stderr.log"})


class ArchiveError(RuntimeError):
    """Archiving stopped because evidence outside the allowance was sensitive."""


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """Size and digest of every file, used to prove the source was read-only."""

    snapshot: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[path.relative_to(root).as_posix()] = (
                path.stat().st_size,
                sha256_file(path),
            )
    return snapshot


@contextlib.contextmanager
def _read_only_tree(root: Path):
    """Make every regular file under ``root`` read-only for the duration."""

    modes: dict[Path, int] = {}
    for path in root.rglob("*"):
        if path.is_file():
            modes[path] = path.stat().st_mode
            path.chmod(0o444)
    try:
        yield
    finally:
        for path, mode in modes.items():
            path.chmod(mode)


def _archive_file(
    relative: str,
    source: Path,
    destination: Path,
    credentials: dict[str, str],
) -> dict | None:
    """Copy one evidence file; scrub only the executor's own two logs.

    The copy is written as bytes so every byte outside a replaced value is
    preserved exactly.  Any credential value in a non-redactable file (gate
    command logs, gate report, manifest, run/state, index, landing plan) is an
    explicit error: the gate output must never be silently rewritten.
    """

    data = source.read_bytes()
    hits = sorted(
        key
        for key, value in credentials.items()
        if value and value.encode("utf-8") in data
    )
    if relative not in EXECUTOR_LOGS:
        if hits:
            raise ArchiveError(
                "credential value present in non-redactable evidence "
                f"{relative} ({','.join(hits)}); refusing to archive"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return None

    redacted = data
    replaced: list[str] = []
    for key, value in sorted(credentials.items(), key=lambda item: -len(item[1])):
        encoded = value.encode("utf-8") if value else b""
        if encoded and encoded in redacted:
            redacted = redacted.replace(encoded, f"<redacted:{key}>".encode("utf-8"))
            replaced.append(key)
    if any(value and value.encode("utf-8") in redacted for value in credentials.values()):
        raise ArchiveError(f"credential value survived redaction in {relative}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(redacted)
    if not replaced:
        return None
    return {
        "path": relative,
        "archived_sha256": sha256_file(destination),
        "archived_size": destination.stat().st_size,
        "redacted_keys": sorted(replaced),
        "reason": "ANTHROPIC_* value screened out of the archived executor log",
    }


def _recompute_archived_index(
    run_dir: Path, redactions: list[dict], *, live_dir: Path = LIVE_DIR
) -> tuple[dict, dict]:
    """Rebuild the archive index against archive bytes, changing only redactions."""

    original = json.loads(
        (run_dir / INDEX_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    archived = json.loads(json.dumps(original))
    source_entries = {entry["path"]: entry for entry in original["files"]}
    redacted_paths = {record["path"] for record in redactions}
    for entry in archived["files"]:
        relative = entry["path"]
        source_entry = source_entries[relative]
        archived_file = live_dir / relative
        assert archived_file.is_file(), relative
        if relative in redacted_paths:
            entry["sha256"] = sha256_file(archived_file)
            entry["size"] = archived_file.stat().st_size
            assert entry != source_entry, relative
        else:
            # Untouched evidence must stay byte-identical to the original.
            assert entry == source_entry, relative
            assert sha256_file(archived_file) == entry["sha256"], relative
            assert archived_file.stat().st_size == entry["size"], relative
    for record in redactions:
        source_entry = source_entries[record["path"]]
        record["source_sha256"] = source_entry["sha256"]
        record["source_size"] = source_entry["size"]
    # The chain headers (identity, base/sync/code, tool digest, gate report)
    # are not redacted files and must survive verbatim.
    for key in (
        "schema_version",
        "requirement_id",
        "identity",
        "base_commit",
        "sync_base",
        "code_commit",
        "tool_sha256",
        "gate_report",
        "verdict",
    ):
        assert archived[key] == original[key], key
    live_dir.joinpath(INDEX_RELATIVE_PATH).write_text(
        json.dumps(archived, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return archived, original


def _live_artifacts(run_dir: Path) -> tuple[str, ...]:
    """Fixed W3 evidence files plus the per-command stdout/stderr logs."""

    commands: list[str] = []
    commands_dir = run_dir / "commands"
    if commands_dir.is_dir():
        commands = sorted(
            f"commands/{path.name}"
            for path in commands_dir.iterdir()
            if path.is_file() and path.name.endswith(".log")
        )
    return tuple(LIVE_ARTIFACTS) + tuple(commands)


def _archive_run_artifacts(
    run_dir: Path,
    credentials: dict[str, str],
    *,
    live_dir: Path = LIVE_DIR,
) -> tuple[dict[str, Path], list[dict]]:
    live_dir.mkdir(parents=True, exist_ok=True)
    # The repository ignores ``*.log`` globally; re-include the archived ones.
    (live_dir / ".gitignore").write_text(
        "!stdout.log\n!stderr.log\n!commands/*.log\n", encoding="utf-8"
    )
    archived: dict[str, Path] = {}
    redactions: list[dict] = []
    for relative in _live_artifacts(run_dir):
        source = run_dir / relative
        if source.is_file():
            destination = live_dir / relative
            record = _archive_file(relative, source, destination, credentials)
            archived[relative] = destination
            if record is not None:
                redactions.append(record)
    return archived, redactions


def test_real_claude_chain(
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    task_status,
    load_run_json,
):
    binary, credentials = _require_prerequisites()
    if LIVE_DIR.exists():
        shutil.rmtree(LIVE_DIR)
    repo = make_repo()
    config = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[binary, "--max-budget-usd", str(BUDGET_USD)],
    )
    task_id = make_task(title="HKL-W3 live smoke", body=PROMPT, assignee="hkl:claude")
    card_path = write_card(
        config.work_root,
        task_id,
        allow=("RESULT.txt",),
        commands=[
            {
                "argv": [
                    PYTHON,
                    "-c",
                    (
                        "import sys,pathlib;"
                        "data=pathlib.Path('RESULT.txt').read_text();"
                        "sys.exit(0 if data == 'hkl-live-ok\\n' else 1)"
                    ),
                ],
                "cwd": ".",
            }
        ],
    )
    # D8 additive key; declared here instead of in the shared fixture so the
    # live test stays within tests/live/.
    document = json.loads(card_path.read_text(encoding="utf-8"))
    document["artifacts"] = ["RESULT.txt"]
    card_path.write_text(json.dumps(document), encoding="utf-8")
    card = load_card(card_path, config)
    assert card.executor == "claude"

    settings = RunSettings(
        max_runtime_seconds=900.0,
        command_timeout_seconds=600.0,
        heartbeat_seconds=30.0,
        poll_seconds=0.5,
        grace_seconds=5.0,
        kill_grace_seconds=3.0,
    )
    report = daemon.tick(
        config,
        settings=settings,
        tick_index=0,
        package_dir=SRC_DIR,
    )

    _write_artifact(
        "live-tick.json",
        {
            "config": {
                "board": str(board.board_path),
                "repository": str(repo),
                "work_root": str(config.work_root),
                "executor_argv": [binary, "--max-budget-usd", str(BUDGET_USD)],
            },
            "report": report,
        },
        credentials,
    )

    assert report["errors"] == [], report["errors"]
    assert len(report["runs"]) == 1, report
    outcome = report["runs"][0]
    assert outcome["task_id"] == task_id

    run_dir = Path(outcome["run_dir"])
    document = load_run_json(run_dir)
    status = task_status(task_id)

    sanitized = {
        "outcome": outcome,
        "run": {
            key: document.get(key)
            for key in (
                "requirement_id",
                "identity",
                "board",
                "category",
                "base_commit",
                "sync_base",
                "run_commit",
                "code_commit",
                "baseline",
                "changed_paths",
                "gate",
                "evidence",
                "recovery",
                "cleanup",
                "terminal",
                "usage",
                "permission_denials",
                "source",
                "started_at",
                "finished_at",
            )
        },
        "task_status": status,
        "task_id": task_id,
        "run_dir": str(run_dir),
        "workspace": outcome.get("workspace"),
        "budget_usd": BUDGET_USD,
    }
    _write_artifact("live-run.json", sanitized, credentials)

    if outcome["category"] != "success":
        # Environment failures (auth/capacity/network) are recorded faithfully
        # and the test fails loudly; nothing here may be reported as a pass.
        assert outcome["category"] in (
            "environment_failure",
            "code_failure",
            "timeout",
            "cancelled",
        ), outcome
        assert status["status"] == "blocked", status
        pytest.fail(
            "live Claude chain did not complete: "
            f"category={outcome['category']} "
            f"terminal={outcome.get('terminal')} "
            f"reason={outcome.get('reason')}",
            pytrace=False,
        )

    # -- success: the full chain must be visible end to end -----------------
    # Archive the verbatim evidence *before* the success assertions: a later
    # failure still leaves a reviewable gate report / manifest / stdout.
    # The run directory is a read-only source: snapshot it and prove it later.
    run_dir_snapshot = _snapshot(run_dir)
    expected_artifacts = _live_artifacts(run_dir)
    with _read_only_tree(run_dir):
        archived, redactions = _archive_run_artifacts(run_dir, credentials)
        recomputed_archived_index, original_index = _recompute_archived_index(
            run_dir, redactions
        )
    assert _snapshot(run_dir) == run_dir_snapshot, "archiving modified the run dir"
    _write_artifact(
        "live-redactions.json",
        {
            "reason": (
                "the executor's own stdout/stderr echoes ANTHROPIC_* model "
                "identifiers; archived copies are scrubbed, only those index "
                "entries are recomputed, and original digests are kept here"
            ),
            "redactions": redactions,
        },
        credentials,
    )
    assert set(archived) == set(expected_artifacts), archived
    # W3-12: the command evidence is archived together with the run.
    assert "commands/0.stdout.log" in archived
    assert "commands/0.stderr.log" in archived
    assert status["status"] == "review", status
    # ``request_review`` closes the run on the host row.
    assert status["current_run_id"] is None
    assert status["claim_lock"] is None
    assert outcome["gate_verdict"] == "pass"
    assert outcome["terminal"] == "request_review"
    assert outcome["baseline"]["original_base"] == document["base_commit"]
    assert outcome["baseline"]["updated"] in (True, False)
    assert outcome["evidence"]["verified"] is True, outcome["evidence"]
    workspace = Path(outcome["workspace"])
    assert (workspace / "RESULT.txt").read_text(encoding="utf-8") == EXPECTED_TEXT

    assert document["requirement_id"] == "HKL-W3"
    assert document["category"] == "success"
    assert document["gate"]["verdict"] == "pass"
    assert document["cleanup"]["terminated"] is True
    assert document["cleanup"]["residual_pids"] == []
    assert document["changed_paths"] == ["RESULT.txt"]
    assert document["code_commit"]
    assert document["run_commit"]
    assert document["sync_base"]
    assert document["baseline"]["merge_commit"] is None or document["baseline"]["updated"]
    assert document["recovery"]["recovered"] is False
    committed = run_git(
        ["cat-file", "-e", document["code_commit"]], cwd=workspace
    )
    assert committed.returncode == 0
    assert run_git(["status", "--porcelain"], cwd=workspace).stdout.strip() == ""

    # Environment isolation: names only, never values.
    env_keys = document["executor"]["env_keys"]
    assert not any(key.startswith("HERMES_") for key in env_keys)
    assert all(key.startswith("ANTHROPIC_") for key in document["executor"]["credential_keys"])
    assert document["executor"]["credential_reason"] is None
    assert str(board.board_path) not in json.dumps(document)

    # Evidence: gate report digest, manifest identity and tool digest.
    manifest_path = run_dir / "evidence" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gate_report_path = manifest["gate_report"]["path"]
    assert manifest["gate_report"]["sha256"] == sha256_file(run_dir / gate_report_path)
    assert manifest["identity"] == document["identity"]
    assert manifest["code_commit"] == document["code_commit"]
    assert manifest["tool_sha256"] == tool_digest(SRC_DIR)
    assert verify_gate_report(
        run_dir, gate_report_path, manifest["gate_report"]["sha256"]
    ) == (True, "ok")

    # Gate report schema 2 and the separately captured command streams.
    gate_report = json.loads(
        (run_dir / gate_report_path).read_text(encoding="utf-8")
    )
    assert gate_report["schema_version"] == 2
    assert gate_report["requirement_id"] == "HKL-W3"
    assert gate_report["verdict"] == "pass"
    checks = {check["name"]: check for check in gate_report["checks"]}
    assert set(checks) == {
        "workspace",
        "scope",
        "progress",
        "commands",
        "artifacts",
    }
    assert all(check["ok"] for check in checks.values()), checks
    command_entries = checks["commands"]["commands"]
    assert command_entries, checks["commands"]
    for entry in command_entries:
        assert entry["exit_code"] == 0
        assert entry["timed_out"] is False
        for stream in ("stdout", "stderr"):
            log_path = run_dir / entry[f"{stream}_log"]
            assert log_path.is_file()
            assert entry[f"{stream}_sha256"] == sha256_file(log_path)
            assert entry[f"{stream}_size"] == log_path.stat().st_size
    assert [item["path"] for item in checks["artifacts"]["artifacts"]] == [
        "RESULT.txt"
    ]

    # ``evidence/index.json`` must be recomputable and fully green.
    identity = RunIdentity.from_dict(document["identity"])
    index = json.loads(
        (run_dir / INDEX_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert index["schema_version"] == 1
    assert index["requirement_id"] == "HKL-W3"
    assert index["verdict"] == "pass"
    assert index["identity"] == document["identity"]
    assert index["code_commit"] == document["code_commit"]
    assert index["sync_base"] == document["sync_base"]
    indexed_paths = {entry["path"] for entry in index["files"]}
    assert "commands/0.stdout.log" in indexed_paths
    assert "commands/0.stderr.log" in indexed_paths
    assert "landing-plan.json" in indexed_paths
    verification = verify_evidence(
        run_dir, identity, SRC_DIR, workspace, repo
    )
    assert verification.ok, verification.reasons
    assert all(verification.checks.values()), verification.checks
    assert document["evidence"]["verified"] is True

    # The landing preflight only plans; W3 never merges into main.
    landing = json.loads(
        (run_dir / "landing-plan.json").read_text(encoding="utf-8")
    )
    assert landing["verdict"] == "ready"
    assert landing["merge_tree"]
    assert landing["code_commit"] == document["code_commit"]
    assert landing["main_ref"] == "refs/heads/main"

    # -- archived evidence is self-consistent and independently readable ----
    archived_run = json.loads(
        (LIVE_DIR / "run.json").read_text(encoding="utf-8")
    )
    archived_manifest = json.loads(
        (LIVE_DIR / "evidence" / "manifest.json").read_text(encoding="utf-8")
    )
    archived_index = json.loads(
        (LIVE_DIR / INDEX_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert archived_index == recomputed_archived_index
    archived_gate_sha = sha256_file(LIVE_DIR / "evidence" / "gate-report.json")
    live_run_record = json.loads(
        (ARTIFACT_DIR / "live-run.json").read_text(encoding="utf-8")
    )
    # One digest across the archived report, the run receipt and the manifest.
    assert archived_gate_sha == archived_run["gate"]["sha256"]
    assert archived_gate_sha == live_run_record["run"]["gate"]["sha256"]
    assert archived_gate_sha == archived_manifest["gate_report"]["sha256"]
    assert archived_manifest["gate_report"]["path"] == archived_run["gate"]["report_path"]
    assert archived_run == document
    assert archived_manifest["identity"] == archived_run["identity"]
    assert archived_manifest["code_commit"] == archived_run["code_commit"]
    assert archived_manifest["tool_sha256"] == tool_digest(SRC_DIR)
    assert (LIVE_DIR / "stdout.log").stat().st_size > 0
    # W3-12 / section 8: the archive index is recomputed against archive bytes.
    # Only the two scrubbed executor logs may differ from the run index; the
    # original digest and size of those files stay in ``live-redactions.json``.
    redacted_by_path = {item["path"]: item for item in redactions}
    assert set(redacted_by_path) <= set(EXECUTOR_LOGS)
    original_by_path = {
        entry["path"]: entry for entry in original_index["files"]
    }
    changed: set[str] = set()
    for entry in archived_index["files"]:
        relative = entry["path"]
        archived_file = LIVE_DIR / relative
        assert archived_file.is_file(), relative
        if relative in redacted_by_path:
            record = redacted_by_path[relative]
            assert record["source_sha256"] == original_by_path[relative]["sha256"]
            assert record["source_size"] == original_by_path[relative]["size"]
            assert sha256_file(archived_file) == record["archived_sha256"]
            assert entry["sha256"] == record["archived_sha256"]
            assert entry["size"] == record["archived_size"]
            assert entry != original_by_path[relative]
            assert record["redacted_keys"]
            changed.add(relative)
        else:
            assert entry == original_by_path[relative]
            assert sha256_file(archived_file) == entry["sha256"]
            assert archived_file.stat().st_size == entry["size"]
    assert changed == set(redacted_by_path)
    for relative in ("commands/0.stdout.log", "commands/0.stderr.log"):
        # Gate command output is archived verbatim and never redacted.
        assert sha256_file(LIVE_DIR / relative) == sha256_file(run_dir / relative)
        assert sha256_file(LIVE_DIR / relative) in {
            entry["sha256"] for entry in archived_index["files"]
        }
    # Identity, code and tool chain survive: same headers, byte-identical
    # manifest/gate report, and the product verifier accepts the archive.
    assert archived_index["identity"] == original_index["identity"]
    assert archived_index["code_commit"] == original_index["code_commit"]
    assert archived_index["sync_base"] == original_index["sync_base"]
    assert archived_index["tool_sha256"] == original_index["tool_sha256"]
    assert archived_index["gate_report"] == original_index["gate_report"]
    assert sha256_file(LIVE_DIR / "evidence" / "manifest.json") == sha256_file(
        run_dir / "evidence" / "manifest.json"
    )
    archived_verification = verify_evidence(
        LIVE_DIR, identity, SRC_DIR, workspace, repo
    )
    assert archived_verification.ok, archived_verification.reasons
    assert all(archived_verification.checks.values()), archived_verification.checks
    # Every scrubbed copy is free of the injected credential values.
    for item in redactions:
        text = (LIVE_DIR / item["path"]).read_text(encoding="utf-8")
        assert all(not value or value not in text for value in credentials.values())

    # Usage and permission denials are recorded, and the budget was respected.
    usage = document["usage"]
    assert isinstance(usage.get("input_tokens"), int)
    assert isinstance(usage.get("output_tokens"), int)
    assert isinstance(usage.get("cost_usd"), (int, float))
    assert 0 <= usage["cost_usd"] <= BUDGET_USD, usage
    assert isinstance(document["permission_denials"], list)
    assert document["source"]["hermes_commit"]
    assert document["source"]["dirty"] in (True, False)


# ---------------------------------------------------------------------------
# W4: live development -> review -> authorized local release merge
# ---------------------------------------------------------------------------

W4_ARTIFACT_DIR = REPO_ROOT / "artifacts" / "w4" / "raw"
W4_LIVE_DIR = W4_ARTIFACT_DIR / "live"
W4_REQUIREMENT = "HKL-W4"
W4_RELEASE_FILES = (
    "release-report.json",
    "merge.json",
    "merge.stdout.log",
    "merge.stderr.log",
    "postmerge/report.json",
    "postmerge/0.stdout.log",
    "postmerge/0.stderr.log",
    "release-state.json",
)


def _review_metadata(
    run_dir: Path,
    document: dict,
    *,
    dev_run_id: int,
    review_run_id: int,
) -> dict:
    workspace = Path(document["workspace"])
    head = document["code_commit"]
    tree = run_git(
        ["rev-parse", f"{head}^{{tree}}"], cwd=workspace, timeout=30
    ).stdout.strip()
    return {
        "schema_version": 1,
        "requirement_id": W4_REQUIREMENT,
        "verdict": "pass",
        "reviewed_head": head,
        "reviewed_tree": tree,
        "dev_run_id": dev_run_id,
        "rework_round": 1,
        "reviewer": "code-reviewer",
        "review_run_id": review_run_id,
        "evidence_index_sha256": sha256_file(run_dir / INDEX_RELATIVE_PATH),
        "reasons": [],
    }


def _write_authorization(
    work_root: str, task_id: str, run_dir: Path, metadata: dict
) -> Path:
    import time

    document = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    landing = json.loads(
        (run_dir / "landing-plan.json").read_text(encoding="utf-8")
    )
    now = int(time.time())
    payload = {
        "schema_version": 1,
        "requirement_id": W4_REQUIREMENT,
        "task_id": task_id,
        "review_run_id": metadata["review_run_id"],
        "dev_run_id": metadata["dev_run_id"],
        "rework_round": metadata["rework_round"],
        "task_base_commit": document["task_base_commit"],
        "reviewed_head": metadata["reviewed_head"],
        "reviewed_tree": metadata["reviewed_tree"],
        "evidence_index_sha256": metadata["evidence_index_sha256"],
        "main_ref": "refs/heads/main",
        "main_commit": landing["main_commit"],
        "approver": "operator",
        "approved_at": now - 10,
        "expires_at": now + 3600,
    }
    target = Path(work_root) / "authorizations" / f"{task_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def test_real_claude_review_release_chain(
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    load_run_json,
):
    from dataclasses import replace

    from hermes_kanban_lanes.config import MergeConfig, ReviewConfig
    from hermes_kanban_lanes.runtime.release import (
        queue_release_cards,
        release_once,
    )
    from hermes_kanban_lanes.workspace.git import repo_head

    binary, credentials = _require_prerequisites()
    w3_before = _snapshot(REPO_ROOT / "artifacts" / "w3" / "raw")
    if W4_LIVE_DIR.exists():
        shutil.rmtree(W4_LIVE_DIR)

    repo = make_repo()
    base = run_git(["rev-parse", "HEAD"], cwd=repo, timeout=30).stdout.strip()
    post_merge_commands = (
        (
            PYTHON,
            "-c",
            (
                "from pathlib import Path;"
                "data=Path('RESULT.txt').read_text();"
                "raise SystemExit(0 if data == 'hkl-live-ok\\n' else 1)"
            ),
        ),
    )
    plain_config = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[binary, "--max-budget-usd", str(BUDGET_USD)],
    )
    config = replace(
        plain_config,
        review=ReviewConfig(
            reviewer="code-reviewer",
            max_rework_rounds=3,
            merge=MergeConfig(
                enabled=True,
                main_ref="refs/heads/main",
                post_merge_commands=post_merge_commands,
                post_merge_timeout_seconds=120.0,
            ),
        ),
    )
    task_id = make_task(
        title="HKL-W4 live chain", body=PROMPT, assignee="hkl:claude"
    )
    card_path = write_card(
        config.work_root,
        task_id,
        allow=("RESULT.txt",),
        commands=[
            {
                "argv": [
                    PYTHON,
                    "-c",
                    (
                        "import sys,pathlib;"
                        "data=pathlib.Path('RESULT.txt').read_text();"
                        "sys.exit(0 if data == 'hkl-live-ok\\n' else 1)"
                    ),
                ],
                "cwd": ".",
            }
        ],
    )
    card_document = json.loads(card_path.read_text(encoding="utf-8"))
    card_document["artifacts"] = ["RESULT.txt"]
    card_path.write_text(json.dumps(card_document), encoding="utf-8")
    card = load_card(card_path, config)

    settings = RunSettings(
        max_runtime_seconds=900.0,
        command_timeout_seconds=600.0,
        heartbeat_seconds=30.0,
        poll_seconds=0.5,
        grace_seconds=5.0,
        kill_grace_seconds=3.0,
    )
    report = daemon.tick(
        config,
        settings=settings,
        tick_index=0,
        package_dir=SRC_DIR,
        requirement_id=W4_REQUIREMENT,
    )
    _write_artifact(
        "live-w4-tick.json",
        {
            "config": {
                "board": str(board.board_path),
                "repository": str(repo),
                "work_root": str(config.work_root),
                "executor_argv": [binary, "--max-budget-usd", str(BUDGET_USD)],
                "merge_enabled": True,
            },
            "report": report,
        },
        credentials,
        artifact_dir=W4_ARTIFACT_DIR,
    )
    assert report["errors"] == [], report["errors"]
    assert len(report["runs"]) == 1, report
    outcome = report["runs"][0]
    assert outcome["task_id"] == task_id
    if outcome["category"] != "success":
        assert outcome["category"] in (
            "environment_failure",
            "code_failure",
            "timeout",
            "cancelled",
        ), outcome
        pytest.fail(
            "live Claude W4 development run did not complete: "
            f"category={outcome['category']} "
            f"terminal={outcome.get('terminal')} "
            f"reason={outcome.get('reason')}",
            pytrace=False,
        )
    assert outcome["terminal"] == "request_review", outcome
    run_dir = Path(outcome["run_dir"])
    run_document = load_run_json(run_dir)
    assert run_document["requirement_id"] == W4_REQUIREMENT
    run_dir_snapshot = _snapshot(run_dir)

    row = board.task_row(task_id) or {}
    assert row["status"] == "review", row
    assert row["assignee"] == "code-reviewer", row
    dev_run_id = int(run_document["identity"]["run_id"])

    review_identity = board.claim_review_task(task_id)
    assert review_identity is not None
    metadata = _review_metadata(
        run_dir,
        run_document,
        dev_run_id=dev_run_id,
        review_run_id=review_identity.run_id,
    )
    assert board.complete_task(
        review_identity,
        result="review pass",
        summary="review pass",
        metadata=metadata,
    )
    assert (board.task_row(task_id) or {}).get("status") == "done"

    authorization_path = _write_authorization(
        config.work_root, task_id, run_dir, metadata
    )
    queued = queue_release_cards(
        config.work_root, board, config, requirement_id=W4_REQUIREMENT
    )
    matches = [entry for entry in queued if entry["dev_task_id"] == task_id]
    assert len(matches) == 1, queued
    merge_task_id = matches[0]["merge_task_id"]
    merge_card = load_card(
        Path(config.work_root) / "cards" / f"{merge_task_id}.json", config
    )
    merge_identity = board.claim(merge_task_id)
    assert merge_identity is not None
    release_outcome = release_once(
        config=config,
        adapter=board,
        card=merge_card,
        identity=merge_identity,
        work_root=config.work_root,
        package_dir=SRC_DIR,
        requirement_id=W4_REQUIREMENT,
    )
    assert release_outcome["action"] == "merged", release_outcome
    assert release_outcome["terminal_ok"] is True, release_outcome
    merge_sha = release_outcome["merge_sha"]
    assert merge_sha
    release_dir = Path(release_outcome["run_dir"])

    release_report = json.loads(
        (release_dir / "release-report.json").read_text(encoding="utf-8")
    )
    assert release_report["verdict"] == "release-ready", release_report
    assert [check["name"] for check in release_report["checks"]] == [
        "workspace",
        "review",
        "evidence",
        "rounds",
        "authorization",
        "merge_enabled",
        "main_unchanged",
        "merge_tree",
    ]
    assert all(check["ok"] for check in release_report["checks"])
    merge_document = json.loads(
        (release_dir / "merge.json").read_text(encoding="utf-8")
    )
    assert merge_document["verdict"] == "merged"
    assert merge_document["parents"] == [base, run_document["code_commit"]]
    assert merge_document["outside_allow"] == 0
    assert merge_document["protected_roots_touched"] == 0
    postmerge = json.loads(
        (release_dir / "postmerge" / "report.json").read_text(encoding="utf-8")
    )
    assert postmerge["verdict"] == "pass", postmerge
    assert [entry["exit_code"] for entry in postmerge["commands"]] == [0]
    assert repo_head(repo) == merge_sha
    parents = run_git(
        ["rev-list", "--parents", "-n", "1", merge_sha], cwd=repo, timeout=30
    ).stdout.split()
    assert parents[0] == merge_sha
    assert set(parents[1:]) == {base, run_document["code_commit"]}
    merge_row = board.task_row(merge_task_id) or {}
    assert merge_row["status"] == "done", merge_row
    merge_runs = board.run_rows(merge_task_id)
    handoff = merge_runs[0]["metadata"]["hkl"]
    assert handoff["merge_sha"] == merge_sha
    assert handoff["reviewed_head"] == run_document["code_commit"]
    assert handoff["evidence_index_sha256"] == metadata["evidence_index_sha256"]
    assert handoff["release_report_sha256"] == sha256_file(
        release_dir / "release-report.json"
    )
    assert handoff["postmerge_report_sha256"] == sha256_file(
        release_dir / "postmerge" / "report.json"
    )
    assert authorization_path.is_file()

    # -- archive: the W4 release record, plus the dev evidence package -------
    archived_dev = W4_LIVE_DIR / "dev-run"
    with _read_only_tree(run_dir):
        archived, redactions = _archive_run_artifacts(
            run_dir, credentials, live_dir=archived_dev
        )
        archived_index, original_index = _recompute_archived_index(
            run_dir, redactions, live_dir=archived_dev
        )
    assert _snapshot(run_dir) == run_dir_snapshot, "archiving modified the run dir"
    assert archived_index["requirement_id"] == W4_REQUIREMENT
    identity = RunIdentity.from_dict(run_document["identity"])
    archived_verification = verify_evidence(
        archived_dev,
        identity,
        SRC_DIR,
        Path(run_document["workspace"]),
        repo,
        requirement_id=W4_REQUIREMENT,
    )
    assert archived_verification.ok, archived_verification.reasons
    _write_artifact(
        "live-w4-redactions.json",
        {
            "reason": (
                "executor stdout/stderr may echo ANTHROPIC_* model identifiers; "
                "the archived dev-run copies are scrubbed and only those index "
                "entries are recomputed"
            ),
            "redactions": redactions,
            "original_index": original_index,
        },
        credentials,
        artifact_dir=W4_ARTIFACT_DIR,
    )

    release_archive = W4_LIVE_DIR / "release-run"
    (release_archive / "postmerge").mkdir(parents=True, exist_ok=True)
    (release_archive / ".gitignore").write_text(
        "!*.log\n!postmerge/*.log\n", encoding="utf-8"
    )
    for relative in W4_RELEASE_FILES:
        source = release_dir / relative
        assert source.is_file(), relative
        _archive_file(
            relative, source, release_archive / relative, credentials
        )
    _write_artifact(
        "live-w4-release.json",
        {
            "task_id": task_id,
            "merge_task_id": merge_task_id,
            "merge_sha": merge_sha,
            "base_commit": base,
            "reviewed_head": run_document["code_commit"],
            "evidence_index_sha256": metadata["evidence_index_sha256"],
            "release_report_sha256": sha256_file(
                release_dir / "release-report.json"
            ),
            "postmerge_report_sha256": sha256_file(
                release_dir / "postmerge" / "report.json"
            ),
            "authorization_sha256": sha256_file(authorization_path),
            "budget_usd": BUDGET_USD,
            "usage": run_document["usage"],
            "release_checks": release_report["checks"],
            "merge": {
                key: merge_document.get(key)
                for key in (
                    "verdict",
                    "parents",
                    "main_before_is_ancestor",
                    "changed_paths",
                    "outside_allow",
                    "protected_roots_touched",
                    "reviewed_paths_changed_by_merge",
                    "command",
                )
            },
            "postmerge": postmerge,
            "archived_index": archived_index,
        },
        credentials,
        artifact_dir=W4_ARTIFACT_DIR,
    )

    # The W3 archive is read-only for this lane.
    assert _snapshot(REPO_ROOT / "artifacts" / "w3" / "raw") == w3_before
    # No credential value reached any W4 artifact.
    for path in sorted(W4_ARTIFACT_DIR.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            for value in credentials.values():
                assert not value or value.encode("utf-8") not in data, path
    assert 0 <= run_document["usage"]["cost_usd"] <= BUDGET_USD
