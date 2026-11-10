"""Strict implementation/review/closure/merge/gate card contract.

A validated ``CardSpec`` is a *declaration* of what a lane may touch and which
user-trusted executor/repository it names.  It is not an execution permission:
a later phase must re-check real filesystem paths before running anything.  In
particular, passing the string path rules here does not claim symlink
protection or same-UID filesystem isolation.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Any

from .config import LaneConfig, read_json_document
from .contracts import (
    SCHEMA_VERSION,
    ContractError,
    _expect_bool,
    _expect_enum,
    _expect_exact_keys,
    _expect_mapping,
    _expect_relative_path,
    _expect_schema_version,
    _expect_str,
    _expect_unique,
)

CARD_KINDS = frozenset({"implementation", "review", "closure", "merge", "gate"})
EXECUTORS = frozenset({"claude", "codex", "dsh"})
RISKS = frozenset({"normal", "high"})
_CLOSURE_REQUIRED = frozenset({"closure", "merge", "gate"})
_CARD_REQUIRED_KEYS = (
    "schema_version",
    "card_kind",
    "lineage",
    "closure_for",
    "executor",
    "repository",
    "allow",
    "commands",
    "risk",
)
# D8 additive key: absent means "no declared artifacts" and is not an error.
# W8 D3): absent ``review`` means the historical request_review terminal.
_CARD_OPTIONAL_KEYS = ("artifacts", "review")

# -- W8 D2: card-body synthesis -------------------------------------------
# The fences and value shapes match the cards written by ``new_card.py`` and
# read by the DSH lane.  Unknown keys are rejected rather than ignored: a card
# whose block has drifted is a contract error, never a guess.
_LANE_FENCE_RE = re.compile(r"```lane[ \t]*\r?\n(.*?)```", re.S)
_DEVELOPMENT_FENCE_RE = re.compile(r"```development[ \t]*\r?\n(.*?)```", re.S)
_LANE_KEYS = frozenset(
    {
        "project",
        "allow",
        "test_cmd",
        "test_cmd_affected",
        "build_cmd",
        "artifacts",
        "review",
        "resources",
        "executor",
        "model",
        "reasoning_effort",
        "model_fallback",
        "test_timeout",
        "build_timeout",
        "test_scale",
    }
)
# The same operator set the DSH command preflight refuses: these commands
# cannot be represented as a plain argv, so synthesis is fail-closed.
_SHELL_OPERATORS = frozenset("|&;<>`$\n")

SYNTHESIS_MISSING = "card-missing"
SYNTHESIS_INVALID = "card-invalid"
SYNTHESIS_PROJECT_MISSING = "card-project-missing"
SYNTHESIS_REPOSITORY_NOT_CONFIGURED = "repository-not-configured"
SYNTHESIS_COMMAND_UNSUPPORTED = "card-command-unsupported"
REVIEW_FORCED_DEVELOPMENT = "development-requires-review"
REVIEW_FORCED_TASK_TYPE_MISSING = "task-type-missing"
REVIEW_FORCED_TASK_TYPE_UNKNOWN = "task-type-unknown"


class CardSynthesisError(ContractError):
    """A card body that cannot be turned into a ``CardSpec``.

    ``reason`` is the stable code the daemon reports and records on the card;
    the ``ContractError`` message keeps the human-readable detail.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True, slots=True)
