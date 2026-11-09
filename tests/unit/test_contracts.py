"""Unit tests for the strict W1 data contracts (spec 07 section 3/5)."""

from __future__ import annotations

import copy
import json

import pytest

from hermes_kanban_lanes.contracts import (
    ContractError,
    EvidenceManifest,
    ExecutorResult,
    GateReport,
    RunIdentity,
    Usage,
)

IDENTITY = {
    "schema_version": 1,
    "task_id": "synthetic-task-0001",
    "run_id": 7,
    "claim_lock": "lock-a",
}


def _identity(**overrides):
    data = dict(IDENTITY)
    data.update(overrides)
    return data


def _usage(**overrides):
    data = {"input_tokens": None, "output_tokens": None, "cost_usd": None}
    data.update(overrides)
    return data


def _result(**overrides):
    data = {
        "schema_version": 1,
        "identity": _identity(),
        "category": "success",
        "exit_code": 0,
        "changed_paths": ["src/a.py"],
        "artifact_paths": [],
        "usage": _usage(),
    }
    data.update(overrides)
    return data


def _manifest(**overrides):
    data = {
        "schema_version": 1,
        "identity": _identity(),
        "base_commit": "a" * 40,
        "code_commit": "b" * 64,
        "tool_sha256": "c" * 64,
        "gate_report": {"path": "artifacts/gate.json", "sha256": "d" * 64},
        "verdict": "pass",
    }
    data.update(overrides)
    return data


def _raises(payload, path_fragment, factory=ContractError, parser=None):
    with pytest.raises(factory) as excinfo:
        (parser or (lambda value: value))(payload)
    assert path_fragment in str(excinfo.value)
    return excinfo.value


# ---------------------------------------------------------------------------
# RunIdentity
# ---------------------------------------------------------------------------


def test_run_identity_round_trip_preserves_input():
    data = _identity()
    snapshot = copy.deepcopy(data)
    identity = RunIdentity.from_dict(data)
    assert data == snapshot
    assert identity.to_dict() == data


def test_run_identity_to_dict_is_independent():
    identity = RunIdentity.from_dict(_identity())
    rendered = identity.to_dict()
    rendered["task_id"] = "changed"
    assert identity.task_id == "synthetic-task-0001"
    assert identity.to_dict()["task_id"] == "synthetic-task-0001"


@pytest.mark.parametrize("missing", ["schema_version", "task_id", "run_id", "claim_lock"])
def test_run_identity_requires_every_field(missing):
    data = _identity()
    del data[missing]
    _raises(data, missing, parser=RunIdentity.from_dict)


def test_run_identity_rejects_unknown_field():
    _raises(_identity(extra=True), "unknown field", parser=RunIdentity.from_dict)


@pytest.mark.parametrize("bad_run_id", [True, False, 0, -3, "7", 7.0, None])
def test_run_identity_rejects_bad_run_id(bad_run_id):
    _raises(_identity(run_id=bad_run_id), "run_id", parser=RunIdentity.from_dict)


@pytest.mark.parametrize("field_name", ["task_id", "claim_lock"])
@pytest.mark.parametrize("bad_value", ["", None, 1, True])
def test_run_identity_rejects_bad_strings(field_name, bad_value):
    _raises(_identity(**{field_name: bad_value}), field_name, parser=RunIdentity.from_dict)


@pytest.mark.parametrize("bad_version", [True, 2, "1", 1.0, None])
def test_run_identity_rejects_bad_schema_version(bad_version):
    _raises(
        _identity(schema_version=bad_version),
        "schema_version",
        parser=RunIdentity.from_dict,
    )


def test_run_identity_direct_construction_is_strict():
    with pytest.raises(ContractError, match="run_id"):
        RunIdentity(schema_version=1, task_id="t", run_id=True, claim_lock="c")


# ---------------------------------------------------------------------------
# ExecutorResult
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "exit_code"),
    [
        ("success", 0),
        ("code_failure", 1),
        ("code_failure", -1),
        ("environment_failure", 2),
        ("cancelled", 0),
        ("cancelled", 9),
        ("timeout", 0),
        ("timeout", 124),
        # D8: baseline conflicts carry no exit-code constraint.
        ("baseline_conflict", 0),
        ("baseline_conflict", 1),
    ],
)
def test_executor_result_accepts_valid_category_exit_pairs(category, exit_code):
    result = ExecutorResult.from_dict(_result(category=category, exit_code=exit_code))
    assert result.category == category
    assert result.exit_code == exit_code


