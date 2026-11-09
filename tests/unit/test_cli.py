"""Unit tests for the read-only validation CLI (spec 07 section 5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_kanban_lanes import __version__
from hermes_kanban_lanes.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples"
VALID_CONFIG = EXAMPLES / "config.json"
VALID_CARD = EXAMPLES / "implementation-card.json"

VALID_CONFIG_DOCUMENT = {
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


def _write(tmp_path, document, name):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _stdout_json(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def _stderr_json(capsys):
    captured = capsys.readouterr()
    assert captured.out == ""
    return json.loads(captured.err), captured.err


# ---------------------------------------------------------------------------
# Version and argument handling
# ---------------------------------------------------------------------------


def test_version_prints_installed_metadata_version(capsys):
    assert main(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == __version__
    assert captured.err == ""


def test_missing_command_exits_two():
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_unknown_command_exits_two():
    with pytest.raises(SystemExit) as excinfo:
        main(["run"])
    assert excinfo.value.code == 2


def test_validate_card_requires_config_argument():
    with pytest.raises(SystemExit) as excinfo:
        main(["validate-card", str(VALID_CARD)])
    assert excinfo.value.code == 2


def test_no_execution_stub_commands_are_offered():
    for command in ("run", "tick", "doctor"):
        with pytest.raises(SystemExit) as excinfo:
            main([command])
        assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Successful validation
# ---------------------------------------------------------------------------


def test_validate_config_success_is_minimal_json(capsys):
    assert main(["validate-config", str(VALID_CONFIG)]) == 0
    payload = _stdout_json(capsys)
    assert payload == {"valid": True, "schema_version": 1, "kind": "config"}
    # The full configuration/body is never echoed.
    assert "hermes_source" not in payload
    assert "/srv/hermes" not in json.dumps(payload)


def test_validate_card_success_is_minimal_json(capsys):
    assert (
        main(
            [
                "validate-card",
                str(VALID_CARD),
                "--config",
                str(VALID_CONFIG),
            ]
        )
        == 0
    )
    payload = _stdout_json(capsys)
    assert payload == {"valid": True, "schema_version": 1, "kind": "card"}


def test_validate_tmp_files_with_no_side_effects(tmp_path, capsys):
    missing = tmp_path / "missing-tree"
    config_document = dict(VALID_CONFIG_DOCUMENT)
    config_document["hermes_source"] = str(missing / "source")
    config_document["boards"] = {
        "engineering": str(missing / "kanban" / "engineering.db")
    }
    config_path = _write(tmp_path, config_document, "config.json")

    card_document = {
        "schema_version": 1,
        "card_kind": "review",
        "lineage": "lineage-1",
        "closure_for": None,
        "executor": "claude",
        "repository": "primary",
        "allow": ["src/"],
        "commands": [],
        "risk": "normal",
    }
    card_path = _write(tmp_path, card_document, "card.json")

    before = sorted(entry.name for entry in tmp_path.iterdir())
    assert main(["validate-config", str(config_path)]) == 0
    capsys.readouterr()
    assert (
        main(["validate-card", str(card_path), "--config", str(config_path)]) == 0
    )
    capsys.readouterr()
    after = sorted(entry.name for entry in tmp_path.iterdir())
    assert before == after
    assert not missing.exists()


# ---------------------------------------------------------------------------
# Rejections: exit 2, JSON on stderr, no traceback
# ---------------------------------------------------------------------------


def test_invalid_json_config_exits_two_with_json_error(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    assert main(["validate-config", str(path)]) == 2
    payload, raw = _stderr_json(capsys)
    assert payload["valid"] is False
    assert "invalid JSON" in payload["error"]
    assert "Traceback" not in raw


def test_unknown_config_field_exits_two_with_field_path(tmp_path, capsys):
    document = dict(VALID_CONFIG_DOCUMENT)
    document["extra"] = True
    path = _write(tmp_path, document, "config.json")
    assert main(["validate-config", str(path)]) == 2
    payload, raw = _stderr_json(capsys)
    assert payload["valid"] is False
    assert "unknown field" in payload["error"]
    assert "Traceback" not in raw


def test_bool_max_concurrent_exits_two(tmp_path, capsys):
    document = dict(VALID_CONFIG_DOCUMENT)
    document["max_concurrent"] = True
    path = _write(tmp_path, document, "config.json")
    assert main(["validate-config", str(path)]) == 2
    payload, _raw = _stderr_json(capsys)
    assert "max_concurrent" in payload["error"]


def test_missing_config_file_exits_two(tmp_path, capsys):
    assert main(["validate-config", str(tmp_path / "missing.json")]) == 2
    payload, _raw = _stderr_json(capsys)
    assert payload["valid"] is False
    assert "cannot read input file" in payload["error"]


def test_untrusted_repository_card_exits_two(tmp_path, capsys):
    document = dict(VALID_CONFIG_DOCUMENT)
    document["repositories"] = {
        "primary": {"path": "/srv/hermes/repos/primary", "trusted": False}
    }
    config_path = _write(tmp_path, document, "config.json")
    assert (
        main(
            [
                "validate-card",
                str(VALID_CARD),
                "--config",
                str(config_path),
            ]
        )
        == 2
    )
    payload, _raw = _stderr_json(capsys)
    assert "trusted" in payload["error"]


def test_card_requesting_unconfigured_repository_exits_two(tmp_path, capsys):
    config_path = _write(tmp_path, VALID_CONFIG_DOCUMENT, "config.json")
    card_document = json.loads(VALID_CARD.read_text(encoding="utf-8"))
    card_document["repository"] = "secondary"
    card_path = _write(tmp_path, card_document, "card.json")
    assert (
        main(["validate-card", str(card_path), "--config", str(config_path)]) == 2
    )
    payload, _raw = _stderr_json(capsys)
    assert "repository" in payload["error"]


def test_invalid_card_json_exits_two_without_body_in_error(tmp_path, capsys):
    config_path = _write(tmp_path, VALID_CONFIG_DOCUMENT, "config.json")
    card_path = tmp_path / "card.json"
    card_path.write_text(
        '{"schema_version": 1, "card_kind": "implementation", '
        '"lineage": "secret-lineage-should-not-appear", "closure_for": null, '
        '"executor": "claude", "repository": "primary", "allow": ["src/"], '
        '"commands": [], "risk": "normal", "extra": true}',
        encoding="utf-8",
    )
    assert (
        main(["validate-card", str(card_path), "--config", str(config_path)]) == 2
    )
    payload, raw = _stderr_json(capsys)
    assert "unknown field" in payload["error"]
    assert "secret-lineage-should-not-appear" not in raw
    assert "Traceback" not in raw


def test_invalid_config_is_reported_before_card_validation(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text("[]", encoding="utf-8")
    assert (
        main(["validate-card", str(VALID_CARD), "--config", str(config_path)]) == 2
    )
    payload, _raw = _stderr_json(capsys)
    # Top-level JSON value is an array, not the required config object.
    assert "expected a JSON object" in payload["error"]
