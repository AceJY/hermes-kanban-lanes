"""W4-07 (unit): explicit release authorization is strict and time-bounded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_kanban_lanes.release.authorization import (
    AUTHORIZATION_FIELDS,
    AUTHORIZATION_SCHEMA_VERSION,
    POLICY_APPROVER,
    ReleaseAuthorization,
    authorization_problems,
    build_policy_authorization,
    load_authorization,
)
from hermes_kanban_lanes.verification.evidence import sha256_file

TASK_ID = "t_w4auth"


def _document(**overrides):
    document = {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "requirement_id": "HKL-W4",
        "task_id": TASK_ID,
        "review_run_id": 7,
        "dev_run_id": 3,
        "rework_round": 1,
        "task_base_commit": "a" * 40,
        "reviewed_head": "b" * 40,
        "reviewed_tree": "c" * 40,
        "evidence_index_sha256": "d" * 64,
        "main_ref": "refs/heads/main",
        "main_commit": "a" * 40,
        "approver": "operator",
        "approved_at": 1_000,
        "expires_at": 2_000,
    }
    document.update(overrides)
    return document


def _write(tmp_path, document, name="auth.json"):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _expected():
    return {
        "requirement_id": "HKL-W4",
        "task_id": TASK_ID,
        "review_run_id": 7,
        "dev_run_id": 3,
        "rework_round": 1,
        "task_base_commit": "a" * 40,
        "reviewed_head": "b" * 40,
        "reviewed_tree": "c" * 40,
        "evidence_index_sha256": "d" * 64,
        "main_ref": "refs/heads/main",
        "main_commit": "a" * 40,
    }


def test_valid_authorization_loads_with_file_digest(tmp_path):
    path = _write(tmp_path, _document())
    authorization, problems = load_authorization(path)
    assert problems == []
    assert isinstance(authorization, ReleaseAuthorization)
    assert authorization.path == str(path)
    assert authorization.sha256 == sha256_file(path)
    rendered = authorization.to_dict()
    assert {key: rendered[key] for key in AUTHORIZATION_FIELDS} == _document()
    assert set(rendered) == set(AUTHORIZATION_FIELDS) | {"path", "sha256"}
    assert authorization_problems(
        authorization,
        _expected(),
        now=1_500,
        implementer="hkl:claude",
        reviewer="code-reviewer",
        executor="claude",
    ) == []


def test_missing_authorization_is_an_explicit_reason(tmp_path):
    authorization, problems = load_authorization(tmp_path / "absent.json")
    assert authorization is None
    assert problems == ["authorization-missing"]
    assert authorization_problems(None, _expected()) == ["authorization-missing"]


def test_invalid_json_is_refused(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("{not json", encoding="utf-8")
    authorization, problems = load_authorization(path)
    assert authorization is None
    assert problems == ["authorization-invalid"]


def test_unknown_key_is_refused(tmp_path):
    document = _document()
    document["force"] = True
    authorization, problems = load_authorization(_write(tmp_path, document))
    assert authorization is None
    assert problems == ["authorization-invalid:force"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("schema_version", 2, "authorization-schema"),
        ("requirement_id", "", "authorization-field-invalid:requirement_id"),
        ("task_id", "", "authorization-field-invalid:task_id"),
        ("review_run_id", 0, "authorization-field-invalid:review_run_id"),
        ("dev_run_id", True, "authorization-field-invalid:dev_run_id"),
        ("rework_round", "1", "authorization-field-invalid:rework_round"),
        ("task_base_commit", "xyz", "authorization-field-invalid:task_base_commit"),
        ("reviewed_head", "b" * 39, "authorization-field-invalid:reviewed_head"),
        ("reviewed_tree", 7, "authorization-field-invalid:reviewed_tree"),
        (
            "evidence_index_sha256",
            "d" * 40,
            "authorization-field-invalid:evidence_index_sha256",
        ),
        ("main_ref", "", "authorization-field-invalid:main_ref"),
        ("main_commit", "a" * 41, "authorization-field-invalid:main_commit"),
        ("approver", "", "authorization-field-invalid:approver"),
        ("approved_at", True, "authorization-field-invalid:approved_at"),
        ("expires_at", "2000", "authorization-field-invalid:expires_at"),
    ],
)
def test_invalid_field_types_are_refused(tmp_path, field, value, reason):
    document = _document()
    document[field] = value
    authorization, problems = load_authorization(_write(tmp_path, document))
    assert authorization is None
    assert reason in problems, problems


def test_missing_expiry_has_its_own_reason(tmp_path):
    document = _document()
    del document["expires_at"]
    authorization, problems = load_authorization(_write(tmp_path, document))
    assert authorization is None
    assert "authorization-no-expiry" in problems
    assert "authorization-missing-field:expires_at" not in problems


def test_missing_other_field_is_named(tmp_path):
    document = _document()
    del document["reviewed_head"]
    authorization, problems = load_authorization(_write(tmp_path, document))
    assert authorization is None
    assert "authorization-missing-field:reviewed_head" in problems


def test_expired_and_future_authorizations_are_refused(tmp_path):
    expired, _ = load_authorization(_write(tmp_path, _document(expires_at=2_000)))
    assert "authorization-expired" in authorization_problems(expired, _expected(), now=2_000)
    future, _ = load_authorization(_write(tmp_path, _document(approved_at=2_000)))
    assert "authorization-not-yet-valid" in authorization_problems(
        future, _expected(), now=1_500
    )


@pytest.mark.parametrize(
    "approver",
    ["hkl:claude", "code-reviewer", "claude", "   "],
)
def test_approver_must_be_independent(tmp_path, approver):
    path = _write(tmp_path, _document(approver=approver))
    authorization, _ = load_authorization(path)
    assert "authorization-approver-invalid" in authorization_problems(
        authorization,
        _expected(),
        now=1_500,
        implementer="hkl:claude",
        reviewer="code-reviewer",
        executor="claude",
    )


@pytest.mark.parametrize(
    "field",
    [
        "requirement_id",
        "task_id",
        "review_run_id",
        "dev_run_id",
        "rework_round",
        "task_base_commit",
        "reviewed_head",
        "reviewed_tree",
        "evidence_index_sha256",
        "main_ref",
        "main_commit",
    ],
)
def test_each_tuple_field_must_match_exactly(tmp_path, field):
    replacement = {
        "requirement_id": "HKL-W3",
        "task_id": "t_other",
        "review_run_id": 8,
        "dev_run_id": 4,
        "rework_round": 2,
        "task_base_commit": "e" * 40,
        "reviewed_head": "f" * 40,
        "reviewed_tree": "0" * 40,
        "evidence_index_sha256": "1" * 64,
        "main_ref": "refs/heads/other",
        "main_commit": "2" * 40,
    }[field]
    path = _write(tmp_path, _document(**{field: replacement}))
    authorization, _ = load_authorization(path)
    problems = authorization_problems(authorization, _expected(), now=1_500)
    assert f"authorization-mismatch:{field}" in problems, problems


def test_note_is_allowed_and_preserved(tmp_path):
    path = _write(tmp_path, _document(note="样本主干合并授权"))
    authorization, problems = load_authorization(path)
    assert problems == []
    assert authorization.note == "样本主干合并授权"
    assert authorization.to_dict()["note"] == "样本主干合并授权"
    assert authorization_problems(authorization, _expected(), now=1_500) == []


def test_note_must_be_a_string(tmp_path):
    authorization, problems = load_authorization(
        _write(tmp_path, _document(note=7))
    )
    assert authorization is None
    assert "authorization-field-invalid:note" in problems


def test_expected_none_fields_are_not_compared(tmp_path):
    authorization, _ = load_authorization(_write(tmp_path, _document()))
    expected = {key: None for key in _expected()}
    assert authorization_problems(authorization, expected, now=1_500) == []


# ---------------------------------------------------------------------------
# Policy authorization (owner decision 2026-11-11)
# ---------------------------------------------------------------------------


def test_file_mode_rejects_a_forged_policy_approver(tmp_path):
    path = _write(tmp_path, _document(approver=POLICY_APPROVER))
    authorization, problems = load_authorization(path)
    # Shape validation accepts the string; the approval check is what refuses.
    assert problems == []
    assert authorization is not None
    assert "authorization-policy-approver-forbidden" in authorization_problems(
        authorization, _expected(), now=1_500
    )
    # Only a policy-synthesized record may carry the reserved identity.
    assert authorization_problems(
        authorization,
        _expected(),
        now=1_500,
        allow_policy_approver=True,
    ) == []


def test_policy_source_field_is_optional_but_validated(tmp_path):
    path = _write(tmp_path, _document(source="policy"))
    authorization, problems = load_authorization(path)
    assert problems == []
    assert authorization is not None
    assert authorization.source == "policy"
    assert authorization.to_dict()["source"] == "policy"

    bad = _write(tmp_path, _document(source="human"), name="bad.json")
    authorization, problems = load_authorization(bad)
    assert authorization is None
    assert "authorization-field-invalid:source" in problems


def test_build_policy_authorization_binds_every_reviewed_field():
    expected = {**_expected(), "main_ref": "refs/heads/main"}
    authorization = build_policy_authorization(
        expected, approved_at=1_500, expires_at=2_400
    )
    assert authorization is not None
    assert authorization.approver == POLICY_APPROVER
    assert authorization.source == "policy"
    assert authorization.approved_at == 1_500
    assert authorization.expires_at == 2_400
    assert authorization.path is None
    assert authorization.sha256 is None
    assert authorization_problems(
        authorization,
        expected,
        now=1_500,
        allow_policy_approver=True,
    ) == []


def test_build_policy_authorization_refuses_an_incomplete_binding():
    expected = {**_expected(), "reviewed_head": None}
    assert build_policy_authorization(
        expected, approved_at=1_500, expires_at=2_400
    ) is None
