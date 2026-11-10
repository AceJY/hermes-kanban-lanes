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

import hashlib
import importlib.metadata
import json
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import __version__
from ..config import LaneConfig
from .host_probe import HOST_PROBE_PREFIX, parse_probe_output

PLATFORMS = ("launchd", "systemd")
LAUNCHD_UNIT_SUFFIX = ".plist"
SYSTEMD_UNIT_NAME = "hermes-kanban-lanes.service"
LAUNCHD_PREFIX = "launchd"
SYSTEMD_PREFIX = "daemon"
_RESTART_MAP = {"on-failure": "on-failure", "always": "always", "never": "no"}

#: Service metadata lives under the user's home, independent of the Hermes
#: dependency generation that happens to be selected.  The bundled wheel, the
#: generated launcher and the failure marker are all written here.
SERVICE_STATE_DIRNAME = "kanban-lanes-service"
SERVICE_STATE_FILENAME = "state.json"
SERVICE_LAUNCHER_FILENAME = "runtime-launcher.py"
SERVICE_MARKER_FILENAME = "launch-failure.json"
SERVICE_LOG_FILENAME = "launcher.log"
SERVICE_WHEEL_DIRNAME = "wheel"
SERVICE_INSTALLED_FILENAME = "installed.json"
PROBE_FLAG = "--package-probe"
DISTRIBUTION_NAME = "hermes-kanban-lanes"

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


# ---------------------------------------------------------------------------
# Generation-independent service launcher
# ---------------------------------------------------------------------------

