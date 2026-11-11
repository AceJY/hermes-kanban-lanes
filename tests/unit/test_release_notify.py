"""The optional merge-notification hook: config, facts, idempotency, failures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from hermes_kanban_lanes.config import (
    DEFAULT_NOTIFY_TIMEOUT_SECONDS,
    DEFAULT_POST_MERGE_TIMEOUT_SECONDS,
    DEFAULT_REVIEWER,
    LaneConfig,
    MergeConfig,
)
from hermes_kanban_lanes.contracts import ContractError
from hermes_kanban_lanes.release.merge import (
    merge_notification_sent,
    run_merge_notify,
)

PYTHON = sys.executable

FACT_KEYS = (
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

FACTS = {
    "merge_sha": "a" * 40,
    "merge_tree": "b" * 40,
    "reviewed_head": "c" * 40,
    "reviewed_tree": "d" * 40,
    "main_before": "e" * 40,
    "evidence_index_sha256": "f" * 64,
}


def _config(**overrides):
    document = {
        "schema_version": 1,
        "hermes_source": "/srv/hermes/source",
        "hermes_commit": "a" * 40,
        "boards": {"engineering": "/srv/hermes/kanban/engineering.db"},
        "repositories": {
            "primary": {"path": "/srv/hermes/repos/primary", "trusted": True}
        },
        "executors": {"claude": {"argv": ["claude"]}},
        "work_root": "/srv/hermes/work",
        "max_concurrent": 2,
    }
    document.update(overrides)
    return document


def _review(**merge_overrides):
    merge = {
        "enabled": True,
        "main_ref": "refs/heads/main",
        "post_merge_commands": [["true"]],
        "post_merge_timeout_seconds": DEFAULT_POST_MERGE_TIMEOUT_SECONDS,
    }
    merge.update(merge_overrides)
    return {
        "reviewer": DEFAULT_REVIEWER,
        "max_rework_rounds": 3,
        "merge": merge,
    }


def _notify(tmp_path, repo, commands, **overrides):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    arguments = {
        "repo": repo,
        "run_dir": run_dir,
        "merge_sha": FACTS["merge_sha"],
        "merge_tree": FACTS["merge_tree"],
        "commands": commands,
        "timeout_seconds": 10.0,
        "requirement_id": "HKL-W4",
        "board": "test",
        "merge_task_id": "t_merge",
        "dev_task_id": "t_dev",
        "reviewed_head": FACTS["reviewed_head"],
        "reviewed_tree": FACTS["reviewed_tree"],
        "main_before": FACTS["main_before"],
        "evidence_index_sha256": FACTS["evidence_index_sha256"],
        "authorization_source": "file",
    }
    arguments.update(overrides)
    return run_merge_notify(**arguments), run_dir


def _dump_env_command(target: Path, cwd_target: Path) -> list[str]:
    script = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"keys = {list(FACT_KEYS)!r}\n"
        "Path(sys.argv[1]).write_text(json.dumps({k: os.environ.get(k) for k in keys}))\n"
        "Path(sys.argv[2]).write_text(os.getcwd())\n"
        "Path(sys.argv[3]).write_text(os.environ.get('HKL_TEST_MARKER', ''))\n"
    )
    return [
        PYTHON,
        "-c",
        script,
        str(target),
        str(cwd_target),
        str(target.with_name("marker.txt")),
    ]


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_notify_defaults_are_disabled_and_absent_from_round_trip():
    config = LaneConfig.from_dict(_config(review=_review()))
    merge = config.review.merge
    assert merge.notify_commands == ()
    assert merge.notify_timeout_seconds == DEFAULT_NOTIFY_TIMEOUT_SECONDS == 60.0
    # Defaults stay omitted so existing configurations round-trip unchanged.
    assert "notify_commands" not in config.to_dict()["review"]["merge"]
    assert "notify_timeout_seconds" not in config.to_dict()["review"]["merge"]


def test_notify_config_round_trips():
    review = _review(
        notify_commands=[["feishu", "dm"], ["true"]],
        notify_timeout_seconds=5,
    )
    document = _config(review=review)
    config = LaneConfig.from_dict(document)
    assert config.to_dict() == document
    assert config.review.merge.notify_commands == (
        ("feishu", "dm"),
        ("true",),
    )
    assert config.review.merge.notify_timeout_seconds == 5.0


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("notify_commands", "feishu dm", "notify_commands"),
        ("notify_commands", [["ok"], []], "notify_commands[1]"),
        ("notify_commands", [["ok", 7]], "notify_commands[0][1]"),
        ("notify_commands", [[]], "notify_commands[0]"),
        ("notify_timeout_seconds", 0, "notify_timeout_seconds"),
        ("notify_timeout_seconds", -1, "notify_timeout_seconds"),
        ("notify_timeout_seconds", True, "notify_timeout_seconds"),
        ("notify_timeout_seconds", "60", "notify_timeout_seconds"),
    ],
)
def test_invalid_notify_config_is_rejected_with_a_readable_path(
    field, value, fragment
):
    document = _config(review=_review(**{field: value}))
    with pytest.raises(ContractError) as excinfo:
        LaneConfig.from_dict(document)
    assert fragment in str(excinfo.value)


def test_merge_config_validates_notify_arguments_defensively():
    with pytest.raises(ContractError):
        MergeConfig(notify_commands=[[]])
    merge = MergeConfig(
        notify_commands=[["feishu", "dm"]], notify_timeout_seconds=5
    )
    assert merge.notify_commands == (("feishu", "dm"),)
    assert merge.notify_timeout_seconds == 5.0


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def test_disabled_notify_creates_no_files_and_runs_nothing(tmp_path):
    result, run_dir = _notify(tmp_path, tmp_path, [])
    assert result["verdict"] == "skipped"
    assert result["notified"] is False
    assert not (run_dir / "notify").exists()


def test_notify_runs_once_with_daemon_env_and_merge_facts(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HKL_TEST_MARKER", "inherited")
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "facts.json"
    cwd_target = tmp_path / "cwd.txt"
    result, run_dir = _notify(
        tmp_path, repo, [_dump_env_command(target, cwd_target)]
    )

    assert result["verdict"] == "pass", result
    assert result["notified"] is True
    facts = json.loads(target.read_text(encoding="utf-8"))
    assert facts == {
        "HKL_REQUIREMENT_ID": "HKL-W4",
        "HKL_BOARD": "test",
        "HKL_MERGE_TASK_ID": "t_merge",
        "HKL_DEV_TASK_ID": "t_dev",
        "HKL_MERGE_SHA": FACTS["merge_sha"],
        "HKL_MERGE_TREE": FACTS["merge_tree"],
        "HKL_REVIEWED_HEAD": FACTS["reviewed_head"],
        "HKL_REVIEWED_TREE": FACTS["reviewed_tree"],
        "HKL_MAIN_BEFORE": FACTS["main_before"],
        "HKL_EVIDENCE_INDEX_SHA256": FACTS["evidence_index_sha256"],
        "HKL_AUTHORIZATION_SOURCE": "file",
        "HKL_REPO": str(repo),
        "HKL_RUN_DIR": str(run_dir),
    }
    # cwd is the merged repository root, and the daemon's own environment is
    # inherited (notification CLIs need HOME/credentials).
    assert cwd_target.read_text(encoding="utf-8") == str(repo)
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == "inherited"

    report = json.loads(
        (run_dir / "notify" / "report.json").read_text(encoding="utf-8")
    )
    assert report["verdict"] == "pass"
    assert report["merge_sha"] == FACTS["merge_sha"]
    assert report["commands"][0]["exit_code"] == 0
    assert report["commands"][0]["timed_out"] is False
    assert report["commands"][0]["stdout_log"] == "notify/0.stdout.log"
    assert report["commands"][0]["stdout_sha256"]
    assert merge_notification_sent(run_dir, FACTS["merge_sha"]) is True


def test_notify_is_idempotent_per_merge_sha(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    counter = tmp_path / "calls.txt"
    command = [
        PYTHON,
        "-c",
        (
            "from pathlib import Path\n"
            f"p = Path({str(counter)!r})\n"
            "p.write_text((p.read_text() if p.exists() else '') + 'x')\n"
        ),
    ]
    first, run_dir = _notify(tmp_path, repo, [command])
    second, _ = _notify(tmp_path, repo, [command])
    assert first["verdict"] == "pass"
    assert second.get("skipped") is True
    assert second["merge_sha"] == FACTS["merge_sha"]
    assert counter.read_text(encoding="utf-8") == "x"

    # A different merge_sha is a different notification.
    third, _ = _notify(
        tmp_path, repo, [command], merge_sha="9" * 40
    )
    assert third["verdict"] == "pass"
    assert counter.read_text(encoding="utf-8") == "xx"


def test_nonzero_notify_command_records_fail_but_does_not_raise(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result, run_dir = _notify(
        tmp_path, repo, [[PYTHON, "-c", "import sys; sys.exit(3)"]]
    )
    assert result["verdict"] == "fail"
    assert result["reasons"] == ["notify-failed:0"]
    report = json.loads(
        (run_dir / "notify" / "report.json").read_text(encoding="utf-8")
    )
    assert report["verdict"] == "fail"
    assert report["commands"][0]["exit_code"] == 3
    assert report["commands"][0]["timed_out"] is False


def test_timed_out_notify_command_records_the_timeout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result, run_dir = _notify(
        tmp_path,
        repo,
        [[PYTHON, "-c", "import time; time.sleep(30)"]],
        timeout_seconds=0.2,
    )
    assert result["verdict"] == "fail"
    assert result["reasons"] == ["notify-timeout:0"]
    report = json.loads(
        (run_dir / "notify" / "report.json").read_text(encoding="utf-8")
    )
    assert report["commands"][0]["timed_out"] is True