class CardCommand:
    """One ``argv`` execution declaration; never a shell string."""

    argv: tuple[str, ...]
    cwd: str

    def __post_init__(self) -> None:
        if not isinstance(self.argv, (list, tuple)):
            raise ContractError("CardSpec.commands.argv: expected an array")
        if not self.argv:
            raise ContractError("CardSpec.commands.argv: must not be empty")
        normalised = tuple(
            _expect_str(item, f"CardSpec.commands.argv[{index}]", non_empty=True)
            for index, item in enumerate(self.argv)
        )
        object.__setattr__(self, "argv", normalised)
        _expect_relative_path(self.cwd, "CardSpec.commands.cwd", allow_dot=True)

    @classmethod
    def from_dict(cls, data: Any, *, field: str = "CardSpec.commands") -> CardCommand:
        mapping = _expect_mapping(data, field)
        _expect_exact_keys(mapping, field, ("argv", "cwd"))
        argv = mapping["argv"]
        if not isinstance(argv, list):
            raise ContractError(f"{field}.argv: expected an array")
        if not argv:
            raise ContractError(f"{field}.argv: must not be empty")
        normalised = tuple(
            _expect_str(item, f"{field}.argv[{index}]", non_empty=True)
            for index, item in enumerate(argv)
        )
        cwd = _expect_relative_path(mapping["cwd"], f"{field}.cwd", allow_dot=True)
        return cls(argv=normalised, cwd=cwd)

    def to_dict(self) -> dict[str, Any]:
        return {"argv": list(self.argv), "cwd": self.cwd}


