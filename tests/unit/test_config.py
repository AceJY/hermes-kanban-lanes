"""Unit tests for the strict, side-effect-free lane configuration contract."""

from __future__ import annotations

import copy
import json

import pytest

from hermes_kanban_lanes.cli import main
from hermes_kanban_lanes.config import (
    DEFAULT_MAIN_REF,
    DEFAULT_MAX_REWORK_ROUNDS,
    DEFAULT_POST_MERGE_TIMEOUT_SECONDS,
    DEFAULT_REVIEWER,
    LaneConfig,
    MergeConfig,
    ReviewConfig,
    default_review_config,
    load_config,
)
from hermes_kanban_lanes.contracts import ContractError


def _config(**overrides):
    data = {
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
    data.update(overrides)
    return data


def _write(tmp_path, document, name="config.json"):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _raises(payload, path_fragment):
    with pytest.raises(ContractError) as excinfo:
        LaneConfig.from_dict(payload)
    assert path_fragment in str(excinfo.value)
    return excinfo.value


def test_valid_config_round_trip_preserves_input():
    data = _config()
    snapshot = copy.deepcopy(data)
    config = LaneConfig.from_dict(data)
    assert data == snapshot
    assert config.to_dict() == data


def test_config_to_dict_is_independent():
    config = LaneConfig.from_dict(_config())
    rendered = config.to_dict()
    rendered["boards"]["engineering"] = "/changed.db"
    rendered["repositories"]["primary"]["path"] = "/changed"
    rendered["executors"]["claude"]["argv"].append("--oops")
    assert config.boards["engineering"] == "/srv/hermes/kanban/engineering.db"
    assert config.repositories["primary"].path == "/srv/hermes/repos/primary"
    assert config.executors["claude"].argv == ("claude",)


@pytest.mark.parametrize(
    "missing",
    [
        "schema_version",
        "hermes_source",
        "hermes_commit",
        "boards",
        "repositories",
        "executors",
        "work_root",
        "max_concurrent",
    ],
)
def test_config_requires_every_top_level_field(missing):
    data = _config()
    del data[missing]
    _raises(data, missing)


def test_config_rejects_unknown_top_level_field():
    _raises(_config(extra=True), "unknown field")


def test_config_rejects_unknown_nested_fields():
    data = _config(
        repositories={
            "primary": {
                "path": "/srv/hermes/repos/primary",
                "trusted": True,
                "role": "admin",
            }
        }
    )
    _raises(data, "unknown field")
    data = _config(executors={"claude": {"argv": ["claude"], "env": {}}})
    _raises(data, "unknown field")


@pytest.mark.parametrize("bad_version", [True, 2, "1", 1.0, None])
def test_config_rejects_bad_schema_version(bad_version):
    _raises(_config(schema_version=bad_version), "schema_version")


@pytest.mark.parametrize("bad_source", ["relative/source", "~/source", "$HOME/source", "", None, 1])
def test_config_rejects_non_absolute_hermes_source(bad_source):
    _raises(_config(hermes_source=bad_source), "hermes_source")


@pytest.mark.parametrize("bad_work_root", ["relative", "~/work", "", None, 1])
def test_config_rejects_non_absolute_work_root(bad_work_root):
    _raises(_config(work_root=bad_work_root), "work_root")


def test_config_rejects_parent_and_backslash_in_absolute_paths():
    _raises(_config(hermes_source="/srv/../etc"), "hermes_source")
    _raises(_config(work_root="/srv/./work"), "work_root")
    _raises(_config(work_root="/srv\\work"), "work_root")


def test_config_rejects_unc_style_absolute_paths():
    _raises(_config(hermes_source="//server/share"), "hermes_source")
    _raises(_config(work_root="//server/share"), "work_root")


@pytest.mark.parametrize("bad_commit", ["a" * 39, "a" * 41, "A" * 40, "g" * 40, "", None])
def test_config_rejects_bad_hermes_commit(bad_commit):
    _raises(_config(hermes_commit=bad_commit), "hermes_commit")


def test_config_accepts_64_char_hermes_commit():
    config = LaneConfig.from_dict(_config(hermes_commit="b" * 64))
    assert config.hermes_commit == "b" * 64


@pytest.mark.parametrize("boards", [{}, None, [], "x"])
def test_config_rejects_empty_or_non_object_boards(boards):
    _raises(_config(boards=boards), "boards")


@pytest.mark.parametrize("repository", [{}, None, []])
def test_config_rejects_empty_or_non_object_repositories(repository):
    _raises(_config(repositories=repository), "repositories")


@pytest.mark.parametrize("executors", [{}, None, []])
def test_config_rejects_empty_or_non_object_executors(executors):
    _raises(_config(executors=executors), "executors")


def test_config_rejects_relative_board_path():
    _raises(_config(boards={"engineering": "kanban.db"}), "boards")


def test_config_rejects_empty_board_alias():
    _raises(_config(boards={"": "/srv/hermes/kanban/x.db"}), "boards")


def test_config_rejects_untrusted_repository():
    _raises(
        _config(repositories={"primary": {"path": "/srv/repo", "trusted": False}}),
        "trusted",
    )
    _raises(
        _config(repositories={"primary": {"path": "/srv/repo"}}),
        "missing required",
    )
    _raises(
        _config(repositories={"primary": {"path": "/srv/repo", "trusted": 1}}),
        "trusted",
    )
    _raises(
        _config(repositories={"primary": {"path": "relative", "trusted": True}}),
        "path",
    )


@pytest.mark.parametrize("executor_key", ["other", "Claude", "", "codex2"])
def test_config_rejects_unknown_executor_key(executor_key):
    _raises(_config(executors={executor_key: {"argv": ["x"]}}), "executors")


def test_config_accepts_both_allowed_executors():
    config = LaneConfig.from_dict(
        _config(
            executors={
                "claude": {"argv": ["claude"]},
                "codex": {"argv": ["codex", "--flag"]},
            }
        )
    )
    assert set(config.executors) == {"claude", "codex"}


# ---------------------------------------------------------------------------
# W8 D1: executor assignees are optional, defaulting to hkl:<alias>
# ---------------------------------------------------------------------------


def test_executor_assignees_default_and_override():
    default = LaneConfig.from_dict(_config())
    assert default.executors["claude"].effective_assignees("claude") == ("hkl:claude",)
    # The omitted default stays out of to_dict so existing configs round-trip.
    assert default.to_dict() == _config()

    overridden = LaneConfig.from_dict(
        _config(
            executors={
                "claude": {
                    "argv": ["claude"],
                    "assignees": ["hkl:claude", "claude-code", "hkl:claude"],
                }
            }
        )
    )
    executor = overridden.executors["claude"]
    assert executor.effective_assignees("claude") == ("hkl:claude", "claude-code")
    assert executor.to_dict()["assignees"] == ["hkl:claude", "claude-code"]
    assert overridden.to_dict()["executors"]["claude"]["assignees"] == [
        "hkl:claude",
        "claude-code",
    ]


@pytest.mark.parametrize(
    "assignees",
    [[], "claude-code", None, [""], ["   "], [1], [True], [["claude-code"]]],
)
def test_executor_assignees_reject_bad_shapes(assignees):
    _raises(
        _config(
            executors={"claude": {"argv": ["claude"], "assignees": assignees}}
        ),
        "assignees",
    )


@pytest.mark.parametrize(
    "argv",
    [[], "claude", None, [""], [1], [True], [["claude"]]],
)
def test_config_rejects_bad_executor_argv(argv):
    _raises(_config(executors={"claude": {"argv": argv}}), "argv")


@pytest.mark.parametrize("value", [True, False, 0, -1, "2", 2.0, None])
def test_config_rejects_bad_max_concurrent(value):
    _raises(_config(max_concurrent=value), "max_concurrent")


# ---------------------------------------------------------------------------
# File loading: JSON strictness and side effects
# ---------------------------------------------------------------------------


def test_load_config_reads_valid_file(tmp_path):
    path = _write(tmp_path, _config())
    config = load_config(path)
    assert config.max_concurrent == 2


def test_load_config_reports_missing_file_as_contract_error(tmp_path):
    with pytest.raises(ContractError) as excinfo:
        load_config(tmp_path / "missing.json")
    assert "cannot read input file" in str(excinfo.value)


def test_load_config_rejects_invalid_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert "invalid JSON" in message
    assert "line" in message


def test_load_config_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        '{"schema_version": 1, "schema_version": 1, '
        '"hermes_source": "/srv/source", "hermes_commit": "' + "a" * 40 + '", '
        '"boards": {"b": "/srv/b.db"}, '
        '"repositories": {"r": {"path": "/srv/r", "trusted": true}}, '
        '"executors": {"claude": {"argv": ["claude"]}}, '
        '"work_root": "/srv/work", "max_concurrent": 2}',
        encoding="utf-8",
    )
    with pytest.raises(ContractError) as excinfo:
        load_config(path)
    assert "duplicate JSON object key" in str(excinfo.value)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_load_config_rejects_non_finite_json_numbers(tmp_path, constant):
    path = tmp_path / "config.json"
    path.write_text(
        '{"schema_version": 1, "hermes_source": "/srv/source", '
        '"hermes_commit": "' + "a" * 40 + '", '
        '"boards": {"b": "/srv/b.db"}, '
        '"repositories": {"r": {"path": "/srv/r", "trusted": true}}, '
        '"executors": {"claude": {"argv": ["claude"]}}, '
        '"work_root": "/srv/work", "max_concurrent": ' + constant + "}",
        encoding="utf-8",
    )
    with pytest.raises(ContractError) as excinfo:
        load_config(path)
    assert "non-finite JSON number" in str(excinfo.value)