def test_executor_result_round_trip_preserves_usage_nulls():
    data = _result()
    snapshot = copy.deepcopy(data)
    result = ExecutorResult.from_dict(data)
    assert data == snapshot
    assert result.to_dict() == data
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.cost_usd is None
    # Missing accounting stays null; it is never silently rewritten to zero.
    assert result.to_dict()["usage"] == {
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
    }


def test_executor_result_success_requires_zero_exit():
    _raises(
        _result(category="success", exit_code=1),
        "exit_code",
        parser=ExecutorResult.from_dict,
    )


@pytest.mark.parametrize("category", ["code_failure", "environment_failure"])
def test_executor_result_failures_require_nonzero_exit(category):
    _raises(
        _result(category=category, exit_code=0),
        "exit_code",
        parser=ExecutorResult.from_dict,
    )


@pytest.mark.parametrize("category", ["ok", "SUCCESS", "", None, 1, True])
def test_executor_result_rejects_bad_category(category):
    _raises(
        _result(category=category), "category", parser=ExecutorResult.from_dict
    )


def test_executor_result_rejects_unknown_fields_and_missing_fields():
    _raises(_result(extra=1), "unknown field", parser=ExecutorResult.from_dict)
    data = _result()
    del data["usage"]
    _raises(data, "missing required", parser=ExecutorResult.from_dict)


def test_executor_result_rejects_unknown_nested_identity_field():
    data = _result(identity=_identity(role="admin"))
    _raises(data, "unknown field", parser=ExecutorResult.from_dict)


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": None, "output_tokens": None},  # missing cost_usd
        {"input_tokens": None, "output_tokens": None, "cost_usd": None, "extra": 0},
        {"input_tokens": True, "output_tokens": None, "cost_usd": None},
        {"input_tokens": -1, "output_tokens": None, "cost_usd": None},
        {"input_tokens": 1.5, "output_tokens": None, "cost_usd": None},
        {"input_tokens": None, "output_tokens": "3", "cost_usd": None},
        {"input_tokens": None, "output_tokens": None, "cost_usd": True},
        {"input_tokens": None, "output_tokens": None, "cost_usd": -0.5},
        {"input_tokens": None, "output_tokens": None, "cost_usd": "1.0"},
        "not-an-object",
    ],
)
def test_executor_result_rejects_bad_usage(usage):
    _raises(_result(usage=usage), "usage", parser=ExecutorResult.from_dict)


@pytest.mark.parametrize("bad_cost", [float("nan"), float("inf"), float("-inf")])
def test_executor_result_rejects_non_finite_cost(bad_cost):
    _raises(
        _result(usage=_usage(cost_usd=bad_cost)),
        "cost_usd",
        parser=ExecutorResult.from_dict,
    )


def test_usage_rejects_huge_integer_cost():
    with pytest.raises(ContractError) as excinfo:
        Usage(input_tokens=None, output_tokens=None, cost_usd=10**400)
    assert "cost_usd" in str(excinfo.value)


def test_executor_result_rejects_huge_integer_cost():
    _raises(
        _result(usage=_usage(cost_usd=10**400)),
        "cost_usd",
        parser=ExecutorResult.from_dict,
    )


@pytest.mark.parametrize("cost", [0, 0.0, 1, 12.5])
def test_usage_accepts_finite_non_negative_costs(cost):
    usage = Usage(input_tokens=None, output_tokens=None, cost_usd=cost)
    assert usage.to_dict()["cost_usd"] == cost


def test_usage_direct_construction_is_strict():
    with pytest.raises(ContractError, match="input_tokens"):
        Usage(input_tokens=True, output_tokens=None, cost_usd=None)
    with pytest.raises(ContractError, match="cost_usd"):
        Usage(input_tokens=None, output_tokens=None, cost_usd=float("nan"))


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
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
    ],
)
def test_executor_result_rejects_unsafe_changed_paths(bad_path):
    _raises(
        _result(changed_paths=[bad_path]),
        "changed_paths",
        parser=ExecutorResult.from_dict,
    )


def test_executor_result_accepts_directory_style_and_empty_lists():
    result = ExecutorResult.from_dict(
        _result(changed_paths=["src/", "docs/a.md"], artifact_paths=[])
    )
    assert result.changed_paths == ("src/", "docs/a.md")
    assert result.artifact_paths == ()


def test_executor_result_rejects_duplicate_paths():
    _raises(
        _result(changed_paths=["src/a.py", "src/a.py"]),
        "duplicate",
        parser=ExecutorResult.from_dict,
    )


