"""W4-11/W4-12: crash recovery, stale identities and parent reopen.

The release run is executed in a child interpreter that calls ``os._exit`` at a
chosen stage, leaving the same durable state a real crash leaves: a release
journal in ``merged`` or ``postmerge`` stage and a still-running board claim.
The next daemon tick must resume without merging twice.  A stale identity may
never write to the board and a reopened parent demotes its merge card, which
must not be released.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.release.merge import merge_release
from hermes_kanban_lanes.release.state import (
    new_release_state,
    read_release_state,
    write_release_state,
)
from hermes_kanban_lanes.runtime.daemon import tick
from hermes_kanban_lanes.runtime.release import (
    queue_release_cards,
    recover_releases,
    release_once,
)
from hermes_kanban_lanes.runtime.supervisor import RunSettings
from hermes_kanban_lanes.verification.evidence import verify_evidence
from hermes_kanban_lanes.workspace.git import is_ancestor, repo_head, run_git

from test_w4_release_failures import _chain
from w4_helpers import (
    PYTHON,
    claim_and_release,
    dev_run_dir,
    queue_merge_card,
    release_run_dir,
    review_body,
    review_changes,
    review_pass,
    run_dev,
    task_row,
    w4_config,
    write_authorization,
)

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
PACKAGE_DIR = SRC_DIR / "hermes_kanban_lanes"
REQUIREMENT = "HKL-W4"

SETTINGS = RunSettings(
    max_runtime_seconds=60,
    heartbeat_seconds=30,
    poll_seconds=0.05,
    grace_seconds=1,
    kill_grace_seconds=1,
)

CRASH_SCRIPT = """
import json, os, sys
from pathlib import Path

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.config import load_config
from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter
from hermes_kanban_lanes.runtime import release as release_module

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
config = load_config(job["config"])
adapter = BoardAdapter(config.boards["test"], config.hermes_source, name="test").open()
identity = adapter.claim(job["merge_task_id"])
if identity is None:
    sys.exit(3)
card = load_card(Path(job["card_path"]), config)


def _die(*args, **kwargs):
    os._exit(9)


if job["crash"] == "merged":
    release_module.run_post_merge = _die
elif job["crash"] == "postmerge":
    adapter.complete_task = _die
elif job["crash"] == "notified":
    _original_notify = release_module.run_merge_notify

    def _notify_then_die(*args, **kwargs):
        _original_notify(*args, **kwargs)
        os._exit(9)

    release_module.run_merge_notify = _notify_then_die
else:
    sys.exit(4)

release_module.release_once(
    config=config,
    adapter=adapter,
    card=card,
    identity=identity,
    work_root=config.work_root,
    package_dir=job["package_dir"],
    requirement_id=job["requirement_id"],
)
os._exit(10)
"""


NOTIFY_FACT_KEYS = (
    "HKL_REQUIREMENT_ID",
    "HKL_BOARD",
    "HKL_MERGE_TASK_ID",
    "HKL_DEV_TASK_ID",
    "HKL_MERGE_SHA",
    "HKL_MERGE_TREE",
    "HKL_REVIEWED_HEAD",
    "HKL_REVIEWED_TREE",
    "HKL_MAIN_BEFORE",
    "HKL_EVIDENCE_INDEX_SHA256",
    "HKL_AUTHORIZATION_SOURCE",
    "HKL_REPO",
    "HKL_RUN_DIR",
)

NOTIFY_ENV_SCRIPT = (
    "import json, os, sys\n"
    "from pathlib import Path\n"
    f"keys = {list(NOTIFY_FACT_KEYS)!r}\n"
    "counter = Path(sys.argv[2])\n"
    "counter.write_text((counter.read_text() if counter.exists() else '') + 'x')\n"
    "Path(sys.argv[1]).write_text(json.dumps({k: os.environ.get(k) for k in keys}))\n"
)

NOTIFY_APPEND_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "p = Path(sys.argv[1])\n"
    "p.write_text((p.read_text() if p.exists() else '') + 'x')\n"
)


def _with_notify(
    facts: dict,
    commands: list[list[str]],
    *,
    timeout_seconds: float = 10.0,
) -> dict:
    """Replace the release config's merge block with a notify-enabled one."""

    config = facts["config"]
    review = config.review
    merge = replace(
        review.merge,
        notify_commands=tuple(tuple(argv) for argv in commands),
        notify_timeout_seconds=timeout_seconds,
    )
    facts["config"] = replace(config, review=replace(review, merge=merge))
    return facts