@dataclass(frozen=True, slots=True)
class CardSpec:
    """A validated card declaration."""

    schema_version: int
    card_kind: str
    lineage: str
    closure_for: str | None
    executor: str
    repository: str
    allow: tuple[str, ...]
    commands: tuple[CardCommand, ...]
    risk: str
    # D8 additive contract: declared run artifacts (relative file paths).
    artifacts: tuple[str, ...] = ()
    # W8 D3: False means "complete after a passing gate instead of review".
    review: bool = True
    # W8 D3: a development (or untyped) card that asked for
    # ``review: false`` is forced back to review; the daemon records the
    # reason as a ``review-forced`` event.  These are derived from the body,
    # never part of the explicit JSON card contract, so ``to_dict`` omits them.
    review_forced: bool = False
    review_forced_reason: str | None = None

    def __post_init__(self) -> None:
        _expect_schema_version(self.schema_version, "CardSpec.schema_version")
        _expect_enum(self.card_kind, "CardSpec.card_kind", CARD_KINDS)
        _expect_str(self.lineage, "CardSpec.lineage", non_empty=True)

        if self.closure_for is not None:
            _expect_str(self.closure_for, "CardSpec.closure_for", non_empty=True)
        if self.card_kind in _CLOSURE_REQUIRED and not self.closure_for:
            raise ContractError(
                f"CardSpec.closure_for: must be non-null for card_kind {self.card_kind}"
            )

        _expect_enum(self.executor, "CardSpec.executor", EXECUTORS)
        _expect_str(self.repository, "CardSpec.repository", non_empty=True)

        allow = self._normalise_allow(self.allow)
        object.__setattr__(self, "allow", allow)

        commands = self._normalise_commands(self.commands)
        if self.card_kind == "implementation" and not commands:
            raise ContractError(
                "CardSpec.commands: implementation cards require at least one command"
            )
        object.__setattr__(self, "commands", commands)

        _expect_enum(self.risk, "CardSpec.risk", RISKS)

        artifacts = self._normalise_artifacts(self.artifacts)
        object.__setattr__(self, "artifacts", artifacts)

        _expect_bool(self.review, "CardSpec.review")

        _expect_bool(self.review_forced, "CardSpec.review_forced")
        if self.review_forced and not self.review:
            raise ContractError(
                "CardSpec.review_forced: requires review=True"
            )
        if self.review_forced_reason is not None:
            _expect_str(
                self.review_forced_reason,
                "CardSpec.review_forced_reason",
                non_empty=True,
            )

    @staticmethod
    def _normalise_artifacts(values: Any) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)):
            raise ContractError("CardSpec.artifacts: expected an array of file paths")
        normalised: list[str] = []
        for index, value in enumerate(values):
            text = _expect_relative_path(value, f"CardSpec.artifacts[{index}]")
            if text.endswith("/"):
                raise ContractError(
                    f"CardSpec.artifacts[{index}]: artifacts must be file paths, "
                    "not directory paths"
                )
            normalised.append(text)
        _expect_unique(normalised, "CardSpec.artifacts")
        return tuple(normalised)

    @staticmethod
    def _normalise_allow(values: Any) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)):
            raise ContractError("CardSpec.allow: expected an array of paths")
        if not values:
            raise ContractError("CardSpec.allow: must not be empty")
        normalised = tuple(
            _expect_relative_path(value, f"CardSpec.allow[{index}]")
            for index, value in enumerate(values)
        )
        _expect_unique(normalised, "CardSpec.allow")
        return normalised

    @staticmethod
    def _normalise_commands(values: Any) -> tuple[CardCommand, ...]:
        if not isinstance(values, (list, tuple)):
            raise ContractError("CardSpec.commands: expected an array")
        commands: list[CardCommand] = []
        for index, value in enumerate(values):
            if isinstance(value, CardCommand):
                commands.append(value)
            elif isinstance(value, dict):
                commands.append(
                    CardCommand.from_dict(value, field=f"CardSpec.commands[{index}]")
                )
            else:
                raise ContractError(
                    f"CardSpec.commands[{index}]: expected an object with argv/cwd"
                )
        return tuple(commands)

    @classmethod
    def from_dict(
        cls,
        data: Any,
        config: LaneConfig,
        *,
        field: str = "CardSpec",
    ) -> CardSpec:
        if not isinstance(config, LaneConfig):
            raise ContractError(f"{field}: a validated LaneConfig is required")

        mapping = _expect_mapping(data, field)
        _expect_exact_keys(
            mapping, field, _CARD_REQUIRED_KEYS, _CARD_OPTIONAL_KEYS
        )

        card = cls(
            schema_version=_expect_schema_version(
                mapping["schema_version"], f"{field}.schema_version"
            ),
            card_kind=_expect_enum(
                mapping["card_kind"], f"{field}.card_kind", CARD_KINDS
            ),
            lineage=_expect_str(
                mapping["lineage"], f"{field}.lineage", non_empty=True
            ),
            closure_for=mapping["closure_for"],
            executor=_expect_enum(
                mapping["executor"], f"{field}.executor", EXECUTORS
            ),
            repository=_expect_str(
                mapping["repository"], f"{field}.repository", non_empty=True
            ),
            allow=mapping["allow"],
            commands=mapping["commands"],
            risk=_expect_enum(mapping["risk"], f"{field}.risk", RISKS),
            artifacts=mapping.get("artifacts", []),
            review=mapping.get("review", True),
        )

        if card.executor not in config.executors:
            raise ContractError(
                f"{field}.executor: executor is not configured as allowed"
            )
        if card.repository not in config.repositories:
            raise ContractError(
                f"{field}.repository: repository is not configured as trusted"
            )
        return card

    def to_dict(self) -> dict[str, Any]:
        document = {
            "schema_version": self.schema_version,
            "card_kind": self.card_kind,
            "lineage": self.lineage,
            "closure_for": self.closure_for,
            "executor": self.executor,
            "repository": self.repository,
            "allow": list(self.allow),
            "commands": [command.to_dict() for command in self.commands],
            "risk": self.risk,
            "artifacts": list(self.artifacts),
        }
        # The default terminal (review) stays out of the document so existing
        # explicit JSON cards round-trip unchanged.
        if not self.review:
            document["review"] = False
        return document


