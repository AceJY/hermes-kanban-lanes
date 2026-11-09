"""Service unit rendering and user-level lifecycle (W6 D6).

``launchd`` (macOS) and ``systemd`` (Linux user units) are rendered from the
validated lane configuration.  Rendering never touches a system directory; the
install/uninstall/status/restart helpers execute only through an injectable
``runner`` that receives an argv list (never a shell string), so tests can use
fake ``launchctl``/``systemctl`` binaries or a pure Python recorder.

The rendered unit intentionally contains no credential: only ``PATH`` and, when
it is an absolute path, ``CODEX_HOME`` appear in the child environment.  The
systemd platform is rendered and asserted against fake commands only; this
release does not claim Linux live support.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..config import LaneConfig
from .host_probe import HOST_PROBE_PREFIX, parse_probe_output

PLATFORMS = ("launchd", "systemd")
LAUNCHD_UNIT_SUFFIX = ".plist"
SYSTEMD_UNIT_NAME = "hermes-kanban-lanes.service"
LAUNCHD_PREFIX = "launchd"
SYSTEMD_PREFIX = "daemon"
_RESTART_MAP = {"on-failure": "on-failure", "always": "always", "never": "no"}

#: The daemon is launched through the product CLI; the probe through its own
#: module.  Both resolve through the Hermes installation-bound launcher.
DEFAULT_LAUNCH_MODULE = "hermes_kanban_lanes.cli"
HOST_PROBE_MODULE = "hermes_kanban_lanes.runtime.host_probe"


class ServiceError(RuntimeError):
    """The unit cannot be rendered or the platform preconditions are missing."""


def lifecycle_support(platform: str) -> str | None:
    """Return ``None`` when a lifecycle action is claimed, else a code.

    v0.1 claims only the user-level launchd lifecycle on macOS.  systemd units
    are rendered on every platform, but install/status/restart/uninstall are
    not a supported promise; they fail closed before any command runs.
    """

    if platform not in PLATFORMS:
        return f"service-platform-unsupported:{platform}"
    if platform == "launchd":
        if sys.platform == "darwin":
            return None
        return "service-platform-not-claimed:launchd"
    return "service-platform-render-only:systemd"


def _require_lifecycle(platform: str) -> None:
    code = lifecycle_support(platform)
    if code is not None:
        raise ServiceError(code)


# ---------------------------------------------------------------------------
# Hermes host import-chain check
# ---------------------------------------------------------------------------

_HOST_CHECK_PREFIX = HOST_PROBE_PREFIX

# The probe implementation lives in ``runtime.host_probe`` so doctor can run
# the same code under the service's resolved launch interpreter.  This tiny
# bootstrap only puts the product root on ``sys.path`` for source checkouts,
# then delegates; ``-B`` keeps the probe from writing bytecode into the
# Hermes checkout.
_HOST_CHECK_CODE = """\
import sys
sys.path.insert(0, sys.argv[2])
from hermes_kanban_lanes.runtime.host_probe import main
main()
"""


def check_host_imports(source: str) -> dict[str, Any]:
    """Return ``{ok: True}`` or a failure code for the host import chain.

    A missing ``hermes_source`` is not this check's failure: the existing
    ``host-hermes``/preflight checks report it with their own guidance.  The
    probe runs with ``sys.executable`` because v0.1 requires the product to be
    installed into the same Python environment as the Hermes host.
    """

    if not os.path.isdir(source):
        return {"ok": True, "skipped": True}
    product_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                _HOST_CHECK_CODE,
                source,
                product_root,
            ],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "code": "host-import-probe-failed", "module": ""}

    payload = parse_probe_output(completed.stdout or "")
    if payload is not None:
        return payload
    return {"ok": False, "code": "host-import-probe-failed", "module": ""}


@dataclass(frozen=True, slots=True)
class RunnerResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], Any]


def default_runner(argv: Sequence[str], *, timeout: float = 30.0) -> RunnerResult:
    """Execute one argv-only command with captured output (no shell)."""

    command = [str(part) for part in argv]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return RunnerResult(tuple(command), 127, "", type(exc).__name__)
    return RunnerResult(
        tuple(command),
        int(result.returncode),
        result.stdout or "",
        result.stderr or "",
    )


def _as_result(value: Any, argv: Sequence[str]) -> RunnerResult:
    if isinstance(value, RunnerResult):
        return value
    if isinstance(value, int):
        return RunnerResult(tuple(str(part) for part in argv), int(value))
    returncode = getattr(value, "returncode", None)
    if returncode is None:
        raise ServiceError("service-runner-invalid-result")
    return RunnerResult(
        tuple(str(part) for part in argv),
        int(returncode),
        str(getattr(value, "stdout", "") or ""),
        str(getattr(value, "stderr", "") or ""),
    )


def _run(
    runner: Runner | None, argv: Sequence[str]
) -> RunnerResult:
    command = [str(part) for part in argv]
    if runner is None:
        return default_runner(command)
    return _as_result(runner(command), command)


def launcher_path(hermes_source: str) -> str:
    """The Hermes installation-bound launcher for ``hermes_source``."""

    return os.path.join(str(hermes_source), ".hermes", "bin", "hermes")


def resolve_launch_argv(
    hermes_source: str,
    *args: str,
    module: str = DEFAULT_LAUNCH_MODULE,
    runner: Runner | None = None,
) -> list[str]:
    """Resolve ``hermes``'s installation-bound launch command for ``args``.

    The launcher re-selects the current dependency generation on every start,
    so the returned argv stays valid across ``hermes update``.  Fail closed:
    never fall back to ``sys.executable``, which resolved to the installer's
    Python and produced a unit that could not import the host chain.
    """

    launcher = launcher_path(hermes_source)
    if not os.path.isfile(launcher):
        raise ServiceError("service-launcher-missing")
    requested = [str(part) for part in args]
    command = [
        launcher,
        "--print-runtime-command",
        "--module",
        str(module),
        "--",
        *requested,
    ]
    result = _run(runner, command)
    if result.returncode != 0:
        raise ServiceError(f"service-launcher-failed:{result.returncode}")
    try:
        payload = json.loads((result.stdout or "").strip())
    except ValueError:
        raise ServiceError("service-launcher-invalid") from None
    if (
        not isinstance(payload, list)
        or not payload
        or not all(isinstance(part, str) and part for part in payload)
    ):
        raise ServiceError("service-launcher-invalid")
    executable = payload[0]
    if not os.path.isabs(executable) or not os.path.isfile(executable):
        raise ServiceError("service-launcher-invalid")
    tail = payload[len(payload) - len(requested):] if requested else []
    if tail != requested:
        raise ServiceError("service-launcher-invalid")
    return payload


def _executed_entry(result: RunnerResult) -> dict[str, Any]:
    return {
        "argv": list(result.argv),
        "returncode": int(result.returncode),
        "executed": True,
    }


def _planned_entry(argv: Sequence[str]) -> dict[str, Any]:
    """A dry-run entry: the command was planned, never executed."""

    return {
        "argv": [str(part) for part in argv],
        "returncode": None,
        "executed": False,
    }


def service_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Only ``PATH`` and an absolute ``CODEX_HOME``; never a token value."""

    source = os.environ if environ is None else environ
    environment: dict[str, str] = {}
    path = source.get("PATH")
    if isinstance(path, str) and path:
        environment["PATH"] = path
    codex_home = source.get("CODEX_HOME")
    if isinstance(codex_home, str) and codex_home and os.path.isabs(codex_home):
        environment["CODEX_HOME"] = codex_home
    return environment


