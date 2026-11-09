"""Strict data contracts for W1.

The three public value types in this module (``RunIdentity``,
``ExecutorResult`` and ``EvidenceManifest``) are frozen dataclasses with strict
``from_dict``/``to_dict`` methods.  "Strict" means:

* unknown keys, missing required keys, wrong JSON types and invalid enum values
  are rejected at every nesting level;
* Python ``bool`` is never accepted where an integer is expected;
* non-finite numbers (``NaN``/``Infinity``) are rejected;
* ``from_dict`` never mutates its input and ``to_dict`` returns fresh,
  JSON-serialisable objects.

A successful validation is only a *structural* check.  It does not prove that
the caller may write to a board, that a path is safe to execute in, or that a
digest matches a real file.  Those are runtime concerns for later phases.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1

_HEX_RE = re.compile(r"^[0-9a-f]+$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class ContractError(ValueError):
    """Raised when a contract document is structurally invalid.

    Messages contain a field path such as ``ExecutorResult.usage.cost_usd`` and
    never the whole input document or credentials.
    """


# ---------------------------------------------------------------------------
# Shared strict helpers
# ---------------------------------------------------------------------------


def _field_path(parent: str, name: str) -> str:
    return f"{parent}.{name}" if parent else name


def _describe(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _expect_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{path}: expected a JSON object, got {_describe(value)}")
    for key in value:
        if not isinstance(key, str):
            raise ContractError(f"{path}: object keys must be strings")
    return value


def _expect_exact_keys(
    mapping: Mapping[str, Any],
    path: str,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    allowed = frozenset(required) | frozenset(optional)
    unknown = sorted(key for key in mapping if key not in allowed)
    if unknown:
        rendered = ", ".join(unknown[:8])
        if len(unknown) > 8:
            rendered += ", ..."
        raise ContractError(f"{path}: unknown field(s): {rendered}")
    missing = [key for key in required if key not in mapping]
    if missing:
        raise ContractError(f"{path}: missing required field(s): {', '.join(missing)}")


def _expect_int(
    value: Any,
    path: str,
    *,
    minimum: int | None = None,
    positive: bool = False,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{path}: expected an integer, got {_describe(value)}")
    if positive and value <= 0:
        raise ContractError(f"{path}: expected a positive integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{path}: expected an integer >= {minimum}")
    return value


def _expect_optional_int(value: Any, path: str, *, minimum: int | None = None) -> int | None:
    if value is None:
        return None
    return _expect_int(value, path, minimum=minimum)


def _expect_number(
    value: Any,
    path: str,
    *,
    minimum: int | float | None = None,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{path}: expected a number, got {_describe(value)}")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        # An ``int`` beyond float's range cannot be tested for finiteness.
        raise ContractError(f"{path}: expected a finite number") from None
    if not finite:
        raise ContractError(f"{path}: expected a finite number")
    if minimum is not None and value < minimum:
        raise ContractError(f"{path}: expected a number >= {minimum}")
    return value


def _expect_optional_number(
    value: Any,
    path: str,
    *,
    minimum: int | float | None = None,
) -> int | float | None:
    if value is None:
        return None
    return _expect_number(value, path, minimum=minimum)


def _expect_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{path}: expected a boolean, got {_describe(value)}")
    return value


def _expect_str(value: Any, path: str, *, non_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{path}: expected a string, got {_describe(value)}")
    if non_empty and value == "":
        raise ContractError(f"{path}: must not be empty")
    if "\x00" in value:
        raise ContractError(f"{path}: must not contain NUL")
    return value


def _expect_enum(value: Any, path: str, allowed: frozenset[str]) -> str:
    text = _expect_str(value, path)
    if text not in allowed:
        raise ContractError(f"{path}: expected one of {', '.join(sorted(allowed))}")
    return text


def _expect_schema_version(value: Any, path: str) -> int:
    version = _expect_int(value, path)
    if version > SCHEMA_VERSION:
        # W6 D7: fail closed on a document written by a newer release; never
        # migrate or rewrite it silently.
        raise ContractError(
            f"{path}: schema-newer-than-supported: {version} > {SCHEMA_VERSION}"
        )
    if version != SCHEMA_VERSION:
        raise ContractError(f"{path}: unsupported schema_version; expected {SCHEMA_VERSION}")
    return version


def _expect_hex(value: Any, path: str, lengths: tuple[int, ...]) -> str:
    text = _expect_str(value, path, non_empty=True)
    if len(text) not in lengths or not _HEX_RE.fullmatch(text):
        rendered = " or ".join(str(length) for length in lengths)
        raise ContractError(f"{path}: expected {rendered} lowercase hexadecimal characters")
    return text


def _expect_relative_path(value: Any, path: str, *, allow_dot: bool = False) -> str:
    """Validate a repository-relative path without normalising it."""

    text = _expect_str(value, path, non_empty=True)
    if text.startswith("/"):
        raise ContractError(f"{path}: absolute paths are not allowed")
    if "\\" in text:
        raise ContractError(f"{path}: backslashes are not allowed")
    if _WINDOWS_DRIVE_RE.match(text):
        raise ContractError(f"{path}: Windows drive paths are not allowed")
    if allow_dot and text == ".":
        return text
    core = text[:-1] if text.endswith("/") else text
    if core == "":
        raise ContractError(f"{path}: path must not be empty")
    segments = core.split("/")
    if any(segment == "" for segment in segments):
        raise ContractError(f"{path}: empty path segments are not allowed")
    if any(segment in (".", "..") for segment in segments):
        raise ContractError(f"{path}: '.' and '..' path segments are not allowed")
    return text


def _expect_absolute_path(value: Any, path: str) -> str:
    """Validate an absolute host path without expanding or normalising it."""

    text = _expect_str(value, path, non_empty=True)
    if "\\" in text:
        raise ContractError(f"{path}: backslashes are not allowed")
    if text.startswith("//"):
        raise ContractError(f"{path}: UNC-style paths are not allowed")
    if not os.path.isabs(text):
        raise ContractError(f"{path}: expected an absolute path")
    segments = text.split("/")
    if "." in segments or ".." in segments:
        raise ContractError(f"{path}: '.' and '..' path segments are not allowed")
    return text


def _expect_unique(items: Iterable[str], path: str) -> None:
    seen: set[str] = set()
    for index, item in enumerate(items):
        if item in seen:
            raise ContractError(f"{path}[{index}]: duplicate path")
        seen.add(item)


def _normalise_path_tuple(values: Any, path: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ContractError(f"{path}: expected an array of paths, got {_describe(values)}")
    items = tuple(
        _expect_relative_path(value, f"{path}[{index}]")
        for index, value in enumerate(values)
    )
    _expect_unique(items, path)
    return items


# ---------------------------------------------------------------------------
# RunIdentity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """The task/run/claim tuple carried by results and evidence.

    This type only carries identity.  Possessing a valid ``RunIdentity`` does
    not demonstrate write permission; the run/claim fence must still be checked
    in the same transaction that performs a board write.
    """

    schema_version: int
    task_id: str
    run_id: int
    claim_lock: str

    def __post_init__(self) -> None:
        _expect_schema_version(self.schema_version, "RunIdentity.schema_version")
        _expect_str(self.task_id, "RunIdentity.task_id", non_empty=True)
        _expect_int(self.run_id, "RunIdentity.run_id", positive=True)
        _expect_str(self.claim_lock, "RunIdentity.claim_lock", non_empty=True)

    @classmethod
    def from_dict(cls, data: Any, *, field: str = "RunIdentity") -> RunIdentity:
        mapping = _expect_mapping(data, field)
        _expect_exact_keys(
            mapping,
            field,
            ("schema_version", "task_id", "run_id", "claim_lock"),
        )
        return cls(
            schema_version=_expect_schema_version(
                mapping["schema_version"], _field_path(field, "schema_version")
            ),
            task_id=_expect_str(
                mapping["task_id"], _field_path(field, "task_id"), non_empty=True
            ),
            run_id=_expect_int(
                mapping["run_id"], _field_path(field, "run_id"), positive=True
            ),
            claim_lock=_expect_str(
                mapping["claim_lock"], _field_path(field, "claim_lock"), non_empty=True
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "claim_lock": self.claim_lock,
        }


# ---------------------------------------------------------------------------
# ExecutorResult
# ---------------------------------------------------------------------------

RESULT_CATEGORIES = frozenset(
    {
        "success",
        "code_failure",
        "environment_failure",
        "cancelled",
        "timeout",
        # W3 D8: a baseline merge conflict is a terminal run category that does
        # not imply a non-zero executor exit code.
        "baseline_conflict",
    }
)
_NONZERO_CATEGORIES = frozenset({"code_failure", "environment_failure"})


@dataclass(frozen=True, slots=True)
class Usage:
    """Token/cost accounting; ``None`` means "not accounted", never zero."""

    input_tokens: int | None
    output_tokens: int | None
    cost_usd: int | float | None

    def __post_init__(self) -> None:
        path = "ExecutorResult.usage"
        _expect_optional_int(self.input_tokens, f"{path}.input_tokens", minimum=0)
        _expect_optional_int(self.output_tokens, f"{path}.output_tokens", minimum=0)
        _expect_optional_number(self.cost_usd, f"{path}.cost_usd", minimum=0)

    @classmethod
    def from_dict(cls, data: Any, *, field: str = "ExecutorResult.usage") -> Usage:
        mapping = _expect_mapping(data, field)
        _expect_exact_keys(mapping, field, ("input_tokens", "output_tokens", "cost_usd"))
        return cls(
            input_tokens=_expect_optional_int(
                mapping["input_tokens"], _field_path(field, "input_tokens"), minimum=0
            ),
            output_tokens=_expect_optional_int(
                mapping["output_tokens"], _field_path(field, "output_tokens"), minimum=0
            ),
            cost_usd=_expect_optional_number(
                mapping["cost_usd"], _field_path(field, "cost_usd"), minimum=0
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True, slots=True)
class ExecutorResult:
    """The reported outcome of one executor run."""

    schema_version: int
    identity: RunIdentity
    category: str
    exit_code: int
    changed_paths: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    usage: Usage

    def __post_init__(self) -> None:
        _expect_schema_version(self.schema_version, "ExecutorResult.schema_version")
        if not isinstance(self.identity, RunIdentity):
            raise ContractError("ExecutorResult.identity: expected a RunIdentity")
        _expect_enum(self.category, "ExecutorResult.category", RESULT_CATEGORIES)
        _expect_int(self.exit_code, "ExecutorResult.exit_code")
        object.__setattr__(
            self,
            "changed_paths",
            _normalise_path_tuple(self.changed_paths, "ExecutorResult.changed_paths"),
        )
        object.__setattr__(
            self,
            "artifact_paths",
            _normalise_path_tuple(self.artifact_paths, "ExecutorResult.artifact_paths"),
        )
        if not isinstance(self.usage, Usage):
            raise ContractError("ExecutorResult.usage: expected a Usage object")
        if self.category == "success" and self.exit_code != 0:
            raise ContractError(
                "ExecutorResult.exit_code: success requires exit_code 0"
            )
        if self.category in _NONZERO_CATEGORIES and self.exit_code == 0:
            raise ContractError(
                f"ExecutorResult.exit_code: {self.category} requires a non-zero exit_code"
            )

    @classmethod
    def from_dict(cls, data: Any, *, field: str = "ExecutorResult") -> ExecutorResult:
        mapping = _expect_mapping(data, field)
        _expect_exact_keys(
            mapping,
            field,
            (
                "schema_version",
                "identity",
                "category",
                "exit_code",
                "changed_paths",
                "artifact_paths",
                "usage",
            ),
        )
        return cls(
            schema_version=_expect_schema_version(
                mapping["schema_version"], _field_path(field, "schema_version")
            ),
            identity=RunIdentity.from_dict(
                mapping["identity"], field=_field_path(field, "identity")
            ),
            category=_expect_enum(
                mapping["category"], _field_path(field, "category"), RESULT_CATEGORIES
            ),
            exit_code=_expect_int(
                mapping["exit_code"], _field_path(field, "exit_code")
            ),
            changed_paths=_normalise_path_tuple(
                mapping["changed_paths"], _field_path(field, "changed_paths")
            ),
            artifact_paths=_normalise_path_tuple(
                mapping["artifact_paths"], _field_path(field, "artifact_paths")
            ),
            usage=Usage.from_dict(mapping["usage"], field=_field_path(field, "usage")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity.to_dict(),
            "category": self.category,
            "exit_code": self.exit_code,
            "changed_paths": list(self.changed_paths),
            "artifact_paths": list(self.artifact_paths),
            "usage": self.usage.to_dict(),
        }


# ---------------------------------------------------------------------------
# EvidenceManifest
# ---------------------------------------------------------------------------

VERDICTS = frozenset({"pass", "fail", "blocked"})


@dataclass(frozen=True, slots=True)
class GateReport:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        _expect_relative_path(self.path, "EvidenceManifest.gate_report.path")
        _expect_hex(self.sha256, "EvidenceManifest.gate_report.sha256", (64,))

    @classmethod
    def from_dict(cls, data: Any, *, field: str = "EvidenceManifest.gate_report") -> GateReport:
        mapping = _expect_mapping(data, field)
        _expect_exact_keys(mapping, field, ("path", "sha256"))
        return cls(
            path=_expect_relative_path(mapping["path"], _field_path(field, "path")),
            sha256=_expect_hex(
                mapping["sha256"], _field_path(field, "sha256"), (64,)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class EvidenceManifest:
    """Structural evidence manifest (W1 does not recompute digests)."""

    schema_version: int
    identity: RunIdentity
    base_commit: str
    code_commit: str
    tool_sha256: str
    gate_report: GateReport
    verdict: str

    def __post_init__(self) -> None:
        _expect_schema_version(self.schema_version, "EvidenceManifest.schema_version")
        if not isinstance(self.identity, RunIdentity):
            raise ContractError("EvidenceManifest.identity: expected a RunIdentity")
        _expect_hex(self.base_commit, "EvidenceManifest.base_commit", (40, 64))
        _expect_hex(self.code_commit, "EvidenceManifest.code_commit", (40, 64))
        _expect_hex(self.tool_sha256, "EvidenceManifest.tool_sha256", (64,))
        if not isinstance(self.gate_report, GateReport):
            raise ContractError("EvidenceManifest.gate_report: expected a GateReport")
        _expect_enum(self.verdict, "EvidenceManifest.verdict", VERDICTS)

    @classmethod
    def from_dict(cls, data: Any, *, field: str = "EvidenceManifest") -> EvidenceManifest:
        mapping = _expect_mapping(data, field)
        _expect_exact_keys(
            mapping,
            field,
            (
                "schema_version",
                "identity",
                "base_commit",
                "code_commit",
                "tool_sha256",
                "gate_report",
                "verdict",
            ),
        )
        return cls(
            schema_version=_expect_schema_version(
                mapping["schema_version"], _field_path(field, "schema_version")
            ),
            identity=RunIdentity.from_dict(
                mapping["identity"], field=_field_path(field, "identity")
            ),
            base_commit=_expect_hex(
                mapping["base_commit"], _field_path(field, "base_commit"), (40, 64)
            ),
            code_commit=_expect_hex(
                mapping["code_commit"], _field_path(field, "code_commit"), (40, 64)
            ),
            tool_sha256=_expect_hex(
                mapping["tool_sha256"], _field_path(field, "tool_sha256"), (64,)
            ),
            gate_report=GateReport.from_dict(
                mapping["gate_report"], field=_field_path(field, "gate_report")
            ),
            verdict=_expect_enum(
                mapping["verdict"], _field_path(field, "verdict"), VERDICTS
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity.to_dict(),
            "base_commit": self.base_commit,
            "code_commit": self.code_commit,
            "tool_sha256": self.tool_sha256,
            "gate_report": self.gate_report.to_dict(),
            "verdict": self.verdict,
        }


__all__ = [
    "ContractError",
    "EvidenceManifest",
    "ExecutorResult",
    "GateReport",
    "RunIdentity",
    "SCHEMA_VERSION",
    "Usage",
    "RESULT_CATEGORIES",
    "VERDICTS",
]
