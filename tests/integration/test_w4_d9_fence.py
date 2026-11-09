"""W4-14..W4-18 (D9-I): the host write transaction is the merge/takeover fence.

Two real OS processes contend for one temporary board.  The merge side holds
``BoardAdapter.write_fence()`` (the host ``BEGIN IMMEDIATE``) across the merge;
the takeover sides either call the host directly (``kanban_db``) or run a plain
``write_txn`` from a second process.  Every assertion is about behaviour (who
waited, what moved, what survived a crash), not call counts.

Nothing here touches the real engineering board: every board and repository is
created under ``tmp_path``.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import signal
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from hermes_kanban_lanes.release import merge as merge_module
from hermes_kanban_lanes.release.merge import MERGE_TIMEOUT, merge_release
from hermes_kanban_lanes.release.state import (
    read_merge_intent,
    read_release_state,
)
from hermes_kanban_lanes.runtime import release as release_module
from hermes_kanban_lanes.runtime.release import release_once, recover_releases
from hermes_kanban_lanes.workspace.git import GitError, repo_head, run_git

from test_w4_release_failures import _chain
from w4_helpers import PYTHON, release_run_dir, task_row

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
PACKAGE_DIR = SRC_DIR / "hermes_kanban_lanes"
REQUIREMENT = "HKL-W4"


# ---------------------------------------------------------------------------
# child programs
# ---------------------------------------------------------------------------


HOLD_MERGE_SCRIPT = r'''
import json, sys, time
from pathlib import Path

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.config import load_config
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter
from hermes_kanban_lanes.runtime import release as release_module

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
config = load_config(job["config"])
adapter = BoardAdapter(config.boards["test"], config.hermes_source, name="test").open()
identity = RunIdentity.from_dict(job["identity"])
card = load_card(Path(job["card_path"]), config)

# The claim is deliberately expired so the takeover sweep has real work.  A
# heartbeat only extends claims; removing that board write keeps this scenario
# about exclusion rather than heartbeat semantics.
release_module._heartbeat = lambda *args, **kwargs: True
original_merge = release_module.merge_release
original_post = release_module.run_post_merge


def held_merge(**kwargs):
    Path(job["merge_entered"]).write_text("1", encoding="utf-8")
    time.sleep(float(job["hold_seconds"]))
    record = original_merge(**kwargs)
    Path(job["merge_done"]).write_text(
        json.dumps({"ok": record.ok, "merge_sha": record.merge_sha, "at": time.time()}),
        encoding="utf-8",
    )
    return record


def held_post(**kwargs):
    # After the fence commit the sweep can win; keep this process busy so the
    # takeover observes a committed merge but no completed release.
    Path(job["post_fence"]).write_text("1", encoding="utf-8")
    time.sleep(float(job["post_hold_seconds"]))
    return original_post(**kwargs)


release_module.merge_release = held_merge
release_module.run_post_merge = held_post
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
'''

HOST_SWEEP_SCRIPT = r'''
import json, sys, time
from pathlib import Path

from hermes_kanban_lanes.config import load_config

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
config = load_config(job["config"])
sys.path.insert(0, config.hermes_source)
from hermes_cli import kanban_db, kanban_db_connect

conn = kanban_db_connect.connect(db_path=Path(config.boards["test"]))
Path(job["ready"]).write_text("1", encoding="utf-8")
while not Path(job["go"]).exists():
    time.sleep(0.005)
started = time.time()
released = kanban_db.release_stale_claims(conn)
finished = time.time()
Path(job["done"]).write_text(
    json.dumps({"released": released, "started_at": started, "finished_at": finished}),
    encoding="utf-8",
)
conn.close()
'''

HOST_RECLAIM_SCRIPT = r'''
import json, sys
from pathlib import Path

from hermes_kanban_lanes.config import load_config

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
config = load_config(job["config"])
sys.path.insert(0, config.hermes_source)
from hermes_cli import kanban_db, kanban_db_connect

conn = kanban_db_connect.connect(db_path=Path(config.boards["test"]))
reclaimed = kanban_db.reclaim_task(conn, job["task_id"], reason="d9 host takeover")
row = conn.execute(
    "SELECT status, claim_lock FROM tasks WHERE id = ?", (job["task_id"],)
).fetchone()
Path(job["done"]).write_text(
    json.dumps(
        {
            "reclaimed": bool(reclaimed),
            "status": row["status"],
            "claim_lock": row["claim_lock"],
        }
    ),
    encoding="utf-8",
)
conn.close()
'''

FENCE_KILL_SCRIPT = r'''
import json, sys, time
from pathlib import Path

from hermes_kanban_lanes.config import load_config
from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
config = load_config(job["config"])
adapter = BoardAdapter(config.boards["test"], config.hermes_source, name="test").open()
with adapter.write_fence():
    adapter.connection.execute(
        "UPDATE tasks SET claim_lock = NULL WHERE id = ?", (job["task_id"],)
    )
    Path(job["signal"]).write_text("1", encoding="utf-8")
    time.sleep(120)
'''

CRASH_BEFORE_MERGED_JOURNAL_SCRIPT = r'''
import json, sys, time
from pathlib import Path

from hermes_kanban_lanes.card_contract import load_card
from hermes_kanban_lanes.config import load_config
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter
from hermes_kanban_lanes.runtime import release as release_module

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
config = load_config(job["config"])
adapter = BoardAdapter(config.boards["test"], config.hermes_source, name="test").open()
identity = RunIdentity.from_dict(job["identity"])
card = load_card(Path(job["card_path"]), config)
original_write_state = release_module.write_release_state


def write_then_wait(run_dir, state):
    if state.stage == "merged":
        # The git merge commit exists but the merged journal is not durable yet.
        Path(job["signal"]).write_text("1", encoding="utf-8")
        while True:
            time.sleep(0.01)
    original_write_state(run_dir, state)


release_module.write_release_state = write_then_wait
release_module.release_once(
    config=config,
    adapter=adapter,
    card=card,
    identity=identity,
    work_root=config.work_root,
    package_dir=job["package_dir"],
    requirement_id=job["requirement_id"],
)
'''

FREEZE_SCRIPT = r'''
import json, sys, time
from pathlib import Path

from hermes_kanban_lanes.config import load_config
from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
config = load_config(job["config"])
adapter = BoardAdapter(config.boards["test"], config.hermes_source, name="test").open()
with adapter.write_fence():
    Path(job["entered"]).write_text("1", encoding="utf-8")
    time.sleep(float(job["seconds"]))
adapter.close()
'''

PLAIN_WRITER_SCRIPT = r'''
import json, sys, time
from pathlib import Path

from hermes_kanban_lanes.config import load_config

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
config = load_config(job["config"])
sys.path.insert(0, config.hermes_source)
from hermes_cli import kanban_db_connect

conn = kanban_db_connect.connect(db_path=Path(config.boards["test"]))
Path(job["ready"]).write_text("1", encoding="utf-8")
while not Path(job["go"]).exists():
    time.sleep(0.005)
started = time.time()
ok = True
error = None
try:
    with kanban_db_connect.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET priority = priority + 1 WHERE id = ?",
            (job["task_id"],),
        )
except Exception as exc:  # noqa: BLE001 - recorded as the test's data
    ok = False
    error = f"{type(exc).__name__}: {exc}"
finished = time.time()
Path(job["done"]).write_text(
    json.dumps(
        {
            "ok": ok,
            "error": error,
            "started_at": started,
            "finished_at": finished,
            "elapsed": finished - started,
        }
    ),
    encoding="utf-8",
)
conn.close()
'''


# ---------------------------------------------------------------------------
# process helpers
# ---------------------------------------------------------------------------


def _child_env(work_root: Path, name: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    # Independent bytecode caches: the children must not share a cache prefix
    # with this process or with each other.
    env["PYTHONPYCACHEPREFIX"] = str(work_root / "pycache" / name)
    return env


def _spawn(
    work_root: Path, name: str, script: str, config, payload: dict
) -> tuple[subprocess.Popen, Path]:
    job_dir = work_root / f"d9-{name}"
    job_dir.mkdir(parents=True, exist_ok=True)
    script_path = job_dir / "child.py"
    script_path.write_text(script, encoding="utf-8")
    config_path = job_dir / "config.json"
    config_path.write_text(
        json.dumps(config.to_dict(), indent=2), encoding="utf-8"
    )
    job = {"config": str(config_path), **payload}
    job_path = job_dir / "job.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    process = subprocess.Popen(
        [PYTHON, str(script_path), str(job_path)],
        cwd=str(job_dir),
        env=_child_env(work_root, name),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process, job_dir


def _wait_for(path: Path, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _finish(process: subprocess.Popen, *, name: str, timeout: float = 240.0) -> None:
    stdout, stderr = process.communicate(timeout=timeout)
    assert process.returncode == 0, (name, process.returncode, stdout, stderr)


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is None:
        os.kill(process.pid, signal.SIGKILL)
    try:
        process.communicate(timeout=60)
    except subprocess.TimeoutExpired:  # pragma: no cover - after SIGKILL this cannot block
        process.kill()
        process.communicate()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _merge_count(repo: Path) -> int:
    result = run_git(
        ["rev-list", "--merges", "--count", "refs/heads/main"], cwd=repo, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def _row(board, task_id: str) -> dict:
    row = board.connection.execute(
        "SELECT status, current_run_id, claim_lock, claim_expires, priority "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


# ---------------------------------------------------------------------------
# W4-14: the host TTL sweep waits for the merge's write transaction
# ---------------------------------------------------------------------------


def test_w4_14_host_sweep_waits_for_the_merge_write_transaction(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    work_root = Path(config.work_root)
    merge_task_id = facts["merge_task_id"]
    card_path = work_root / "cards" / f"{merge_task_id}.json"

    identity = board.claim(merge_task_id)
    assert identity is not None
    # Production shape: the merge outlives the claim TTL.
    board.connection.execute(
        "UPDATE tasks SET claim_expires = ? WHERE id = ?",
        (int(time.time()) - 300, merge_task_id),
    )

    sweep_process, _ = _spawn(
        work_root,
        "w4-14-sweep",
        HOST_SWEEP_SCRIPT,
        config,
        {
            "ready": str(work_root / "w4-14-sweep.ready"),
            "go": str(work_root / "w4-14-sweep.go"),
            "done": str(work_root / "w4-14-sweep.done.json"),
        },
    )
    release_process = None
    try:
        _wait_for(work_root / "w4-14-sweep.ready")
        release_process, _ = _spawn(
            work_root,
            "w4-14-release",
            HOLD_MERGE_SCRIPT,
            config,
            {
                "identity": identity.to_dict(),
                "card_path": str(card_path),
                "package_dir": str(PACKAGE_DIR),
                "requirement_id": REQUIREMENT,
                "hold_seconds": 1.0,
                "post_hold_seconds": 2.0,
                "merge_entered": str(work_root / "w4-14.merge-entered"),
                "post_fence": str(work_root / "w4-14.post-fence"),
                "merge_done": str(work_root / "w4-14.merge-done.json"),
                "done": str(work_root / "w4-14.release-done.json"),
            },
        )
        _wait_for(work_root / "w4-14.merge-entered")

        # The merge side is inside the fence; the expired claim is untouched.
        during = _row(board, merge_task_id)
        assert during["status"] == "running", during
        assert during["claim_lock"] == identity.claim_lock, during
        assert during["claim_expires"] <= int(time.time()), during

        (work_root / "w4-14-sweep.go").write_text("1", encoding="utf-8")

        _wait_for(work_root / "w4-14-sweep.done.json")
        _wait_for(work_root / "w4-14.merge-done.json")
        merge_done = _load(work_root / "w4-14.merge-done.json")
        _finish(sweep_process, name="w4-14-sweep")
        _finish(release_process, name="w4-14-release")
    finally:
        for process in (release_process, sweep_process):
            if process is not None and process.poll() is None:
                _kill(process)

    takeover = _load(work_root / "w4-14-sweep.done.json")
    release_outcome = _load(work_root / "w4-14.release-done.json")

    # The sweep reclaimed the expired claim, but only after the merge committed:
    # ``merge_done`` is written inside the fence, before it is released.
    assert takeover["released"] == 1, takeover
    assert merge_done["ok"] is True, merge_done
    assert takeover["started_at"] <= merge_done["at"], (takeover, merge_done)
    assert takeover["finished_at"] > merge_done["at"], (takeover, merge_done)

    # ``main`` moved exactly once, and the takeover did not interrupt the merge.
    assert _merge_count(facts["repo"]) == 1
    assert repo_head(facts["repo"]) == merge_done["merge_sha"]
    assert release_outcome["merge_sha"] == merge_done["merge_sha"]
    # The release cannot claim completion after the takeover won the claim.
    assert release_outcome["action"] in (
        "merged",
        "merged-unrecorded",
        "stale_run",
    ), release_outcome
    assert task_row(board, merge_task_id)["status"] != "running"


# ---------------------------------------------------------------------------
# W4-15: the takeover wins first -> the old run has zero git side effects
# ---------------------------------------------------------------------------


def test_w4_15_host_takeover_first_leaves_the_old_run_with_zero_git_effects(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    work_root = Path(config.work_root)
    merge_task_id = facts["merge_task_id"]

    old = board.claim(merge_task_id)
    assert old is not None
    main_before = repo_head(facts["repo"])

    reclaim_process, _ = _spawn(
        work_root,
        "w4-15-reclaim",
        HOST_RECLAIM_SCRIPT,
        config,
        {"task_id": merge_task_id, "done": str(work_root / "w4-15.done.json")},
    )
    try:
        _finish(reclaim_process, name="w4-15-reclaim")
    finally:
        if reclaim_process.poll() is None:  # pragma: no cover
            _kill(reclaim_process)

    takeover = _load(work_root / "w4-15.done.json")
    assert takeover["reclaimed"] is True, takeover
    assert takeover["status"] == "ready", takeover
    assert takeover["claim_lock"] is None, takeover

    # Only now does the old run enter the critical section, with its old fence.
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
    assert outcome["terminal_ok"] is False, outcome
    assert outcome["merge_sha"] is None, outcome

    # Zero git side effects and no write-ahead intent.
    assert repo_head(facts["repo"]) == main_before
    run_dir = release_run_dir(work_root, merge_task_id)
    assert not (run_dir / "merge.json").exists()
    assert not (run_dir / "merge-intent.json").exists()
    assert not (run_dir / "postmerge").exists()
    state = read_release_state(run_dir)
    assert state is not None and state.stage == "terminal", state
    assert _merge_count(facts["repo"]) == 0


# ---------------------------------------------------------------------------
# W4-16: nesting rules and heartbeat placement
# ---------------------------------------------------------------------------


def test_w4_16_nested_writes_raise_and_savepoints_stay_invisible(
    tmp_path, board, make_task, fresh_adapter
):
    task_id = make_task(title="w4-16 fence")
    identity = board.claim(task_id)
    assert identity is not None
    # Make the claim stale so ``release_stale_claims`` reaches its write txn.
    board.connection.execute(
        "UPDATE tasks SET claim_expires = ? WHERE id = ?",
        (int(time.time()) - 10, task_id),
    )
    before = _row(board, task_id)

    reader = fresh_adapter(board.board_path)
    assert reader.connection.execute(
        "SELECT priority FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["priority"] == before["priority"]

    with board.write_fence():
        with pytest.raises(RuntimeError, match="already inside a transaction"):
            with board._kbc.write_txn(board.connection):
                board.connection.execute(
                    "UPDATE tasks SET priority = 5 WHERE id = ?", (task_id,)
                )
        with pytest.raises(RuntimeError, match="already inside a transaction"):
            board.release_stale_claims()
        with pytest.raises(RuntimeError):
            board.heartbeat(identity)

        # A savepoint is legal but not durable until the outer COMMIT.
        with board._kbc.write_txn(board.connection, allow_nested=True):
            board.connection.execute(
                "UPDATE tasks SET priority = 77 WHERE id = ?", (task_id,)
            )
        assert board.connection.in_transaction is True
        assert reader.connection.execute(
            "SELECT priority FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["priority"] == before["priority"]

    # After the fence the outer transaction committed, visible to the reader.
    assert board.connection.in_transaction is False
    assert reader.connection.execute(
        "SELECT priority FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["priority"] == 77

    # Production placement: heartbeat before the fence, terminal writes after.
    source = textwrap.dedent(inspect.getsource(release_module.release_once))
    fence_at = source.index("adapter.write_fence")
    assert source.index("_heartbeat(adapter, identity)") < fence_at
    assert source.index("adapter.complete_task") > fence_at

    # The fence body itself contains no board write or heartbeat: the four
    # steps are re-check, intent, merge and journal (D9-I rule 3).
    tree = ast.parse(source)
    fence_bodies: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        if not any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "write_fence"
            for item in node.items
        ):
            continue
        for statement in node.body:
            segment = ast.get_source_segment(source, statement)
            assert segment is not None
            fence_bodies.append(segment)
    assert fence_bodies, "release_once no longer holds adapter.write_fence"
    fence_body = "\n".join(fence_bodies)
    assert "heartbeat" not in fence_body, fence_body
    assert "adapter.write_txn" not in fence_body, fence_body
    assert "release_stale_claims" not in fence_body, fence_body
    assert "complete_task" not in fence_body, fence_body
    assert "record_existing_merge" not in fence_body, fence_body


# ---------------------------------------------------------------------------
# W4-17: SIGKILL inside the fence; adopt an existing merge without re-merging
# ---------------------------------------------------------------------------


def test_w4_17_sigkill_rolls_back_and_leaves_no_lock(
    tmp_path, board, make_task, make_repo, make_config
):
    repo = make_repo()
    config = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[PYTHON, "-c", "pass"],
    )
    work_root = Path(config.work_root)
    task_id = make_task(title="w4-17 rollback")
    identity = board.claim(task_id)
    assert identity is not None
    before = _row(board, task_id)

    process, _ = _spawn(
        work_root,
        "w4-17-rollback",
        FENCE_KILL_SCRIPT,
        config,
        {"task_id": task_id, "signal": str(work_root / "in-fence")},
    )
    try:
        _wait_for(work_root / "in-fence")
        # The uncommitted write is invisible to this (second) connection.
        assert _row(board, task_id)["claim_lock"] == before["claim_lock"]
        _kill(process)
    finally:
        if process.poll() is None:  # pragma: no cover
            _kill(process)

    # The killed transaction rolled back completely and left no lock behind.
    after = _row(board, task_id)
    assert after["claim_lock"] == before["claim_lock"], after
    assert after["status"] == before["status"], after
    fresh = board._kbc.connect(db_path=Path(board.board_path))
    try:
        started = time.monotonic()
        fresh.execute("BEGIN IMMEDIATE")
        fresh.execute("ROLLBACK")
        assert time.monotonic() - started < 5.0
    finally:
        fresh.close()
    assert not (work_root / "locks").exists()
    assert not list(work_root.glob("*.lock*"))


def test_w4_17_adopts_an_existing_merge_without_creating_a_second_one(
    tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
):
    facts = _chain(
        tmp_path, board, make_task, make_repo, make_config, write_card, fake_executor
    )
    config = facts["config"]
    work_root = Path(config.work_root)
    merge_task_id = facts["merge_task_id"]
    identity = board.claim(merge_task_id)
    assert identity is not None
    before = _row(board, merge_task_id)
    merges_before = _merge_count(facts["repo"])
    dev_run = _load(Path(facts["run_dir"]) / "run.json")
    reviewed_head = dev_run["code_commit"]
    assert reviewed_head != facts["base"]

    process, _ = _spawn(
        work_root,
        "w4-17-adopt",
        CRASH_BEFORE_MERGED_JOURNAL_SCRIPT,
        config,
        {
            "identity": identity.to_dict(),
            "card_path": str(work_root / "cards" / f"{merge_task_id}.json"),
            "package_dir": str(PACKAGE_DIR),
            "requirement_id": REQUIREMENT,
            "signal": str(work_root / "w4-17.merged-pending"),
        },
    )
    try:
        _wait_for(work_root / "w4-17.merged-pending")
        # git merged, the merged journal is not durable, the fence is held.
        assert _merge_count(facts["repo"]) == merges_before + 1
        _kill(process)
    finally:
        if process.poll() is None:  # pragma: no cover
            _kill(process)

    release_dir = release_run_dir(work_root, merge_task_id)
    state = read_release_state(release_dir)
    assert state is not None and state.stage == "gated", state
    intent = read_merge_intent(release_dir)
    assert intent is not None
    assert intent.main_before == facts["base"]
    assert intent.reviewed_head == reviewed_head
    # The claim row survived the kill intact.
    assert _row(board, merge_task_id)["claim_lock"] == before["claim_lock"]
    assert _row(board, merge_task_id)["status"] == "running"
    merged_head = repo_head(facts["repo"])
    assert merged_head != facts["base"]

    recovered = recover_releases(work_root, board, config)
    actions = [e for e in recovered if e["merge_task_id"] == merge_task_id]
    assert len(actions) == 1, recovered
    assert actions[0]["action"] == "completed", actions[0]
    assert actions[0]["adopted"] is True, actions[0]

    # Adoption recorded the merge; it never produced a second merge commit.
    assert _merge_count(facts["repo"]) == merges_before + 1
    assert repo_head(facts["repo"]) == merged_head
    final_state = read_release_state(release_dir)
    assert final_state is not None and final_state.stage == "terminal", final_state
    assert final_state.merge_sha == merged_head
    merge_document = _load(release_dir / "merge.json")
    assert merge_document["merge_sha"] == merged_head
    assert merge_document["parents"] == [facts["base"], reviewed_head]
    assert merge_document["command"]["recovered"] is True
    postmerge = _load(release_dir / "postmerge" / "report.json")
    assert postmerge["verdict"] == "pass", postmerge
    assert task_row(board, merge_task_id)["status"] == "done"


# ---------------------------------------------------------------------------
# W4-18: the freeze upper bound and the bounded merge
# ---------------------------------------------------------------------------


def test_w4_18_second_process_writer_waits_out_a_ten_second_freeze(
    tmp_path, board, make_task, make_repo, make_config
):
    repo = make_repo()
    config = make_config(
        board_path=board.board_path,
        repo_path=repo,
        executor_argv=[PYTHON, "-c", "pass"],
    )
    work_root = Path(config.work_root)
    task_id = make_task(title="w4-18 writer")
    before = _row(board, task_id)

    writer_process, _ = _spawn(
        work_root,
        "w4-18-writer",
        PLAIN_WRITER_SCRIPT,
        config,
        {
            "task_id": task_id,
            "ready": str(work_root / "writer.ready"),
            "go": str(work_root / "writer.go"),
            "done": str(work_root / "writer.done.json"),
        },
    )
    freeze_process = None
    try:
        _wait_for(work_root / "writer.ready")
        freeze_process, _ = _spawn(
            work_root,
            "w4-18-freeze",
            FREEZE_SCRIPT,
            config,
            {"seconds": 10.0, "entered": str(work_root / "freeze.entered")},
        )
        _wait_for(work_root / "freeze.entered")
        (work_root / "writer.go").write_text("1", encoding="utf-8")
        _wait_for(work_root / "writer.done.json", timeout=90.0)
        _finish(writer_process, name="w4-18-writer")
        _finish(freeze_process, name="w4-18-freeze")
    finally:
        for process in (freeze_process, writer_process):
            if process is not None and process.poll() is None:
                _kill(process)

    writer = _load(work_root / "writer.done.json")
    assert writer["ok"] is True, writer
    assert writer["error"] is None, writer
    # It really waited for the fence instead of failing fast or timing out.
    assert writer["elapsed"] >= 8.0, writer
    assert writer["elapsed"] < 120.0, writer
    assert _row(board, task_id)["priority"] == before["priority"] + 1


def test_w4_18_merge_timeout_aborts_and_releases_the_fence(
    tmp_path, board, make_task, make_repo
):
    # The constant is a hard ceiling below the host busy_timeout (120s).
    assert MERGE_TIMEOUT <= 60.0
    assert MERGE_TIMEOUT < 120.0
    assert inspect.signature(merge_release).parameters["timeout"].default == MERGE_TIMEOUT

    repo = make_repo()
    git = lambda *args: run_git(list(args), cwd=repo, timeout=60)  # noqa: E731
    base = repo_head(repo)
    # Two sides edit the same line: a real merge conflicts and leaves MERGE_HEAD.
    (repo / "README.txt").write_text("main side\n", encoding="utf-8")
    git("add", "README.txt")
    git("commit", "-q", "-m", "main side")
    main_side = repo_head(repo)
    workspace = tmp_path / "review-wt"
    added = git("worktree", "add", "-q", "-b", "review", str(workspace), base)
    assert added.returncode == 0, added.stderr
    (workspace / "README.txt").write_text("review side\n", encoding="utf-8")
    assert run_git(["add", "README.txt"], cwd=workspace, timeout=60).returncode == 0
    committed = run_git(
        ["commit", "-q", "-m", "review side"], cwd=workspace, timeout=60
    )
    assert committed.returncode == 0, committed.stderr
    reviewed_head = repo_head(workspace)

    task_id = make_task(title="w4-18 timeout")
    identity = board.claim(task_id)
    assert identity is not None
    run_dir = tmp_path / "timeout-release"
    run_dir.mkdir()

    original_run_git = merge_module.run_git

    def timed_out_merge(args, **kwargs):
        if "merge" in args and "--no-ff" in args:
            # Leave a real in-progress merge, then report the timeout.
            original_run_git(list(args), **kwargs)
            raise GitError("git command timed out: merge --no-ff")
        return original_run_git(args, **kwargs)

    merge_module.run_git = timed_out_merge
    try:
        with board.write_fence():
            record = merge_release(
                repo=repo,
                main_ref="refs/heads/main",
                reviewed_head=reviewed_head,
                main_before=main_side,
                allow=("README.txt",),
                run_dir=run_dir,
                identity=identity,
                requirement_id=REQUIREMENT,
            )
            assert board.connection.in_transaction is True
    finally:
        merge_module.run_git = original_run_git

    assert record.ok is False
    assert record.document["command"]["timed_out"] is True, record.document
    assert record.document["verdict"] == "refused"
    # The fence released, main did not move and no merge is left in progress.
    assert board.connection.in_transaction is False
    assert repo_head(repo) == main_side
    probe = run_git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=repo, timeout=30)
    assert probe.returncode != 0
    # A later writer recovers normally in the same connection.
    with board._kbc.write_txn(board.connection):
        board.connection.execute(
            "UPDATE tasks SET priority = priority + 1 WHERE id = ?", (task_id,)
        )