@dataclass(frozen=True, slots=True)
class RenderedUnit:
    platform: str
    filename: str
    content: str
    label: str
    log_root: str
    program_arguments: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "filename": self.filename,
            "label": self.label,
            "log_root": self.log_root,
            "program_arguments": list(self.program_arguments),
        }


def _program_arguments(config_path: str, python_executable: str) -> list[str]:
    return [
        python_executable,
        "-m",
        "hermes_kanban_lanes.cli",
        "run",
        "--config",
        config_path,
    ]


def _keep_alive(restart: str) -> bool | dict[str, bool]:
    """Map ``service.restart`` onto launchd's ``KeepAlive``.

    The default matches D6's template: restart unless the daemon exited 0.
    ``always`` keeps the job alive unconditionally; ``never`` disables it.
    """

    if restart == "always":
        return True
    if restart == "never":
        return False
    return {"SuccessfulExit": False}


def _plist_document(
    *,
    label: str,
    arguments: list[str],
    work_root: str,
    log_root: str,
    restart: str,
    environment: dict[str, str],
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": arguments,
        "WorkingDirectory": work_root,
        "RunAtLoad": True,
        # KeepAlive.SuccessfulExit=false == restart unless the daemon exited 0.
        "KeepAlive": _keep_alive(restart),
        "StandardOutPath": f"{log_root}/{LAUNCHD_PREFIX}.out.log",
        "StandardErrorPath": f"{log_root}/{LAUNCHD_PREFIX}.err.log",
    }
    if environment:
        document["EnvironmentVariables"] = environment
    return document