def _run_until_crash(facts: dict, crash: str) -> None:
    """Run one release in a child that ``os._exit``s at ``crash``."""

    work_root = Path(facts["config"].work_root)
    job_dir = work_root / "crash-job"
    job_dir.mkdir(parents=True, exist_ok=True)
    config_path = job_dir / "config.json"
    config_path.write_text(
        json.dumps(facts["config"].to_dict()), encoding="utf-8"
    )
    script_path = job_dir / "crash.py"
    script_path.write_text(CRASH_SCRIPT, encoding="utf-8")
    job_path = job_dir / "job.json"
    job_path.write_text(
        json.dumps(
            {
                "config": str(config_path),
                "merge_task_id": facts["merge_task_id"],
                "card_path": str(
                    work_root / "cards" / f"{facts['merge_task_id']}.json"
                ),
                "package_dir": str(PACKAGE_DIR),
                "requirement_id": REQUIREMENT,
                "crash": crash,
            }
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [PYTHON, str(script_path), str(job_path)],
        cwd=str(job_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 9, (result.returncode, result.stdout, result.stderr)


# --- two-process D9 fixtures ------------------------------------------------
#
# These children are real OS processes.  The release process holds the board's
# write transaction (D9-I) and the takeover process mutates claim state through
# its own connection; exclusivity comes from the SQLite write lock both need.

TAKEOVER_SCRIPT = """
import json, sys, time
from pathlib import Path

from hermes_kanban_lanes.config import load_config
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
config = load_config(job["config"])
adapter = BoardAdapter(
    config.boards["test"],
    config.hermes_source,
    name="test",
).open()
identity = RunIdentity.from_dict(job["identity"])
# A fresh host connection needs the write lock too (its open path probes the
# schema).  Connect before the release enters the fence, then hold at the
# barrier so the test observes contention inside ``reclaim`` itself.
if job.get("ready"):
    Path(job["ready"]).write_text("1", encoding="utf-8")
if job.get("go"):
    while not Path(job["go"]).exists():
        time.sleep(0.005)
Path(job["attempt"]).write_text("1", encoding="utf-8")
started = time.time()
reclaimed = adapter.reclaim(identity, "test takeover")
result = {
    "reclaimed": reclaimed,
    "started_at": started,
    "finished_at": time.time(),
}
if reclaimed and job.get("reclaim_new"):
    new = adapter.claim(identity.task_id)
    result["new_identity"] = new.to_dict() if new is not None else None
Path(job["done"]).write_text(json.dumps(result), encoding="utf-8")
adapter.close()
"""

RELEASE_WAIT_SCRIPT = """
import json, sys, time
from pathlib import Path

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.config import load_config
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter
from hermes_kanban_lanes.runtime.release import release_once

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
config = load_config(job["config"])
adapter = BoardAdapter(
    config.boards["test"],
    config.hermes_source,
    name="test",
).open()
identity = RunIdentity.from_dict(job["identity"])
card = load_card(Path(job["card_path"]), config)
while not Path(job["go"]).exists():
    time.sleep(0.01)
outcome = release_once(
    config=config,
    adapter=adapter,
    card=card,
    identity=identity,
    work_root=config.work_root,
    package_dir=job["package_dir"],
    requirement_id=job["requirement_id"],
)
Path(job["done"]).write_text(json.dumps(outcome), encoding="utf-8")
adapter.close()
"""

RELEASE_MERGE_HOLD_SCRIPT = """
import json, sys, time
from pathlib import Path

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.config import load_config
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter
from hermes_kanban_lanes.runtime import release as release_module

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
config = load_config(job["config"])
adapter = BoardAdapter(
    config.boards["test"],
    config.hermes_source,
    name="test",
).open()
identity = RunIdentity.from_dict(job["identity"])
card = load_card(Path(job["card_path"]), config)
original_merge = release_module.merge_release


def held_merge(**kwargs):
    # ``release_once`` holds the exclusive lock while calling this; writing the
    # signal here proves the merge side is inside the critical section.
    Path(job["merge_entered"]).write_text("1", encoding="utf-8")
    while not Path(job["allow_merge"]).exists():
        time.sleep(0.01)
    record = original_merge(**kwargs)
    Path(job["merge_done"]).write_text(
        json.dumps({"ok": record.ok, "merge_sha": record.merge_sha, "at": time.time()}),
        encoding="utf-8",
    )
    return record


release_module.merge_release = held_merge
outcome = release_module.release_once(
    config=config,
    adapter=adapter,
    card=card,
    identity=identity,
    work_root=config.work_root,
    package_dir=job["package_dir"],
    requirement_id=job["requirement_id"],
)
Path(job["done"]).write_text(json.dumps(outcome), encoding="utf-8")
adapter.close()
"""


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _write_child_job(
    work_root: Path, name: str, script: str, payload: dict
) -> tuple[Path, Path]:
    job_dir = work_root / name
    job_dir.mkdir(parents=True, exist_ok=True)
    config_path = job_dir / "config.json"
    config_path.write_text(json.dumps(payload["config"].to_dict()), encoding="utf-8")
    script_path = job_dir / "child.py"
    script_path.write_text(script, encoding="utf-8")
    document = {key: value for key, value in payload.items() if key != "config"}
    document["config"] = str(config_path)
    job_path = job_dir / "job.json"
    job_path.write_text(json.dumps(document), encoding="utf-8")
    return script_path, job_path


def _spawn_child(script_path: Path, job_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [PYTHON, str(script_path), str(job_path)],
        cwd=str(job_path.parent),
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(path: Path, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _finish(process: subprocess.Popen, *, name: str) -> str:
    stdout, stderr = process.communicate(timeout=180)
    assert process.returncode == 0, (name, process.returncode, stdout, stderr)
    return stdout


def _merge_count(repo: Path) -> int:
    result = run_git(
        ["rev-list", "--merges", "--count", "refs/heads/main"],
        cwd=repo,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def _assert_recovered_once(facts: dict, *, stage: str) -> None:
    config = facts["config"]
    work_root = Path(config.work_root)
    merge_task_id = facts["merge_task_id"]
    release_dir = release_run_dir(work_root, merge_task_id)
    state = read_release_state(release_dir)
    assert state is not None
    assert state.stage == stage, state.stage

    first = tick(
        config,
        adapters={"test": facts["board"]},
        settings=SETTINGS,
        package_dir=str(PACKAGE_DIR),
        requirement_id=REQUIREMENT,
    )
    assert first["errors"] == [], first["errors"]
    actions = [
        entry["action"]
        for entry in first["release_recovered"]
        if entry["merge_task_id"] == merge_task_id
    ]
    assert actions == ["completed"], first["release_recovered"]

    recovered_state = read_release_state(release_dir)
    assert recovered_state is not None
    assert recovered_state.stage == "terminal"
    assert recovered_state.merge_sha
    assert repo_head(facts["repo"]) == recovered_state.merge_sha
    assert task_row(facts["board"], merge_task_id)["status"] == "done"
    postmerge = json.loads(
        (release_dir / "postmerge" / "report.json").read_text(encoding="utf-8")
    )
    assert postmerge["verdict"] == "pass", postmerge

    children = facts["board"].child_ids(facts["task_id"])
    assert children == [merge_task_id]
    release_dirs = [
        path
        for path in (work_root / "runs" / merge_task_id).iterdir()
        if path.is_dir()
    ]
    assert len(release_dirs) == 1
    assert _merge_count(facts["repo"]) == 1

    # A second tick must not merge again or add another run/card.
    second = tick(
        config,
        adapters={"test": facts["board"]},
        settings=SETTINGS,
        package_dir=str(PACKAGE_DIR),
        requirement_id=REQUIREMENT,
    )
    assert second["errors"] == [], second["errors"]
    assert second["release_recovered"] == []
    assert _merge_count(facts["repo"]) == 1
    assert repo_head(facts["repo"]) == recovered_state.merge_sha
    assert facts["board"].child_ids(facts["task_id"]) == [merge_task_id]
    assert len(
        [path for path in (work_root / "runs" / merge_task_id).iterdir() if path.is_dir()]
    ) == 1


def test_crash_after_merged_is_resumed_without_a_second_merge(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    _run_until_crash(facts, "merged")
    _assert_recovered_once(facts, stage="merged")


def test_crash_after_postmerge_is_resumed_without_a_second_merge(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    _run_until_crash(facts, "postmerge")
    _assert_recovered_once(facts, stage="postmerge")


def test_successful_release_report_carries_the_real_merge_facts(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """Shape B: one verdict, backed by the actual git state.

    ``release-report.json`` must name the same commit that ``merge.json``
    recorded and that ``main`` actually points at, and it must carry the real
    post-merge report instead of ``null``.
    """

    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    _identity, outcome = claim_and_release(
        board, facts["config"], facts["merge_task_id"], facts["merge_card"]
    )
    assert outcome["action"] == "merged", outcome

    release_dir = release_run_dir(
        Path(facts["config"].work_root), facts["merge_task_id"]
    )
    report = json.loads(
        (release_dir / "release-report.json").read_text(encoding="utf-8")
    )
    merge_document = json.loads(
        (release_dir / "merge.json").read_text(encoding="utf-8")
    )
    state = read_release_state(release_dir)
    head = repo_head(facts["repo"])

    assert report["merged"] is True, report
    assert report["release_verdict"] == "merged", report
    assert report["reasons"] == [], report
    assert report["merge_sha"] == merge_document["merge_sha"] == head, report
    assert report["merge_tree"] == merge_document["merge_tree"], report
    assert report["merge"]["verdict"] == "merged", report["merge"]
    assert state is not None and state.stage == "terminal", state
    assert state.merge_sha == head
    assert report["postmerge"] is not None, report
    assert report["postmerge"]["verdict"] == "pass", report["postmerge"]
    assert report["postmerge"]["merge_sha"] == head
    assert task_row(board, facts["merge_task_id"])["status"] == "done"
    assert _merge_count(facts["repo"]) == 1


def test_postmerge_failure_after_a_crash_is_merged_not_refused(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """Shape C: a real merge plus a failing follow-up is never ``refused``.

    The crash leaves the run in ``merged``.  Recovery must reuse the recorded
    ``merge_sha`` (no second merge), run the failing post-merge command, and
    write one honest verdict: merged, with the post-merge failure named.
    """

    facts = _chain(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        post_merge_commands=[[PYTHON, "-c", "import sys; sys.exit(7)"]],
    )
    _run_until_crash(facts, "merged")
    release_dir = release_run_dir(
        Path(facts["config"].work_root), facts["merge_task_id"]
    )
    crashed_state = read_release_state(release_dir)
    assert crashed_state is not None and crashed_state.stage == "merged"
    merge_sha = crashed_state.merge_sha
    assert merge_sha and repo_head(facts["repo"]) == merge_sha
    assert _merge_count(facts["repo"]) == 1

    config = facts["config"]
    first = tick(
        config,
        adapters={"test": facts["board"]},
        settings=SETTINGS,
        package_dir=str(PACKAGE_DIR),
        requirement_id=REQUIREMENT,
    )
    assert first["errors"] == [], first["errors"]
    entry = next(
        item
        for item in first["release_recovered"]
        if item["merge_task_id"] == facts["merge_task_id"]
    )
    assert entry["action"] == "blocked:postmerge-failed:0", entry

    report = json.loads(
        (release_dir / "release-report.json").read_text(encoding="utf-8")
    )
    merge_document = json.loads(
        (release_dir / "merge.json").read_text(encoding="utf-8")
    )
    recovered_state = read_release_state(release_dir)
    assert recovered_state is not None and recovered_state.stage == "terminal"
    assert recovered_state.merge_sha == merge_sha
    assert report["merged"] is True, report
    assert report["release_verdict"] == "merged-with-failures", report
    assert report["verdict"] != "refused", report
    assert report["merge_sha"] == merge_document["merge_sha"] == merge_sha
    assert report["merge_sha"] == repo_head(facts["repo"])
    assert report["postmerge"] is not None, report
    assert report["postmerge"]["verdict"] == "fail", report["postmerge"]
    assert "postmerge-failed:0" in report["reasons"], report
    row = task_row(board, facts["merge_task_id"])
    assert row["status"] == "blocked", row
    assert row["block_kind"] == "needs_input", row
    assert _merge_count(facts["repo"]) == 1

    # Recovery is idempotent: the second tick neither merges nor re-runs the
    # post-merge command (the card is terminal).
    second = tick(
        config,
        adapters={"test": facts["board"]},
        settings=SETTINGS,
        package_dir=str(PACKAGE_DIR),
        requirement_id=REQUIREMENT,
    )
    assert second["errors"] == [], second["errors"]
    assert second["release_recovered"] == []
    assert _merge_count(facts["repo"]) == 1
    assert repo_head(facts["repo"]) == merge_sha


def test_protected_root_is_refused_before_main_moves_and_the_card_records_the_reading(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """Shape A through ``release_once``: refusal precedes any git side effect.

    The reviewed branch changed ``tools/y.sh`` while the card's allow list only
    names ``docs/x.md``.  The protected root must be decided before
    ``git merge``: ``main`` and every ref stay byte-identical, no ``MERGE_HEAD``
    appears, and the card carries a re-checkable main-unchanged reading.
    """

    repo = make_repo()
    base = repo_head(repo)
    executor = fake_executor(
        "Path('RESULT.txt').write_text('ok\\n', encoding='utf-8')\n"
        "Path('tools').mkdir(exist_ok=True)\n"
        "Path('tools/y.sh').write_text('#!/bin/sh\\nexit 0\\n', encoding='utf-8')\n"
        'print(json.dumps({"is_error": False, "result": "done"}))\n'
    )
    config = w4_config(
        make_config,
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[executor],
        merge_enabled=True,
        post_merge_commands=[[PYTHON, "-c", "pass"]],
    )
    task_id = make_task(title="w4 protected root refusal")
    card = load_card(
        write_card(
            config.work_root, task_id, allow=("RESULT.txt", "tools/y.sh")
        ),
        config,
    )
    identity, outcome = run_dev(
        board, config, card, task_id, executor_argv=executor
    )
    assert outcome.category == "success", outcome.reason
    assert outcome.terminal == "request_review", outcome.terminal
    run_dir = dev_run_dir(config.work_root, task_id, identity.run_id)
    review_identity, _ = review_pass(
        board, task_id, run_dir, dev_run_id=identity.run_id, rework_round=1
    )
    write_authorization(
        Path(config.work_root),
        task_id,
        run_dir,
        dev_run_id=identity.run_id,
        review_run_id=review_identity.run_id,
        rework_round=1,
    )
    merge_task_id, merge_card = queue_merge_card(board, config, task_id)

    # The card is narrowed after the branch was built, exactly like a stale
    # allow list.  The release must trust the card that exists when it runs.
    card_path = Path(config.work_root) / "cards" / f"{task_id}.json"
    document = json.loads(card_path.read_text(encoding="utf-8"))
    document["allow"] = ["docs/x.md"]
    card_path.write_text(json.dumps(document), encoding="utf-8")

    main_before = repo_head(repo)
    status_before = run_git(
        ["status", "--porcelain"], cwd=repo, timeout=30
    ).stdout
    refs_before = run_git(
        ["for-each-ref", "--format=%(refname) %(objectname)"],
        cwd=repo,
        timeout=30,
    ).stdout
    assert status_before == ""

    _identity, outcome = claim_and_release(board, config, merge_task_id, merge_card)
    assert outcome["action"] == "block", outcome
    assert outcome["reason"] == "protected-root-touched:1", outcome
    assert outcome["terminal_ok"] is True
    assert outcome["merge_sha"] is None, outcome

    # Zero git side effects: main, refs and the worktree are unchanged.
    assert repo_head(repo) == main_before
    assert run_git(["status", "--porcelain"], cwd=repo, timeout=30).stdout == ""
    assert (
        run_git(
            ["for-each-ref", "--format=%(refname) %(objectname)"],
            cwd=repo,
            timeout=30,
        ).stdout
        == refs_before
    )
    assert (
        run_git(
            ["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=repo, timeout=30
        ).returncode
        != 0
    )
    assert _merge_count(repo) == 0

    # The record says exactly what happened: refused, no merge commit, and the
    # protected path is named first.
    release_dir = release_run_dir(Path(config.work_root), merge_task_id)
    report = json.loads(
        (release_dir / "release-report.json").read_text(encoding="utf-8")
    )
    merge_document = json.loads(
        (release_dir / "merge.json").read_text(encoding="utf-8")
    )
    assert report["release_verdict"] == "refused", report
    assert report["merged"] is False, report
    assert report["merge_sha"] is None, report
    assert report["reasons"][0] == "protected-root-touched:1", report
    assert merge_document["verdict"] == "refused", merge_document
    assert merge_document["merged"] is False, merge_document
    assert merge_document["merge_sha"] is None, merge_document
    assert merge_document["protected_root_paths"] == ["tools/y.sh"]
    assert merge_document["protected_roots_touched"] == 1

    rows = board.connection.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'release-main-reading' ORDER BY id DESC LIMIT 1",
        (merge_task_id,),
    ).fetchall()
    assert len(rows) == 1, rows
    event = json.loads(rows[0]["payload"])
    assert event["reason"] == f"main-unchanged:{main_before[:12]}", event
    reading = json.loads(event["detail"])
    assert reading["unchanged"] is True, reading
    assert reading["merged"] is False, reading
    assert reading["merge_sha"] is None, reading
    assert reading["main_before"] == reading["main_after"] == main_before, reading
    assert reading["reasons"][0] == "protected-root-touched:1", reading
    assert task_row(board, merge_task_id)["status"] == "blocked"


def test_stale_identity_cannot_recover_or_write_back(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    merge_task_id = facts["merge_task_id"]
    old = board.claim(merge_task_id)
    assert old is not None
    assert board.reclaim(old, "test takeover") is True
    new = board.claim(merge_task_id)
    assert new is not None and new != old

    # A release journal left by the old run, as if that run had crashed after
    # the merge but before the takeover.
    stale_dir = (
        Path(config.work_root)
        / "runs"
        / merge_task_id
        / f"{old.run_id}-stale"
    )
    stale_dir.mkdir(parents=True)
    state = new_release_state(
        identity=old, dev_task_id=facts["task_id"], requirement_id=REQUIREMENT
    )
    state.advance("merged", merge_sha=facts["base"])
    write_release_state(stale_dir, state)

    recovered = recover_releases(config.work_root, board, config)
    assert [
        entry["action"]
        for entry in recovered
        if entry["merge_task_id"] == merge_task_id
    ] == ["stale_run"]

    # Nothing merged and the new claim is untouched.
    assert repo_head(facts["repo"]) == facts["base"]
    assert board.identity(merge_task_id) == new
    assert not any(
        run["outcome"] == "completed" for run in board.run_rows(merge_task_id)
    )

    # Every terminal write from the old identity is rejected by the host fence.
    assert board.complete_task(
        old, result="stale", summary="stale", metadata={"hkl": {"stale": True}}
    ) is False
    assert board.block(old, "stale", "capability") is False
    assert board.identity(merge_task_id) == new

    # ``merge_release`` re-checks the main tip immediately before merging.
    (facts["repo"] / "EXTRA.txt").write_text("moved\n", encoding="utf-8")
    run_git(["add", "EXTRA.txt"], cwd=facts["repo"], timeout=30)
    run_git(
        ["commit", "-q", "-m", "main moved"], cwd=facts["repo"], timeout=30
    )
    moved = repo_head(facts["repo"])
    run_document = json.loads(
        (facts["run_dir"] / "run.json").read_text(encoding="utf-8")
    )
    merge_dir = Path(config.work_root) / "merge-recheck"
    record = merge_release(
        repo=facts["repo"],
        main_ref="refs/heads/main",
        reviewed_head=run_document["code_commit"],
        main_before=facts["base"],
        allow=("RESULT.txt",),
        run_dir=merge_dir,
        identity=new,
        requirement_id=REQUIREMENT,
    )
    assert record.ok is False
    assert record.reasons == ("main-moved",)
    assert json.loads(
        (merge_dir / "merge.json").read_text(encoding="utf-8")
    )["verdict"] == "refused"
    assert repo_head(facts["repo"]) == moved


def test_stale_identity_release_once_has_zero_git_side_effects(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """R2/D9: reclaiming the merge card fences the old identity's merge too."""

    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    merge_task_id = facts["merge_task_id"]
    old = board.claim(merge_task_id)
    assert old is not None
    assert board.reclaim(old, "test takeover") is True
    new = board.claim(merge_task_id)
    assert new is not None and new != old
    main_before = repo_head(facts["repo"])

    outcome = release_once(
        config=config,
        adapter=board,
        card=facts["merge_card"],
        identity=old,
        work_root=config.work_root,
        package_dir=PACKAGE_DIR,
        requirement_id=REQUIREMENT,
    )
    assert outcome["action"] == "stale_run", outcome
    assert outcome["terminal_ok"] is False
    assert outcome["merge_sha"] is None

    # Zero git side effects: main did not move and no merge record was written.
    assert repo_head(facts["repo"]) == main_before
    run_dir = release_run_dir(Path(config.work_root), merge_task_id)
    assert not (run_dir / "merge.json").exists()
    assert not (run_dir / "postmerge").exists()
    state = read_release_state(run_dir)
    assert state is not None and state.stage == "terminal", state

    # The new claim is untouched and the old identity cannot write back.
    assert board.identity(merge_task_id) == new
    assert not any(
        run["outcome"] == "completed" for run in board.run_rows(merge_task_id)
    )


def test_takeover_wins_before_the_old_run_enters_the_merge_critical_section(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """R2/D9 (a), two real processes.

    A takeover process reclaims and re-claims the merge card, then a barrier
    releases the old run into ``release_once``.  The old run must observe the
    stale fence inside the critical section and leave ``main`` untouched.
    """

    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    work_root = Path(config.work_root)
    merge_task_id = facts["merge_task_id"]
    old = board.claim(merge_task_id)
    assert old is not None
    main_before = repo_head(facts["repo"])
    card_path = work_root / "cards" / f"{merge_task_id}.json"

    release_script, release_job = _write_child_job(
        work_root,
        "release-a",
        RELEASE_WAIT_SCRIPT,
        {
            "config": config,
            "identity": old.to_dict(),
            "card_path": str(card_path),
            "package_dir": str(PACKAGE_DIR),
            "requirement_id": REQUIREMENT,
            "go": str(work_root / "release-a.go"),
            "done": str(work_root / "release-a.done.json"),
        },
    )
    takeover_script, takeover_job = _write_child_job(
        work_root,
        "takeover-a",
        TAKEOVER_SCRIPT,
        {
            "config": config,
            "identity": old.to_dict(),
            "reclaim_new": True,
            "attempt": str(work_root / "takeover-a.attempt"),
            "done": str(work_root / "takeover-a.done.json"),
        },
    )

    # The old run is alive but parked outside the critical section.
    release_process = _spawn_child(release_script, release_job)
    try:
        takeover_process = _spawn_child(takeover_script, takeover_job)
        _finish(takeover_process, name="takeover-a")
        takeover = json.loads(
            (work_root / "takeover-a.done.json").read_text(encoding="utf-8")
        )
        assert takeover["reclaimed"] is True, takeover
        assert takeover["new_identity"] is not None, takeover
        new = RunIdentity.from_dict(takeover["new_identity"])
        assert board.identity(merge_task_id) == new

        # The handover has fully completed; only now does the old run proceed.
        (work_root / "release-a.go").write_text("1", encoding="utf-8")
        _finish(release_process, name="release-a")
    finally:
        if release_process.poll() is None:
            release_process.kill()
            release_process.communicate()

    outcome = json.loads(
        (work_root / "release-a.done.json").read_text(encoding="utf-8")
    )
    assert outcome["action"] == "stale_run", outcome
    assert outcome["terminal_ok"] is False, outcome
    assert outcome["merge_sha"] is None, outcome

    # Zero git side effects: main unmoved, no merge journal, no post-merge dir.
    assert repo_head(facts["repo"]) == main_before
    run_dir = release_run_dir(work_root, merge_task_id)
    assert not (run_dir / "merge.json").exists()
    assert not (run_dir / "postmerge").exists()
    state = read_release_state(run_dir)
    assert state is not None and state.stage == "terminal", state

    # The new claim is untouched and the old identity still cannot write back.
    assert board.identity(merge_task_id) == new
    assert not any(
        run["outcome"] == "completed" for run in board.run_rows(merge_task_id)
    )


def test_takeover_waits_while_the_merge_holds_the_critical_section(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """R2/D9 (b), two real processes.

    The release process parks inside ``merge_release`` (still holding the
    exclusive lock).  A takeover process must block in ``reclaim`` until the
    merge critical section ends; it must never return ``True`` while the old
    run still holds the right to move ``main``.
    """

    facts = _chain(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        # Keep the run busy after the merge so the takeover's observation of
        # the released lock is unambiguous and the post-merge fence is hit.
        post_merge_commands=[[PYTHON, "-c", "import time; time.sleep(2)"]],
    )
    config = facts["config"]
    work_root = Path(config.work_root)
    merge_task_id = facts["merge_task_id"]
    old = board.claim(merge_task_id)
    assert old is not None
    main_before = repo_head(facts["repo"])
    card_path = work_root / "cards" / f"{merge_task_id}.json"

    release_script, release_job = _write_child_job(
        work_root,
        "release-b",
        RELEASE_MERGE_HOLD_SCRIPT,
        {
            "config": config,
            "identity": old.to_dict(),
            "card_path": str(card_path),
            "package_dir": str(PACKAGE_DIR),
            "requirement_id": REQUIREMENT,
            "merge_entered": str(work_root / "release-b.merge-entered"),
            "allow_merge": str(work_root / "release-b.allow-merge"),
            "merge_done": str(work_root / "release-b.merge-done.json"),
            "done": str(work_root / "release-b.done.json"),
        },
    )
    takeover_script, takeover_job = _write_child_job(
        work_root,
        "takeover-b",
        TAKEOVER_SCRIPT,
        {
            "config": config,
            "identity": old.to_dict(),
            "reclaim_new": False,
            "ready": str(work_root / "takeover-b.ready"),
            "go": str(work_root / "takeover-b.go"),
            "attempt": str(work_root / "takeover-b.attempt"),
            "done": str(work_root / "takeover-b.done.json"),
        },
    )

    # Connect the takeover first: a fresh host connection itself needs the
    # write lock, so it must not be the thing that is (correctly) blocked here.
    takeover_process = _spawn_child(takeover_script, takeover_job)
    release_process = None
    try:
        _wait_for(work_root / "takeover-b.ready")
        release_process = _spawn_child(release_script, release_job)
        # Wait until the merge side is demonstrably inside the critical section.
        _wait_for(work_root / "release-b.merge-entered")
        assert board.identity(merge_task_id) == old

        (work_root / "takeover-b.go").write_text("1", encoding="utf-8")
        _wait_for(work_root / "takeover-b.attempt")
        # The takeover has entered ``reclaim`` and must stay blocked on the
        # same lock while the merge critical section is still held.
        time.sleep(0.8)
        assert not (work_root / "takeover-b.done.json").exists(), (
            "takeover returned while the merge critical section was held"
        )
        assert not (work_root / "release-b.merge-done.json").exists()

        # Release the merge; only now may the takeover observe the claim.
        (work_root / "release-b.allow-merge").write_text("1", encoding="utf-8")
        _wait_for(work_root / "release-b.merge-done.json")
        _finish(takeover_process, name="takeover-b")
        _finish(release_process, name="release-b")
    finally:
        for process in (release_process, takeover_process):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    takeover = json.loads(
        (work_root / "takeover-b.done.json").read_text(encoding="utf-8")
    )
    merge_done = json.loads(
        (work_root / "release-b.merge-done.json").read_text(encoding="utf-8")
    )
    assert takeover["reclaimed"] is True, takeover
    assert merge_done["ok"] is True, merge_done

    # The reclaim completed only after the merge wrote its journal: the old run
    # never "lost the claim first and then moved main".
    assert takeover["finished_at"] >= merge_done["at"] - 0.05, (
        takeover,
        merge_done,
    )
    assert repo_head(facts["repo"]) == merge_done["merge_sha"]
    assert repo_head(facts["repo"]) != main_before
    assert _merge_count(facts["repo"]) == 1

    # The old run cannot complete the card after the takeover; the merge is
    # recorded locally and terminal_ok reflects the lost fence.
    outcome = json.loads(
        (work_root / "release-b.done.json").read_text(encoding="utf-8")
    )
    assert outcome["terminal_ok"] is False, outcome


def test_parent_reopen_demotes_the_merge_card_and_it_is_not_released(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    task_id = facts["task_id"]
    merge_task_id = facts["merge_task_id"]
    assert task_row(board, merge_task_id)["status"] == "ready"

    # The parent is reopened (the dashboard path) and every descendant is
    # retracted by the host's own invalidation.
    board.connection.execute(
        "UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?",
        (task_id,),
    )
    board.connection.commit()
    result = board._kb.invalidate_descendants_for_parent_reopen(  # noqa: SLF001
        board.connection, task_id, author="operator"
    )
    board.connection.commit()
    assert [entry["id"] for entry in result["invalidated"]] == [merge_task_id]
    assert task_row(board, merge_task_id)["status"] == "todo"

    # The product does not recreate or release the demoted card.
    assert queue_release_cards(
        config.work_root, board, config, requirement_id=REQUIREMENT
    ) == []
    assert board.child_ids(task_id) == [merge_task_id]

    report = tick(
        config,
        adapters={"test": board},
        settings=SETTINGS,
        max_workers=0,
        package_dir=str(PACKAGE_DIR),
        requirement_id=REQUIREMENT,
    )
    assert report["errors"] == [], report["errors"]
    assert report["releases"] == [], report["releases"]
    assert report["claimed"] == [], report["claimed"]
    assert task_row(board, merge_task_id)["status"] == "todo"
    assert repo_head(facts["repo"]) == facts["base"]
    assert not (
        Path(config.work_root) / "runs" / merge_task_id
    ).exists()


# ---------------------------------------------------------------------------
# optional merge notification hook
# ---------------------------------------------------------------------------


def test_release_without_notify_config_creates_no_notify_files(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    # Default ``notify_commands`` is empty: the release behaves as before.
    _identity, outcome = claim_and_release(
        board, facts["config"], facts["merge_task_id"], facts["merge_card"]
    )
    assert outcome["action"] == "merged", outcome
    assert outcome["notify"]["verdict"] == "skipped"
    release_dir = release_run_dir(
        Path(facts["config"].work_root), facts["merge_task_id"]
    )
    assert not (release_dir / "notify").exists()
    assert task_row(board, facts["merge_task_id"])["status"] == "done"


def test_successful_merge_notifies_once_with_real_facts(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    target = tmp_path / "notify-facts.json"
    counter = tmp_path / "notify-count.txt"
    _with_notify(
        facts,
        [[PYTHON, "-c", NOTIFY_ENV_SCRIPT, str(target), str(counter)]],
    )
    _identity, outcome = claim_and_release(
        board, facts["config"], facts["merge_task_id"], facts["merge_card"]
    )
    assert outcome["action"] == "merged", outcome
    assert outcome["notify"]["verdict"] == "pass", outcome["notify"]
    assert counter.read_text(encoding="utf-8") == "x"

    release_dir = release_run_dir(
        Path(facts["config"].work_root), facts["merge_task_id"]
    )
    state = read_release_state(release_dir)
    merge_document = json.loads(
        (release_dir / "merge.json").read_text(encoding="utf-8")
    )
    release_report = json.loads(
        (release_dir / "release-report.json").read_text(encoding="utf-8")
    )
    env = json.loads(target.read_text(encoding="utf-8"))
    assert env == {
        "HKL_REQUIREMENT_ID": REQUIREMENT,
        "HKL_BOARD": board.name,
        "HKL_MERGE_TASK_ID": facts["merge_task_id"],
        "HKL_DEV_TASK_ID": facts["task_id"],
        "HKL_MERGE_SHA": merge_document["merge_sha"],
        "HKL_MERGE_TREE": merge_document["merge_tree"],
        "HKL_REVIEWED_HEAD": release_report["reviewed_head"],
        "HKL_REVIEWED_TREE": release_report["reviewed_tree"],
        "HKL_MAIN_BEFORE": merge_document["main_before"],
        "HKL_EVIDENCE_INDEX_SHA256": release_report["evidence_index_sha256"],
        "HKL_AUTHORIZATION_SOURCE": "file",
        "HKL_REPO": str(facts["repo"]),
        "HKL_RUN_DIR": str(release_dir),
    }
    assert state is not None and state.merge_sha == merge_document["merge_sha"]
    notify_report = json.loads(
        (release_dir / "notify" / "report.json").read_text(encoding="utf-8")
    )
    assert notify_report["verdict"] == "pass", notify_report
    assert notify_report["merge_sha"] == merge_document["merge_sha"]
    assert len(notify_report["commands"]) == 1
    assert notify_report["commands"][0]["exit_code"] == 0
    assert task_row(board, facts["merge_task_id"])["status"] == "done"


@pytest.mark.parametrize(
    ("argv_factory", "timeout_seconds", "expected_reason"),
    [
        (
            lambda: [PYTHON, "-c", "import sys; sys.exit(5)"],
            10.0,
            "notify-failed:0",
        ),
        (
            lambda: [PYTHON, "-c", "import time; time.sleep(30)"],
            0.3,
            "notify-timeout:0",
        ),
    ],
)
def test_notify_failure_keeps_the_card_done_and_logs_a_warning(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    argv_factory,
    timeout_seconds,
    expected_reason,
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    _with_notify(facts, [argv_factory()], timeout_seconds=timeout_seconds)
    _identity, outcome = claim_and_release(
        board, facts["config"], facts["merge_task_id"], facts["merge_card"]
    )

    # The merge result is independent of the hook: the card stays done and
    # main is not rolled back.
    assert outcome["action"] == "merged", outcome
    assert outcome["notify"]["verdict"] == "fail", outcome["notify"]
    assert outcome["notify"]["reasons"] == [expected_reason]
    release_dir = release_run_dir(
        Path(facts["config"].work_root), facts["merge_task_id"]
    )
    state = read_release_state(release_dir)
    assert state is not None and state.stage == "terminal"
    assert task_row(board, facts["merge_task_id"])["status"] == "done"
    assert repo_head(facts["repo"]) == state.merge_sha

    notify_report = json.loads(
        (release_dir / "notify" / "report.json").read_text(encoding="utf-8")
    )
    assert notify_report["verdict"] == "fail"
    assert notify_report["reasons"] == [expected_reason]

    log_records = [
        json.loads(line)
        for line in (
            Path(facts["config"].work_root) / "logs" / "daemon.log"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    warnings = [
        record
        for record in log_records
        if record.get("record") == "release-notify"
        and record.get("merge_sha") == state.merge_sha
    ]
    assert warnings, log_records
    assert warnings[-1]["verdict"] == "fail"
    assert warnings[-1]["reasons"] == [expected_reason]


def test_crash_after_notification_is_not_notified_again_on_recovery(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    counter = tmp_path / "notify-count.txt"
    _with_notify(
        facts, [[PYTHON, "-c", NOTIFY_APPEND_SCRIPT, str(counter)]]
    )

    # The child notifies and then dies before writing the terminal journal, so
    # the card is done while ``release-state.json`` still says ``postmerge``.
    _run_until_crash(facts, "notified")
    assert counter.read_text(encoding="utf-8") == "x"
    assert task_row(board, facts["merge_task_id"])["status"] == "done"
    release_dir = release_run_dir(
        Path(facts["config"].work_root), facts["merge_task_id"]
    )
    assert read_release_state(release_dir).stage == "postmerge"

    config = facts["config"]
    first = recover_releases(
        config.work_root, board, config, requirement_id=REQUIREMENT
    )
    entry = next(
        item
        for item in first
        if item["merge_task_id"] == facts["merge_task_id"]
    )
    assert entry["action"] == "completed-already", entry
    assert counter.read_text(encoding="utf-8") == "x"
    assert read_release_state(release_dir).stage == "terminal"

    # A second recovery pass is a no-op and still does not notify again.
    second = recover_releases(
        config.work_root, board, config, requirement_id=REQUIREMENT
    )
    assert [
        item
        for item in second
        if item["merge_task_id"] == facts["merge_task_id"]
    ] == []
    assert counter.read_text(encoding="utf-8") == "x"
    assert _merge_count(facts["repo"]) == 1


# ---------------------------------------------------------------------------
# W4-13: execution reads the same derived review facts as the queue
# ---------------------------------------------------------------------------


def _rewrite_review_metadata(board, run_id: int, metadata: dict) -> None:
    board.connection.execute(
        "UPDATE task_runs SET metadata = ? WHERE id = ?",
        (json.dumps(metadata), run_id),
    )
    board.connection.commit()


def _native_review_metadata(facts: dict, *, reviewed_commit: str) -> dict:
    """The metadata a native reviewer actually writes: no derived fields."""

    original = next(
        run
        for run in facts["board"].review_runs(facts["task_id"])
        if run["id"] == facts["review_run_id"]
    )["metadata"]
    return {
        "verdict": "pass",
        "review_outcome": "approved",
        "reviewed_commit": reviewed_commit,
        "reviewed_tree": original["reviewed_tree"],
        "review_round": 1,
    }


def test_release_once_derives_missing_dev_run_id_like_the_queue(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """A card that queued from native metadata must also execute.

    The reviewer records no ``dev_run_id`` / ``evidence_index_sha256``; the
    development run index supplies them.  ``release_once`` previously read the
    raw document and blocked every such card with ``review-dev-run-invalid``.
    """

    facts = _chain(
        tmp_path,
        board,
        make_task,
        make_repo,
        make_config,
        write_card,
        fake_executor,
        authorization=False,
    )
    review_run_id = facts["review_run_id"]
    review_run = next(
        run
        for run in board.run_rows(facts["task_id"])
        if run["id"] == review_run_id
    )
    assert review_run["profile"] == "code-reviewer"

    run_document = json.loads(
        (facts["run_dir"] / "run.json").read_text(encoding="utf-8")
    )
    native = _native_review_metadata(
        facts, reviewed_commit=run_document["code_commit"]
    )
    assert "dev_run_id" not in native
    _rewrite_review_metadata(board, review_run_id, native)

    _, outcome = claim_and_release(
        board, facts["config"], facts["merge_task_id"], facts["merge_card"]
    )

    # The binding accepted the derived facts and the run reached the gate;
    # the only remaining refusal is the missing authorization file.
    assert outcome["action"] == "block", outcome
    assert outcome["reason"] == "release:authorization-missing", outcome
    assert outcome["reason"] != "review-dev-run-invalid"


def test_release_once_names_the_unmatched_reviewed_commit(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """An unresolvable reviewed commit reports which fact is missing."""

    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    review_run_id = facts["review_run_id"]
    # A syntactically valid head that no development run recorded.
    missing_head = "0" * 40
    native = _native_review_metadata(facts, reviewed_commit=missing_head)
    _rewrite_review_metadata(board, review_run_id, native)

    _, outcome = claim_and_release(
        board, facts["config"], facts["merge_task_id"], facts["merge_card"]
    )

    assert outcome["action"] == "block", outcome
    assert outcome["reason"] == "review-metadata-incomplete:dev-run-not-found", outcome
    assert outcome["reason"] != "review-dev-run-invalid"


# ---------------------------------------------------------------------------
# W4-04 rework shapes: no-op rounds, sync ancestry and visible review input
# ---------------------------------------------------------------------------

NO_OP_BODY = 'print(json.dumps({"is_error": False, "result": "no changes"}))\n'


def _advance_main(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    added = run_git(["add", name], cwd=repo)
    assert added.returncode == 0, added.stderr
    committed = run_git(
        [
            "-c",
            "user.name=HKL Tests",
            "-c",
            "user.email=hkl@example.invalid",
            "commit",
            "--no-verify",
            "-m",
            f"advance {name}",
        ],
        cwd=repo,
    )
    assert committed.returncode == 0, committed.stderr
    return repo_head(repo)


def _rework_setup(
    tmp_path,
    board,
    make_task,
    make_repo,
    make_config,
    write_card,
    fake_executor,
    *,
    allow=("RESULT.txt", "RESULT2.txt"),
):
    repo = make_repo()
    base = repo_head(repo)
    round_one = fake_executor(review_body())
    no_op = fake_executor(NO_OP_BODY)
    config = w4_config(
        make_config,
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[round_one],
        max_rework_rounds=3,
    )
    task_id = make_task(title="w4 rework shape")
    card = load_card(
        write_card(config.work_root, task_id, allow=allow), config
    )
    identity_one, outcome_one = run_dev(
        board, config, card, task_id, executor_argv=round_one
    )
    assert outcome_one.category == "success", outcome_one.reason
    assert outcome_one.terminal == "request_review"
    run_one = dev_run_dir(config.work_root, task_id, identity_one.run_id)
    first_code = json.loads(
        (run_one / "run.json").read_text(encoding="utf-8")
    )["code_commit"]
    assert first_code != base
    return {
        "repo": repo,
        "base": base,
        "config": config,
        "task_id": task_id,
        "card": card,
        "no_op": no_op,
        "identity_one": identity_one,
        "first_code": first_code,
    }


def _run_json(run_dir: Path) -> dict:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def test_no_op_rework_is_named_and_carries_the_review_request(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """Shape A: ``run_commit == continuation_base`` is named, never ancestry."""

    facts = _rework_setup(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    task_id = facts["task_id"]
    review_text = "fix docs/10 hard protection root"
    assert review_changes(board, task_id, review_text)[0] is True

    # Main advances after round 1, exactly like the production incident: the
    # rework branch is still cut from the old code commit.
    sync = _advance_main(facts["repo"], "MAIN.txt")
    assert sync != facts["first_code"]

    identity_two, outcome_two = run_dev(
        board, config, facts["card"], task_id, executor_argv=facts["no_op"]
    )
    run_two = dev_run_dir(config.work_root, task_id, identity_two.run_id)
    document = _run_json(run_two)

    assert document["round"] == 2
    assert document["continuation_base"] == facts["first_code"]
    assert document["run_commit"] == facts["first_code"]
    assert document["code_commit"] == facts["first_code"]
    assert document["rework"]["changes_requested"] == [review_text]
    assert document["rework"]["summary"] == review_text

    assert outcome_two.category == "code_failure"
    assert outcome_two.reason.startswith("rework-no-changes"), outcome_two.reason
    assert review_text in outcome_two.reason
    assert "sync-base-not-ancestor" not in outcome_two.reason
    assert outcome_two.terminal == "block"
    assert outcome_two.terminal_ok is True
    row = task_row(board, task_id)
    assert row["status"] == "blocked"
    assert row["block_kind"] == "needs_input"
    blocked_event = board.connection.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert blocked_event is not None
    blocked_reason = json.loads(blocked_event["payload"])["reason"]
    assert blocked_reason.startswith("rework-no-changes"), blocked_reason
    assert review_text in blocked_reason

    # The round stopped before the gate/evidence stages: the old shape used to
    # reach evidence and be rejected as ``sync-base-not-ancestor``.
    assert document["gate"]["verdict"] is None
    assert document["evidence"]["index_path"] is None
    assert not (run_two / "evidence" / "index.json").exists()


def test_rework_with_a_new_commit_merges_the_advanced_base(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """Shape B: a real rework commit aligns ``sync_base`` with a merge commit."""

    facts = _rework_setup(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    task_id = facts["task_id"]
    round_two = fake_executor(
        review_body(write="RESULT2.txt", content="two\n", require="RESULT.txt")
    )

    sync = _advance_main(facts["repo"], "MAIN.txt")
    assert review_changes(board, task_id, "continue the docs fix")[0] is True

    identity_two, outcome_two = run_dev(
        board, config, facts["card"], task_id, executor_argv=round_two
    )
    assert outcome_two.category == "success", outcome_two.reason
    assert outcome_two.terminal == "request_review", outcome_two.terminal
    run_two = dev_run_dir(config.work_root, task_id, identity_two.run_id)
    document = _run_json(run_two)

    assert document["round"] == 2
    assert document["continuation_base"] == facts["first_code"]
    assert document["run_commit"] != facts["first_code"]
    assert document["sync_base"] == sync
    # The baseline merge is what makes the sync commit part of the run history.
    assert document["baseline"]["merge_commit"] == document["code_commit"]
    assert is_ancestor(
        document["sync_base"], document["code_commit"], repo=facts["repo"]
    )
    assert is_ancestor(
        document["continuation_base"], document["code_commit"], repo=facts["repo"]
    )

    verification = verify_evidence(
        run_two,
        identity_two,
        PACKAGE_DIR,
        Path(outcome_two.workspace),
        facts["repo"],
        requirement_id=REQUIREMENT,
    )
    assert verification.ok is True, verification.reasons
    assert "sync-base-not-ancestor" not in verification.reasons


def test_main_advancing_twice_including_after_a_no_op_round(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    """Shape C: two main advances (one after a no-op) never mislabel a rework."""

    facts = _rework_setup(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    task_id = facts["task_id"]
    review_text = "fix docs/10 hard protection root"
    assert review_changes(board, task_id, review_text)[0] is True

    # First advance, then a zero-change round that must be named.
    _advance_main(facts["repo"], "MAIN.txt")
    identity_two, outcome_two = run_dev(
        board, config, facts["card"], task_id, executor_argv=facts["no_op"]
    )
    assert outcome_two.reason.startswith("rework-no-changes"), outcome_two.reason
    run_two = dev_run_dir(config.work_root, task_id, identity_two.run_id)

    # Second advance after the no-op round: the stored verdict stays named.
    _advance_main(facts["repo"], "MAIN2.txt")
    stored = _run_json(run_two)
    assert stored["run_commit"] == stored["continuation_base"]
    assert stored["rework"]["changes_requested"] == [review_text]
    assert stored["gate"]["verdict"] is None

    # Re-open the card and run a real rework round from the same continuation.
    promoted, problem = board._kb.promote_task(  # noqa: SLF001 - test-only
        board.connection, task_id, actor="test"
    )
    assert promoted is True, problem
    round_three = fake_executor(
        review_body(write="RESULT2.txt", content="two\n", require="RESULT.txt")
    )
    identity_three, outcome_three = run_dev(
        board, config, facts["card"], task_id, executor_argv=round_three
    )
    assert outcome_three.category == "success", outcome_three.reason
    assert outcome_three.terminal == "request_review", outcome_three.terminal
    run_three = dev_run_dir(config.work_root, task_id, identity_three.run_id)
    document = _run_json(run_three)

    assert document["continuation_base"] == facts["first_code"]
    assert is_ancestor(
        document["sync_base"], document["code_commit"], repo=facts["repo"]
    )
    verification = verify_evidence(
        run_three,
        identity_three,
        PACKAGE_DIR,
        Path(outcome_three.workspace),
        facts["repo"],
        requirement_id=REQUIREMENT,
    )
    assert verification.ok is True, verification.reasons
    assert "sync-base-not-ancestor" not in verification.reasons
