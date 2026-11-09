"""Deterministic evidence digests and manifest construction.

The supervisor — never the executor — builds these files.  ``tool_sha256`` is
reproducible by a reviewer from the source tree alone (spec section 5.4).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..contracts import (
    ContractError,
    EvidenceManifest,
    GateReport,
    RunIdentity,
)
from ..workspace.git import GitError, branch_tip, is_ancestor, resolve_ref

EVIDENCE_REQUIREMENT_ID = "HKL-W3"
INDEX_SCHEMA_VERSION = 1
INDEX_RELATIVE_PATH = "evidence/index.json"

_EXCLUDED_SUFFIXES = (".pyc", ".pyo")
_EXCLUDED_DIRS = frozenset({"__pycache__"})


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tool_digest(package_dir: str | os.PathLike[str]) -> str:
    """Hash every regular file under ``package_dir`` (relpath-sorted lines)."""

    root = Path(package_dir)
    entries: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.suffix in _EXCLUDED_SUFFIXES:
            continue
        entries.append((relative.as_posix(), sha256_file(path)))
    entries.sort(key=lambda item: item[0])
    text = "".join(f"{relpath}\t{digest}\n" for relpath, digest in entries)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_manifest(
    identity: RunIdentity,
    *,
    base_commit: str,
    code_commit: str,
    gate_report_path: str,
    gate_report_sha256: str,
    verdict: str,
    package_dir: str | os.PathLike[str],
) -> EvidenceManifest:
    return EvidenceManifest(
        schema_version=1,
        identity=identity,
        base_commit=base_commit,
        code_commit=code_commit,
        tool_sha256=tool_digest(package_dir),
        gate_report=GateReport(path=gate_report_path, sha256=gate_report_sha256),
        verdict=verdict,
    )


def write_json(path: str | os.PathLike[str], payload: Any) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, target)
    return str(target)


def verify_gate_report(
    run_dir: str | os.PathLike[str], path: str, expected_sha256: str
) -> tuple[bool, str]:
    """Recompute the gate report digest; a mismatch is an explicit error."""

    target = Path(run_dir) / path
    if not target.is_file():
        return False, "gate-report-missing"
    if target.stat().st_size == 0:
        return False, "gate-report-empty"
    actual = sha256_file(target)
    if actual != expected_sha256:
        return False, "gate-report-digest-mismatch"
    return True, "ok"


# ---------------------------------------------------------------------------
# W3 evidence index and independent verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceVerification:
    """Read-only verdict of :func:`verify_evidence`."""

    ok: bool
    reasons: tuple[str, ...]
    checks: dict[str, bool]
    index: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "checks": dict(self.checks),
        }


def _unsafe_relative(path: Any) -> bool:
    if not isinstance(path, str) or not path:
        return True
    if path.startswith("/") or "\\" in path:
        return True
    if len(path) > 1 and path[1] == ":":
        return True
    return any(segment in ("", ".", "..") for segment in path.split("/"))


def _inside(root: Path, path: str) -> bool:
    candidate = os.path.realpath(os.fspath(root / path))
    base = os.path.realpath(os.fspath(root))
    return candidate == base or candidate.startswith(base + os.sep)


def evidence_package_paths(run_dir: str | os.PathLike[str]) -> list[str]:
    """The files registered by ``evidence/index.json`` (index excluded)."""

    root = Path(run_dir)
    paths: set[str] = set()
    for relative in ("evidence/gate-report.json", "evidence/manifest.json"):
        if (root / relative).is_file():
            paths.add(relative)
    commands = root / "commands"
    if commands.is_dir():
        for entry in commands.iterdir():
            if entry.is_file() and (
                entry.name.endswith(".stdout.log")
                or entry.name.endswith(".stderr.log")
            ):
                paths.add(f"commands/{entry.name}")
    for relative in ("stdout.log", "stderr.log", "landing-plan.json"):
        if (root / relative).is_file():
            paths.add(relative)
    return sorted(paths)


def write_evidence_index(
    run_dir: str | os.PathLike[str],
    *,
    identity: RunIdentity,
    base_commit: str,
    sync_base: str,
    code_commit: str,
    tool_sha256: str,
    gate_report_path: str,
    gate_report_sha256: str,
    verdict: str,
    extra_paths: Iterable[str] = (),
    requirement_id: str = EVIDENCE_REQUIREMENT_ID,
) -> tuple[dict[str, Any], str]:
    """Write ``evidence/index.json`` and return ``(document, sha256)``.

    Paths are run-directory relative; every entry is a regular file inside the
    run directory.  The index never registers itself.
    """

    root = Path(run_dir)
    candidates = set(evidence_package_paths(root)) | set(extra_paths)
    candidates.discard(INDEX_RELATIVE_PATH)
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for relative in sorted(candidates):
        if _unsafe_relative(relative) or not _inside(root, relative):
            raise ValueError(f"evidence path escapes run directory: {relative}")
        if relative in seen:
            continue
        seen.add(relative)
        target = root / relative
        if not target.is_file():
            raise FileNotFoundError(f"evidence file missing: {relative}")
        files.append(
            {
                "path": relative,
                "sha256": sha256_file(target),
                "size": target.stat().st_size,
            }
        )
    gate_sha = gate_report_sha256
    for entry in files:
        if entry["path"] == gate_report_path:
            gate_sha = entry["sha256"]
    document = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "requirement_id": requirement_id,
        "identity": identity.to_dict(),
        "base_commit": base_commit,
        "sync_base": sync_base,
        "code_commit": code_commit,
        "tool_sha256": tool_sha256,
        "gate_report": {"path": gate_report_path, "sha256": gate_sha},
        "files": files,
        "verdict": verdict,
    }
    index_path = root / INDEX_RELATIVE_PATH
    write_json(index_path, document)
    return document, sha256_file(index_path)


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, "missing"
    if path.stat().st_size == 0:
        return None, "empty"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "invalid"
    if not isinstance(document, dict):
        return None, "invalid"
    return document, None


def verify_evidence(
    run_dir: str | os.PathLike[str],
    identity: RunIdentity,
    package_dir: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
    repo: str | os.PathLike[str] | None,
    main_ref: str | None = None,
    *,
    requirement_id: str = EVIDENCE_REQUIREMENT_ID,
) -> EvidenceVerification:
    """Recompute every registered digest and every W3 invalidation rule.

    Read-only: this function never creates, rewrites or deletes a file.  Every
    failure appends a distinct reason so a reviewer can tell *what* was
    tampered with, not merely that something was.
    """

    root = Path(run_dir)
    check_names = (
        "index",
        "identity",
        "files",
        "gate_report",
        "manifest",
        "tool_sha256",
        "tip",
        "ancestry",
    )
    checks = {name: False for name in check_names}
    reasons: list[str] = []
    index: dict[str, Any] | None = None

    # Main-ref resolution and merge-tree are landing concerns (D6); D4 does not
    # invalidate evidence merely because a caller did not name a main ref.
    _ = main_ref

    index, problem = _read_json_object(root / INDEX_RELATIVE_PATH)
    if problem == "missing":
        reasons.append("index-missing")
    elif problem == "empty":
        reasons.append("index-empty")
    elif problem == "invalid":
        reasons.append("index-invalid")
    else:
        assert index is not None
        if index.get("schema_version") != INDEX_SCHEMA_VERSION:
            reasons.append("index-schema-version")
        if index.get("requirement_id") != requirement_id:
            reasons.append("index-requirement-mismatch")
        if index.get("verdict") != "pass":
            reasons.append("index-verdict-not-pass")
        checks["index"] = not any(
            reason.startswith("index-") for reason in reasons
        )

    if index is None:
        return EvidenceVerification(False, tuple(reasons), checks, None)

    if index.get("identity") != identity.to_dict():
        reasons.append("identity-mismatch")
    else:
        checks["identity"] = True

    files = index.get("files")
    files_ok = isinstance(files, list)
    registered: set[str] = set()
    if not files_ok:
        reasons.append("index-files-invalid")
    else:
        for entry in files:
            if not isinstance(entry, dict):
                files_ok = False
                reasons.append("index-file-entry-invalid")
                continue
            relative = entry.get("path")
            if _unsafe_relative(relative) or not _inside(root, str(relative)):
                files_ok = False
                reasons.append(f"index-path-unsafe:{relative}")
                continue
            path_text = str(relative)
            if path_text == INDEX_RELATIVE_PATH:
                files_ok = False
                reasons.append("index-self-registered")
                continue
            if path_text in registered:
                files_ok = False
                reasons.append(f"index-duplicate-path:{path_text}")
                continue
            registered.add(path_text)
            target = root / path_text
            if not target.is_file():
                files_ok = False
                reasons.append(f"index-file-missing:{path_text}")
                continue
            actual_size = target.stat().st_size
            recorded_size = entry.get("size")
            if actual_size == 0 and recorded_size != 0:
                # A registered non-empty file was truncated to zero bytes.
                files_ok = False
                reasons.append(f"index-file-empty:{path_text}")
                continue
            if recorded_size != actual_size:
                files_ok = False
                reasons.append(f"index-file-size-mismatch:{path_text}")
                continue
            if entry.get("sha256") != sha256_file(target):
                files_ok = False
                reasons.append(f"index-file-digest-mismatch:{path_text}")
        if files_ok:
            # D3: the index registers *every* file in the evidence package, so
            # a missing or unregistered entry is itself an invalidation.
            expected = set(evidence_package_paths(root))
            for relative in sorted(expected - registered):
                files_ok = False
                reasons.append(f"index-files-incomplete:{relative}")
            for relative in sorted(registered - expected):
                files_ok = False
                reasons.append(f"index-file-unregistered:{relative}")
    checks["files"] = bool(files_ok)

    # -- gate report -------------------------------------------------------
    gate_entry = index.get("gate_report")
    gate_path = gate_entry.get("path") if isinstance(gate_entry, dict) else None
    gate_sha = gate_entry.get("sha256") if isinstance(gate_entry, dict) else None
    gate_ok = isinstance(gate_path, str) and isinstance(gate_sha, str)
    gate_document: dict[str, Any] | None = None
    if not gate_ok:
        reasons.append("gate-report-entry-invalid")
    else:
        assert isinstance(gate_path, str)
        if _unsafe_relative(gate_path) or not _inside(root, gate_path):
            gate_ok = False
            reasons.append("gate-report-path-unsafe")
        else:
            target = root / gate_path
            if not target.is_file():
                gate_ok = False
                reasons.append("gate-report-missing")
            elif target.stat().st_size == 0:
                gate_ok = False
                reasons.append("gate-report-empty")
            elif sha256_file(target) != gate_sha:
                gate_ok = False
                reasons.append("gate-report-digest-mismatch")
            else:
                gate_document, problem = _read_json_object(target)
                if gate_document is None:
                    gate_ok = False
                    reasons.append("gate-report-invalid")
    if gate_ok and gate_document is not None:
        if gate_document.get("verdict") != "pass":
            gate_ok = False
            reasons.append("gate-report-not-pass")
        if gate_document.get("identity") != index.get("identity"):
            gate_ok = False
            reasons.append("gate-report-identity-mismatch")
        if gate_document.get("base_commit") != index.get("base_commit"):
            gate_ok = False
            reasons.append("gate-report-base-mismatch")
        report_sync = gate_document.get("sync_base")
        if report_sync is not None and report_sync != index.get("sync_base"):
            gate_ok = False
            reasons.append("gate-report-sync-base-mismatch")
        if gate_document.get("requirement_id") != requirement_id:
            gate_ok = False
            reasons.append("gate-report-requirement-mismatch")
    checks["gate_report"] = bool(gate_ok)

    # -- manifest ----------------------------------------------------------
    manifest, problem = _read_json_object(root / "evidence" / "manifest.json")
    manifest_ok = manifest is not None
    if problem == "missing":
        reasons.append("manifest-missing")
    elif problem == "empty":
        reasons.append("manifest-empty")
    elif problem == "invalid":
        reasons.append("manifest-invalid")
    elif manifest is not None:
        try:
            parsed_manifest = EvidenceManifest.from_dict(manifest)
        except ContractError:
            manifest_ok = False
            reasons.append("manifest-invalid")
        else:
            if parsed_manifest.identity.to_dict() != index.get("identity"):
                manifest_ok = False
                reasons.append("manifest-identity-mismatch")
            if parsed_manifest.base_commit != index.get("base_commit"):
                manifest_ok = False
                reasons.append("manifest-base-mismatch")
            if parsed_manifest.code_commit != index.get("code_commit"):
                manifest_ok = False
                reasons.append("manifest-code-mismatch")
            if parsed_manifest.verdict != "pass":
                manifest_ok = False
                reasons.append("manifest-verdict-not-pass")
            if (
                parsed_manifest.gate_report.path != gate_path
                or parsed_manifest.gate_report.sha256 != gate_sha
            ):
                manifest_ok = False
                reasons.append("manifest-gate-report-mismatch")
    checks["manifest"] = bool(manifest_ok)

    # -- tool digest -------------------------------------------------------
    try:
        current_tool_digest = tool_digest(package_dir)
    except OSError:
        current_tool_digest = None
    tool_ok = (
        current_tool_digest is not None
        and index.get("tool_sha256") == current_tool_digest
        and (
            manifest is None or manifest.get("tool_sha256") == current_tool_digest
        )
    )
    if not tool_ok:
        reasons.append("tool-digest-mismatch")
    checks["tool_sha256"] = tool_ok

    # -- git identity of the code -----------------------------------------
    git_repo = repo if repo is not None else workspace
    code_commit = index.get("code_commit")
    base_commit = index.get("base_commit")
    sync_base = index.get("sync_base")
    tip_ok = False
    if not isinstance(code_commit, str) or resolve_ref(
        code_commit, repo=git_repo
    ) is None:
        reasons.append("code-commit-not-on-branch")
    else:
        try:
            tip = branch_tip(workspace)
        except GitError:
            reasons.append("workspace-unreadable")
        else:
            if tip != code_commit:
                reasons.append("branch-tip-advanced")
            else:
                tip_ok = True
    checks["tip"] = tip_ok

    ancestry_ok = False
    if isinstance(code_commit, str):
        try:
            base_ok = isinstance(base_commit, str) and is_ancestor(
                base_commit, code_commit, repo=git_repo
            )
            sync_ok = isinstance(sync_base, str) and is_ancestor(
                sync_base, code_commit, repo=git_repo
            )
        except GitError:
            base_ok = sync_ok = False
        if not base_ok:
            reasons.append("base-not-ancestor")
        if not sync_ok:
            reasons.append("sync-base-not-ancestor")
        ancestry_ok = bool(base_ok and sync_ok)
    else:
        reasons.append("base-not-ancestor")
        reasons.append("sync-base-not-ancestor")
    checks["ancestry"] = ancestry_ok

    ok = all(checks.values()) and not reasons
    return EvidenceVerification(ok, tuple(reasons), checks, index)


__all__ = [
    "EVIDENCE_REQUIREMENT_ID",
    "EvidenceVerification",
    "INDEX_RELATIVE_PATH",
    "build_manifest",
    "evidence_package_paths",
    "sha256_bytes",
    "sha256_file",
    "tool_digest",
    "verify_evidence",
    "verify_gate_report",
    "write_evidence_index",
    "write_json",
]