#: The service unit must survive a Hermes dependency-generation switch.  The
#: rendered ``ProgramArguments`` therefore point at this small stdlib-only
#: launcher, stored outside any generation.  On every start it re-resolves the
#: installation-bound runtime, probes the product in that exact generation and
#: only then ``exec``s the daemon.  A failed probe is healed offline from the
#: wheel staged next to this file; every failure is fail-closed: no fallback to
#: ``sys.executable`` and a JSON marker under the service metadata directory.
_RUNTIME_LAUNCHER_SOURCE = '''#!/usr/bin/env python3
"""Hermes Kanban Lanes service runtime launcher (generated; stdlib only).

This file is written by ``hermes-kanban-lanes service render|install``.  It is
intentionally independent of the installed product: delete or replace every
Hermes dependency generation and this launcher still runs.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROBE_FLAG = "--package-probe"
SITE_MODULE = "site"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_state(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _append_log(state, line):
    path = state.get("log")
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s %s\\n" % (_now(), line))
    except OSError:
        pass


def _write_marker(state, reason, **extra):
    payload = {
        "schema_version": 1,
        "at": _now(),
        "reason": reason,
        "runtime": extra.pop("runtime", None),
        "generation": extra.pop("generation", None),
        "install_error": extra.pop("install_error", None),
        "product_version": state.get("product_version"),
        "wheel": state.get("wheel"),
        "state": state.get("state_file"),
    }
    payload.update(extra)
    path = state.get("marker")
    if path:
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except OSError:
            pass
    _append_log(state, "FAIL %s" % reason)
    sys.stderr.write(
        "hermes-kanban-lanes: service launcher failed: %s\\n" % reason
    )
    return payload


def _run(argv, timeout=180):
    command = [str(part) for part in argv]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", type(exc).__name__
    return (
        int(completed.returncode),
        completed.stdout or "",
        completed.stderr or "",
    )


def _resolve_runtime(state):
    command = [
        state["hermes_launcher"],
        "--print-runtime-command",
        "--module",
        state["module"],
        "--",
        *state["daemon_argv"],
    ]
    _append_log(state, "resolve %s" % " ".join(command))
    returncode, stdout, _stderr = _run(command)
    if returncode != 0:
        return None, "launcher-resolve-failed:%s" % returncode
    try:
        argv = json.loads(stdout.strip())
    except ValueError:
        return None, "launcher-resolve-invalid"
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(part, str) and part for part in argv)
    ):
        return None, "launcher-resolve-invalid"
    if not os.path.isabs(argv[0]) or not os.path.isfile(argv[0]):
        return None, "launcher-resolve-invalid"
    tail = state["daemon_argv"]
    if len(argv) < len(tail) or argv[-len(tail):] != tail:
        return None, "launcher-resolve-invalid"
    return argv, None


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _sha256(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _anchored_generation(candidate, hermes_home):
    """Only accept ``HERMES_HOME/installs/*/environments/*/venv`` generations."""

    if not hermes_home:
        return None
    try:
        resolved = Path(candidate).resolve()
        relative = resolved.relative_to(Path(hermes_home).resolve())
    except (OSError, ValueError):
        return None
    parts = relative.parts
    if (
        len(parts) >= 7
        and parts[0] == "installs"
        and parts[2] == "environments"
        and parts[4] == "venv"
        and parts[-1] in ("site-packages", "dist-packages")
    ):
        return str(resolved)
    return None


def _site_packages(state):
    """Resolve the selected generation's ``site-packages`` via stdlib ``site``.

    The resolved runtime can put a store/tool ``site-packages`` on ``sys.path``
    ahead of the activated generation.  Only a directory anchored at
    ``HERMES_HOME/installs/*/environments/*/venv`` is trusted; anything else is
    returned as a fail-closed reason so the caller never installs into the
    store interpreter.
    """

    command = [
        state["hermes_launcher"],
        "--print-runtime-command",
        "--module",
        SITE_MODULE,
    ]
    returncode, stdout, _stderr = _run(command)
    if returncode != 0:
        return None, "generation-site-packages-unresolved"
    try:
        argv = json.loads(stdout.strip())
    except ValueError:
        return None, "generation-site-packages-unresolved"
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(part, str) and part for part in argv)
    ):
        return None, "generation-site-packages-unresolved"
    returncode, output, _stderr = _run(argv)
    if returncode != 0:
        return None, "generation-site-packages-unresolved"
    start = output.find("sys.path = ")
    if start < 0:
        return None, "generation-site-packages-unresolved"
    start = output.find("[", start)
    if start < 0:
        return None, "generation-site-packages-unresolved"
    depth = 0
    for position in range(start, len(output)):
        character = output[position]
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                try:
                    entries = ast.literal_eval(output[start:position + 1])
                except (ValueError, SyntaxError):
                    return None, "generation-site-packages-unresolved"
                for entry in entries:
                    if not isinstance(entry, str):
                        continue
                    candidate = Path(entry)
                    if candidate.is_dir() and candidate.name in (
                        "site-packages",
                        "dist-packages",
                    ):
                        anchored = _anchored_generation(
                            candidate, state.get("hermes_home")
                        )
                        if anchored:
                            return anchored, None
                        return None, "generation-unanchored:%s" % candidate
                return None, "generation-site-packages-unresolved"
    return None, "generation-site-packages-unresolved"


def _under(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return True


def _probe(state, runtime, generation):
    if not generation:
        return False, "generation-site-packages-unresolved"
    tail = state["daemon_argv"]
    prefix = runtime[: len(runtime) - len(tail)] if tail else runtime
    command = prefix + [PROBE_FLAG]
    returncode, stdout, _stderr = _run(command)
    _append_log(state, "probe rc=%s" % returncode)
    if returncode != 0:
        return False, "package-probe-failed:%s" % returncode
    payload = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(candidate, dict)
            and "version" in candidate
            and "dist_version" in candidate
        ):
            payload = candidate
            break
    if payload is None:
        return False, "package-probe-invalid"
    expected = str(state.get("product_version") or "")
    if payload.get("dist_version") != expected:
        return False, "plugin-version-mismatch:%s" % payload.get("dist_version")
    if payload.get("version") != expected:
        return False, "plugin-version-mismatch:%s" % payload.get("version")
    module_file = payload.get("file")
    if not module_file or not _under(module_file, generation):
        return False, "plugin-import-outside-generation:%s" % module_file
    return True, None


def _tail(text, limit=800):
    return (text or "").strip()[-limit:]


def _write_installed(state, generation):
    path = state.get("installed")
    if not path:
        return
    wheel = state.get("wheel")
    payload = {
        "wheel_sha256": _sha256(wheel) if wheel else None,
        "installed_at": _now(),
        "generation": generation,
    }
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    except OSError:
        pass


def _wheel_sha256(state):
    wheel = state.get("wheel")
    if wheel and os.path.isfile(wheel):
        digest = _sha256(wheel)
        if digest:
            return digest
    return state.get("wheel_sha256")


def _installed_is_current(state, generation):
    expected = _wheel_sha256(state)
    if not expected or not generation:
        return False
    record = _read_json(state.get("installed") or "")
    if not isinstance(record, dict):
        return False
    return (
        record.get("wheel_sha256") == expected
        and record.get("generation") == generation
    )


def _install(state, runtime, generation):
    wheel = state.get("wheel")
    if not wheel or not os.path.isfile(wheel):
        return "wheel-missing", None
    if not generation:
        return "generation-site-packages-unresolved", None
    command = [
        runtime[0],
        "-m",
        "pip",
        "install",
        "--no-index",
        "--no-deps",
        "--force-reinstall",
        "--upgrade",
        "--target",
        generation,
        wheel,
    ]
    returncode, _stdout, stderr = _run(command)
    _append_log(
        state,
        "install rc=%s argv=%s" % (returncode, " ".join(command)),
    )
    pip_error = _tail(stderr)
    if pip_error:
        _append_log(state, "install stderr: %s" % pip_error)
    if returncode == 0:
        _write_installed(state, generation)
        return None, None
    uv = state.get("uv")
    if uv and os.path.isfile(uv):
        fallback = [
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--reinstall",
            "--python",
            runtime[0],
            "--target",
            generation,
            wheel,
        ]
        fallback_rc, _o, fallback_stderr = _run(fallback)
        _append_log(
            state,
            "install-uv rc=%s argv=%s"
            % (fallback_rc, " ".join(fallback)),
        )
        uv_error = _tail(fallback_stderr)
        if uv_error:
            _append_log(state, "install-uv stderr: %s" % uv_error)
        if fallback_rc == 0:
            _write_installed(state, generation)
            return None, None
        return (
            "wheel-install-failed:%s; uv-rc=%s" % (returncode, fallback_rc),
            uv_error or pip_error,
        )
    return "wheel-install-failed:%s" % returncode, pip_error


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    state_file = None
    if "--state" in arguments:
        index = arguments.index("--state")
        if index + 1 < len(arguments):
            state_file = arguments[index + 1]
    if state_file is None:
        state_file = str(Path(__file__).with_name("state.json"))
    try:
        state = _read_state(state_file)
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            "hermes-kanban-lanes: service launcher state unreadable: %s\\n" % exc
        )
        return 1
    state.setdefault("state_file", state_file)
    runtime, reason = _resolve_runtime(state)
    if runtime is None:
        _write_marker(state, reason)
        return 1
    generation, generation_reason = _site_packages(state)
    if generation is None:
        _write_marker(
            state,
            generation_reason or "generation-site-packages-unresolved",
            runtime=runtime[0],
        )
        return 2
    if not _installed_is_current(state, generation):
        # A missing/stale ``installed.json`` means a same-version wheel may
        # still have different content, so reinstall instead of trusting the
        # distribution version alone.
        _append_log(
            state,
            "staged wheel is not recorded for this generation; reinstalling",
        )
        install_reason, install_error = _install(state, runtime, generation)
        if install_reason is not None:
            _write_marker(
                state,
                install_reason,
                runtime=runtime[0],
                generation=generation,
                install_error=install_error,
            )
            return 2
    ok, detail = _probe(state, runtime, generation)
    if ok:
        _append_log(state, "exec daemon")
        os.execv(runtime[0], runtime)
        return 127
    _append_log(state, "probe failed: %s; healing" % detail)
    install_reason, install_error = _install(state, runtime, generation)
    if install_reason is not None:
        _write_marker(
            state,
            install_reason,
            runtime=runtime[0],
            generation=generation,
            install_error=install_error,
        )
        return 2
    ok, detail = _probe(state, runtime, generation)
    if ok:
        _append_log(state, "exec daemon after heal")
        os.execv(runtime[0], runtime)
        return 127
    _write_marker(state, detail, runtime=runtime[0], generation=generation)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
'''


