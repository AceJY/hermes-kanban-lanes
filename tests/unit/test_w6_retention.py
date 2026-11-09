"""W6 D8: daemon.log rotation and safe run/workspace retention."""

from __future__ import annotations

import json
import os
from pathlib import Path

from hermes_kanban_lanes.config import LoggingConfig
from hermes_kanban_lanes.runtime import retention

NOW = 1_000_000_000
DAY = 86400


def _logging_config(**overrides) -> LoggingConfig:
    values = {
        "retention_days": 14,
        "max_total_bytes": 1048576,
        "max_log_bytes": 65536,
        "backups": 2,
        "keep_failed_runs": 1,
    }
    values.update(overrides)
    return LoggingConfig(**values)


def _make_run(
    work_root: Path,
    task: str,
    run: str,
    *,
    stage: str = "terminal",
    finished_at: int | None = NOW - 30 * DAY,
    category: str = "success",
    workspace: Path | None = None,
    child: dict | None = None,
    size: int = 64,
    unreadable_state: bool = False,
) -> Path:
    run_dir = work_root / "runs" / task / run
    run_dir.mkdir(parents=True)
    if unreadable_state:
        (run_dir / "state.json").write_text("{not json", encoding="utf-8")
    else:
        state = {
            "schema_version": 1,
            "requirement_id": "HKL-W3",
            "identity": {
                "schema_version": 1,
                "task_id": task,
                "run_id": 1,
                "claim_lock": "claim",
            },
            "board": "test",
            "task_id": task,
            "run_id": 1,
            "claim_lock": "claim",
            "run_dir": str(run_dir),
            "workspace": str(workspace) if workspace is not None else "",
            "stage": stage,
            "child": child,
            "round": 1,
            "updated_at": 0,
        }
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    run_document = {
        "finished_at": finished_at,
        "category": category,
        "workspace": str(workspace) if workspace is not None else None,
        "cleanup": {},
    }
    (run_dir / "run.json").write_text(json.dumps(run_document), encoding="utf-8")
    (run_dir / "blob.bin").write_bytes(b"x" * max(0, size))
    return run_dir


# ---------------------------------------------------------------------------
# prune_runs
# ---------------------------------------------------------------------------


def test_prune_by_age_protects_recent_failures(tmp_path):
    old_success = _make_run(tmp_path, "t1", "r1", finished_at=NOW - 40 * DAY)
    old_failure_kept = _make_run(
        tmp_path, "t2", "r1", finished_at=NOW - 30 * DAY, category="code_failure"
    )
    old_failure_deleted = _make_run(
        tmp_path, "t3", "r1", finished_at=NOW - 50 * DAY, category="code_failure"
    )
    fresh = _make_run(tmp_path, "t4", "r1", finished_at=NOW - DAY)

    report = retention.prune_runs(tmp_path, _logging_config(), now=NOW)

    deleted = {item["run_dir"] for item in report["deleted"]}
    assert str(old_success) in deleted
    assert str(old_failure_deleted) in deleted
    assert str(old_failure_kept) not in deleted
    assert str(fresh) not in deleted
    assert old_success.exists() is False
    assert old_failure_deleted.exists() is False
    assert old_failure_kept.is_dir()
    assert fresh.is_dir()
    assert {item["run_dir"] for item in report["retained_failed"]} == {
        str(old_failure_kept)
    }
    assert report["freed_bytes"] > 0
    assert report["schema_version"] == 1
    assert report["now"] == NOW


def test_prune_preview_matches_prune_runs_and_deletes_nothing(tmp_path):
    old_success = _make_run(tmp_path, "t1", "r1", finished_at=NOW - 40 * DAY)
    old_failure_kept = _make_run(
        tmp_path, "t2", "r1", finished_at=NOW - 30 * DAY, category="code_failure"
    )
    fresh = _make_run(tmp_path, "t3", "r1", finished_at=NOW - DAY)
    non_terminal = _make_run(tmp_path, "t4", "r1", stage="claimed")

    before = {path for path in tmp_path.rglob("*")}
    preview = retention.prune_preview(tmp_path, _logging_config(), now=NOW)
    after = {path for path in tmp_path.rglob("*")}
    assert before == after, "doctor preview must not delete or create files"
    assert preview["prunable_runs"] == 1
    assert preview["prunable_bytes"] == sum(
        path.stat().st_size for path in old_success.rglob("*") if path.is_file()
    )
    assert preview["skipped_runs"] == 1
    assert preview["retained_failed_runs"] == 1
    assert preview["total_runs"] == 4

    report = retention.prune_runs(tmp_path, _logging_config(), now=NOW)
    deleted = {item["run_dir"] for item in report["deleted"]}
    assert deleted == {str(old_success)}
    assert old_failure_kept.is_dir()
    assert fresh.is_dir()
    assert non_terminal.is_dir()


