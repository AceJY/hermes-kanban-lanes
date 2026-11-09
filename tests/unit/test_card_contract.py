"""Unit tests for the strict card contract (spec 07 section 3.4/5)."""

from __future__ import annotations

import copy
import json

import pytest

from hermes_kanban_lanes.card_contract import CardSpec, load_card
from hermes_kanban_lanes.config import LaneConfig
from hermes_kanban_lanes.contracts import ContractError


def _config(*, executors=None, repositories=None):
    return LaneConfig.from_dict(
        {
            "schema_version": 1,
            "hermes_source": "/srv/hermes/source",
            "hermes_commit": "a" * 40,
            "boards": {"engineering": "/srv/hermes/kanban/engineering.db"},
            "repositories": repositories
            or {
                "primary": {"path": "/srv/hermes/repos/primary", "trusted": True}
            },
            "executors": executors or {"claude": {"argv": ["claude"]}},
            "work_root": "/srv/hermes/work",
            "max_concurrent": 2,
        }
    )


def _card(**overrides):
    data = {
        "schema_version": 1,
        "card_kind": "implementation",
        "lineage": "lineage-1",
        "closure_for": None,
        "executor": "claude",
        "repository": "primary",
        "allow": ["src/", "tests/"],
        "commands": [{"argv": ["pytest", "-q"], "cwd": "."}],
        "risk": "normal",
        # D8 additive key; ``to_dict`` always emits it.
        "artifacts": [],
    }
    data.update(overrides)
    return data


def _raises(payload, path_fragment, config=None):
    with pytest.raises(ContractError) as excinfo:
        CardSpec.from_dict(payload, config or _config())
    assert path_fragment in str(excinfo.value)
    return excinfo.value


def test_valid_implementation_card_round_trip_preserves_input():
    data = _card()
    snapshot = copy.deepcopy(data)
    card = CardSpec.from_dict(data, _config())
    assert data == snapshot
    assert card.to_dict() == data
    assert card.card_kind == "implementation"
    assert card.commands[0].argv == ("pytest", "-q")


@pytest.mark.parametrize("kind", ["review", "closure", "merge", "gate"])
def test_non_implementation_cards_allow_explicit_empty_commands(kind):
    closure_for = "t_parent" if kind in {"closure", "merge", "gate"} else None
    card = CardSpec.from_dict(
        _card(card_kind=kind, closure_for=closure_for, commands=[]), _config()
    )
    assert card.commands == ()


@pytest.mark.parametrize("kind", ["closure", "merge", "gate"])
@pytest.mark.parametrize("closure_for", [None, ""])
def test_closure_kinds_require_a_closure_target(kind, closure_for):
    _raises(
        _card(card_kind=kind, closure_for=closure_for),
        "closure_for",
    )


def test_implementation_and_review_may_have_null_closure_for():
    assert CardSpec.from_dict(_card(), _config()).closure_for is None
    assert (
        CardSpec.from_dict(
            _card(card_kind="review", commands=[]), _config()
        ).closure_for
        is None
    )


def test_implementation_requires_at_least_one_command():
    _raises(_card(commands=[]), "commands")


def test_card_rejects_unknown_top_level_field():
    _raises(_card(extra=True), "unknown field")


def test_card_rejects_unknown_nested_command_field():
    _raises(
        _card(commands=[{"argv": ["pytest"], "cwd": ".", "shell": True}]),
        "unknown field",
    )


@pytest.mark.parametrize(
    "missing",
    [
        "schema_version",
        "card_kind",
        "lineage",
        "closure_for",
        "executor",
        "repository",
        "allow",
        "commands",
        "risk",
    ],
)
def test_card_requires_every_field(missing):
    data = _card()
    del data[missing]
    _raises(data, missing)


@pytest.mark.parametrize("kind", ["task", "IMPLEMENTATION", "", None, 1, True])
def test_card_rejects_bad_card_kind(kind):
    _raises(_card(card_kind=kind), "card_kind")


@pytest.mark.parametrize("risk", ["low", "NORMAL", "", None, True])
def test_card_rejects_bad_risk(risk):
    _raises(_card(risk=risk), "risk")


@pytest.mark.parametrize("executor", ["other", "Claude", "", None, True])
def test_card_rejects_bad_executor_enum(executor):
    _raises(_card(executor=executor), "executor")


def test_card_rejects_unconfigured_executor():
    # "codex" is a valid enum value, but this configuration only allows claude.
    _raises(_card(executor="codex"), "executor")


def test_card_rejects_unconfigured_repository():
    _raises(_card(repository="secondary"), "repository")
    _raises(_card(repository=""), "repository")
    _raises(_card(repository=None), "repository")


def test_card_accepts_configured_codex_and_secondary_repository():
    config = _config(
        executors={
            "claude": {"argv": ["claude"]},
            "codex": {"argv": ["codex"]},
        },
        repositories={
            "primary": {"path": "/srv/hermes/repos/primary", "trusted": True},
            "secondary": {"path": "/srv/hermes/repos/secondary", "trusted": True},
        },
    )
    card = CardSpec.from_dict(
        _card(executor="codex", repository="secondary"), config
    )
    assert card.executor == "codex"
    assert card.repository == "secondary"