def service_state_paths(
    home: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Deterministic metadata paths for the installed service."""

    root = (
        Path(home if home is not None else Path.home())
        / ".hermes"
        / SERVICE_STATE_DIRNAME
    )
    return {
        "state_dir": str(root),
        "state_file": str(root / SERVICE_STATE_FILENAME),
        "launcher": str(root / SERVICE_LAUNCHER_FILENAME),
        "marker": str(root / SERVICE_MARKER_FILENAME),
        "log": str(root / SERVICE_LOG_FILENAME),
        "wheel_dir": str(root / SERVICE_WHEEL_DIRNAME),
        "installed": str(root / SERVICE_INSTALLED_FILENAME),
    }


def service_state_dir(
    home: str | os.PathLike[str] | None = None,
) -> Path:
    return Path(service_state_paths(home)["state_dir"])


def _wheel_matches_version(path: Path) -> bool:
    name = path.name
    return (
        name.startswith(f"hermes_kanban_lanes-{__version__}-")
        and name.endswith(".whl")
    )


def _direct_url_wheel() -> Path | None:
    """A wheel path recorded by pip/uv for the current installation, if any."""

    try:
        distribution = importlib.metadata.distribution(DISTRIBUTION_NAME)
        raw = distribution.read_text("direct_url.json")
    except (importlib.metadata.PackageNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    url = document.get("url") if isinstance(document, dict) else None
    if not isinstance(url, str) or not url.startswith("file://"):
        return None
    candidate = Path(url[len("file://"):].replace("%20", " "))
    return candidate if candidate.suffix == ".whl" else None


def _locate_wheel(
    *,
    home: str | os.PathLike[str] | None = None,
    wheel_path: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Find the wheel for the running product version.

    Precedence: an explicit path, ``HERMES_KANBAN_LANES_WHEEL``/``HKL_WHEEL``,
    the current distribution's ``direct_url.json``, the staged wheel, and the
    usual local build directories.  No network access is ever attempted.
    """

    if wheel_path is not None:
        candidate = Path(wheel_path)
        if not candidate.is_file() or not _wheel_matches_version(candidate):
            raise ServiceError(f"service-wheel-not-found:{candidate}")
        return candidate

    candidates: list[Path] = []
    for variable in ("HERMES_KANBAN_LANES_WHEEL", "HKL_WHEEL"):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value))
    direct = _direct_url_wheel()
    if direct is not None:
        candidates.append(direct)
    state = service_state_paths(home)
    candidates.append(Path(state["wheel_dir"]))
    product_root = Path(__file__).resolve().parents[3]
    candidates.extend(
        [Path.cwd() / "dist", Path.cwd(), product_root / "dist", product_root]
    )

    for candidate in candidates:
        if candidate.is_file() and _wheel_matches_version(candidate):
            return candidate
        if candidate.is_dir():
            for wheel in sorted(candidate.glob("*.whl")):
                if _wheel_matches_version(wheel):
                    return wheel
    return None


