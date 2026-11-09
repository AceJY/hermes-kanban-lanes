"""W3-06: evidence index construction, recomputation and invalidation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.verification.evidence import (
    INDEX_RELATIVE_PATH,
    build_manifest,
    sha256_file,
    verify_evidence,
    write_evidence_index,
    write_json,
)
from hermes_kanban_lanes.workspace.git import repo_head, run_git

IDENTITY = RunIdentity(
    schema_version=1,
    task_id="t_evidence",
    run_id=3,
    claim_lock="lock-evidence",
)


def _identity(**overrides) -> RunIdentity:
    data = IDENTITY.to_dict()
    data.update(overrides)
    return RunIdentity.from_dict(data)


def _commit(repo: Path, message: str = "run") -> str:
    added = run_git(["add", "-A"], cwd=repo)
    assert added.returncode == 0, added.stderr
    committed = run_git(
        [
            "-c",
            "user.name=HKL Tests",
            "-c",
            "user.email=hkl@example.invalid",
            "commit",
            "--no-verify",
            "-m",
            message,
        ],
        cwd=repo,
    )
    assert committed.returncode == 0, committed.stderr
    return repo_head(repo)


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "package"
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = '0'\n", encoding="utf-8")
    (package / "sub" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    return package


@pytest.fixture
def package(tmp_path):
    return _package(tmp_path)


@pytest.fixture
def evidence(tmp_path, make_repo, package):
    repo = make_repo()
    base = repo_head(repo)
    (repo / "RESULT.txt").write_text("ok\n", encoding="utf-8")
    code = _commit(repo)

    run_dir = tmp_path / "run"
    (run_dir / "evidence").mkdir(parents=True)
    (run_dir / "commands").mkdir()
    (run_dir / "commands" / "0.stdout.log").write_bytes(b"out")
    (run_dir / "commands" / "0.stderr.log").write_bytes(b"")
    (run_dir / "stdout.log").write_bytes(b"executor stdout")
    (run_dir / "stderr.log").write_bytes(b"")
    write_json(
        run_dir / "landing-plan.json",
        {"schema_version": 1, "requirement_id": "HKL-W3", "verdict": "ready"},
    )

    gate_path = "evidence/gate-report.json"
    gate_report = {
        "schema_version": 2,
        "requirement_id": "HKL-W3",
        "identity": IDENTITY.to_dict(),
        "base_commit": base,
        "run_commit": code,
        "sync_base": base,
        "checks": [],
        "verdict": "pass",
    }
    write_json(run_dir / gate_path, gate_report)
    gate_sha = sha256_file(run_dir / gate_path)

    manifest = build_manifest(
        IDENTITY,
        base_commit=base,
        code_commit=code,
        gate_report_path=gate_path,
        gate_report_sha256=gate_sha,
        verdict="pass",
        package_dir=package,
    )
    write_json(run_dir / "evidence" / "manifest.json", manifest.to_dict())

    document, index_sha = write_evidence_index(
        run_dir,
        identity=IDENTITY,
        base_commit=base,
        sync_base=base,
        code_commit=code,
        tool_sha256=manifest.tool_sha256,
        gate_report_path=gate_path,
        gate_report_sha256=gate_sha,
        verdict="pass",
    )

    return {
        "repo": repo,
        "base": base,
        "code": code,
        "run_dir": run_dir,
        "package": package,
        "gate_path": gate_path,
        "gate_sha": gate_sha,
        "manifest": manifest,
        "index": document,
        "index_sha": index_sha,
    }


def _verify(evidence, *, identity=IDENTITY):
    return verify_evidence(
        evidence["run_dir"],
        identity,
        evidence["package"],
        evidence["repo"],
        evidence["repo"],
        "refs/heads/main",
    )


def _read_index(evidence) -> dict:
    return json.loads(
        (evidence["run_dir"] / INDEX_RELATIVE_PATH).read_text(encoding="utf-8")
    )


def _write_index(evidence, document: dict) -> None:
    write_json(evidence["run_dir"] / INDEX_RELATIVE_PATH, document)


def _rebuild_index(evidence, **overrides) -> None:
    values = {
        "base_commit": evidence["base"],
        "sync_base": evidence["base"],
        "code_commit": evidence["code"],
        "tool_sha256": evidence["manifest"].tool_sha256,
        "gate_report_path": evidence["gate_path"],
        "gate_report_sha256": evidence["gate_sha"],
        "verdict": "pass",
    }
    values.update(overrides)
    write_evidence_index(evidence["run_dir"], identity=IDENTITY, **values)


def _reasons(verification) -> set[str]:
    return set(verification.reasons)


# ---------------------------------------------------------------------------
# construction and the green path
# ---------------------------------------------------------------------------


def test_index_document_shape_and_registered_files(evidence):
    document = evidence["index"]
    assert document["schema_version"] == 1
    assert document["requirement_id"] == "HKL-W3"
    assert document["identity"] == IDENTITY.to_dict()
    assert document["base_commit"] == evidence["base"]
    assert document["sync_base"] == evidence["base"]
    assert document["code_commit"] == evidence["code"]
    assert document["tool_sha256"] == evidence["manifest"].tool_sha256
    assert document["verdict"] == "pass"
    assert document["gate_report"] == {
        "path": evidence["gate_path"],
        "sha256": evidence["gate_sha"],
    }

    paths = [entry["path"] for entry in document["files"]]
    assert paths == sorted(paths)
    assert INDEX_RELATIVE_PATH not in paths
    assert set(paths) == {
        "commands/0.stderr.log",
        "commands/0.stdout.log",
        "evidence/gate-report.json",
        "evidence/manifest.json",
        "landing-plan.json",
        "stderr.log",
        "stdout.log",
    }
    for entry in document["files"]:
        target = evidence["run_dir"] / entry["path"]
        assert entry["size"] == target.stat().st_size
        assert entry["sha256"] == sha256_file(target)
    assert evidence["index_sha"] == sha256_file(
        evidence["run_dir"] / INDEX_RELATIVE_PATH
    )


def test_verify_evidence_is_green(evidence):
    verification = _verify(evidence)
    assert verification.ok is True, verification.reasons
    assert verification.reasons == ()
    assert verification.checks == {
        "index": True,
        "identity": True,
        "files": True,
        "gate_report": True,
        "manifest": True,
        "tool_sha256": True,
        "tip": True,
        "ancestry": True,
    }
    assert verification.index == evidence["index"]


def test_verify_evidence_never_writes(evidence):
    root = evidence["run_dir"]
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    _verify(evidence)
    _verify(evidence)
    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert before == after


# ---------------------------------------------------------------------------
# invalidation reasons (D4)
# ---------------------------------------------------------------------------


def test_missing_index(evidence):
    (evidence["run_dir"] / INDEX_RELATIVE_PATH).unlink()
    verification = _verify(evidence)
    assert verification.ok is False
    assert _reasons(verification) == {"index-missing"}


@pytest.mark.parametrize(
    ("content", "reason"),
    [("", "index-empty"), ("{", "index-invalid"), ("[]", "index-invalid")],
)
def test_broken_index_document(evidence, content, reason):
    (evidence["run_dir"] / INDEX_RELATIVE_PATH).write_text(
        content, encoding="utf-8"
    )
    verification = _verify(evidence)
    assert verification.ok is False
    assert reason in _reasons(verification)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("schema_version", 2, "index-schema-version"),
        ("requirement_id", "HKL-W2", "index-requirement-mismatch"),
        ("verdict", "fail", "index-verdict-not-pass"),
    ],
)
def test_index_header_tampering(evidence, field, value, reason):
    document = _read_index(evidence)
    document[field] = value
    _write_index(evidence, document)
    verification = _verify(evidence)
    assert verification.ok is False
    assert reason in _reasons(verification)


def test_identity_mismatch(evidence):
    verification = _verify(evidence, identity=_identity(claim_lock="other"))
    assert verification.ok is False
    assert "identity-mismatch" in _reasons(verification)
    assert verification.checks["identity"] is False


def test_deleted_gate_report(evidence):
    (evidence["run_dir"] / evidence["gate_path"]).unlink()
    verification = _verify(evidence)
    assert verification.ok is False
    reasons = _reasons(verification)
    assert "gate-report-missing" in reasons
    assert f"index-file-missing:{evidence['gate_path']}" in reasons


def test_replaced_gate_report(evidence):
    target = evidence["run_dir"] / evidence["gate_path"]
    replacement = target.read_text(encoding="utf-8").replace(
        '"pass"', '"fail"', 1
    )
    target.write_text(replacement, encoding="utf-8")
    verification = _verify(evidence)
    assert verification.ok is False
    assert "gate-report-digest-mismatch" in _reasons(verification)


def test_gate_report_not_pass_after_index_rebuild(evidence):
    target = evidence["run_dir"] / evidence["gate_path"]
    document = json.loads(target.read_text(encoding="utf-8"))
    document["verdict"] = "fail"
    write_json(target, document)
    _rebuild_index(
        evidence, gate_report_sha256=sha256_file(target)
    )
    verification = _verify(evidence)
    assert verification.ok is False
    assert "gate-report-not-pass" in _reasons(verification)


def test_gate_report_identity_mismatch(evidence):
    target = evidence["run_dir"] / evidence["gate_path"]
    document = json.loads(target.read_text(encoding="utf-8"))
    document["identity"] = _identity(run_id=9).to_dict()
    write_json(target, document)
    _rebuild_index(evidence, gate_report_sha256=sha256_file(target))
    verification = _verify(evidence)
    assert verification.ok is False
    assert "gate-report-identity-mismatch" in _reasons(verification)


def test_gate_report_sync_base_mismatch(evidence):
    target = evidence["run_dir"] / evidence["gate_path"]
    document = json.loads(target.read_text(encoding="utf-8"))
    document["sync_base"] = "f" * 40
    write_json(target, document)
    _rebuild_index(evidence, gate_report_sha256=sha256_file(target))
    verification = _verify(evidence)
    assert verification.ok is False
    assert "gate-report-sync-base-mismatch" in _reasons(verification)


def test_command_log_digest_mismatch(evidence):
    log = evidence["run_dir"] / "commands" / "0.stdout.log"
    log.write_bytes(b"BAD")  # same size as b"out"
    verification = _verify(evidence)
    assert verification.ok is False
    assert "index-file-digest-mismatch:commands/0.stdout.log" in _reasons(
        verification
    )


def test_command_log_truncated_to_zero(evidence):
    log = evidence["run_dir"] / "commands" / "0.stdout.log"
    log.write_bytes(b"")
    verification = _verify(evidence)
    assert verification.ok is False
    assert "index-file-empty:commands/0.stdout.log" in _reasons(verification)


def test_index_file_missing(evidence):
    (evidence["run_dir"] / "commands" / "0.stdout.log").unlink()
    verification = _verify(evidence)
    assert verification.ok is False
    assert "index-file-missing:commands/0.stdout.log" in _reasons(verification)


def test_index_file_size_mismatch(evidence):
    document = _read_index(evidence)
    direct = next(
        entry
        for entry in document["files"]
        if entry["path"] == "commands/0.stdout.log"
    )
    direct["size"] = direct["size"] + 5
    _write_index(evidence, document)
    verification = _verify(evidence)
    assert verification.ok is False
    assert "index-file-size-mismatch:commands/0.stdout.log" in _reasons(
        verification
    )


def test_manifest_tampering(evidence):
    manifest_path = evidence["run_dir"] / "evidence" / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["identity"] = _identity(task_id="t_other").to_dict()
    write_json(manifest_path, document)
    _rebuild_index(evidence)
    verification = _verify(evidence)
    assert verification.ok is False
    assert "manifest-identity-mismatch" in _reasons(verification)


def test_manifest_invalid_document(evidence):
    manifest_path = evidence["run_dir"] / "evidence" / "manifest.json"
    manifest_path.write_text("{", encoding="utf-8")
    verification = _verify(evidence)
    assert verification.ok is False
    reasons = _reasons(verification)
    assert "manifest-invalid" in reasons
    # The truncation is also visible as a registered-file mismatch.
    assert (
        "index-file-size-mismatch:evidence/manifest.json" in reasons
        or "index-file-digest-mismatch:evidence/manifest.json" in reasons
    )


def test_tool_digest_change(evidence):
    (evidence["package"] / "sub" / "mod.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    verification = _verify(evidence)
    assert verification.ok is False
    assert "tool-digest-mismatch" in _reasons(verification)
    assert verification.checks["tool_sha256"] is False


def test_branch_tip_advanced(evidence):
    (evidence["repo"] / "LATER.txt").write_text("later\n", encoding="utf-8")
    _commit(evidence["repo"], "later")
    verification = _verify(evidence)
    assert verification.ok is False
    assert "branch-tip-advanced" in _reasons(verification)
    assert verification.checks["tip"] is False


def test_base_and_sync_must_be_ancestors(evidence):
    target = evidence["run_dir"] / evidence["gate_path"]
    document = json.loads(target.read_text(encoding="utf-8"))
    document["base_commit"] = "f" * 40
    document["sync_base"] = "f" * 40
    write_json(target, document)
    _rebuild_index(
        evidence,
        base_commit="f" * 40,
        sync_base="f" * 40,
        gate_report_sha256=sha256_file(target),
    )
    verification = _verify(evidence)
    assert verification.ok is False
    reasons = _reasons(verification)
    assert "base-not-ancestor" in reasons
    assert "sync-base-not-ancestor" in reasons
    assert verification.checks["ancestry"] is False


def test_code_commit_must_be_resolvable(evidence):
    _rebuild_index(evidence, code_commit="e" * 40)
    verification = _verify(evidence)
    assert verification.ok is False
    assert "code-commit-not-on-branch" in _reasons(verification)


# ---------------------------------------------------------------------------
# hostile index documents
# ---------------------------------------------------------------------------


def test_index_never_registers_itself(evidence):
    document = _read_index(evidence)
    document["files"].append(
        {
            "path": INDEX_RELATIVE_PATH,
            "sha256": sha256_file(evidence["run_dir"] / INDEX_RELATIVE_PATH),
            "size": (evidence["run_dir"] / INDEX_RELATIVE_PATH).stat().st_size,
        }
    )
    _write_index(evidence, document)
    verification = _verify(evidence)
    assert verification.ok is False
    assert "index-self-registered" in _reasons(verification)


def test_index_rejects_unsafe_and_duplicate_paths(evidence):
    document = _read_index(evidence)
    original = document["files"][0]
    document["files"].append(dict(original, path="../escape"))
    _write_index(evidence, document)
    assert "index-path-unsafe:../escape" in _reasons(_verify(evidence))

    document = _read_index(evidence)
    document["files"].append(dict(document["files"][0]))
    _write_index(evidence, document)
    assert (
        f"index-duplicate-path:{document['files'][0]['path']}"
        in _reasons(_verify(evidence))
    )


def test_unregistered_package_file_invalidates(evidence):
    (evidence["run_dir"] / "commands" / "1.stdout.log").write_bytes(b"late")

    verification = _verify(evidence)

    assert verification.ok is False
    assert "index-files-incomplete:commands/1.stdout.log" in _reasons(verification)


def test_empty_file_registry_invalidates(evidence):
    document = _read_index(evidence)
    document["files"] = []
    _write_index(evidence, document)

    verification = _verify(evidence)

    assert verification.ok is False
    assert {
        "index-files-incomplete:evidence/gate-report.json",
        "index-files-incomplete:evidence/manifest.json",
        "index-files-incomplete:stdout.log",
    } <= _reasons(verification)
