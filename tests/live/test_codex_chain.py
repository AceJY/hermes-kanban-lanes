"""W5-11 live smoke: one real Codex chain on temporary board/repo/work-root.

Opt-in only.  ``HKL_LIVE_CODEX=1`` is required; without it the module is
skipped, so the default suite never spawns Codex, spends tokens or touches the
network.  When the switch *is* set but the binary or authentication is missing,
the test fails explicitly instead of skipping as if it had passed.

The dev card is executed by the real Codex CLI through ``daemon.tick``.  The
review metadata used to drive the release gate and the ``--no-ff`` merge is
**synthetic test input**: it is not an independent review conclusion.  The
release gate, merge and post-merge regression are the unchanged product code.

Archives live under ``artifacts/w5/raw/live/`` plus
``artifacts/w5/raw/live-w5-release.json``.  Only binary paths, versions,
``thread_id`` and token counts are recorded -- never a token, ``auth.json``
content or a dollar cost (the CLI does not report one).  ``artifacts/w2``,
``artifacts/w3`` and ``artifacts/w4`` are snapshotted before and asserted
byte-identical afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.config import Executor, MergeConfig, ReviewConfig
from hermes_kanban_lanes.executors.codex import CodexAdapter
from hermes_kanban_lanes.runtime import daemon
from hermes_kanban_lanes.runtime.release import queue_release_cards, release_once
from hermes_kanban_lanes.runtime.supervisor import RunSettings
from hermes_kanban_lanes.verification.evidence import (
    INDEX_RELATIVE_PATH,
    sha256_file,
    verify_evidence,
)
from hermes_kanban_lanes.workspace.git import repo_head, run_git

pytestmark = pytest.mark.skipif(
    os.environ.get("HKL_LIVE_CODEX") != "1",
    reason="live Codex smoke is opt-in (set HKL_LIVE_CODEX=1)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src" / "hermes_kanban_lanes"
ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "w5" / "raw"
LIVE_DIR = ARTIFACT_ROOT / "live"
LIVE_RECORD = ARTIFACT_ROOT / "live-w5-release.json"
READ_ONLY_ARCHIVES = (
    REPO_ROOT / "artifacts" / "w2",
    REPO_ROOT / "artifacts" / "w3",
    REPO_ROOT / "artifacts" / "w4",
)
REQUIREMENT = "HKL-W5"
REVIEWER = "code-reviewer"
PROMPT = (
    "Create a file named RESULT.txt in the current working directory containing "
    "exactly the text hkl-live-codex-ok followed by a newline. Do not create or "
    "modify any other file. Do not run git commands."
)
EXPECTED_TEXT = "hkl-live-codex-ok\n"

DEVELOPMENT_FILES = (
    "run.json",
    "state.json",
    "identity.json",
    "evidence/gate-report.json",
    "evidence/manifest.json",
    "evidence/index.json",
    "landing-plan.json",
    "codex-settings.json",
    "codex-last-message.txt",
    "stdout.log",
    "stderr.log",
)
RELEASE_FILES = (
    "release-report.json",
    "merge.json",
    "merge.stdout.log",
    "merge.stderr.log",
    "postmerge/report.json",
    "postmerge/0.stdout.log",
    "postmerge/0.stderr.log",
    "release-state.json",
)


def _require_prerequisites() -> tuple[str, dict[str, str]]:
    binary = os.environ.get("HKL_CODEX_BIN") or shutil.which("codex")
    if not binary or not os.access(binary, os.X_OK):
        pytest.fail(
            "HKL_LIVE_CODEX=1 but no executable Codex binary was found "
            "(set HKL_CODEX_BIN)",
            pytrace=False,
        )
    credentials, reason = CodexAdapter().credential_env()
    if not credentials:
        pytest.fail(
            f"HKL_LIVE_CODEX=1 but Codex credentials are unavailable: {reason}",
            pytrace=False,
        )
    return binary, credentials


def _secret_values(credentials: dict[str, str]) -> dict[str, str]:
    # CODEX_HOME is a path, explicitly allowed in the record; only real
    # secrets must never reach an artifact.
    return {
        key: value
        for key, value in credentials.items()
        if key not in ("CODEX_HOME",) and value
    }


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    snapshot: dict[str, tuple[int, str]] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[path.relative_to(root).as_posix()] = (
                path.stat().st_size,
                sha256_file(path),
            )
    return snapshot


def _assert_clean(text: str, credentials: dict[str, str]) -> None:
    for key, value in _secret_values(credentials).items():
        assert value not in text, f"credential value for {key} would leak"


def _write_json(path: Path, document, credentials: dict[str, str]) -> None:
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    _assert_clean(text, credentials)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _copy(source: Path, destination: Path, credentials: dict[str, str]) -> None:
    if not source.is_file():
        return
    data = source.read_bytes()
    for key, value in _secret_values(credentials).items():
        assert value.encode("utf-8") not in data, f"{key} would leak into {source}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def _review_metadata(
    run_dir: Path, document: dict, *, dev_run_id: int, review_run_id: int
) -> dict:
    head = document["code_commit"]
    tree = run_git(
        ["rev-parse", f"{head}^{{tree}}"],
        cwd=Path(document["workspace"]),
        timeout=30,
    ).stdout.strip()
    return {
        "schema_version": 1,
        "requirement_id": REQUIREMENT,
        "verdict": "pass",
        "reviewed_head": head,
        "reviewed_tree": tree,
        "dev_run_id": dev_run_id,
        "rework_round": 1,
        "reviewer": REVIEWER,
        "review_run_id": review_run_id,
        "evidence_index_sha256": sha256_file(run_dir / INDEX_RELATIVE_PATH),
        "reasons": [],
        "synthetic": True,
    }


def _write_authorization(work_root, task_id: str, run_dir: Path, metadata: dict) -> Path:
    document = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    landing = json.loads((run_dir / "landing-plan.json").read_text(encoding="utf-8"))
    now = int(time.time())
    payload = {
        "schema_version": 1,
        "requirement_id": REQUIREMENT,
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
        "note": "synthetic test input; not an independent review conclusion",
    }
    target = Path(work_root) / "authorizations" / f"{task_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def _codex_config(make_config, *, board_path, repo_path, binary: str):
    base = make_config(
        board_path=board_path, repo_path=repo_path, executor_argv=[binary]
    )
    executors = dict(base.executors)
    executors["codex"] = Executor(argv=(binary,))
    review = ReviewConfig(
        reviewer=REVIEWER,
        max_rework_rounds=3,
        merge=MergeConfig(
            enabled=True,
            main_ref="refs/heads/main",
            post_merge_commands=[
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "raise SystemExit(0 if "
                    "Path('RESULT.txt').read_text() == 'hkl-live-codex-ok\\n' "
                    "else 1)",
                )
            ],
            post_merge_timeout_seconds=120.0,
        ),
    )
    return replace(base, executors=executors, review=review)


def test_real_codex_chain(
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    load_run_json,
):
    binary, credentials = _require_prerequisites()
    archives_before = {path: _snapshot(path) for path in READ_ONLY_ARCHIVES}
    if LIVE_DIR.exists():
        shutil.rmtree(LIVE_DIR)

    repo = make_repo()
    base = repo_head(repo)
    config = _codex_config(
        make_config, board_path=board.board_path, repo_path=repo, binary=binary
    )
    task_id = make_task(title="HKL-W5 live Codex chain", body=PROMPT, assignee="hkl:codex")
    card_path = write_card(
        config.work_root,
        task_id,
        executor="codex",
        allow=("RESULT.txt",),
        commands=[
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "raise SystemExit(0 if "
                    "Path('RESULT.txt').read_text() == 'hkl-live-codex-ok\\n' "
                    "else 1)",
                ],
                "cwd": ".",
            }
        ],
    )
    document = json.loads(card_path.read_text(encoding="utf-8"))
    document["artifacts"] = ["RESULT.txt"]
    card_path.write_text(json.dumps(document), encoding="utf-8")
    card = load_card(card_path, config)
    assert card.executor == "codex"

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
        requirement_id=REQUIREMENT,
    )
    _write_json(
        ARTIFACT_ROOT / "live-w5-tick.json",
        {
            "config": {
                "board": str(board.board_path),
                "repository": str(repo),
                "work_root": str(config.work_root),
                "executor_binary": binary,
                "codex_home": credentials.get("CODEX_HOME"),
                "credential_keys": sorted(credentials),
            },
            "report": report,
        },
        credentials,
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
        assert (board.task_row(task_id) or {}).get("status") == "blocked"
        pytest.fail(
            "live Codex development run did not complete: "
            f"category={outcome['category']} "
            f"terminal={outcome.get('terminal')} "
            f"reason={outcome.get('reason')}",
            pytrace=False,
        )

    assert outcome["terminal"] == "request_review", outcome
    assert outcome["gate_verdict"] == "pass"
    assert outcome["evidence"]["verified"] is True, outcome["evidence"]
    run_dir = Path(outcome["run_dir"])
    run_document = load_run_json(run_dir)
    dev_run_id = int(run_document["identity"]["run_id"])
    executor = run_document["executor"]

    assert executor["name"] == "codex"
    assert executor["version"], executor
    assert executor["executor_session_id"], executor
    assert executor["credential_reason"] is None
    assert set(executor["credential_keys"]) <= {"CODEX_HOME", "OPENAI_API_KEY"}
    usage = run_document["usage"]
    assert isinstance(usage["input_tokens"], int)
    assert isinstance(usage["output_tokens"], int)
    assert usage["cost_usd"] is None
    assert (run_dir / "codex-settings.json").is_file()
    assert not (run_dir / "claude-settings.json").exists()
    workspace = Path(outcome["workspace"])
    assert (workspace / "RESULT.txt").read_text(encoding="utf-8") == EXPECTED_TEXT
    row = board.task_row(task_id) or {}
    assert row["status"] == "review", row
    assert row["assignee"] == REVIEWER

    # Evidence is independently verifiable with the product verifier.
    identity = run_document["identity"]
    from hermes_kanban_lanes.contracts import RunIdentity

    verification = verify_evidence(
        run_dir,
        RunIdentity.from_dict(identity),
        SRC_DIR,
        workspace,
        repo,
        requirement_id=REQUIREMENT,
    )
    assert verification.ok, verification.reasons
    assert verification.checks

    # -- synthetic review input (NOT an independent review conclusion) -----
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
        result="synthetic review pass (test input)",
        summary="synthetic review pass; not an independent review",
        metadata=metadata,
    )
    authorization_path = _write_authorization(
        config.work_root, task_id, run_dir, metadata
    )

    queued = queue_release_cards(
        config.work_root, board, config, requirement_id=REQUIREMENT
    )
    matches = [entry for entry in queued if entry["dev_task_id"] == task_id]
    assert len(matches) == 1, queued
    merge_task_id = matches[0]["merge_task_id"]
    merge_card = load_card(
        Path(config.work_root) / "cards" / f"{merge_task_id}.json", config
    )
    merge_identity = board.claim(merge_task_id)
    assert merge_identity is not None
    release = release_once(
        config=config,
        adapter=board,
        card=merge_card,
        identity=merge_identity,
        work_root=config.work_root,
        package_dir=SRC_DIR,
        requirement_id=REQUIREMENT,
    )
    assert release["action"] == "merged", release
    assert release["terminal_ok"] is True
    merge_sha = release["merge_sha"]
    release_dir = Path(release["run_dir"])
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
    postmerge = json.loads(
        (release_dir / "postmerge" / "report.json").read_text(encoding="utf-8")
    )
    assert postmerge["verdict"] == "pass", postmerge
    assert postmerge["merge_sha"] == merge_sha
    assert postmerge["worktree"] == "postmerge/worktree"
    assert [entry["exit_code"] for entry in postmerge["commands"]] == [0]
    assert repo_head(repo) == merge_sha
    parents = run_git(
        ["rev-list", "--parents", "-n", "1", merge_sha], cwd=repo, timeout=30
    ).stdout.split()
    assert parents[0] == merge_sha
    assert set(parents[1:]) == {base, run_document["code_commit"]}
    merge_row = board.task_row(merge_task_id) or {}
    assert merge_row["status"] == "done", merge_row
    handoff = board.run_rows(merge_task_id)[0]["metadata"]["hkl"]
    assert handoff["merge_sha"] == merge_sha
    assert handoff["reviewed_head"] == run_document["code_commit"]

    # -- archive: verbatim dev evidence + the release record -----------------
    (LIVE_DIR / "dev-run").mkdir(parents=True, exist_ok=True)
    (LIVE_DIR / ".gitignore").write_text(
        "!*.log\n!dev-run/*.log\n!dev-run/commands/*.log\n!release-run/*.log\n"
        "!release-run/postmerge/*.log\n",
        encoding="utf-8",
    )
    for relative in DEVELOPMENT_FILES:
        _copy(run_dir / relative, LIVE_DIR / "dev-run" / relative, credentials)
    commands_dir = run_dir / "commands"
    if commands_dir.is_dir():
        for path in sorted(commands_dir.iterdir()):
            if path.is_file() and path.name.endswith(".log"):
                _copy(
                    path,
                    LIVE_DIR / "dev-run" / "commands" / path.name,
                    credentials,
                )
    for relative in RELEASE_FILES:
        _copy(release_dir / relative, LIVE_DIR / "release-run" / relative, credentials)
    _copy(authorization_path, LIVE_DIR / "authorization.json", credentials)

    # The archived package is verbatim evidence and must stay independently
    # verifiable; nothing was redacted because no secret value appeared.
    archived_dev_run = LIVE_DIR / "dev-run"
    assert sha256_file(archived_dev_run / INDEX_RELATIVE_PATH) == (
        metadata["evidence_index_sha256"]
    )
    archived_verification = verify_evidence(
        archived_dev_run,
        RunIdentity.from_dict(identity),
        SRC_DIR,
        workspace,
        repo,
        requirement_id=REQUIREMENT,
    )
    assert archived_verification.ok, archived_verification.reasons
    assert all(archived_verification.checks.values()), archived_verification.checks

    _write_json(
        LIVE_RECORD,
        {
            "requirement_id": REQUIREMENT,
            "note": (
                "The review metadata is synthetic test input, not an independent "
                "review conclusion. No dollar cost is recorded because the Codex "
                "CLI does not report one; token counts are not a bill."
            ),
            "task_id": task_id,
            "merge_task_id": merge_task_id,
            "executor": {
                "name": executor["name"],
                "binary": binary,
                "version": executor["version"],
                "thread_id": executor["executor_session_id"],
                "credential_keys": executor["credential_keys"],
                "codex_home": json.loads(
                    (run_dir / "codex-settings.json").read_text(encoding="utf-8")
                ).get("codex_home"),
            },
            "usage": usage,
            "gate_verdict": run_document["gate"]["verdict"],
            "evidence": run_document["evidence"],
            "base_commit": base,
            "reviewed_head": run_document["code_commit"],
            "merge_sha": merge_sha,
            "merge_parents": merge_document["parents"],
            "release_checks": release_report["checks"],
            "postmerge": postmerge,
            "evidence_index_sha256": metadata["evidence_index_sha256"],
            "release_report_sha256": sha256_file(release_dir / "release-report.json"),
            "postmerge_report_sha256": sha256_file(
                release_dir / "postmerge" / "report.json"
            ),
            "authorization_sha256": sha256_file(authorization_path),
        },
        credentials,
    )

    # ``artifacts/w2|w3|w4`` are read-only for this lane.
    for path in READ_ONLY_ARCHIVES:
        assert _snapshot(path) == archives_before[path], path
    for path in sorted(ARTIFACT_ROOT.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            for key, value in _secret_values(credentials).items():
                assert value.encode("utf-8") not in data, f"{key} leaked into {path}"
