"""Product CLI: validation, diagnosis, single tick and the service lifecycle.

``hermes-kanban-lanes`` and ``hermes kanban-lanes`` are two entry points into
the same ``main``/``dispatch`` pair; the plugin's ``setup_fn`` only attaches
this argparse tree to the host parser.

Exit codes (W6 D2):

* ``0`` success (including a clean SIGTERM/SIGINT shutdown of ``run``);
* ``1`` internal error (unexpected exception, credential-leak assertion);
* ``2`` invalid input, failed preflight or unsupported platform precondition;
* ``3`` single-instance lock held, a tick report with errors, or a fatal tick.

``doctor`` is strictly read-only: it never claims, writes the board or config,
takes the daemon lock, starts a process or runs a model.  The only process
calls it makes are ``--version`` probes and the read-only service status query.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform as platform_module
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .card_contract import load_card
from .config import LaneConfig, load_config
from .contracts import ContractError

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INVALID = 2
EXIT_LOCK = 3

DISTRIBUTION_NAME = "hermes-kanban-lanes"
LEAK_MIN_LENGTH = 8


class CredentialLeakError(RuntimeError):
    """The serialized report would contain a loaded credential value."""


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _emit_success(kind: str) -> None:
    payload = {"valid": True, "schema_version": 1, "kind": kind}
    print(json.dumps(payload, ensure_ascii=False))


def _emit_error(
    message: str,
    *,
    code: str | None = None,
    impact: str | None = None,
    next_steps: Sequence[str] | None = None,
) -> None:
    payload: dict[str, Any] = {"valid": False, "error": message}
    if code is not None:
        payload["reason"] = code
    if impact is not None:
        payload["impact"] = impact
    if next_steps is not None:
        payload["next_steps"] = [str(step) for step in next_steps]
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)


def _emit_object(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _emit_object_error(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)


def _reason_code(detail: str) -> str:
    """The machine-readable token at the start of a check detail."""

    token = detail.split(";", 1)[0].strip()
    return token or "check-failed"


def _structured_error(
    *,
    error: str,
    reason: str,
    impact: str,
    next_steps: Sequence[str],
    **extra: Any,
) -> dict[str, Any]:
    """One JSON error object with the D2 reason/impact/next_steps contract.

    Credential values never enter this helper: callers only pass config field
    names, missing module names and user-facing commands.
    """

    payload: dict[str, Any] = {
        "ok": False,
        "error": error,
        "reason": reason,
        "impact": impact,
        "next_steps": [str(step) for step in next_steps],
    }
    payload.update(extra)
    return payload


def _config_error(exc: BaseException, config_path: str) -> dict[str, Any]:
    return _structured_error(
        error=str(exc),
        reason="invalid-config",
        impact=(
            "The command did not start; no board task was claimed, written "
            "or changed."
        ),
        next_steps=[
            "Fix the configuration field named in error, then re-run: "
            f"hermes-kanban-lanes validate-config {config_path}",
            "See README.md for every accepted configuration field, default "
            "and range.",
        ],
    )


# ---------------------------------------------------------------------------
# Hermes host import-chain probe
# ---------------------------------------------------------------------------


def _host_dependency_failure(source: str) -> dict[str, Any] | None:
    """A structured failure when the configured host chain cannot import."""

    from .runtime.service import check_host_imports

    probe = check_host_imports(source)
    if probe.get("ok"):
        return None

    code = str(probe.get("code") or "host-import-failed")
    module = str(probe.get("module") or "")
    reason = f"{code}:{module}" if module else code
    diagnose = (
        "hermes-kanban-lanes doctor --config <absolute config path> --json"
    )
    if code == "host-dependency-missing":
        impact = (
            "tick and run cannot import the Hermes host import chain from the "
            "configured hermes_source, so the daemon cannot start and no task "
            "would be claimed."
        )
        next_steps = [
            "Install hermes-kanban-lanes into the same Python environment as "
            "the Hermes host; an independent virtual environment is not a "
            "supported installation form.",
            "Install the missing Hermes host dependency in that environment, "
            f"then verify: python -c \"import {module}\"",
            "Re-run: " + diagnose,
        ]
    else:
        impact = (
            "tick and run cannot load the configured Hermes host, so the "
            "daemon cannot start and no task would be claimed."
        )
        next_steps = [
            "Install hermes-kanban-lanes into the same Python environment as "
            "the Hermes host.",
            "Point hermes_source at the Hermes host checkout and install the "
            "host's own dependencies there.",
            "Re-run: " + diagnose,
        ]
    return {
        "reason": reason,
        "impact": impact,
        "next_steps": next_steps,
        "detail": reason,
    }


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


PLATFORM_CHOICES = ("launchd", "systemd")


def _add_commands(parser: argparse.ArgumentParser, *, with_version: bool) -> None:
    """Attach the shared command tree to ``parser`` (product or plugin)."""

    if with_version:
        parser.add_argument(
            "--version",
            action="store_true",
            help="print the installed distribution version and exit",
        )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    config_parser = subparsers.add_parser(
        "validate-config", help="validate a lane configuration JSON document"
    )
    config_parser.add_argument("path", help="path to the configuration JSON file")

    card_parser = subparsers.add_parser(
        "validate-card", help="validate a card JSON document against a configuration"
    )
    card_parser.add_argument("path", help="path to the card JSON file")
    card_parser.add_argument(
        "--config",
        required=True,
        dest="config_path",
        help="path to the lane configuration JSON file",
    )

    doctor_parser = subparsers.add_parser(
        "doctor", help="read-only diagnosis of one lane configuration"
    )
    doctor_parser.add_argument(
        "--config",
        required=True,
        dest="config_path",
        help="path to the lane configuration JSON file",
    )
    doctor_parser.add_argument(
        "--json", action="store_true", help="emit one JSON object on stdout"
    )

    tick_parser = subparsers.add_parser(
        "tick", help="take the daemon lock and run exactly one tick"
    )
    tick_parser.add_argument(
        "--config",
        required=True,
        dest="config_path",
        help="path to the lane configuration JSON file",
    )
    tick_parser.add_argument(
        "--json", action="store_true", help="emit one JSON object on stdout"
    )

    run_parser = subparsers.add_parser(
        "run", help="take the daemon lock and run the polling daemon"
    )
    run_parser.add_argument(
        "--config",
        required=True,
        dest="config_path",
        help="path to the lane configuration JSON file",
    )
    run_parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        dest="poll_seconds",
        help="seconds between ticks (default: 5.0)",
    )
    run_parser.add_argument(
        "--json", action="store_true", help="emit one JSON object on stdout"
    )

    service_parser = subparsers.add_parser(
        "service", help="render and manage the user-level service unit"
    )
    service_sub = service_parser.add_subparsers(
        dest="service_command", metavar="ACTION", required=True
    )

    render_parser = service_sub.add_parser(
        "render", help="render one unit file into --out (writes nothing else)"
    )
    render_parser.add_argument("--platform", choices=PLATFORM_CHOICES, required=True)
    render_parser.add_argument(
        "--config", required=True, dest="config_path"
    )
    render_parser.add_argument("--out", required=True, dest="out_dir")

    for action, help_text in (
        ("install", "install and load the user-level unit"),
        ("uninstall", "unload the job and remove the unit file"),
        ("status", "read-only status of the user-level unit"),
    ):
        action_parser = service_sub.add_parser(action, help=help_text)
        action_parser.add_argument(
            "--platform", choices=PLATFORM_CHOICES, required=True
        )
        action_parser.add_argument(
            "--config", required=True, dest="config_path"
        )
        action_parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="print the commands without writing or running them",
        )

    restart_parser = service_sub.add_parser(
        "restart", help="restart an installed user-level unit"
    )
    restart_parser.add_argument("--platform", choices=PLATFORM_CHOICES, required=True)
    restart_parser.add_argument("--config", required=True, dest="config_path")


def attach_commands(parser: argparse.ArgumentParser) -> None:
    """Plugin seam: attach the command tree without product-only flags."""

    _add_commands(parser, with_version=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-kanban-lanes",
        description=(
            "Validate, diagnose and run Hermes Kanban lanes, and manage the "
            "user-level service unit."
        ),
    )
    _add_commands(parser, with_version=True)
    return parser


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _distribution_version() -> str | None:
    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def _executable_available(executable: str) -> bool:
    if os.path.sep in executable:
        return os.path.isfile(executable) and os.access(executable, os.X_OK)
    return shutil.which(executable) is not None


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _secret_candidates(loaded: Sequence[str]) -> list[str]:
    values = [value for value in loaded if isinstance(value, str) and value]
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        values.append(api_key)
    for key, value in os.environ.items():
        if key.startswith("ANTHROPIC_") and value:
            values.append(value)
    # Do not assert on trivially short strings: they would collide with normal
    # identifiers by accident.  Real keys are far longer than this bound.
    return [value for value in values if len(value) >= LEAK_MIN_LENGTH]


def _assert_no_leak(serialized: str, secrets: Sequence[str]) -> None:
    for value in secrets:
        if value and value in serialized:
            raise CredentialLeakError("doctor-credential-leak")


def _doctor_checks(
    config: LaneConfig,
    config_path: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Build the fixed D5 check list plus the top-level lock and secrets."""

    from .executors import adapter_for
    from .hermes_adapter.board import BoardAdapter
    from .runtime.daemon import probe_daemon_lock
    from .runtime.retention import (
        last_error_line,
        prune_preview,
        rotate_count,
    )
    from .runtime.host_probe import parse_probe_output
    from .runtime.service import (
        HOST_PROBE_MODULE,
        ServiceError,
        check_host_imports,
        default_runner,
        lifecycle_support,
        resolve_launch_argv,
        service_status,
    )
    from .workspace.git import (
        GitError,
        preflight_repo,
        repo_head,
        repository_dirty,
    )

    lock_probe = probe_daemon_lock(config.work_root)
    checks: list[dict[str, Any]] = []
    secrets: list[str] = []

    # -- package ---------------------------------------------------------
    distribution = _distribution_version()
    if distribution is None:
        checks.append(
            _check(
                "package",
                False,
                "package-metadata-missing; python -m pip show "
                f"{DISTRIBUTION_NAME}",
            )
        )
    elif distribution != __version__:
        checks.append(
            _check(
                "package",
                False,
                "package-version-mismatch; python -m pip show "
                f"{DISTRIBUTION_NAME}",
            )
        )
    else:
        checks.append(_check("package", True, __version__))

    # -- platform --------------------------------------------------------
    python_version = platform_module.python_version()
    runnable = sys.platform in {"darwin", "linux"}
    service_claimed = sys.platform == "darwin"
    detail = f"{sys.platform} python {python_version}"
    if not runnable:
        detail = f"{detail}; service not claimed on this platform"
    elif not service_claimed:
        detail = (
            f"{detail}; systemd units are render-only and the service "
            "lifecycle is not claimed on this platform"
        )
    checks.append(_check("platform", runnable, detail))

    # -- host-hermes -----------------------------------------------------
    source = config.hermes_source
    rerun_head = f"git -C {source} rev-parse HEAD"
    if not os.path.isdir(source):
        checks.append(
            _check("host-hermes", False, f"hermes-source-missing; {rerun_head}")
        )
    else:
        try:
            head = repo_head(source)
        except GitError:
            checks.append(
                _check(
                    "host-hermes",
                    False,
                    f"hermes-source-git-failed; {rerun_head}",
                )
            )
        else:
            if head != config.hermes_commit:
                checks.append(
                    _check(
                        "host-hermes",
                        False,
                        f"hermes-commit-mismatch; {rerun_head}",
                    )
                )
            else:
                probe = check_host_imports(source)
                if probe.get("ok"):
                    checks.append(
                        _check("host-hermes", True, f"HEAD {head}; host imports ok")
                    )
                else:
                    code = str(probe.get("code") or "host-import-failed")
                    module = str(probe.get("module") or "")
                    token = f"{code}:{module}" if module else code
                    rerun_import = (
                        f"python -c \"import {module}\"" if module else "python -V"
                    )
                    checks.append(
                        _check(
                            "host-hermes",
                            False,
                            f"{token}; {rerun_import}; "
                            "install hermes-kanban-lanes into the same Python "
                            "environment as the Hermes host and install the "
                            "host's own dependencies; "
                            f"hermes-kanban-lanes doctor --config {config_path} "
                            "--json",
                        )
                    )

    # -- board:<alias> ---------------------------------------------------
    for alias, board_path in config.boards.items():
        ok, reason = BoardAdapter.preflight_file(board_path)
        if ok:
            checks.append(_check(f"board:{alias}", True, "ok"))
        else:
            checks.append(
                _check(
                    f"board:{alias}",
                    False,
                    f"{reason}; sqlite3 {board_path} "
                    "'SELECT 1 FROM tasks LIMIT 1'",
                )
            )

    # -- workspace:<alias> ----------------------------------------------
    work_root = config.work_root
    work_root_ok = os.path.isdir(work_root) and os.access(
        work_root, os.W_OK | os.X_OK
    )
    for alias, repository in config.repositories.items():
        repo_ok, repo_reason = preflight_repo(repository.path)
        if not repo_ok:
            checks.append(
                _check(
                    f"workspace:{alias}",
                    False,
                    f"{repo_reason}; git -C {repository.path} status "
                    "--porcelain -uall",
                )
            )
        elif not work_root_ok:
            checks.append(
                _check(
                    f"workspace:{alias}",
                    False,
                    f"work-root-not-writable; mkdir -p {work_root}",
                )
            )
        else:
            # A dirty checkout is reported, not failed: daily WIP must not turn
            # doctor fully red while the merge gate still enforces cleanliness.
            try:
                dirty = repository_dirty(repository.path)
            except GitError:
                checks.append(
                    _check(
                        f"workspace:{alias}",
                        False,
                        f"repository-unreadable; git -C {repository.path} status "
                        "--porcelain -uall",
                    )
                )
            else:
                checks.append(
                    _check(
                        f"workspace:{alias}",
                        True,
                        "dirty=true" if dirty else "dirty=false",
                    )
                )

    # -- executor:<alias> / credential:<alias> ---------------------------
    for alias, executor in config.executors.items():
        argv = executor.argv
        adapter = adapter_for(alias)
        rerun_version = f"{argv[0]} --version"
        if not _executable_available(argv[0]):
            checks.append(
                _check(
                    f"executor:{alias}",
                    False,
                    f"executor-not-resolvable; {rerun_version}",
                )
            )
        else:
            supported, reason = adapter.preflight(argv)
            if not supported:
                checks.append(
                    _check(
                        f"executor:{alias}",
                        False,
                        f"{reason}; {rerun_version}",
                    )
                )
            else:
                version = type(adapter).version(argv)
                if version is None:
                    checks.append(
                        _check(
                            f"executor:{alias}",
                            False,
                            f"executor-version-unavailable; {rerun_version}",
                        )
                    )
                else:
                    checks.append(
                        _check(
                            f"executor:{alias}",
                            True,
                            f"{os.path.basename(argv[0])} {version}",
                        )
                    )

        credentials, reason = adapter.credential_env()
        secrets.extend(str(value) for value in credentials.values())
        if reason is None:
            keys = ",".join(sorted(credentials)) or "credentials-present"
            checks.append(_check(f"credential:{alias}", True, keys))
        else:
            checks.append(
                _check(
                    f"credential:{alias}",
                    False,
                    f"{reason}; hermes-kanban-lanes doctor --config "
                    f"{config_path} --json",
                )
            )

    # -- lock ------------------------------------------------------------
    state = lock_probe.get("state")
    if state == "unsupported":
        checks.append(
            _check(
                "lock",
                False,
                "daemon-lock-unsupported; doctor requires a POSIX platform",
            )
        )
    elif state == "held":
        holder = lock_probe.get("holder") or {}
        checks.append(
            _check(
                "lock",
                True,
                f"held pid={holder.get('pid')} version={holder.get('version')}",
            )
        )
    else:
        checks.append(_check("lock", True, "free"))

    # -- service ---------------------------------------------------------
    service_options = config.service_options()
    if service_options.platform is None:
        checks.append(
            _check("service", True, "service-platform-not-configured")
        )
    else:
        service_platform = service_options.platform
        rerun_status = (
            "hermes-kanban-lanes service status "
            f"--platform {service_platform} --config {config_path}"
        )
        unsupported = lifecycle_support(service_platform)
        if unsupported is not None:
            checks.append(
                _check(
                    "service",
                    False,
                    f"{unsupported}; service render remains available: "
                    f"hermes-kanban-lanes service render --platform "
                    f"{service_platform} --config {config_path} --out <dir>",
                )
            )
        else:
            try:
                status = service_status(
                    service_platform,
                    config,
                    config_path,
                    environ=environ if environ is not None else os.environ,
                )
            except ServiceError as exc:
                checks.append(
                    _check("service", False, f"{exc}; {rerun_status}")
                )
            else:
                if not status.get("installed"):
                    checks.append(
                        _check(
                            "service",
                            False,
                            "service-not-installed; hermes-kanban-lanes service "
                            f"install --platform {service_platform} --config "
                            f"{config_path}",
                        )
                    )
                elif not status.get("running"):
                    checks.append(
                        _check(
                            "service",
                            False,
                            "service-not-running; hermes-kanban-lanes service "
                            f"restart --platform {service_platform} --config "
                            f"{config_path}",
                        )
                    )
                else:
                    checks.append(
                        _check("service", True, "installed=true running=true")
                    )

    # -- service-runtime -------------------------------------------------
    if service_options.platform is not None:
        launcher = os.path.join(source, ".hermes", "bin", "hermes")
        probe_rerun = (
            f"{launcher} --print-runtime-command --module "
            f"{HOST_PROBE_MODULE} -- {source}"
        )
        rerun_doctor = (
            f"hermes-kanban-lanes doctor --config {config_path} --json"
        )
        try:
            probe_argv = resolve_launch_argv(
                source, source, module=HOST_PROBE_MODULE
            )
        except ServiceError as exc:
            checks.append(
                _check(
                    "service-runtime",
                    False,
                    f"{exc}; {probe_rerun}; {rerun_doctor}",
                )
            )
        else:
            completed = default_runner(probe_argv)
            probe = parse_probe_output(completed.stdout or "")
            if probe is None:
                checks.append(
                    _check(
                        "service-runtime",
                        False,
                        "host-import-probe-failed; "
                        f"rc={completed.returncode}; {probe_rerun}; "
                        f"{rerun_doctor}",
                    )
                )
            elif probe.get("ok"):
                checks.append(
                    _check(
                        "service-runtime",
                        True,
                        f"launcher={probe_argv[0]}; host imports ok",
                    )
                )
            else:
                code = str(probe.get("code") or "host-import-failed")
                missing = str(probe.get("module") or "")
                token = f"{code}:{missing}" if missing else code
                checks.append(
                    _check(
                        "service-runtime",
                        False,
                        f"{token}; {probe_rerun}; {rerun_doctor}",
                    )
                )

    # -- logs ------------------------------------------------------------
    logging_config = config.logging_options()
    log_path = Path(work_root) / "logs" / "daemon.log"
    exists = log_path.is_file()
    size = log_path.stat().st_size if exists else 0
    modified_at = int(log_path.stat().st_mtime) if exists else None
    preview = prune_preview(work_root, logging_config)
    log_detail = json.dumps(
        {
            "exists": exists,
            "size_bytes": size,
            "modified_at": modified_at,
            "rotated": rotate_count(log_path),
            "last_error": last_error_line(log_path),
            "retention_days": logging_config.retention_days,
            "max_total_bytes": logging_config.max_total_bytes,
            "prunable_runs": preview["prunable_runs"],
            "skipped_runs": preview["skipped_runs"],
        },
        sort_keys=True,
    )
    checks.append(_check("logs", True, log_detail))

    return checks, lock_probe, secrets


