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
from pathlib import Path

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
from hermes_kanban_lanes.workspace.git import repo_head, run_git

from test_w4_release_failures import _chain
from w4_helpers import PYTHON, release_run_dir, task_row

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
