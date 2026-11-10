"""Strict, read-only lane configuration.

``load_config`` reads a JSON document and validates it.  It never creates
directories or databases, probes executables, expands ``~``/environment
variables, or contacts the network.  Whether the configured paths exist is a
runtime question for a later phase.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .contracts import (
    ContractError,
    _expect_absolute_path,
    _expect_bool,
    _expect_enum,
    _expect_exact_keys,
    _expect_hex,
    _expect_int,
    _expect_mapping,
    _expect_schema_version,
    _expect_str,
)

CONFIG_KEYS = (
    "schema_version",
    "hermes_source",
    "hermes_commit",
    "boards",
    "repositories",
    "executors",
    "work_root",
    "max_concurrent",
    "review",
    "service",
    "logging",
)
_OPTIONAL_CONFIG_KEYS = ("review", "service", "logging")
_REQUIRED_CONFIG_KEYS = tuple(
    key for key in CONFIG_KEYS if key not in _OPTIONAL_CONFIG_KEYS
)
_ALLOWED_EXECUTORS = frozenset({"claude", "codex", "dsh"})
REVIEW_KEYS = (
    "reviewer",
    "max_rework_rounds",
    "authorization_dir",
    "merge",
)
MERGE_KEYS = (
    "enabled",
    "main_ref",
    "post_merge_commands",
    "post_merge_timeout_seconds",
)
DEFAULT_REVIEWER = "code-reviewer"
DEFAULT_MAX_REWORK_ROUNDS = 3
DEFAULT_MAIN_REF = "refs/heads/main"
DEFAULT_POST_MERGE_TIMEOUT_SECONDS = 3600.0

# -- W6 D3: optional service/logging blocks --------------------------------
SERVICE_KEYS = ("platform", "label", "log_root", "restart")
LOGGING_KEYS = (
    "retention_days",
    "max_total_bytes",
    "max_log_bytes",
    "backups",
    "keep_failed_runs",
)
SERVICE_PLATFORMS = frozenset({"launchd", "systemd"})
SERVICE_RESTARTS = frozenset({"on-failure", "always", "never"})
DEFAULT_SERVICE_LABEL = "com.hermes-kanban-lanes.daemon"
DEFAULT_SERVICE_RESTART = "on-failure"
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_LOG_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 3
DEFAULT_KEEP_FAILED_RUNS = 10


def read_json_document(path: str | os.PathLike[str], *, root: str) -> Any:
    """Read one UTF-8 JSON document with strict parsing.

    Duplicate object keys and non-finite numbers are rejected rather than
    silently collapsed.  File-system errors are converted to ``ContractError``
    so callers (notably the CLI) can return a JSON error instead of a traceback.
    """

    try:
        file_path = os.fspath(path)
    except TypeError:
        raise ContractError(f"{root}: expected a filesystem path") from None

    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise ContractError(f"{root}: cannot read input file ({reason})") from None
    except UnicodeDecodeError:
        raise ContractError(f"{root}: input is not valid UTF-8") from None
    except ValueError:
        # ``open()`` raises ``ValueError`` for paths it cannot represent, such
        # as one containing an embedded NUL byte.
        raise ContractError(f"{root}: cannot read input file (invalid path)") from None

    def _reject_constant(token: str) -> Any:
        raise ContractError(f"{root}: non-finite JSON number {token} is not allowed")

    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContractError(f"{root}: duplicate JSON object key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except ContractError:
        # ``parse_constant``/``object_pairs_hook`` may raise ``ContractError``
        # (a ``ValueError`` subclass); keep its message intact.
        raise
    except json.JSONDecodeError as exc:
        raise ContractError(
            f"{root}: invalid JSON at line {exc.lineno} column {exc.colno}"
        ) from None
    except RecursionError:
        raise ContractError(f"{root}: input JSON is nested too deeply") from None
    except ValueError:
        # e.g. an integer literal too long to convert; never echo the value.
        raise ContractError(f"{root}: invalid JSON value") from None


@dataclass(frozen=True, slots=True)
class Repository:
    """A user-trusted repository entry."""

    path: str
    trusted: bool

    def __post_init__(self) -> None:
        _expect_absolute_path(self.path, "LaneConfig.repositories.path")
        _expect_bool(self.trusted, "LaneConfig.repositories.trusted")
        if self.trusted is not True:
            raise ContractError("LaneConfig.repositories.trusted: must be true")

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "trusted": True}


@dataclass(frozen=True, slots=True)
class Executor:
    """An executor command line and the board assignees it claims (D1).

    ``assignees`` omitted (``()``) means the historical ``hkl:<alias>``
    convention; :meth:`effective_assignees` resolves that default.  An explicit
    list is validated as non-empty and de-duplicated in declaration order by
    ``LaneConfig.from_dict``.
    """

    argv: tuple[str, ...]
    assignees: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.argv, (list, tuple)):
            raise ContractError("LaneConfig.executors.argv: expected an array")
        if not self.argv:
            raise ContractError("LaneConfig.executors.argv: must not be empty")
        normalised = tuple(
            _expect_str(item, f"LaneConfig.executors.argv[{index}]", non_empty=True)
            for index, item in enumerate(self.argv)
        )
        object.__setattr__(self, "argv", normalised)

        if not isinstance(self.assignees, (list, tuple)):
            raise ContractError(
                "LaneConfig.executors.assignees: expected an array"
            )
        seen: set[str] = set()
        assignees: list[str] = []
        for index, item in enumerate(self.assignees):
            text = _expect_str(
                item,
                f"LaneConfig.executors.assignees[{index}]",
                non_empty=True,
            )
            if not text.strip():
                raise ContractError(
                    f"LaneConfig.executors.assignees[{index}]: must not be blank"
                )
            if text in seen:
                continue
            seen.add(text)
            assignees.append(text)
        object.__setattr__(self, "assignees", tuple(assignees))

    def effective_assignees(self, alias: str) -> tuple[str, ...]:
        """The assignees to discover for ``alias`` (default ``hkl:<alias>``)."""

        return self.assignees or (f"hkl:{alias}",)

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {"argv": list(self.argv)}
        # Omitted defaults stay omitted so an existing config round-trips
        # byte-for-byte (and its fingerprint does not change).
        if self.assignees:
            document["assignees"] = list(self.assignees)
        return document


@dataclass(frozen=True, slots=True)
class MergeConfig:
    """The optional merge block of the review configuration (D11)."""

    enabled: bool = False
    main_ref: str = DEFAULT_MAIN_REF
    post_merge_commands: tuple[tuple[str, ...], ...] = ()
    post_merge_timeout_seconds: float = DEFAULT_POST_MERGE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        _expect_bool(self.enabled, "LaneConfig.review.merge.enabled")
        _expect_str(self.main_ref, "LaneConfig.review.merge.main_ref", non_empty=True)
        if not isinstance(self.post_merge_commands, (list, tuple)):
            raise ContractError(
                "LaneConfig.review.merge.post_merge_commands: expected an array"
            )
        commands = []
        for index, argv in enumerate(self.post_merge_commands):
            base = f"LaneConfig.review.merge.post_merge_commands[{index}]"
            if not isinstance(argv, (list, tuple)):
                raise ContractError(f"{base}: expected an array")
            if not argv:
                raise ContractError(f"{base}: must not be empty")
            commands.append(
                tuple(
                    _expect_str(item, f"{base}[{position}]", non_empty=True)
                    for position, item in enumerate(argv)
                )
            )
        object.__setattr__(self, "post_merge_commands", tuple(commands))
        timeout = self.post_merge_timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ContractError(
                "LaneConfig.review.merge.post_merge_timeout_seconds: "
                "expected a number"
            )
        if timeout <= 0:
            raise ContractError(
                "LaneConfig.review.merge.post_merge_timeout_seconds: "
                "expected a positive number"
            )
        object.__setattr__(self, "post_merge_timeout_seconds", float(timeout))

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "main_ref": self.main_ref,
            "post_merge_commands": [list(argv) for argv in self.post_merge_commands],
            "post_merge_timeout_seconds": self.post_merge_timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    """The optional ``review`` block of the lane configuration (D11)."""

    reviewer: str = DEFAULT_REVIEWER
    max_rework_rounds: int = DEFAULT_MAX_REWORK_ROUNDS
    authorization_dir: str | None = None
    merge: MergeConfig = MergeConfig()

    def __post_init__(self) -> None:
        _expect_str(self.reviewer, "LaneConfig.review.reviewer", non_empty=True)
        _expect_int(
            self.max_rework_rounds,
            "LaneConfig.review.max_rework_rounds",
            positive=True,
        )
        if self.authorization_dir is not None:
            _expect_absolute_path(
                self.authorization_dir, "LaneConfig.review.authorization_dir"
            )
        if not isinstance(self.merge, MergeConfig):
            raise ContractError(
                "LaneConfig.review.merge: expected a MergeConfig object"
            )

    def resolved_authorization_dir(self, work_root: str) -> str:
        if self.authorization_dir is not None:
            return self.authorization_dir
        return f"{work_root}/authorizations"

    def to_dict(self) -> dict[str, Any]:
        document = {
            "reviewer": self.reviewer,
            "max_rework_rounds": self.max_rework_rounds,
            "merge": self.merge.to_dict(),
        }
        if self.authorization_dir is not None:
            document["authorization_dir"] = self.authorization_dir
        return document


def default_review_config(work_root: str) -> ReviewConfig:
    """Defaults used when a configuration omits the optional ``review`` block."""

    return ReviewConfig(
        reviewer=DEFAULT_REVIEWER,
        max_rework_rounds=DEFAULT_MAX_REWORK_ROUNDS,
        authorization_dir=None,
        merge=MergeConfig(),
    )


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """The optional ``service`` block (W6 D3); never writes a unit file."""

    platform: str | None = None
    label: str = DEFAULT_SERVICE_LABEL
    log_root: str | None = None
    restart: str = DEFAULT_SERVICE_RESTART

    def __post_init__(self) -> None:
        if self.platform is not None:
            _expect_enum(
                self.platform, "LaneConfig.service.platform", SERVICE_PLATFORMS
            )
        _expect_str(self.label, "LaneConfig.service.label", non_empty=True)
        if self.log_root is not None:
            _expect_absolute_path(self.log_root, "LaneConfig.service.log_root")
        _expect_enum(
            self.restart, "LaneConfig.service.restart", SERVICE_RESTARTS
        )

    def resolved_log_root(self, work_root: str) -> str:
        """``log_root`` when configured, else ``<work_root>/logs``."""

        if self.log_root is not None:
            return self.log_root
        return f"{work_root.rstrip('/')}/logs"

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "label": self.label,
            "log_root": self.log_root,
            "restart": self.restart,
        }


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """The optional ``logging`` block (W6 D8) with strict bounds."""

    retention_days: int = DEFAULT_RETENTION_DAYS
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES
    backups: int = DEFAULT_BACKUPS
    keep_failed_runs: int = DEFAULT_KEEP_FAILED_RUNS

    def __post_init__(self) -> None:
        _expect_int(
            self.retention_days,
            "LaneConfig.logging.retention_days",
            minimum=1,
        )
        _expect_int(
            self.max_total_bytes,
            "LaneConfig.logging.max_total_bytes",
            minimum=1048576,
        )
        _expect_int(
            self.max_log_bytes,
            "LaneConfig.logging.max_log_bytes",
            minimum=65536,
        )
        _expect_int(self.backups, "LaneConfig.logging.backups", minimum=1)
        _expect_int(
            self.keep_failed_runs,
            "LaneConfig.logging.keep_failed_runs",
            minimum=0,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "retention_days": self.retention_days,
            "max_total_bytes": self.max_total_bytes,
            "max_log_bytes": self.max_log_bytes,
            "backups": self.backups,
            "keep_failed_runs": self.keep_failed_runs,
        }


@dataclass(frozen=True, slots=True)
class LaneConfig:
    """The validated lane configuration."""

    schema_version: int
    hermes_source: str
    hermes_commit: str
    boards: dict[str, str]
    repositories: dict[str, Repository]
    executors: dict[str, Executor]
    work_root: str
    max_concurrent: int
    review: ReviewConfig | None = None
    service: ServiceConfig | None = None
    logging: LoggingConfig | None = None

    def review_options(self) -> ReviewConfig:
        """The effective review configuration (explicit or defaults)."""

        if self.review is not None:
            return self.review
        return default_review_config(self.work_root)

    def service_options(self) -> ServiceConfig:
        """The effective service configuration (explicit or defaults)."""

        if self.service is not None:
            return self.service
        return ServiceConfig()

    def logging_options(self) -> LoggingConfig:
        """The effective logging/retention configuration (explicit or defaults)."""

        if self.logging is not None:
            return self.logging
        return LoggingConfig()

    def __post_init__(self) -> None:
        _expect_schema_version(self.schema_version, "LaneConfig.schema_version")
        _expect_absolute_path(self.hermes_source, "LaneConfig.hermes_source")
        _expect_hex(self.hermes_commit, "LaneConfig.hermes_commit", (40, 64))

        if not isinstance(self.boards, dict):
            raise ContractError("LaneConfig.boards: expected a JSON object")
        boards = dict(self.boards)
        if not boards:
            raise ContractError("LaneConfig.boards: must not be empty")
        for alias, board_path in boards.items():
            base = f"LaneConfig.boards.{alias}"
            _expect_str(alias, "LaneConfig.boards", non_empty=True)
            _expect_absolute_path(board_path, base)
        object.__setattr__(self, "boards", boards)

        if not isinstance(self.repositories, dict):
            raise ContractError("LaneConfig.repositories: expected a JSON object")
        repositories = dict(self.repositories)
        if not repositories:
            raise ContractError("LaneConfig.repositories: must not be empty")
        for alias, repository in repositories.items():
            base = f"LaneConfig.repositories.{alias}"
            _expect_str(alias, "LaneConfig.repositories", non_empty=True)
            if not isinstance(repository, Repository):
                raise ContractError(f"{base}: expected a Repository object")
        object.__setattr__(self, "repositories", repositories)

        if not isinstance(self.executors, dict):
            raise ContractError("LaneConfig.executors: expected a JSON object")
        executors = dict(self.executors)
        if not executors:
            raise ContractError("LaneConfig.executors: must not be empty")
        for alias, executor in executors.items():
            if alias not in _ALLOWED_EXECUTORS:
                raise ContractError(
                    "LaneConfig.executors: keys must be one of claude, codex, dsh"
                )
            if not isinstance(executor, Executor):
                raise ContractError(
                    f"LaneConfig.executors.{alias}: expected an Executor object"
                )
        object.__setattr__(self, "executors", executors)

        _expect_absolute_path(self.work_root, "LaneConfig.work_root")
        _expect_int(self.max_concurrent, "LaneConfig.max_concurrent", positive=True)
        if self.review is not None and not isinstance(self.review, ReviewConfig):
            raise ContractError("LaneConfig.review: expected a ReviewConfig object")
        if self.service is not None and not isinstance(self.service, ServiceConfig):
            raise ContractError("LaneConfig.service: expected a ServiceConfig object")
        if self.logging is not None and not isinstance(self.logging, LoggingConfig):
            raise ContractError(
                "LaneConfig.logging: expected a LoggingConfig object"
            )

    @classmethod
    def from_dict(cls, data: Any, *, field: str = "LaneConfig") -> LaneConfig:
        mapping = _expect_mapping(data, field)
        _expect_exact_keys(
            mapping, field, _REQUIRED_CONFIG_KEYS, optional=_OPTIONAL_CONFIG_KEYS
        )

        boards_raw = _expect_mapping(mapping["boards"], f"{field}.boards")
        if not boards_raw:
            raise ContractError(f"{field}.boards: must not be empty")
        boards: dict[str, str] = {}
        for alias, value in boards_raw.items():
            _expect_str(alias, f"{field}.boards", non_empty=True)
            boards[alias] = _expect_absolute_path(value, f"{field}.boards.{alias}")

        repositories_raw = _expect_mapping(
            mapping["repositories"], f"{field}.repositories"
        )
        if not repositories_raw:
            raise ContractError(f"{field}.repositories: must not be empty")
        repositories: dict[str, Repository] = {}
        for alias, value in repositories_raw.items():
            _expect_str(alias, f"{field}.repositories", non_empty=True)
            base = f"{field}.repositories.{alias}"
            entry = _expect_mapping(value, base)
            _expect_exact_keys(entry, base, ("path", "trusted"))
            repo_path = _expect_absolute_path(entry["path"], f"{base}.path")
            trusted = _expect_bool(entry["trusted"], f"{base}.trusted")
            if trusted is not True:
                raise ContractError(f"{base}.trusted: must be true")
            repositories[alias] = Repository(path=repo_path, trusted=True)

        executors_raw = _expect_mapping(mapping["executors"], f"{field}.executors")
        if not executors_raw:
            raise ContractError(f"{field}.executors: must not be empty")
        executors: dict[str, Executor] = {}
        for alias, value in executors_raw.items():
            if alias not in _ALLOWED_EXECUTORS:
                raise ContractError(
                    f"{field}.executors: keys must be one of claude, codex, dsh"
                )
            base = f"{field}.executors.{alias}"
            entry = _expect_mapping(value, base)
            _expect_exact_keys(entry, base, ("argv",), optional=("assignees",))
            argv = entry["argv"]
            if not isinstance(argv, list):
                raise ContractError(f"{base}.argv: expected an array")
            if not argv:
                raise ContractError(f"{base}.argv: must not be empty")
            normalised = tuple(
                _expect_str(item, f"{base}.argv[{index}]", non_empty=True)
                for index, item in enumerate(argv)
            )
            assignees: tuple[str, ...] = ()
            if "assignees" in entry:
                raw_assignees = entry["assignees"]
                if not isinstance(raw_assignees, list):
                    raise ContractError(
                        f"{base}.assignees: expected an array"
                    )
                if not raw_assignees:
                    raise ContractError(
                        f"{base}.assignees: must not be empty"
                    )
                seen: set[str] = set()
                collected: list[str] = []
                for index, item in enumerate(raw_assignees):
                    text = _expect_str(
                        item,
                        f"{base}.assignees[{index}]",
                        non_empty=True,
                    )
                    if not text.strip():
                        raise ContractError(
                            f"{base}.assignees[{index}]: must not be blank"
                        )
                    if text in seen:
                        continue
                    seen.add(text)
                    collected.append(text)
                assignees = tuple(collected)
            executors[alias] = Executor(argv=normalised, assignees=assignees)

        work_root = _expect_absolute_path(
            mapping["work_root"], f"{field}.work_root"
        )
        review = cls._parse_review(
            mapping.get("review"), f"{field}.review", work_root
        )
        service = cls._parse_service(mapping.get("service"), f"{field}.service")
        logging_config = cls._parse_logging(
            mapping.get("logging"), f"{field}.logging"
        )

        return cls(
            schema_version=_expect_schema_version(
                mapping["schema_version"], f"{field}.schema_version"
            ),
            hermes_source=_expect_absolute_path(
                mapping["hermes_source"], f"{field}.hermes_source"
            ),
            hermes_commit=_expect_hex(
                mapping["hermes_commit"], f"{field}.hermes_commit", (40, 64)
            ),
            boards=boards,
            repositories=repositories,
            executors=executors,
            work_root=work_root,
            max_concurrent=_expect_int(
                mapping["max_concurrent"], f"{field}.max_concurrent", positive=True
            ),
            review=review,
            service=service,
            logging=logging_config,
        )

    @staticmethod
    def _parse_service(value: Any, field: str) -> ServiceConfig | None:
        """Parse the optional ``service`` block; every key is optional."""

        if value is None:
            return None
        entry = _expect_mapping(value, field)
        _expect_exact_keys(entry, field, (), optional=SERVICE_KEYS)

        platform = entry.get("platform")
        if platform is not None:
            platform = _expect_enum(
                platform, f"{field}.platform", SERVICE_PLATFORMS
            )
        label = entry.get("label", DEFAULT_SERVICE_LABEL)
        log_root = entry.get("log_root")
        if log_root is not None:
            log_root = _expect_absolute_path(log_root, f"{field}.log_root")
        restart = entry.get("restart", DEFAULT_SERVICE_RESTART)
        restart = _expect_enum(restart, f"{field}.restart", SERVICE_RESTARTS)
        return ServiceConfig(
            platform=platform,
            label=_expect_str(label, f"{field}.label", non_empty=True),
            log_root=log_root,
            restart=restart,
        )

    @staticmethod
    def _parse_logging(value: Any, field: str) -> LoggingConfig | None:
        """Parse the optional ``logging`` block with D8 bounds."""

        if value is None:
            return None
        entry = _expect_mapping(value, field)
        _expect_exact_keys(entry, field, (), optional=LOGGING_KEYS)
        return LoggingConfig(
            retention_days=_expect_int(
                entry.get("retention_days", DEFAULT_RETENTION_DAYS),
                f"{field}.retention_days",
                minimum=1,
            ),
            max_total_bytes=_expect_int(
                entry.get("max_total_bytes", DEFAULT_MAX_TOTAL_BYTES),
                f"{field}.max_total_bytes",
                minimum=1048576,
            ),
            max_log_bytes=_expect_int(
                entry.get("max_log_bytes", DEFAULT_MAX_LOG_BYTES),
                f"{field}.max_log_bytes",
                minimum=65536,
            ),
            backups=_expect_int(
                entry.get("backups", DEFAULT_BACKUPS),
                f"{field}.backups",
                minimum=1,
            ),
            keep_failed_runs=_expect_int(
                entry.get("keep_failed_runs", DEFAULT_KEEP_FAILED_RUNS),
                f"{field}.keep_failed_runs",
                minimum=0,
            ),
        )

    @staticmethod
    def _parse_review(
        value: Any, field: str, work_root: str
    ) -> ReviewConfig | None:
        if value is None:
            return None
        entry = _expect_mapping(value, field)
        _expect_exact_keys(entry, field, ("reviewer", "max_rework_rounds", "merge"),
                           optional=("authorization_dir",))
        reviewer = _expect_str(entry["reviewer"], f"{field}.reviewer", non_empty=True)
        max_rework_rounds = _expect_int(
            entry["max_rework_rounds"], f"{field}.max_rework_rounds", positive=True
        )
        if "authorization_dir" in entry:
            authorization_dir = _expect_absolute_path(
                entry["authorization_dir"], f"{field}.authorization_dir"
            )
        else:
            authorization_dir = None

        merge_entry = _expect_mapping(entry["merge"], f"{field}.merge")
        _expect_exact_keys(merge_entry, f"{field}.merge", MERGE_KEYS)
        enabled = _expect_bool(merge_entry["enabled"], f"{field}.merge.enabled")
        main_ref = _expect_str(
            merge_entry["main_ref"], f"{field}.merge.main_ref", non_empty=True
        )
        raw_commands = merge_entry["post_merge_commands"]
        if not isinstance(raw_commands, list):
            raise ContractError(f"{field}.merge.post_merge_commands: expected an array")
        post_merge_commands = []
        for index, argv in enumerate(raw_commands):
            base = f"{field}.merge.post_merge_commands[{index}]"
            if not isinstance(argv, list):
                raise ContractError(f"{base}: expected an array")
            if not argv:
                raise ContractError(f"{base}: must not be empty")
            post_merge_commands.append(
                tuple(
                    _expect_str(item, f"{base}[{position}]", non_empty=True)
                    for position, item in enumerate(argv)
                )
            )
        timeout = merge_entry["post_merge_timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ContractError(
                f"{field}.merge.post_merge_timeout_seconds: expected a number"
            )
        if timeout <= 0:
            raise ContractError(
                f"{field}.merge.post_merge_timeout_seconds: "
                "expected a positive number"
            )

        return ReviewConfig(
            reviewer=reviewer,
            max_rework_rounds=max_rework_rounds,
            authorization_dir=authorization_dir,
            merge=MergeConfig(
                enabled=enabled,
                main_ref=main_ref,
                post_merge_commands=tuple(post_merge_commands),
                post_merge_timeout_seconds=float(timeout),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        document = {
            "schema_version": self.schema_version,
            "hermes_source": self.hermes_source,
            "hermes_commit": self.hermes_commit,
            "boards": dict(self.boards),
            "repositories": {
                alias: repository.to_dict()
                for alias, repository in self.repositories.items()
            },
            "executors": {
                alias: executor.to_dict()
                for alias, executor in self.executors.items()
            },
            "work_root": self.work_root,
            "max_concurrent": self.max_concurrent,
        }
        if self.review is not None:
            document["review"] = self.review.to_dict()
        if self.service is not None:
            document["service"] = self.service.to_dict()
        if self.logging is not None:
            document["logging"] = self.logging.to_dict()
        return document


def load_config(path: str | os.PathLike[str]) -> LaneConfig:
    """Read and validate a configuration file.  No side effects beyond reading."""

    document = read_json_document(path, root="LaneConfig")
    return LaneConfig.from_dict(document)


__all__ = [
    "Executor",
    "LaneConfig",
    "LoggingConfig",
    "MergeConfig",
    "Repository",
    "ReviewConfig",
    "ServiceConfig",
    "default_review_config",
    "load_config",
    "read_json_document",
]