def _parse_fence_block(
    body: str | None, pattern: re.Pattern[str], name: str
) -> dict[str, Any] | None:
    """Parse one fenced YAML block; ``None`` only when the fence is absent."""

    matches = list(pattern.finditer(body or ""))
    if not matches:
        return None
    if len(matches) > 1:
        raise CardSynthesisError(
            SYNTHESIS_INVALID, f"card body has {len(matches)} ```{name}``` blocks"
        )
    try:
        import yaml  # imported lazily: the host runtime provides PyYAML
    except ImportError:
        raise CardSynthesisError(
            SYNTHESIS_INVALID, "YAML parser is unavailable"
        ) from None
    try:
        data = yaml.safe_load(matches[0].group(1))
    except yaml.YAMLError as exc:
        raise CardSynthesisError(
            SYNTHESIS_INVALID, f"```{name}``` block YAML is invalid: {exc}"
        ) from None
    if not isinstance(data, dict):
        raise CardSynthesisError(
            SYNTHESIS_INVALID, f"```{name}``` block must be a YAML mapping"
        )
    return data


def _synthesised_command(command: Any, name: str) -> tuple[str, ...] | None:
    """Convert one declared command string to argv, or fail closed."""

    if not isinstance(command, str):
        raise CardSynthesisError(
            SYNTHESIS_INVALID, f"lane.{name} must be a string"
        )
    text = command.strip()
    if not text:
        return None
    if any(character in text for character in _SHELL_OPERATORS):
        raise CardSynthesisError(
            SYNTHESIS_COMMAND_UNSUPPORTED,
            f"lane.{name} contains shell operators/redirection/variables",
        )
    try:
        argv = shlex.split(text)
    except ValueError as exc:
        raise CardSynthesisError(
            SYNTHESIS_COMMAND_UNSUPPORTED,
            f"lane.{name} cannot be split into argv: {exc}",
        ) from None
    if not argv:
        raise CardSynthesisError(
            SYNTHESIS_COMMAND_UNSUPPORTED, f"lane.{name} is empty after splitting"
        )
    return tuple(argv)


