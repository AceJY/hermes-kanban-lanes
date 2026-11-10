"""W11c: terminal tmp cleanup and retention protection for releasable runs.

A successful run that is still inside the retention window (or whose review
has passed but whose release has not merged) must not be removed by byte
pressure.  Byte pressure first reclaims disposable ``tmp/`` and ``home/``
caches; only ``retention_days`` may remove the whole run directory.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_kanban_lanes.config import LoggingConfig
from hermes_kanban_lanes.runtime import retention

NOW = 1_000_000_000
DAY = 86400
MIB = 1024 * 1024


def _logging_config(**overrides) -> LoggingConfig:
    values = {
        "retention_days": 14,
        "max_total_bytes": 2 * 1024 * MIB,
        "max_log_bytes": 65536,
        "backups": 2,
        "keep_failed_runs": 1,
    }
    values.update(overrides)
    return LoggingConfig(**values)


def _sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)


def _make_run(
    work_root: Path,
    *,
    task: str = "t1",
    run: str = "1-aaaa",
    task_id: str = "t1",
    run_id: int = 1,
    finished_at: int | None = NOW - DAY,
    category: str = "success",
    size: int = 64,
    tmp_bytes: int = 0,
    cache_bytes: int = 0,
    evidence: bool = True,
    attested: bool = True,
    code_commit: str | None = None,
) -> Path:
    run_dir = work_root / "runs" / task / run
    run_dir.mkdir(parents=True)
    state = {
        "schema_version": 1,
        "requirement_id": "HKL-W3",
        "identity": {
            "schema_version": 1,
            "task_id": task_id,
            "run_id": run_id,
            "claim_lock": "claim",
        },
        "board": "test",
        "task_id": task_id,
        "run_id": run_id,
        "claim_lock": "claim",
        "run_dir": str(run_dir),
        "workspace": "",
        "stage": "terminal",
        "child": None,
        "round": 1,
        "updated_at": 0,
    }
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    run_document = {
        "finished_at": finished_at,
        "category": category,
        "workspace": None,
        "cleanup": {},
        "code_commit": code_commit,
    }
    if attested:
        run_document["identity"] = state["identity"]
    (run_dir / "run.json").write_text(json.dumps(run_document), encoding="utf-8")
    (run_dir / "blob.bin").write_bytes(b"x" * max(0, size))
    if evidence:
        (run_dir / "evidence").mkdir()
        (run_dir / "evidence" / "index.json").write_text(
            json.dumps({"verdict": "pass"}), encoding="utf-8"
        )
    if tmp_bytes:
        _sparse(run_dir / "tmp" / "pytest-of-root" / "blob", tmp_bytes)
    if cache_bytes:
        home = run_dir / "home"
        _sparse(home / ".npm" / "_cacache" / "blob", cache_bytes)
        (home / ".claude" / "sessions").mkdir(parents=True)
        (home / ".claude" / "sessions" / "session.jsonl").write_text(
            "keep\n", encoding="utf-8"
        )
        (home / ".claude.json").write_text("{}\n", encoding="utf-8")
    return run_dir


def _approve(work_root: Path, task_id: str, dev_run_id: int, head: str | None = None):
    ledger = work_root / "ledger" / f"{task_id}.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": task_id,
                "reviews": [
                    {
                        "review_run_id": 99,
                        "dev_run_id": dev_run_id,
                        "verdict": "pass",
                        "reviewed_head": head,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _record_merge(
    work_root: Path,
    *,
    dev_task_id: str,
    merge_task_id: str,
    reviewed_head: str,
) -> None:
    cards = work_root / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / f"{merge_task_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "card_kind": "merge",
                "lineage": merge_task_id,
                "closure_for": dev_task_id,
            }
        ),
        encoding="utf-8",
    )
    run_dir = work_root / "runs" / merge_task_id / "900-merge"
    run_dir.mkdir(parents=True)
    (run_dir / "merge.json").write_text(
        json.dumps({"verdict": "merged", "reviewed_head": reviewed_head}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# tmp cleanup
# ---------------------------------------------------------------------------


def test_terminal_run_tmp_is_removed(tmp_path):
    run_dir = _make_run(tmp_path, tmp_bytes=100 * MIB, evidence=True)

    report = retention.prune_runs(tmp_path, _logging_config(), now=NOW)

    assert not (run_dir / "tmp").exists()
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "evidence" / "index.json").is_file()
    assert run_dir.is_dir()
    assert str(run_dir) not in {item["run_dir"] for item in report["deleted"]}
    assert report["tmp_freed_bytes"] >= 100 * MIB
    assert report["tmp_removed"] == [
        {"run_dir": str(run_dir), "bytes": report["tmp_freed_bytes"]}
    ]


def test_remove_run_tmp_is_idempotent(tmp_path):
    run_dir = _make_run(tmp_path, tmp_bytes=MIB)

    first = retention.remove_run_tmp(run_dir)
    second = retention.remove_run_tmp(run_dir)

    assert first >= MIB
    assert second == 0
    assert not (run_dir / "tmp").exists()
    assert (run_dir / "run.json").is_file()


# ---------------------------------------------------------------------------
# retention-window protection
# ---------------------------------------------------------------------------


def test_recent_success_run_is_protected(tmp_path):
    first = _make_run(
        tmp_path, task="t1", run="1-aaaa", task_id="t1", run_id=1,
        finished_at=NOW - DAY, size=900_000, tmp_bytes=MIB,
    )
    second = _make_run(
        tmp_path, task="t2", run="2-bbbb", task_id="t2", run_id=2,
        finished_at=NOW - 2 * DAY, size=900_000, tmp_bytes=MIB,
    )
    config = _logging_config(max_total_bytes=1048576, keep_failed_runs=0)

    report = retention.prune_runs(tmp_path, config, now=NOW)

    deleted = {item["run_dir"] for item in report["deleted"]}
    assert str(first) not in deleted
    assert str(second) not in deleted
    assert first.is_dir() and second.is_dir()
    assert (first / "run.json").is_file()
    assert (first / "evidence" / "index.json").is_file()
    assert not (first / "tmp").exists()
    assert not (second / "tmp").exists()


def test_pending_release_run_is_protected_without_evidence(tmp_path):
    """A passing review alone keeps the run until its release merges."""

    run_dir = _make_run(
        tmp_path,
        size=1_100_000,
        evidence=False,
        attested=False,
        finished_at=NOW - DAY,
    )
    _approve(tmp_path, "t1", dev_run_id=1)

    report = retention.prune_runs(
        tmp_path, _logging_config(max_total_bytes=1048576, keep_failed_runs=0), now=NOW
    )

    assert str(run_dir) not in {item["run_dir"] for item in report["deleted"]}
    assert run_dir.is_dir()
    assert (run_dir / "run.json").is_file()
    assert report["protected_pending_release"] == [str(run_dir)]


def test_completed_merge_releases_the_protection(tmp_path):
    run_dir = _make_run(
        tmp_path,
        size=1_100_000,
        evidence=False,
        attested=False,
        finished_at=NOW - DAY,
        code_commit="deadbeef",
    )
    _approve(tmp_path, "t1", dev_run_id=1, head="deadbeef")
    _record_merge(
        tmp_path,
        dev_task_id="t1",
        merge_task_id="t1-merge",
        reviewed_head="deadbeef",
    )

    report = retention.prune_runs(
        tmp_path, _logging_config(max_total_bytes=1048576, keep_failed_runs=0), now=NOW
    )

    assert str(run_dir) in {item["run_dir"] for item in report["deleted"]}
    assert not run_dir.exists()


# ---------------------------------------------------------------------------
# age and idempotency
# ---------------------------------------------------------------------------


def test_expired_run_is_deleted(tmp_path):
    run_dir = _make_run(
        tmp_path, finished_at=NOW - 40 * DAY, tmp_bytes=MIB, evidence=True
    )

    report = retention.prune_runs(tmp_path, _logging_config(), now=NOW)

    assert str(run_dir) in {item["run_dir"] for item in report["deleted"]}
    assert not run_dir.exists()
    # tmp is reclaimed as part of the same pass even though the directory goes.
    assert report["tmp_freed_bytes"] >= MIB


def test_repeated_prune_is_idempotent(tmp_path):
    run_dir = _make_run(
        tmp_path,
        finished_at=NOW - DAY,
        tmp_bytes=MIB,
        cache_bytes=MIB,
        evidence=True,
    )
    config = _logging_config(max_total_bytes=1048576, keep_failed_runs=0)

    first = retention.prune_runs(tmp_path, config, now=NOW)
    second = retention.prune_runs(tmp_path, config, now=NOW)

    assert first["tmp_freed_bytes"] >= MIB
    assert first["home_cache_freed_bytes"] >= MIB
    assert second["tmp_removed"] == []
    assert second["home_cache_removed"] == []
    assert second["deleted"] == []
    assert run_dir.is_dir()
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "evidence" / "index.json").is_file()
    # Session logs and identity survive the cache purge.
    assert (run_dir / "home" / ".claude" / "sessions" / "session.jsonl").is_file()
    assert (run_dir / "home" / ".claude.json").is_file()