@pytest.mark.parametrize("bad_version", [True, 2, "1", 1.0, None])
def test_card_rejects_bad_schema_version(bad_version):
    _raises(_card(schema_version=bad_version), "schema_version")


@pytest.mark.parametrize("lineage", ["", None, 1, True])
def test_card_rejects_bad_lineage(lineage):
    _raises(_card(lineage=lineage), "lineage")


@pytest.mark.parametrize("allow", [{}, "src", None, []])
def test_card_rejects_bad_allow_shape(allow):
    _raises(_card(allow=allow), "allow")


@pytest.mark.parametrize(
    "bad_path",
    [
        "/abs/path",
        "../escape",
        "a/../b",
        "./a",
        "C:/windows",
        "C:\\windows",
        "\\\\server\\share",
        "//server/share",
        "a\\b",
        "a\x00b",
        "",
        "a//b",
        ".",
    ],
)
def test_card_rejects_unsafe_allow_paths(bad_path):
    _raises(_card(allow=[bad_path]), "allow")


def test_card_accepts_directory_style_allow_paths():
    card = CardSpec.from_dict(_card(allow=["src/", "tests/"]), _config())
    assert card.allow == ("src/", "tests/")


def test_card_rejects_duplicate_allow_paths():
    _raises(_card(allow=["src/", "src/"]), "duplicate")


def test_card_rejects_string_shell_commands():
    _raises(_card(commands="pytest -q"), "commands")
    _raises(_card(commands=["pytest -q"]), "commands")


@pytest.mark.parametrize(
    "command",
    [
        {"cwd": "."},  # missing argv
        {"argv": ["pytest"]},  # missing cwd
        {"argv": "pytest -q", "cwd": "."},
        {"argv": [], "cwd": "."},
        {"argv": [""], "cwd": "."},
        {"argv": [1], "cwd": "."},
        {"argv": [True], "cwd": "."},
        {"argv": ["pytest"], "cwd": "/abs"},
        {"argv": ["pytest"], "cwd": "../up"},
        {"argv": ["pytest"], "cwd": "C:/x"},
        {"argv": ["pytest"], "cwd": ""},
        "pytest -q",
    ],
)
def test_card_rejects_bad_command_entries(command):
    _raises(_card(commands=[command]), "commands")


def test_card_allows_dot_and_relative_command_cwd():
    card = CardSpec.from_dict(
        _card(
            commands=[
                {"argv": ["pytest", "-q"], "cwd": "."},
                {"argv": ["make"], "cwd": "sub/dir"},
            ]
        ),
        _config(),
    )
    assert [command.cwd for command in card.commands] == [".", "sub/dir"]


def test_card_requires_a_validated_config():
    with pytest.raises(ContractError, match="LaneConfig"):
        CardSpec.from_dict(_card(), {"schema_version": 1})


def test_card_to_dict_is_independent():
    card = CardSpec.from_dict(_card(), _config())
    rendered = card.to_dict()
    rendered["allow"].append("extra/")
    rendered["commands"][0]["argv"].append("--oops")
    assert card.allow == ("src/", "tests/")
    assert card.commands[0].argv == ("pytest", "-q")


def test_load_card_reads_valid_file(tmp_path):
    path = tmp_path / "card.json"
    path.write_text(json.dumps(_card()), encoding="utf-8")
    assert load_card(path, _config()).card_kind == "implementation"


def test_load_card_reports_missing_file(tmp_path):
    with pytest.raises(ContractError, match="cannot read input file"):
        load_card(tmp_path / "missing.json", _config())


# ---------------------------------------------------------------------------
# W8 D2: synthesise a CardSpec from the card body's lane/development blocks
# ---------------------------------------------------------------------------


def _body(lane: str, development: str | None = None) -> str:
    text = f"Intro prose.\n\n```lane\n{lane.strip()}\n```\n"
    if development is not None:
        text += f"\n```development\n{development.strip()}\n```\n"
    return text


def test_synthesize_card_from_lane_and_development():
    from hermes_kanban_lanes.card_contract import synthesize_card

    body = _body(
        """
project: primary
allow: [RESULT.txt, tests/]
test_cmd: "pytest -q"
build_cmd: "make build"
artifacts: [REPORT.md]
""",
        """
contract_version: 2
task_type: development
risk: high
negative_check: isolate and mutate
review: false
""",
    )
    card = synthesize_card(body, "t_synth", "claude", _config())
    assert card.schema_version == 1
    assert card.card_kind == "implementation"
    assert card.lineage == "t_synth"
    assert card.closure_for is None
    assert card.executor == "claude"
    assert card.repository == "primary"
    assert card.allow == ("RESULT.txt", "tests/")
    assert [command.argv for command in card.commands] == [
        ("pytest", "-q"),
        ("make", "build"),
    ]
    assert [command.cwd for command in card.commands] == [".", "."]
    assert card.risk == "high"
    assert card.artifacts == ("REPORT.md",)
    # D3: development cards are reviewed even when the body says otherwise.
    assert card.review is True
    assert card.review_forced is True
    assert card.review_forced_reason == "development-requires-review"