def test_load_config_rejects_non_utf8(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(ContractError) as excinfo:
        load_config(path)
    assert "UTF-8" in str(excinfo.value)


def test_load_config_rejects_deeply_nested_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[" * 20000 + "]" * 20000, encoding="utf-8")
    with pytest.raises(ContractError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert message.startswith("LaneConfig:")
    assert "nested too deeply" in message
    assert "Traceback" not in message


def test_load_config_rejects_deeply_nested_json_via_cli(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text("[" * 20000 + "]" * 20000, encoding="utf-8")
    assert main(["validate-config", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["valid"] is False
    assert "Traceback" not in captured.err


def test_load_config_moderately_nested_json_still_parses(tmp_path):
    # Control: a depth that json.loads can handle still reaches validation.
    path = tmp_path / "config.json"
    path.write_text("[" * 500 + "]" * 500, encoding="utf-8")
    with pytest.raises(ContractError) as excinfo:
        load_config(path)
    assert "expected a JSON object" in str(excinfo.value)


def test_load_config_rejects_oversized_integer(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        '{"schema_version": 1, "hermes_source": "/srv/source", '
        '"hermes_commit": "' + "a" * 40 + '", '
        '"boards": {"b": "/srv/b.db"}, '
        '"repositories": {"r": {"path": "/srv/r", "trusted": true}}, '
        '"executors": {"claude": {"argv": ["claude"]}}, '
        '"work_root": "/srv/work", "max_concurrent": ' + "9" * 5000 + "}",
        encoding="utf-8",
    )
    with pytest.raises(ContractError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert message.startswith("LaneConfig:")
    assert "9" * 10 not in message


def test_load_config_rejects_oversized_integer_via_cli(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(
        '{"schema_version": 1, "hermes_source": "/srv/source", '
        '"hermes_commit": "' + "a" * 40 + '", '
        '"boards": {"b": "/srv/b.db"}, '
        '"repositories": {"r": {"path": "/srv/r", "trusted": true}}, '
        '"executors": {"claude": {"argv": ["claude"]}}, '
        '"work_root": "/srv/work", "max_concurrent": ' + "9" * 5000 + "}",
        encoding="utf-8",
    )
    assert main(["validate-config", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["valid"] is False
    assert "Traceback" not in captured.err


def test_load_config_rejects_nul_path():
    with pytest.raises(ContractError) as excinfo:
        load_config("bad\x00name.json")
    message = str(excinfo.value)
    assert message.startswith("LaneConfig:")
    assert "cannot read input file" in message


def test_config_validation_has_no_filesystem_side_effects(tmp_path):
    missing = tmp_path / "does-not-exist"
    data = _config(
        hermes_source=str(missing / "source"),
        boards={"engineering": str(missing / "kanban" / "engineering.db")},
        repositories={
            "primary": {"path": str(missing / "repo"), "trusted": True}
        },
        work_root=str(missing / "work"),
    )
    path = _write(tmp_path, data)
    before = sorted(entry.name for entry in tmp_path.iterdir())
    config = load_config(path)
    after = sorted(entry.name for entry in tmp_path.iterdir())
    assert config.boards["engineering"] == str(
        missing / "kanban" / "engineering.db"
    )
    assert before == after
    assert not missing.exists()


# ---------------------------------------------------------------------------
# W4 D11: the optional ``review`` block is additive and strict
# ---------------------------------------------------------------------------


def _review(**overrides):
    document = {
        "reviewer": DEFAULT_REVIEWER,
        "max_rework_rounds": DEFAULT_MAX_REWORK_ROUNDS,
        "merge": {
            "enabled": False,
            "main_ref": DEFAULT_MAIN_REF,
            "post_merge_commands": [],
            "post_merge_timeout_seconds": DEFAULT_POST_MERGE_TIMEOUT_SECONDS,
        },
    }
    document.update(overrides)
    return document


def test_omitted_review_block_leaves_w1_w3_round_trip_unchanged():
    data = _config()
    snapshot = copy.deepcopy(data)
    config = LaneConfig.from_dict(data)
    assert config.to_dict() == data
    assert data == snapshot
    assert config.review is None
    defaults = config.review_options()
    assert defaults.reviewer == "code-reviewer"
    assert defaults.max_rework_rounds == 3
    assert defaults.merge.enabled is False
    assert defaults.merge.main_ref == "refs/heads/main"
    assert defaults.merge.post_merge_commands == ()
    assert defaults.merge.post_merge_timeout_seconds == 3600.0
    assert defaults.resolved_authorization_dir("/srv/hermes/work") == (
        "/srv/hermes/work/authorizations"
    )


def test_default_review_config_does_not_manufacture_a_directory():
    config = default_review_config("/srv/work")
    assert config.authorization_dir is None
    assert config.to_dict() == _review()


def test_review_block_round_trips_with_authorization_dir():
    review = _review(
        authorization_dir="/srv/auth",
        merge={
            "enabled": True,
            "main_ref": "refs/heads/prod",
            "post_merge_commands": [["pytest", "-q"], ["true"]],
            "post_merge_timeout_seconds": 12.5,
        },
    )
    data = _config(review=review)
    config = LaneConfig.from_dict(data)
    assert config.to_dict() == data
    assert config.review is not None
    assert config.review.authorization_dir == "/srv/auth"
    assert config.review.merge.enabled is True
    assert config.review.merge.post_merge_commands == (
        ("pytest", "-q"),
        ("true",),
    )
    assert config.review_options() is config.review


def test_review_block_without_authorization_dir_resolves_under_work_root():
    config = LaneConfig.from_dict(_config(review=_review()))
    assert config.review is not None
    assert config.review.authorization_dir is None
    assert "authorization_dir" not in config.to_dict()["review"]
    assert config.review.resolved_authorization_dir("/srv/work") == (
        "/srv/work/authorizations"
    )


def test_review_unknown_key_is_rejected():
    with pytest.raises(ContractError) as excinfo:
        LaneConfig.from_dict(_config(review=_review(publish=True)))
    assert "publish" in str(excinfo.value)


def test_merge_unknown_key_is_rejected():
    merge = _review()["merge"]
    merge["push"] = True
    with pytest.raises(ContractError) as excinfo:
        LaneConfig.from_dict(_config(review=_review(merge=merge)))
    assert "push" in str(excinfo.value)


def test_review_requires_its_fixed_keys():
    review = _review()
    del review["merge"]
    with pytest.raises(ContractError) as excinfo:
        LaneConfig.from_dict(_config(review=review))
    assert "merge" in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewer", ""),
        ("reviewer", 7),
        ("max_rework_rounds", 0),
        ("max_rework_rounds", True),
        ("authorization_dir", "relative/path"),
        ("authorization_dir", ""),
        ("merge", []),
    ],
)
def test_review_field_validation(field, value):
    with pytest.raises(ContractError):
        LaneConfig.from_dict(_config(review=_review(**{field: value})))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", 1),
        ("enabled", "false"),
        ("main_ref", ""),
        ("post_merge_commands", "pytest -q"),
        ("post_merge_commands", [[]]),
        ("post_merge_commands", [["pytest", 1]]),
        ("post_merge_timeout_seconds", 0),
        ("post_merge_timeout_seconds", True),
        ("post_merge_timeout_seconds", "60"),
    ],
)
def test_merge_field_validation(field, value):
    merge = _review()["merge"]
    merge[field] = value
    with pytest.raises(ContractError):
        LaneConfig.from_dict(_config(review=_review(merge=merge)))


def test_merge_config_is_immutable_and_defensive():
    merge = MergeConfig(
        enabled=True,
        post_merge_commands=[["pytest", "-q"]],
    )
    assert merge.post_merge_commands == (("pytest", "-q"),)
    rendered = merge.to_dict()
    rendered["post_merge_commands"].append(["oops"])
    assert merge.post_merge_commands == (("pytest", "-q"),)
    with pytest.raises(Exception):
        merge.enabled = False


def test_review_config_loads_from_json_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_config(review=_review())), encoding="utf-8")
    config = load_config(path)
    assert isinstance(config.review, ReviewConfig)
    assert config.review.merge.enabled is False