def synthesize_card(
    document_body: str,
    task_id: str,
    executor: str,
    config: LaneConfig,
) -> CardSpec:
    """Build a ``CardSpec`` from the card body's ```lane```/```development``` blocks.

    This is the W8 D2 ingest path used when ``cards/<task_id>.json`` does not
    exist.  It never guesses: a missing block, an unknown key, an unconfigured
    project, a non-argv command or a malformed YAML document raises
    :class:`CardSynthesisError` whose ``reason`` the daemon reports on the card.
    """

    if not isinstance(config, LaneConfig):
        raise CardSynthesisError(
            SYNTHESIS_INVALID, "a validated LaneConfig is required"
        )
    if executor not in config.executors:
        raise CardSynthesisError(
            SYNTHESIS_INVALID, f"executor {executor!r} is not configured"
        )

    lane = _parse_fence_block(document_body, _LANE_FENCE_RE, "lane")
    if lane is None:
        raise CardSynthesisError(
            SYNTHESIS_MISSING, "card body has no ```lane``` block"
        )
    unknown = sorted(set(lane) - _LANE_KEYS)
    if unknown:
        raise CardSynthesisError(
            SYNTHESIS_INVALID,
            "lane block has unknown key(s): " + ", ".join(unknown),
        )

    project = lane.get("project")
    if not isinstance(project, str) or not project.strip():
        raise CardSynthesisError(
            SYNTHESIS_PROJECT_MISSING, "lane.project is required"
        )
    project = project.strip()
    if project not in config.repositories:
        raise CardSynthesisError(
            SYNTHESIS_REPOSITORY_NOT_CONFIGURED,
            f"lane.project {project!r} is not a configured repository",
        )

    allow = lane.get("allow")
    if not isinstance(allow, list) or not allow:
        raise CardSynthesisError(
            SYNTHESIS_INVALID, "lane.allow must be a non-empty array"
        )
    normalised_allow: list[str] = []
    for index, item in enumerate(allow):
        if not isinstance(item, str) or not item.strip():
            raise CardSynthesisError(
                SYNTHESIS_INVALID,
                f"lane.allow[{index}] must be a non-empty string",
            )
        normalised_allow.append(item.strip())

    artifacts = lane.get("artifacts") or []
    if not isinstance(artifacts, list) or not all(
        isinstance(item, str) for item in artifacts
    ):
        raise CardSynthesisError(
            SYNTHESIS_INVALID, "lane.artifacts must be an array of strings"
        )

    requested_review = lane.get("review", True)
    if not isinstance(requested_review, bool):
        raise CardSynthesisError(
            SYNTHESIS_INVALID, "lane.review must be a boolean"
        )

    development = _parse_fence_block(
        document_body, _DEVELOPMENT_FENCE_RE, "development"
    )
    task_type: str | None = None
    risk = "normal"
    if development is not None:
        if "review" in development:
            requested_review = development["review"]
            if not isinstance(requested_review, bool):
                raise CardSynthesisError(
                    SYNTHESIS_INVALID, "development.review must be a boolean"
                )
        task_type = development.get("task_type")
        if task_type is not None and not isinstance(task_type, str):
            raise CardSynthesisError(
                SYNTHESIS_INVALID, "development.task_type must be a string"
            )
        risk = development.get("risk", "normal")
        if not isinstance(risk, str) or risk not in RISKS:
            raise CardSynthesisError(
                SYNTHESIS_INVALID, "development.risk must be normal or high"
            )

    # W8 D3: only a ``non_development`` card may skip review.
    # ``development`` and every untyped/unknown card are fail-closed: they are
    # reviewed anyway, and the override is recorded on the card.
    review = requested_review
    review_forced = False
    review_forced_reason: str | None = None
    if not requested_review:
        if task_type == "non_development":
            review = False
        else:
            review = True
            review_forced = True
            if task_type is None:
                review_forced_reason = REVIEW_FORCED_TASK_TYPE_MISSING
            elif task_type == "development":
                review_forced_reason = REVIEW_FORCED_DEVELOPMENT
            else:
                review_forced_reason = REVIEW_FORCED_TASK_TYPE_UNKNOWN

    commands: list[CardCommand] = []
    for name in ("test_cmd", "build_cmd"):
        argv = _synthesised_command(lane.get(name, ""), name)
        if argv is None:
            continue
        try:
            commands.append(CardCommand(argv=argv, cwd="."))
        except ContractError as exc:
            raise CardSynthesisError(SYNTHESIS_INVALID, str(exc)) from None
    if not commands:
        raise CardSynthesisError(
            SYNTHESIS_INVALID,
            "an implementation card requires lane.test_cmd or lane.build_cmd",
        )

    try:
        return CardSpec(
            schema_version=SCHEMA_VERSION,
            card_kind="implementation",
            lineage=task_id,
            closure_for=None,
            executor=executor,
            repository=project,
            allow=tuple(normalised_allow),
            commands=tuple(commands),
            risk=risk,
            artifacts=tuple(artifacts),
            review=review,
            review_forced=review_forced,
            review_forced_reason=review_forced_reason,
        )
    except CardSynthesisError:
        raise
    except ContractError as exc:
        raise CardSynthesisError(SYNTHESIS_INVALID, str(exc)) from None


def load_card(path: str | os.PathLike[str], config: LaneConfig) -> CardSpec:
    """Read and validate a card file against a validated configuration."""

    document = read_json_document(path, root="CardSpec")
    return CardSpec.from_dict(document, config)


__all__ = [
    "CardCommand",
    "CardSpec",
    "CardSynthesisError",
    "CARD_KINDS",
    "EXECUTORS",
    "RISKS",
    "REVIEW_FORCED_DEVELOPMENT",
    "REVIEW_FORCED_TASK_TYPE_MISSING",
    "REVIEW_FORCED_TASK_TYPE_UNKNOWN",
    "SYNTHESIS_COMMAND_UNSUPPORTED",
    "SYNTHESIS_INVALID",
    "SYNTHESIS_MISSING",
    "SYNTHESIS_PROJECT_MISSING",
    "SYNTHESIS_REPOSITORY_NOT_CONFIGURED",
    "load_card",
    "synthesize_card",
]