def test_executor_result_rejects_non_array_paths():
    _raises(
        _result(changed_paths="src/a.py"),
        "changed_paths",
        parser=ExecutorResult.from_dict,
    )


def test_executor_result_rejects_bool_exit_code():
    _raises(_result(exit_code=True), "exit_code", parser=ExecutorResult.from_dict)


def test_executor_result_to_dict_is_independent():
    result = ExecutorResult.from_dict(_result())
    rendered = result.to_dict()
    rendered["usage"]["input_tokens"] = 123
    rendered["changed_paths"].append("src/other.py")
    rendered["identity"]["task_id"] = "changed"
    assert result.usage.input_tokens is None
    assert result.changed_paths == ("src/a.py",)
    assert result.identity.task_id == "synthetic-task-0001"


# ---------------------------------------------------------------------------
# EvidenceManifest
# ---------------------------------------------------------------------------


def test_evidence_manifest_round_trip_and_commit_lengths():
    for base, code in [("a" * 40, "b" * 40), ("a" * 64, "b" * 64)]:
        data = _manifest(base_commit=base, code_commit=code)
        snapshot = copy.deepcopy(data)
        manifest = EvidenceManifest.from_dict(data)
        assert data == snapshot
        assert manifest.to_dict() == data
        assert manifest.base_commit == base
        assert manifest.code_commit == code


@pytest.mark.parametrize(
    "commit",
    [
        "a" * 39,
        "a" * 41,
        "a" * 63,
        "a" * 65,
        "A" * 40,
        "g" * 40,
        "",
        None,
        1,
    ],
)
def test_evidence_manifest_rejects_bad_commits(commit):
    _raises(
        _manifest(base_commit=commit),
        "base_commit",
        parser=EvidenceManifest.from_dict,
    )
    _raises(
        _manifest(code_commit=commit),
        "code_commit",
        parser=EvidenceManifest.from_dict,
    )


@pytest.mark.parametrize("digest", ["a" * 40, "a" * 63, "a" * 65, "A" * 64, "z" * 64])
def test_evidence_manifest_requires_64_char_tool_sha(digest):
    _raises(
        _manifest(tool_sha256=digest),
        "tool_sha256",
        parser=EvidenceManifest.from_dict,
    )


@pytest.mark.parametrize(
    "gate_report",
    [
        {"path": "gate.json"},  # missing sha256
        {"path": "gate.json", "sha256": "d" * 64, "extra": 1},
        {"path": "/abs/gate.json", "sha256": "d" * 64},
        {"path": "../gate.json", "sha256": "d" * 64},
        {"path": "C:/gate.json", "sha256": "d" * 64},
        {"path": "gate.json", "sha256": "d" * 63},
        "gate.json",
    ],
)
def test_evidence_manifest_rejects_bad_gate_report(gate_report):
    _raises(
        _manifest(gate_report=gate_report),
        "gate_report",
        parser=EvidenceManifest.from_dict,
    )


@pytest.mark.parametrize("verdict", ["PASS", "ok", "", None, True])
def test_evidence_manifest_rejects_bad_verdict(verdict):
    _raises(
        _manifest(verdict=verdict), "verdict", parser=EvidenceManifest.from_dict
    )


def test_evidence_manifest_rejects_unknown_and_missing_nested_fields():
    _raises(
        _manifest(identity=_identity(extra="x")),
        "unknown field",
        parser=EvidenceManifest.from_dict,
    )
    data = _manifest()
    del data["gate_report"]["sha256"]
    _raises(data, "missing required", parser=EvidenceManifest.from_dict)


def test_gate_report_direct_construction_is_strict():
    with pytest.raises(ContractError, match="sha256"):
        GateReport(path="gate.json", sha256="a" * 40)


def test_evidence_manifest_to_dict_is_independent():
    manifest = EvidenceManifest.from_dict(_manifest())
    rendered = manifest.to_dict()
    rendered["gate_report"]["path"] = "changed.json"
    rendered["identity"]["claim_lock"] = "changed"
    assert manifest.gate_report.path == "artifacts/gate.json"
    assert manifest.identity.claim_lock == "lock-a"


def test_to_dict_outputs_are_json_serialisable_without_nan():
    values = (
        RunIdentity.from_dict(_identity()),
        ExecutorResult.from_dict(_result()),
        EvidenceManifest.from_dict(_manifest()),
    )
    for value in values:
        rendered = json.dumps(value.to_dict(), allow_nan=False)
        assert json.loads(rendered) == value.to_dict()