def _systemd_quote(value: str) -> str:
    if value and all(
        character.isalnum() or character in "/._-:=+@%,"
        for character in value
    ):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _systemd_environment(environment: Mapping[str, str]) -> list[str]:
    return [
        f"Environment={_systemd_quote(f'{key}={value}')}"
        for key, value in environment.items()
    ]


def _systemd_content(
    *,
    label: str,
    arguments: list[str],
    work_root: str,
    log_root: str,
    restart: str,
    environment: Mapping[str, str],
) -> str:
    lines = [
        "[Unit]",
        f"Description=Hermes Kanban Lanes daemon ({label})",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(_systemd_quote(part) for part in arguments),
        f"WorkingDirectory={_systemd_quote(work_root)}",
        f"Restart={_RESTART_MAP.get(restart, 'on-failure')}",
        "RestartSec=5",
        f"StandardOutput=append:{log_root}/{SYSTEMD_PREFIX}.out.log",
        f"StandardError=append:{log_root}/{SYSTEMD_PREFIX}.err.log",
        *_systemd_environment(environment),
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def render_unit(
    platform: str,
    config: LaneConfig,
    config_path: str,
    *,
    python_executable: str | None = None,
    environ: Mapping[str, str] | None = None,
    log_root: str | None = None,
) -> RenderedUnit:
    """Render one unit as text; write nothing and run no platform command."""

    if platform not in PLATFORMS:
        raise ServiceError(f"service-platform-unsupported:{platform}")
    if not isinstance(config_path, str) or not os.path.isabs(config_path):
        raise ServiceError("service-config-path-must-be-absolute")
    options = config.service_options()
    root = log_root or options.resolved_log_root(config.work_root)
    if not os.path.isabs(root):
        raise ServiceError("service-log-root-must-be-absolute")
    if python_executable is not None:
        # Explicit override: keep the historical direct-interpreter form for
        # tests, advanced callers and the upgrade/rollback paths.
        if not os.path.isabs(python_executable):
            raise ServiceError("service-python-must-be-absolute")
        arguments = _program_arguments(config_path, python_executable)
    else:
        arguments = resolve_launch_argv(
            config.hermes_source, "run", "--config", config_path
        )
    environment = service_environment(environ)

    if platform == "launchd":
        filename = f"{options.label}{LAUNCHD_UNIT_SUFFIX}"
        document = _plist_document(
            label=options.label,
            arguments=arguments,
            work_root=config.work_root,
            log_root=root,
            restart=options.restart,
            environment=environment,
        )
        content = plistlib.dumps(document, sort_keys=True).decode("utf-8")
    else:
        filename = SYSTEMD_UNIT_NAME
        content = _systemd_content(
            label=options.label,
            arguments=arguments,
            work_root=config.work_root,
            log_root=root,
            restart=options.restart,
            environment=environment,
        )
    return RenderedUnit(
        platform=platform,
        filename=filename,
        content=content,
        label=options.label,
        log_root=root,
        program_arguments=tuple(arguments),
    )


def render_to(
    unit: RenderedUnit, out_dir: str | os.PathLike[str]
) -> Path:
    """Write ``unit`` into ``out_dir`` (the only directory render touches)."""

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / unit.filename
    target.write_text(unit.content, encoding="utf-8")
    return target


def user_unit_path(
    platform: str,
    unit: RenderedUnit,
    *,
    home: str | os.PathLike[str] | None = None,
) -> Path:
    base = Path(home if home is not None else Path.home())
    if platform == "launchd":
        return base / "Library" / "LaunchAgents" / unit.filename
    if platform == "systemd":
        return base / ".config" / "systemd" / "user" / unit.filename
    raise ServiceError(f"service-platform-unsupported:{platform}")


def _unit_identity(
    platform: str,
    config: LaneConfig,
    home: str | os.PathLike[str] | None,
) -> tuple[str, Path]:
    """``(label, path)`` from configuration identity only.

    ``status``/``uninstall``/``restart`` must keep working when the launch
    command cannot be resolved: they only need the label and file name, never
    the rendered ``ProgramArguments``.
    """

    label = config.service_options().label
    base = Path(home if home is not None else Path.home())
    if platform == "launchd":
        filename = f"{label}{LAUNCHD_UNIT_SUFFIX}"
        return label, base / "Library" / "LaunchAgents" / filename
    if platform == "systemd":
        return label, base / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
    raise ServiceError(f"service-platform-unsupported:{platform}")


def _uid(uid: int | None) -> int:
    return int(uid if uid is not None else os.getuid())


def install_service(
    platform: str,
    config: LaneConfig,
    config_path: str,
    *,
    runner: Runner | None = None,
    home: str | os.PathLike[str] | None = None,
    python_executable: str | None = None,
    environ: Mapping[str, str] | None = None,
    log_root: str | None = None,
    dry_run: bool = False,
    uid: int | None = None,
) -> dict[str, Any]:
    """Write the user unit and load it; return the argv that was executed."""

    _require_lifecycle(platform)
    unit = render_unit(
        platform,
        config,
        config_path,
        python_executable=python_executable,
        environ=environ,
        log_root=log_root,
    )
    target = user_unit_path(platform, unit, home=home)
    commands: list[list[str]] = []
    if platform == "launchd":
        commands.append(["launchctl", "bootstrap", f"gui/{_uid(uid)}", str(target)])
    else:
        commands.append(["systemctl", "--user", "daemon-reload"])
        commands.append(
            ["systemctl", "--user", "enable", "--now", unit.filename]
        )

    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(unit.content, encoding="utf-8")
        results = [_run(runner, argv) for argv in commands]
        entries = [_executed_entry(result) for result in results]
        ok = all(result.returncode == 0 for result in results)
    else:
        entries = [_planned_entry(argv) for argv in commands]
        ok = True

    return {
        "ok": ok,
        "platform": platform,
        "unit_path": str(target),
        "unit": unit.to_dict(),
        "commands": commands,
        "results": entries,
        "dry_run": bool(dry_run),
        "executed": not dry_run,
    }


def uninstall_service(
    platform: str,
    config: LaneConfig,
    config_path: str,
    *,
    runner: Runner | None = None,
    home: str | os.PathLike[str] | None = None,
    python_executable: str | None = None,
    environ: Mapping[str, str] | None = None,
    dry_run: bool = False,
    uid: int | None = None,
) -> dict[str, Any]:
    """Remove the unit and unload the job.

    Only the unit file and the job are touched: repositories, ``work_root``
    run evidence and board tasks are never deleted.
    """

    _require_lifecycle(platform)
    _, target = _unit_identity(platform, config, home)
    existed = target.is_file()
    if platform == "launchd":
        commands = [
            ["launchctl", "bootout", f"gui/{_uid(uid)}", str(target)]
        ]
    else:
        commands = [
            ["systemctl", "--user", "disable", "--now", target.name],
            ["systemctl", "--user", "daemon-reload"],
        ]

    if not dry_run:
        results = [_run(runner, commands[0])]
        if target.exists():
            target.unlink()
        if platform == "systemd":
            results.append(_run(runner, commands[1]))
        entries = [_executed_entry(result) for result in results]
        ok = all(result.returncode == 0 for result in results) and existed
    else:
        entries = [_planned_entry(argv) for argv in commands]
        ok = True

    return {
        "ok": ok,
        "platform": platform,
        "unit_path": str(target),
        "existed": existed,
        "commands": commands,
        "results": entries,
        "dry_run": bool(dry_run),
        "executed": not dry_run,
    }


def service_status(
    platform: str,
    config: LaneConfig,
    config_path: str,
    *,
    runner: Runner | None = None,
    home: str | os.PathLike[str] | None = None,
    python_executable: str | None = None,
    environ: Mapping[str, str] | None = None,
    dry_run: bool = False,
    uid: int | None = None,
) -> dict[str, Any]:
    """Read-only user-level status; never writes a file or starts the daemon."""

    _require_lifecycle(platform)
    label, target = _unit_identity(platform, config, home)
    installed = target.is_file()
    if platform == "launchd":
        command = [
            "launchctl",
            "print",
            f"gui/{_uid(uid)}/{label}",
        ]
    else:
        command = ["systemctl", "--user", "is-active", target.name]

    if dry_run:
        entry = _planned_entry(command)
        running: bool | None = None
    else:
        result = _run(runner, command)
        entry = _executed_entry(result)
        if platform == "launchd":
            running = result.returncode == 0
        else:
            running = (
                result.returncode == 0
                and (result.stdout or "").strip() == "active"
            )

    return {
        "ok": installed,
        "platform": platform,
        "unit_path": str(target),
        "installed": installed,
        "running": running,
        "commands": [command],
        "results": [entry],
        "dry_run": bool(dry_run),
        "executed": not dry_run,
    }


def restart_service(
    platform: str,
    config: LaneConfig,
    config_path: str,
    *,
    runner: Runner | None = None,
    home: str | os.PathLike[str] | None = None,
    python_executable: str | None = None,
    environ: Mapping[str, str] | None = None,
    uid: int | None = None,
) -> dict[str, Any]:
    """Restart an installed user-level unit; not-installed is ``precondition``."""

    _require_lifecycle(platform)
    label, target = _unit_identity(platform, config, home)
    installed = target.is_file()
    if platform == "launchd":
        command = [
            "launchctl",
            "kickstart",
            "-k",
            f"gui/{_uid(uid)}/{label}",
        ]
    else:
        command = ["systemctl", "--user", "restart", target.name]
    if not installed:
        return {
            "ok": False,
            "precondition": "service-not-installed",
            "platform": platform,
            "unit_path": str(target),
            "installed": False,
            "commands": [command],
            "results": [],
        }
    result = _run(runner, command)
    return {
        "ok": result.returncode == 0,
        "precondition": None,
        "platform": platform,
        "unit_path": str(target),
        "installed": True,
        "commands": [command],
        "results": [_executed_entry(result)],
        "executed": True,
    }


__all__ = [
    "DEFAULT_LAUNCH_MODULE",
    "HOST_PROBE_MODULE",
    "PLATFORMS",
    "RenderedUnit",
    "Runner",
    "RunnerResult",
    "ServiceError",
    "check_host_imports",
    "default_runner",
    "install_service",
    "launcher_path",
    "lifecycle_support",
    "render_to",
    "render_unit",
    "resolve_launch_argv",
    "restart_service",
    "service_environment",
    "service_status",
    "uninstall_service",
    "user_unit_path",
]