def test_prune_preview_counts_size_pressure_candidates(tmp_path):
    oldest = _make_run(tmp_path, "t1", "r1", finished_at=NOW - 2 * DAY, size=600_000)
    newer = _make_run(tmp_path, "t2", "r1", finished_at=NOW - DAY, size=600_000)
    _make_run(
        tmp_path, "t3", "r1", finished_at=NOW - DAY, category="code_failure", size=1
    )

    preview = retention.prune_preview(
        tmp_path, _logging_config(max_total_bytes=1048576), now=NOW
    )
    assert preview["prunable_runs"] == 1
    assert preview["prunable_bytes"] >= 600_000
    assert oldest.is_dir() and newer.is_dir()

    report = retention.prune_runs(
        tmp_path, _logging_config(max_total_bytes=1048576), now=NOW
    )
    assert {item["run_dir"] for item in report["deleted"]} == {str(oldest)}


def test_non_terminal_unreadable_and_running_runs_are_skipped(tmp_path):
    non_terminal = _make_run(tmp_path, "t1", "r1", stage="claimed")
    missing_finished = _make_run(tmp_path, "t2", "r1", finished_at=None)
    unreadable = _make_run(tmp_path, "t3", "r1", unreadable_state=True)
    running = _make_run(
        tmp_path,
        "t4",
        "r1",
        child={"pid": os.getpid(), "pgid": os.getpid(), "fingerprint": None},
    )

    report = retention.prune_runs(tmp_path, _logging_config(), now=NOW)

    assert report["deleted"] == []
    reasons = {item["reason"] for item in report["skipped"]}
    assert {"non-terminal", "missing-finished-at", "unreadable-state", "running"} <= reasons
    for run_dir in (non_terminal, missing_finished, unreadable, running):
        assert run_dir.is_dir()


def test_total_size_pruning_is_oldest_first(tmp_path):
    first = _make_run(tmp_path, "t1", "r1", finished_at=NOW - 3 * DAY, size=700_000)
    second = _make_run(tmp_path, "t2", "r1", finished_at=NOW - 2 * DAY, size=700_000)
    third = _make_run(tmp_path, "t3", "r1", finished_at=NOW - DAY, size=700_000)

    report = retention.prune_runs(
        tmp_path, _logging_config(max_total_bytes=1048576, keep_failed_runs=0), now=NOW
    )

    deleted = [item["run_dir"] for item in report["deleted"]]
    assert deleted == [str(first), str(second)]
    assert third.is_dir()
    assert report["total_bytes_before"] > 1048576
    assert report["total_bytes_after"] <= 1048576


def test_workspace_deleted_only_when_termination_is_proven(tmp_path, monkeypatch):
    workspace_ok = tmp_path / "workspaces" / "t1"
    workspace_ok.mkdir(parents=True)
    (workspace_ok / "file.txt").write_text("x", encoding="utf-8")
    run_ok = _make_run(
        tmp_path,
        "t1",
        "r1",
        finished_at=NOW - 30 * DAY,
        workspace=workspace_ok,
        child={"pid": 111, "pgid": 111, "fingerprint": "fp"},
    )

    workspace_uncertain = tmp_path / "workspaces" / "t2"
    workspace_uncertain.mkdir(parents=True)
    run_uncertain = _make_run(
        tmp_path,
        "t2",
        "r1",
        finished_at=NOW - 30 * DAY,
        workspace=workspace_uncertain,
        child={"pid": 222, "pgid": 222, "fingerprint": "fp"},
    )

    def fake_prove(pid, fingerprint, pgid):
        return (pid == 111, {"pid": pid})

    monkeypatch.setattr(retention, "prove_terminated", fake_prove)

    report = retention.prune_runs(
        tmp_path, _logging_config(keep_failed_runs=0), now=NOW
    )

    assert not run_ok.exists()
    assert not run_uncertain.exists()
    assert not workspace_ok.exists()
    assert workspace_uncertain.is_dir()
    assert report["workspace_skipped"] == [
        {
            "workspace": str(workspace_uncertain),
            "reason": "workspace-prune-skipped",
        }
    ]