def test_synthesize_card_non_development_may_skip_review():
    from hermes_kanban_lanes.card_contract import synthesize_card

    card = synthesize_card(
        _body(
            """
project: primary
allow: [RESULT.txt]
test_cmd: "pytest -q"
review: false
""",
            """
contract_version: 2
task_type: non_development
""",
        ),
        "t_non_dev",
        "claude",
        _config(),
    )
    assert card.review is False
    assert card.review_forced is False


@pytest.mark.parametrize(
    ("lane_review", "development", "reason"),
    [
        (None, "task_type: development\nreview: false\n", "development-requires-review"),
        (False, "task_type: development\n", "development-requires-review"),
        (False, "contract_version: 2\n", "task-type-missing"),
        (False, "task_type: experiment\n", "task-type-unknown"),
    ],
)
def test_synthesize_card_forces_review_for_development_or_untyped(
    lane_review, development, reason
):
    from hermes_kanban_lanes.card_contract import synthesize_card

    lane = "project: primary\nallow: [RESULT.txt]\ntest_cmd: \"pytest -q\"\n"
    if lane_review is not None:
        lane += f"review: {'true' if lane_review else 'false'}\n"
    card = synthesize_card(
        _body(lane, development), "t_forced", "claude", _config()
    )
    assert card.review is True
    assert card.review_forced is True
    assert card.review_forced_reason == reason


def test_synthesized_card_defaults_review_true_and_risk_normal():
    from hermes_kanban_lanes.card_contract import synthesize_card

    card = synthesize_card(
        _body(
            """
project: primary
allow: [RESULT.txt]
test_cmd: "pytest -q"
"""
        ),
        "t_defaults",
        "claude",
        _config(),
    )
    assert card.review is True
    assert card.risk == "normal"
    assert card.artifacts == ()


@pytest.mark.parametrize(
    ("case", "lane", "reason"),
    [
        (
            "missing project",
            """
allow: [RESULT.txt]
test_cmd: "pytest -q"
""",
            "card-project-missing",
        ),
        (
            "unconfigured project",
            """
project: secondary
allow: [RESULT.txt]
test_cmd: "pytest -q"
""",
            "repository-not-configured",
        ),
        (
            "and operator",
            """
project: primary
allow: [RESULT.txt]
test_cmd: "pytest -q && echo ok"
""",
            "card-command-unsupported",
        ),
        (
            "pipe",
            """
project: primary
allow: [RESULT.txt]
test_cmd: "pytest -q | cat"
""",
            "card-command-unsupported",
        ),
        (
            "redirection",
            """
project: primary
allow: [RESULT.txt]
test_cmd: "pytest -q > out.txt"
""",
            "card-command-unsupported",
        ),
        (
            "unparseable quoting",
            """
project: primary
allow: [RESULT.txt]
test_cmd: "pytest 'unclosed"
""",
            "card-command-unsupported",
        ),
        (
            "directory artifact",
            """
project: primary
allow: [RESULT.txt]
test_cmd: "pytest -q"
artifacts: [artifacts/w3/]
""",
            "card-invalid",
        ),
        (
            "broken yaml",
            """
project: [unclosed
allow: [RESULT.txt]
""",
            "card-invalid",
        ),
        (
            "no command",
            """
project: primary
allow: [RESULT.txt]
test_cmd: ""
build_cmd: ""
""",
            "card-invalid",
        ),
    ],
)
def test_synthesize_card_fails_closed(case, lane, reason):
    from hermes_kanban_lanes.card_contract import CardSynthesisError, synthesize_card

    with pytest.raises(CardSynthesisError) as excinfo:
        synthesize_card(_body(lane), "t_fail", "claude", _config())
    assert excinfo.value.reason == reason, case


def test_synthesize_card_requires_a_lane_block():
    from hermes_kanban_lanes.card_contract import CardSynthesisError, synthesize_card

    with pytest.raises(CardSynthesisError) as excinfo:
        synthesize_card("plain card without a block", "t_plain", "claude", _config())
    assert excinfo.value.reason == "card-missing"


def test_synthesize_card_rejects_unknown_lane_key():
    from hermes_kanban_lanes.card_contract import CardSynthesisError, synthesize_card

    with pytest.raises(CardSynthesisError) as excinfo:
        synthesize_card(
            _body(
                """
project: primary
allow: [RESULT.txt]
test_cmd: "pytest -q"
projcet: typo
"""
            ),
            "t_typo",
            "claude",
            _config(),
        )
    assert excinfo.value.reason == "card-invalid"
    assert "projcet" in str(excinfo.value)


def test_explicit_card_review_flag_round_trips_only_when_false():
    card = CardSpec.from_dict(_card(review=False), _config())
    assert card.review is False
    assert card.to_dict()["review"] is False
    assert "review" not in CardSpec.from_dict(_card(), _config()).to_dict()