def doctor_report(
    config: LaneConfig,
    config_path: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Produce the D5 report; raises :class:`CredentialLeakError` on a leak."""

    checks, lock_probe, secrets = _doctor_checks(
        config, config_path, environ=environ
    )
    failing = [check for check in checks if not check["ok"]]
    next_steps = [check["detail"] for check in failing]
    report = {
        "ok": not failing,
        "checks": checks,
        "version": __version__,
        "platform": {
            "sys_platform": sys.platform,
            "python_version": platform_module.python_version(),
            # v0.1 claims only the macOS launchd lifecycle; systemd is
            # render-only and Windows is unsupported.
            "claimed": sys.platform == "darwin",
        },
        "lock": lock_probe,
        "next_steps": next_steps,
    }
    if failing:
        report["reason"] = _reason_code(failing[0]["detail"])
        report["impact"] = (
            "doctor reported failing checks; tick and run will fail until "
            "they are fixed. doctor itself wrote nothing to the board, the "
            "configuration or the service state."
        )
    _assert_no_leak(
        json.dumps(report, ensure_ascii=False, sort_keys=True),
        _secret_candidates(secrets),
    )
    return report


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------


def _load(path: str) -> LaneConfig:
    return load_config(path)


def _validate_config(args: argparse.Namespace) -> int:
    _load(args.path)
    _emit_success("config")
    return EXIT_OK


def _validate_card(args: argparse.Namespace) -> int:
    config = _load(args.config_path)
    load_card(args.path, config)
    _emit_success("card")
    return EXIT_OK


def _doctor(args: argparse.Namespace) -> int:
    try:
        config = _load(args.config_path)
    except ContractError as exc:
        _emit_object_error(_config_error(exc, args.config_path))
        return EXIT_INVALID
    config_path = os.path.abspath(args.config_path)
    try:
        report = doctor_report(config, config_path)
    except CredentialLeakError:
        _emit_object_error(
            _structured_error(
                error="doctor-credential-leak",
                reason="credential-leak",
                impact=(
                    "doctor refused to print a report that would contain a "
                    "loaded credential value; no board or configuration write "
                    "happened."
                ),
                next_steps=[
                    "Unset or rotate the credential variable reported by the "
                    "host and re-run doctor --json.",
                ],
            )
        )
        return EXIT_INTERNAL
    _emit_object(report)
    return EXIT_OK if report["ok"] else EXIT_INVALID


def _preflight_failure_payload(
    config: LaneConfig,
    config_path: str,
    errors: Sequence[str],
    *,
    host: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Preflight errors plus the shared host import-chain diagnosis."""

    if host is None:
        host = _host_dependency_failure(config.hermes_source)
    if host is not None:
        return _structured_error(
            error="preflight-failed",
            reason=host["reason"],
            impact=host["impact"],
            next_steps=host["next_steps"],
            errors=list(errors),
        )
    return _structured_error(
        error="preflight-failed",
        reason=_reason_code(errors[0]) if errors else "preflight-failed",
        impact=(
            "tick/run did not acquire the daemon lock or claim any task; the "
            "board was not written."
        ),
        next_steps=[
            f"hermes-kanban-lanes doctor --config {config_path} --json",
            "Fix each item in errors, then re-run tick or run.",
        ],
        errors=list(errors),
    )


def _tick(args: argparse.Namespace) -> int:
    from .runtime import daemon

    try:
        config = _load(args.config_path)
    except ContractError as exc:
        _emit_object_error(_config_error(exc, args.config_path))
        return EXIT_INVALID

    errors = daemon.preflight(config)
    host_failure = _host_dependency_failure(config.hermes_source)
    if errors or host_failure is not None:
        _emit_object_error(
            _preflight_failure_payload(
                config,
                os.path.abspath(args.config_path),
                errors,
                host=host_failure,
            )
        )
        return EXIT_INVALID

    try:
        lock = daemon.acquire_daemon_lock(config.work_root, config)
    except daemon.DaemonLockUnsupported:
        _emit_object_error(
            _structured_error(
                error="daemon-lock-unsupported",
                reason="daemon-lock-unsupported",
                impact=(
                    "tick cannot take the single-instance lock on this "
                    "platform; no task was claimed or written."
                ),
                next_steps=[
                    "Run tick only on a POSIX platform with fcntl.flock "
                    "support.",
                ],
            )
        )
        return EXIT_INVALID
    except daemon.DaemonLockHeld as exc:
        _emit_object_error(
            _structured_error(
                error="daemon-lock-held",
                reason="daemon-lock-held",
                impact=(
                    "Another tick or run holds the daemon lock; this tick did "
                    "not claim or write any task."
                ),
                next_steps=[
                    "Wait for the running daemon to exit or stop the "
                    "user-level service, then retry.",
                ],
                holder=exc.holder,
            )
        )
        return EXIT_LOCK

    try:
        daemon.log_lock_event(
            config.work_root, lock, logging_config=config.logging_options()
        )
        report = daemon.tick(config)
        daemon._append_log(
            config.work_root, report, logging_config=config.logging_options()
        )
        try:
            from .runtime.retention import maybe_prune_runs

            maybe_prune_runs(config.work_root, config.logging_options())
        except Exception:  # noqa: BLE001 - retention never fails a tick
            pass
    finally:
        lock.release()

    _emit_object(report)
    return EXIT_LOCK if report.get("errors") else EXIT_OK


def _run(args: argparse.Namespace) -> int:
    from .runtime import daemon

    config_path = os.path.abspath(args.config_path)
    try:
        config = _load(args.config_path)
    except ContractError as exc:
        _emit_object_error(_config_error(exc, args.config_path))
        return EXIT_INVALID

    errors = daemon.preflight(config)
    host_failure = _host_dependency_failure(config.hermes_source)
    if errors or host_failure is not None:
        _emit_object_error(
            _preflight_failure_payload(
                config, config_path, errors, host=host_failure
            )
        )
        return EXIT_INVALID

    try:
        lock = daemon.acquire_daemon_lock(config.work_root, config)
    except daemon.DaemonLockUnsupported:
        _emit_object_error(
            _structured_error(
                error="daemon-lock-unsupported",
                reason="daemon-lock-unsupported",
                impact=(
                    "run cannot take the single-instance lock on this "
                    "platform; the daemon did not start."
                ),
                next_steps=[
                    "Run the daemon only on a POSIX platform with fcntl.flock "
                    "support.",
                ],
            )
        )
        return EXIT_INVALID
    except daemon.DaemonLockHeld as exc:
        _emit_object_error(
            _structured_error(
                error="daemon-lock-held",
                reason="daemon-lock-held",
                impact=(
                    "Another tick or run holds the daemon lock; this run did "
                    "not start."
                ),
                next_steps=[
                    "Wait for the running daemon to exit or stop the "
                    "user-level service, then retry.",
                ],
                holder=exc.holder,
            )
        )
        return EXIT_LOCK

    try:
        daemon.log_lock_event(
            config.work_root, lock, logging_config=config.logging_options()
        )
        report = daemon.run_daemon(
            config,
            poll_seconds=float(args.poll_seconds),
        )
    except Exception as exc:  # noqa: BLE001 - fatal tick: exit 3, no traceback
        if isinstance(exc, ModuleNotFoundError):
            reason = f"host-dependency-missing:{exc.name or 'unknown'}"
            impact = (
                "The daemon stopped because a Hermes host dependency could "
                "not be imported; in-flight work may have been interrupted."
            )
            next_steps = [
                "Install hermes-kanban-lanes into the same Python environment "
                "as the Hermes host and install the host's own dependencies.",
                f"hermes-kanban-lanes doctor --config {config_path} --json",
            ]
        else:
            reason = type(exc).__name__
            impact = (
                "The daemon stopped after taking the single-instance lock; "
                "in-flight work may have been interrupted."
            )
            next_steps = [
                f"hermes-kanban-lanes doctor --config {config_path} --json",
                f"Inspect the daemon log under {config.work_root}/logs.",
            ]
        _emit_object_error(
            _structured_error(
                error="daemon-fatal",
                reason=reason,
                impact=impact,
                next_steps=next_steps,
            )
        )
        return EXIT_LOCK
    finally:
        lock.release()

    _emit_object(
        {
            "ok": True,
            "stopped": True,
            "last_report": report,
        }
    )
    return EXIT_OK


def _service_render(args: argparse.Namespace) -> int:
    from .runtime import service

    config_path = os.path.abspath(args.config_path)
    try:
        config = _load(args.config_path)
    except ContractError as exc:
        _emit_object_error(_config_error(exc, args.config_path))
        return EXIT_INVALID
    try:
        unit = service.render_unit(
            args.platform,
            config,
            config_path,
        )
        target = service.render_to(unit, args.out_dir)
    except service.ServiceError as exc:
        _emit_object_error(
            _structured_error(
                error=str(exc),
                reason=_reason_code(str(exc)),
                impact=(
                    "No usable unit was rendered; nothing was written outside "
                    "the requested --out directory."
                ),
                next_steps=[
                    f"hermes-kanban-lanes doctor --config {config_path} --json",
                    f"hermes-kanban-lanes service render --platform "
                    f"{args.platform} --config {config_path} --out <absolute dir>",
                ],
            )
        )
        return EXIT_INVALID
    except OSError as exc:
        _emit_object_error(
            _structured_error(
                error=str(exc),
                reason="service-io-error",
                impact=(
                    "The unit could not be written to the requested --out "
                    "directory; no platform command was run."
                ),
                next_steps=[
                    f"Choose a writable absolute directory for --out and retry: "
                    f"hermes-kanban-lanes service render --platform "
                    f"{args.platform} --config {config_path} --out <absolute dir>",
                ],
            )
        )
        return EXIT_INVALID
    _emit_object(
        {
            "ok": True,
            "platform": args.platform,
            "unit_path": str(target),
            "unit": unit.to_dict(),
        }
    )
    return EXIT_OK


def _service_install_command(platform: str, config_path: str) -> str:
    return (
        "hermes-kanban-lanes service install "
        f"--platform {platform} --config {config_path}"
    )


def _annotate_service_result(
    action: str,
    result: dict[str, Any],
    platform: str,
    config_path: str,
) -> dict[str, Any]:
    """Add reason/impact/next_steps to failed lifecycle results."""

    install = _service_install_command(platform, config_path)
    status = (
        "hermes-kanban-lanes service status "
        f"--platform {platform} --config {config_path}"
    )
    if action == "status" and not result.get("installed"):
        result.setdefault("reason", "service-not-installed")
        result.setdefault(
            "impact",
            "The user-level service unit is not installed, so no daemon is "
            "running.",
        )
        result.setdefault("next_steps", [install])
    elif action == "uninstall" and not result.get("existed"):
        result.setdefault("reason", "service-not-installed")
        result.setdefault(
            "impact",
            "Nothing was uninstalled: the user-level service unit did not "
            "exist. Repositories, run evidence and board tasks are untouched.",
        )
        result.setdefault("next_steps", [install, status])
    elif action == "restart" and result.get("precondition"):
        result.setdefault("reason", result["precondition"])
        result.setdefault(
            "impact",
            "The service was not restarted because the user-level unit is not "
            "installed.",
        )
        result.setdefault("next_steps", [install])
    elif not result.get("ok") and not result.get("dry_run"):
        result.setdefault("reason", f"service-{action}-failed")
        result.setdefault(
            "impact",
            f"The {action} command did not complete successfully; review "
            "results[].returncode and the platform output.",
        )
        result.setdefault("next_steps", [status])
    return result


def _service_platform_error(
    error: str, action: str, platform: str, config_path: str
) -> dict[str, Any]:
    """Structured guidance for an unsupported lifecycle platform."""

    if error.startswith("service-platform-render-only"):
        return _structured_error(
            error=error,
            reason="service-platform-render-only",
            impact=(
                f"v0.1 renders {platform} units but does not claim the {platform} "
                "service lifecycle; no service command was run and systemctl "
                "was not invoked."
            ),
            next_steps=[
                f"Render the unit instead: hermes-kanban-lanes service render "
                f"--platform {platform} --config {config_path} --out <dir>",
                "Use the claimed launchd user service on macOS for install, "
                "status, restart and uninstall.",
            ],
        )
    if error.startswith("service-platform-not-claimed"):
        return _structured_error(
            error=error,
            reason="service-platform-not-claimed",
            impact=(
                f"The {platform} service lifecycle is not claimed on this "
                f"platform; the {action} command was not executed."
            ),
            next_steps=[
                "Use the claimed launchd user service on macOS.",
                f"hermes-kanban-lanes service render --platform {platform} "
                f"--config {config_path} --out <dir>",
            ],
        )
    return _structured_error(
        error=error,
        reason=_reason_code(error),
        impact=f"The service {action} command did not complete.",
        next_steps=[
            f"hermes-kanban-lanes service status --platform {platform} "
            f"--config {config_path}",
            f"hermes-kanban-lanes doctor --config {config_path} --json",
        ],
    )


def _service_action(args: argparse.Namespace) -> int:
    from .runtime import service

    action = args.service_command
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        config = _load(args.config_path)
    except ContractError as exc:
        _emit_object_error(_config_error(exc, args.config_path))
        return EXIT_INVALID
    config_path = os.path.abspath(args.config_path)

    try:
        if action == "install":
            result = service.install_service(
                args.platform, config, config_path, dry_run=dry_run
            )
            exit_code = EXIT_OK if result["ok"] else EXIT_INVALID
        elif action == "uninstall":
            result = service.uninstall_service(
                args.platform, config, config_path, dry_run=dry_run
            )
            exit_code = EXIT_OK if result["ok"] else EXIT_INVALID
        elif action == "status":
            result = service.service_status(
                args.platform, config, config_path, dry_run=dry_run
            )
            exit_code = EXIT_OK if result["installed"] else EXIT_INVALID
        elif action == "restart":
            result = service.restart_service(
                args.platform, config, config_path
            )
            if result.get("precondition"):
                exit_code = EXIT_LOCK
            else:
                exit_code = EXIT_OK if result["ok"] else EXIT_INVALID
        else:  # pragma: no cover - argparse constrains the choices
            _emit_object_error(
                _structured_error(
                    error=f"service-unknown:{action}",
                    reason="service-unknown-action",
                    impact="No service action was taken.",
                    next_steps=["Use render, install, uninstall, status or restart."],
                )
            )
            return EXIT_INVALID
    except service.ServiceError as exc:
        _emit_object_error(
            _service_platform_error(
                str(exc), action, args.platform, config_path
            )
        )
        return EXIT_INVALID
    except OSError as exc:
        _emit_object_error(
            _structured_error(
                error=str(exc),
                reason="service-io-error",
                impact=f"The service {action} command could not write or read the user-level unit.",
                next_steps=[
                    f"hermes-kanban-lanes service status --platform "
                    f"{args.platform} --config {config_path}",
                ],
            )
        )
        return EXIT_INVALID

    _annotate_service_result(action, result, args.platform, config_path)
    _emit_object(result)
    return exit_code


def _service(args: argparse.Namespace) -> int:
    if args.service_command == "render":
        return _service_render(args)
    return _service_action(args)


_HANDLERS = {
    "validate-config": _validate_config,
    "validate-card": _validate_card,
    "doctor": _doctor,
    "tick": _tick,
    "run": _run,
    "service": _service,
}


def dispatch(args: argparse.Namespace) -> int:
    """Dispatch a parsed command; shared by both entry points."""

    command = getattr(args, "command", None)
    if command is None:
        _emit_error(
            "command-required",
            code="command-required",
            impact="No command ran and nothing was read, claimed or written.",
            next_steps=["Run hermes-kanban-lanes --help to list commands."],
        )
        return EXIT_INVALID
    handler = _HANDLERS.get(command)
    if handler is None:  # pragma: no cover - argparse constrains the choices
        _emit_error(
            f"unknown command: {command}",
            code="unknown-command",
            impact="No command ran and nothing was read, claimed or written.",
            next_steps=["Run hermes-kanban-lanes --help to list commands."],
        )
        return EXIT_INVALID
    try:
        return handler(args)
    except CredentialLeakError:
        _emit_object_error(
            _structured_error(
                error="doctor-credential-leak",
                reason="credential-leak",
                impact=(
                    "The command refused to print a report that would contain "
                    "a loaded credential value; nothing was claimed or written."
                ),
                next_steps=[
                    "Unset or rotate the credential variable reported by the "
                    "host and re-run the command.",
                ],
            )
        )
        return EXIT_INTERNAL
    except ContractError as exc:
        if command == "validate-card":
            document = getattr(args, "path", "<card path>")
            config = getattr(args, "config_path", "<absolute config path>")
            code = "invalid-card"
            steps = [
                f"Fix the card field named in error, then re-run: "
                f"hermes-kanban-lanes validate-card {document} --config {config}",
                "See README.md for the card contract.",
            ]
        else:
            document = getattr(args, "path", "<absolute config path>")
            code = "invalid-config"
            steps = [
                f"Fix the configuration field named in error, then re-run: "
                f"hermes-kanban-lanes validate-config {document}",
                "See README.md for every accepted configuration field, default "
                "and range.",
            ]
        _emit_error(
            str(exc),
            code=code,
            impact=(
                "The command rejected its input; no board task was claimed, "
                "written or changed."
            ),
            next_steps=steps,
        )
        return EXIT_INVALID
    except Exception as exc:  # noqa: BLE001 - internal error, never a traceback
        _emit_object_error(
            _structured_error(
                error="internal-error",
                reason=type(exc).__name__,
                impact=(
                    "The command stopped on an unexpected internal error; "
                    "no JSON report was produced."
                ),
                next_steps=[
                    "Re-run with --json and report the command, exit code and "
                    "error JSON.",
                    "Run: hermes-kanban-lanes doctor --config <absolute "
                    "config path> --json",
                ],
            )
        )
        return EXIT_INTERNAL


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "version", False):
        print(__version__)
        return EXIT_OK

    if getattr(args, "command", None) is None:
        parser.error("a command is required")

    return dispatch(args)


__all__ = [
    "EXIT_INTERNAL",
    "EXIT_INVALID",
    "EXIT_LOCK",
    "EXIT_OK",
    "attach_commands",
    "dispatch",
    "doctor_report",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
