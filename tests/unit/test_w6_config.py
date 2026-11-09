"""W6 D3: optional ``service`` / ``logging`` blocks are additive and strict."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_kanban_lanes.config import (
    DEFAULT_BACKUPS,
    DEFAULT_KEEP_FAILED_RUNS,
    DEFAULT_MAX_LOG_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SERVICE_LABEL,
    DEFAULT_SERVICE_RESTART,
    LaneConfig,
    load_config,
)
from hermes_kanban_lanes.contracts import ContractError


def _document(tmp_path: Path) -> dict:
    return {
        "schema_version": 1,
        "hermes_source": str(tmp_path / "hermes"),
        "hermes_commit": "a" * 40,
        "boards": {"engineering": str(tmp_path / "kanban" / "engineering.db")},
        "repositories": {
            "primary": {"path": str(tmp_path / "repo"), "trusted": True}
        },
        "executors": {"claude": {"argv": ["claude"]}},
        "work_root": str(tmp_path / "work"),
        "max_concurrent": 2,
    }


def _write(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_missing_blocks_keep_existing_behaviour(tmp_path):
    config = LaneConfig.from_dict(_document(tmp_path))
    assert config.service is None
    assert config.logging is None
    assert "service" not in config.to_dict()
    assert "logging" not in config.to_dict()

    service = config.service_options()
    assert service.platform is None
    assert service.label == DEFAULT_SERVICE_LABEL
    assert service.restart == DEFAULT_SERVICE_RESTART
    assert service.log_root is None
    assert service.resolved_log_root(config.work_root) == f"{config.work_root}/logs"

    logging_config = config.logging_options()
    assert logging_config.retention_days == DEFAULT_RETENTION_DAYS
    assert logging_config.max_total_bytes == DEFAULT_MAX_TOTAL_BYTES
    assert logging_config.max_log_bytes == DEFAULT_MAX_LOG_BYTES
    assert logging_config.backups == DEFAULT_BACKUPS
    assert logging_config.keep_failed_runs == DEFAULT_KEEP_FAILED_RUNS


def test_explicit_blocks_round_trip(tmp_path):
    document = _document(tmp_path)
    document["service"] = {
        "platform": "launchd",
        "label": "com.example.lane",
        "log_root": str(tmp_path / "service-logs"),
        "restart": "always",
    }
    document["logging"] = {
        "retention_days": 3,
        "max_total_bytes": 1048576,
        "max_log_bytes": 65536,
        "backups": 1,
        "keep_failed_runs": 0,
    }
    config = LaneConfig.from_dict(document)
    rendered = config.to_dict()
    assert rendered["service"] == document["service"]
    assert rendered["logging"] == document["logging"]
    assert LaneConfig.from_dict(rendered).to_dict() == rendered


def test_partial_service_block_uses_defaults(tmp_path):
    document = _document(tmp_path)
    document["service"] = {}
    config = LaneConfig.from_dict(document)
    assert config.service is not None
    assert config.service.platform is None
    assert config.service.label == DEFAULT_SERVICE_LABEL
    assert config.service.restart == DEFAULT_SERVICE_RESTART
    # An empty explicit block is still emitted, byte-stable across reload.
    assert config.to_dict()["service"] == {
        "platform": None,
        "label": DEFAULT_SERVICE_LABEL,
        "log_root": None,
        "restart": DEFAULT_SERVICE_RESTART,
    }


def test_partial_logging_block_uses_defaults(tmp_path):
    document = _document(tmp_path)
    document["logging"] = {"retention_days": 30}
    logging_config = LaneConfig.from_dict(document).logging_options()
    assert logging_config.retention_days == 30
    assert logging_config.max_total_bytes == DEFAULT_MAX_TOTAL_BYTES
    assert logging_config.max_log_bytes == DEFAULT_MAX_LOG_BYTES


@pytest.mark.parametrize(
    "block, field, value",
    [
        ("logging", "retention_days", 0),
        ("logging", "retention_days", 1.5),
        ("logging", "retention_days", True),
        ("logging", "max_total_bytes", 1048575),
        ("logging", "max_log_bytes", 65535),
        ("logging", "backups", 0),
        ("logging", "keep_failed_runs", -1),
        ("service", "platform", "windows"),
        ("service", "platform", 3),
        ("service", "restart", "sometimes"),
        ("service", "label", ""),
        ("service", "label", 7),
        ("service", "log_root", "relative/logs"),
        ("service", "log_root", 9),
    ],
)
def test_out_of_bounds_and_bad_types_are_rejected(tmp_path, block, field, value):
    document = _document(tmp_path)
    document[block] = {field: value}
    with pytest.raises(ContractError):
        LaneConfig.from_dict(document)


def test_unknown_keys_in_new_blocks_are_rejected(tmp_path):
    for block, key in (
        ("service", "port"),
        ("logging", "compress"),
    ):
        document = _document(tmp_path)
        document[block] = {key: 1}
        with pytest.raises(ContractError) as excinfo:
            LaneConfig.from_dict(document)
        assert "unknown field" in str(excinfo.value)


def test_keep_failed_runs_zero_is_legal(tmp_path):
    document = _document(tmp_path)
    document["logging"] = {"keep_failed_runs": 0}
    assert LaneConfig.from_dict(document).logging_options().keep_failed_runs == 0


def test_validate_config_accepts_new_blocks_without_rewriting(tmp_path):
    from hermes_kanban_lanes.cli import main

    document = _document(tmp_path)
    document["service"] = {"platform": "systemd"}
    document["logging"] = {"retention_days": 7}
    path = _write(tmp_path, document)
    before = path.read_bytes()
    before_stat = path.stat()
    assert main(["validate-config", str(path)]) == 0
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_validate_config_rejects_new_block_bounds_at_cli(tmp_path, capsys):
    from hermes_kanban_lanes.cli import main

    document = _document(tmp_path)
    document["logging"] = {"retention_days": 0}
    path = _write(tmp_path, document)
    assert main(["validate-config", str(path)]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["valid"] is False
    assert "retention_days" in payload["error"]


def test_load_config_does_not_create_paths(tmp_path):
    document = _document(tmp_path)
    document["service"] = {"log_root": str(tmp_path / "never-created")}
    path = _write(tmp_path, document)
    load_config(path)
    assert not (tmp_path / "never-created").exists()
    assert not (tmp_path / "work").exists()


def test_schema_newer_than_supported_fails_closed(tmp_path):
    document = _document(tmp_path)
    document["schema_version"] = 2
    path = _write(tmp_path, document)
    before = path.read_bytes()
    with pytest.raises(ContractError) as excinfo:
        load_config(path)
    assert "schema-newer-than-supported" in str(excinfo.value)
    assert path.read_bytes() == before