def _find_uv(home: str | os.PathLike[str] | None = None) -> str | None:
    candidates = [shutil.which("uv")]
    base = Path(home if home is not None else Path.home())
    candidates.append(str(base / ".hermes" / "bin" / "uv"))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return str(candidate)
    return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalise_hermes_root(path: Path) -> Path:
    """Strip a session profile: ``<root>/profiles/<name>`` is not the root."""

    if path.name and path.parent.name == "profiles":
        return path.parent.parent
    return path


def _resolve_hermes_home(
    home: str | os.PathLike[str] | None = None,
) -> str:
    """The Hermes data root that owns ``installs/*/environments``.

    The generated launcher only trusts generation ``site-packages`` inside this
    root.  A Hermes session exports ``HERMES_HOME=<root>/profiles/<name>``
    while generations live under ``<root>/installs``, so profile paths are
    normalised back to their root before the ``installs`` check.  The first
    candidate that actually owns an ``installs`` directory wins; otherwise
    rendering fails closed instead of recording a root every start will reject.
    """

    candidates: list[Path] = []
    configured = os.environ.get("HERMES_HOME")
    if configured and os.path.isabs(configured):
        candidates.append(_normalise_hermes_root(Path(configured)))
    candidates.append(Path(home if home is not None else Path.home()) / ".hermes")

    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normpath(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        if (candidate / "installs").is_dir():
            return str(candidate)
    raise ServiceError("service-hermes-home-unresolved")


def stage_service_state(
    config: LaneConfig,
    config_path: str,
    *,
    home: str | os.PathLike[str] | None = None,
    wheel_path: str | os.PathLike[str] | None = None,
    hermes_home: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Stage the generated launcher, wheel and state under the metadata dir.

    ``service render`` and ``service install`` call this so the unit can keep
    starting after Hermes switches dependency generations.  A missing wheel is
    recorded as ``wheel: null``: the unit still renders, but the launcher will
    fail closed with a ``wheel-missing`` marker on its next start.
    """

    resolved_hermes_home = (
        str(hermes_home)
        if hermes_home is not None
        else _resolve_hermes_home(home)
    )
    paths = service_state_paths(home)
    root = Path(paths["state_dir"])
    root.mkdir(parents=True, exist_ok=True)

    hermes_launcher = launcher_path(config.hermes_source)
    if not os.path.isfile(hermes_launcher):
        raise ServiceError("service-launcher-missing")

    wheel = _locate_wheel(home=home, wheel_path=wheel_path)
    staged_wheel: str | None = None
    staged_sha256: str | None = None
    if wheel is not None:
        wheel_dir = Path(paths["wheel_dir"])
        wheel_dir.mkdir(parents=True, exist_ok=True)
        target = wheel_dir / wheel.name
        if os.path.realpath(wheel) != os.path.realpath(target):
            shutil.copy2(wheel, target)
        staged_wheel = str(target)
        staged_sha256 = _file_sha256(target)

    launcher = Path(paths["launcher"])
    if not launcher.is_file() or launcher.read_text(encoding="utf-8") != (
        _RUNTIME_LAUNCHER_SOURCE
    ):
        launcher.write_text(_RUNTIME_LAUNCHER_SOURCE, encoding="utf-8")
    launcher.chmod(0o755)

    document = {
        "schema_version": 1,
        "product_version": __version__,
        "hermes_source": config.hermes_source,
        "hermes_launcher": hermes_launcher,
        "hermes_home": resolved_hermes_home,
        "module": DEFAULT_LAUNCH_MODULE,
        "daemon_argv": ["run", "--config", config_path],
        "config_path": config_path,
        "wheel": staged_wheel,
        "wheel_sha256": staged_sha256,
        "uv": _find_uv(home),
        "state_dir": paths["state_dir"],
        "state_file": paths["state_file"],
        "launcher": paths["launcher"],
        "marker": paths["marker"],
        "log": paths["log"],
        "installed": paths["installed"],
    }
    state_file = Path(paths["state_file"])
    temporary = state_file.with_name(state_file.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, state_file)

    return {
        "state_dir": paths["state_dir"],
        "state_file": paths["state_file"],
        "launcher": paths["launcher"],
        "marker": paths["marker"],
        "log": paths["log"],
        "installed": paths["installed"],
        "wheel": staged_wheel,
        "wheel_sha256": staged_sha256,
    }


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
    home: str | os.PathLike[str] | None = None,
) -> RenderedUnit:
    """Render one unit as text; write nothing and run no platform command.

    Without an explicit ``python_executable`` the unit points at the generated
    generation-independent launcher staged by :func:`stage_service_state`.
    That launcher re-resolves the Hermes runtime on every start and can repair
    a missing product after ``hermes update`` switches generations.  The
    explicit-interpreter form is retained for tests, advanced callers and the
    rollback path, and deliberately opts out of self-healing.
    """

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
        # Resolve once to prove the installation-bound launcher exists and to
        # learn the store Python; execution itself stays inside the staged
        # launcher, which re-resolves the runtime on every start.
        launch_argv = resolve_launch_argv(
            config.hermes_source, "run", "--config", config_path
        )
        state = service_state_paths(home)
        arguments = [
            launch_argv[0],
            state["launcher"],
            "--state",
            state["state_file"],
        ]
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
    wheel_path: str | os.PathLike[str] | None = None,
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
        home=home,
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

    state = service_state_paths(home)
    if not dry_run:
        # Stage the launcher (and the wheel, when one is available) before the
        # unit is loaded: a service that cannot heal itself must not start.
        staged = stage_service_state(
            config,
            config_path,
            home=home,
            wheel_path=wheel_path,
        )
        state = {**state, **staged}
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
        "state_path": state["state_file"],
        "wheel_path": state.get("wheel") if not dry_run else None,
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
    "PROBE_FLAG",
    "SERVICE_INSTALLED_FILENAME",
    "SERVICE_LAUNCHER_FILENAME",
    "SERVICE_LOG_FILENAME",
    "SERVICE_MARKER_FILENAME",
    "SERVICE_STATE_DIRNAME",
    "SERVICE_STATE_FILENAME",
    "SERVICE_WHEEL_DIRNAME",
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
    "service_state_dir",
    "service_state_paths",
    "service_status",
    "stage_service_state",
    "uninstall_service",
    "user_unit_path",
]