def test_workspace_outside_the_lane_tree_is_never_deleted(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    _make_run(
        tmp_path,
        "t1",
        "r1",
        finished_at=NOW - 30 * DAY,
        workspace=outside,
        child={"pid": 111, "pgid": 111, "fingerprint": "fp"},
    )
    monkeypatch.setattr(
        retention, "prove_terminated", lambda *args, **kwargs: (True, {})
    )

    report = retention.prune_runs(
        tmp_path, _logging_config(keep_failed_runs=0), now=NOW
    )

    assert report["deleted"]
    assert (outside / "keep.txt").is_file()


def test_failed_runs_protected_even_under_size_pressure(tmp_path):
    failure = _make_run(
        tmp_path, "t1", "r1", finished_at=NOW - 5 * DAY,
        category="code_failure", size=900_000,
    )
    success = _make_run(
        tmp_path, "t2", "r1", finished_at=NOW - 4 * DAY, size=900_000
    )
    report = retention.prune_runs(
        tmp_path, _logging_config(max_total_bytes=1048576, keep_failed_runs=1), now=NOW
    )
    deleted = {item["run_dir"] for item in report["deleted"]}
    assert str(failure) not in deleted
    assert str(success) in deleted
    assert failure.is_dir()
    assert not success.exists()


def test_maybe_prune_runs_is_interval_gated(tmp_path):
    _make_run(tmp_path, "t1", "r1", finished_at=NOW - 30 * DAY)
    first = retention.maybe_prune_runs(tmp_path, _logging_config(), now=NOW)
    assert first["deleted"]
    assert (tmp_path / "logs" / "prune-state.json").is_file()
    assert (tmp_path / "logs" / "prune-report.json").is_file()

    second = retention.maybe_prune_runs(
        tmp_path, _logging_config(), now=NOW + 10
    )
    assert second["skipped"] == "interval"
    assert second["last_pruned_at"] == NOW

    third = retention.maybe_prune_runs(
        tmp_path, _logging_config(), now=NOW + 3601
    )
    assert third["skipped"] == []
    assert third["deleted"] == []


# ---------------------------------------------------------------------------
# daemon.log rotation
# ---------------------------------------------------------------------------


def test_rotating_log_handler_respects_backups(tmp_path):
    log_path = tmp_path / "logs" / "daemon.log"
    messages = [f"{index:02d}-" + "x" * 120 for index in range(10)]
    for message in messages:
        retention.rotate_daemon_log(
            log_path, message, max_bytes=300, backups=2
        )
    assert log_path.is_file()
    rotated = retention.rotate_count(log_path)
    assert 1 <= rotated <= 2

    lines: list[str] = []
    for path in sorted(log_path.parent.glob("daemon.log*")):
        lines.extend(path.read_text(encoding="utf-8").splitlines())
    # Rotation is bounded: old lines may be dropped, never duplicated.
    assert len(lines) <= len(messages)
    assert messages[-1] in lines
    assert len(lines) == len(set(lines))


def test_last_error_line(tmp_path):
    log_path = tmp_path / "daemon.log"
    log_path.write_text(
        json.dumps({"tick": 1, "errors": []})
        + "\n"
        + json.dumps({"tick": 2, "errors": ["board:test:boom"]})
        + "\n"
        + json.dumps({"tick": 3, "errors": []})
        + "\n",
        encoding="utf-8",
    )
    record = retention.last_error_line(log_path)
    assert record == {"tick": 2, "errors": ["board:test:boom"]}
    assert retention.last_error_line(tmp_path / "missing.log") is None


def test_last_error_line_accepts_single_error_field(tmp_path):
    log_path = tmp_path / "daemon.log"
    log_path.write_text('{"error": "daemon-fatal"}\n', encoding="utf-8")
    assert retention.last_error_line(log_path) == {"error": "daemon-fatal"}
