"""W6 D6: unit rendering and user-level lifecycle through an injected runner."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_kanban_lanes import __version__
from hermes_kanban_lanes.config import LaneConfig
from hermes_kanban_lanes.runtime.service import (
    RunnerResult,
    ServiceError,
    install_service,
    render_to,
    render_unit,
    resolve_launch_argv,
    restart_service,
    service_environment,
    service_state_paths,
    service_status,
    stage_service_state,
    uninstall_service,
    user_unit_path,
)

PYTHON = "/opt/python/bin/python3.13"
BOOTSTRAP = "import os, sys, runpy; runpy.run_module('hermes_kanban_lanes.cli')"


def _launcher(source: Path) -> Path:
    return source / ".hermes" / "bin" / "hermes"


def _write_launcher(
    source: Path,
    *,
    prefix: list[str] | None = None,
    interpreter: str | None = None,
    stdout: str | None = None,
    exit_code: int = 0,
) -> Path:
    """Write an executable fake launcher that mimics ``--print-runtime-command``."""

    binary = _launcher(source)
    binary.parent.mkdir(parents=True, exist_ok=True)
    if stdout is not None:
        script = (
            f"#!{sys.executable}\n"
            "import sys\n"
            f"sys.stdout.write({stdout!r})\n"
            f"raise SystemExit({exit_code})\n"
        )
    else:
        head = [interpreter or sys.executable]
        head += prefix if prefix is not None else ["-m", "hermes_kanban_lanes.cli"]
        script = (
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "args = sys.argv[1:]\n"
            "tail = args[args.index('--') + 1:] if '--' in args else []\n"
            f"parts = {head!r} + tail\n"
            "print(json.dumps(parts))\n"
            f"raise SystemExit({exit_code})\n"
        )
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _config(
    tmp_path: Path,
    service: dict | None = None,
    *,
    launcher: bool = True,
) -> LaneConfig:
    document = {
        "schema_version": 1,
        "hermes_source": str(tmp_path / "hermes"),
        "hermes_commit": "a" * 40,
        "boards": {"engineering": str(tmp_path / "board.db")},
        "repositories": {
            "primary": {"path": str(tmp_path / "repo"), "trusted": True}
        },
        "executors": {"claude": {"argv": ["claude"]}},
        "work_root": str(tmp_path / "work"),
        "max_concurrent": 1,
    }
    if service is not None:
        document["service"] = service
    if launcher:
        _write_launcher(tmp_path / "hermes")
    return LaneConfig.from_dict(document)


def _config_path(tmp_path: Path) -> str:
    return str(tmp_path / "config.json")


def _seed_hermes_installs(home: Path) -> None:
    """A real Hermes data root owns ``installs/``; staging now requires it."""

    (home / ".hermes" / "installs").mkdir(parents=True, exist_ok=True)


class Recorder:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append([str(part) for part in argv])
        return RunnerResult(tuple(str(part) for part in argv), self.returncode, self.stdout)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_launchd_plist_fields(tmp_path):
    config = _config(tmp_path)
    unit = render_unit(
        "launchd",
        config,
        _config_path(tmp_path),
        python_executable=PYTHON,
        environ={
            "PATH": "/usr/bin:/bin",
            "CODEX_HOME": "/abs/codex",
            "OPENAI_API_KEY": "sk-should-never-appear",
        },
    )
    document = plistlib.loads(unit.content.encode("utf-8"))
    assert document["Label"] == "com.hermes-kanban-lanes.daemon"
    assert document["ProgramArguments"] == [
        PYTHON,
        "-m",
        "hermes_kanban_lanes.cli",
        "run",
        "--config",
        _config_path(tmp_path),
    ]
    assert document["WorkingDirectory"] == config.work_root
    assert document["RunAtLoad"] is True
    assert document["KeepAlive"] == {"SuccessfulExit": False}
    assert document["StandardOutPath"] == f"{config.work_root}/logs/launchd.out.log"
    assert document["StandardErrorPath"] == f"{config.work_root}/logs/launchd.err.log"
    assert document["EnvironmentVariables"] == {
        "PATH": "/usr/bin:/bin",
        "CODEX_HOME": "/abs/codex",
    }
    assert "ProcessType" not in document
    assert "OPENAI_API_KEY" not in unit.content
    assert "sk-should-never-appear" not in unit.content
    assert unit.filename == "com.hermes-kanban-lanes.daemon.plist"


def test_launchd_custom_label_and_log_root(tmp_path):
    config = _config(
        tmp_path,
        {
            "platform": "launchd",
            "label": "com.example.hkl.test",
            "log_root": str(tmp_path / "custom-logs"),
        },
    )
    unit = render_unit(
        "launchd", config, _config_path(tmp_path), python_executable=PYTHON, environ={}
    )
    document = plistlib.loads(unit.content.encode("utf-8"))
    assert document["Label"] == "com.example.hkl.test"
    assert document["StandardOutPath"] == f"{tmp_path}/custom-logs/launchd.out.log"
    assert "EnvironmentVariables" not in document
    assert unit.filename == "com.example.hkl.test.plist"


def test_systemd_unit_fields(tmp_path):
    config = _config(tmp_path)
    unit = render_unit(
        "systemd",
        config,
        _config_path(tmp_path),
        python_executable=PYTHON,
        environ={"PATH": "/usr/bin", "CODEX_HOME": "/abs/codex"},
    )
    assert unit.filename == "hermes-kanban-lanes.service"
    text = unit.content
    assert "Type=simple" in text
    assert (
        "ExecStart="
        f"{PYTHON} -m hermes_kanban_lanes.cli run --config {_config_path(tmp_path)}"
    ) in text
    assert f"WorkingDirectory={config.work_root}" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=5" in text
    assert f"StandardOutput=append:{config.work_root}/logs/daemon.out.log" in text
    assert f"StandardError=append:{config.work_root}/logs/daemon.err.log" in text
    assert "Environment=PATH=/usr/bin" in text
    assert "Environment=CODEX_HOME=/abs/codex" in text
    assert "WantedBy=default.target" in text
    assert "OPENAI_API_KEY" not in text


@pytest.mark.parametrize(
    "restart, expected",
    [
        ("on-failure", {"SuccessfulExit": False}),
        ("always", True),
        ("never", False),
    ],
)
def test_launchd_restart_policy(tmp_path, restart, expected):
    config = _config(tmp_path, {"platform": "launchd", "restart": restart})
    unit = render_unit(
        "launchd", config, _config_path(tmp_path), python_executable=PYTHON, environ={}
    )
    document = plistlib.loads(unit.content.encode("utf-8"))
    assert document["KeepAlive"] == expected


@pytest.mark.parametrize(
    "restart, expected",
    [("on-failure", "Restart=on-failure"), ("always", "Restart=always"), ("never", "Restart=no")],
)
def test_systemd_restart_policy(tmp_path, restart, expected):
    config = _config(tmp_path, {"platform": "systemd", "restart": restart})
    unit = render_unit(
        "systemd", config, _config_path(tmp_path), python_executable=PYTHON, environ={}
    )
    assert expected in unit.content


def test_render_rejects_unsupported_platform_and_relative_paths(tmp_path):
    config = _config(tmp_path)
    with pytest.raises(ServiceError):
        render_unit("windows", config, _config_path(tmp_path), python_executable=PYTHON)
    with pytest.raises(ServiceError):
        render_unit("launchd", config, "relative/config.json", python_executable=PYTHON)
    with pytest.raises(ServiceError):
        render_unit("launchd", config, _config_path(tmp_path), python_executable="python")
    with pytest.raises(ServiceError):
        render_unit(
            "launchd",
            config,
            _config_path(tmp_path),
            python_executable=PYTHON,
            log_root="relative/logs",
        )


def test_render_to_writes_only_the_out_directory(tmp_path):
    config = _config(tmp_path)
    unit = render_unit(
        "launchd", config, _config_path(tmp_path), python_executable=PYTHON, environ={}
    )
    out = tmp_path / "rendered"
    target = render_to(unit, out)
    assert target == out / unit.filename
    assert target.read_text(encoding="utf-8") == unit.content
    assert sorted(path.name for path in out.iterdir()) == [unit.filename]
    assert not (tmp_path / "Library").exists()


def test_service_environment_excludes_credentials_and_relative_codex_home():
    environment = service_environment(
        {
            "PATH": "/usr/bin",
            "CODEX_HOME": "/abs/codex",
            "OPENAI_API_KEY": "sk-secret-value",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
        }
    )
    assert environment == {"PATH": "/usr/bin", "CODEX_HOME": "/abs/codex"}
    assert service_environment({"CODEX_HOME": "relative/codex"}) == {}


def test_user_unit_path(tmp_path):
    config = _config(tmp_path)
    unit = render_unit(
        "launchd", config, _config_path(tmp_path), python_executable=PYTHON, environ={}
    )
    assert user_unit_path("launchd", unit, home=tmp_path) == (
        tmp_path / "Library" / "LaunchAgents" / unit.filename
    )
    systemd = render_unit(
        "systemd", config, _config_path(tmp_path), python_executable=PYTHON, environ={}
    )
    assert user_unit_path("systemd", systemd, home=tmp_path) == (
        tmp_path / ".config" / "systemd" / "user" / systemd.filename
    )


# ---------------------------------------------------------------------------
# Hermes installation-bound launch command
# ---------------------------------------------------------------------------


def test_service_uses_hermes_launch_command(tmp_path):
    """The unit resolves through Hermes, never through the installer's Python.

    W14: the resolved store interpreter only starts the generation-independent
    launcher; the launcher re-resolves ``--print-runtime-command`` on every
    start, so the unit itself carries no generation-bound import path.
    """

    config = _config(tmp_path, launcher=False)
    _write_launcher(
        tmp_path / "hermes",
        prefix=["-I", "-c", BOOTSTRAP],
        interpreter=sys.executable,
    )
    config_path = _config_path(tmp_path)
    home = tmp_path / "home"

    expected = [sys.executable, "-I", "-c", BOOTSTRAP, "run", "--config", config_path]
    assert (
        resolve_launch_argv(
            config.hermes_source, "run", "--config", config_path
        )
        == expected
    )

    state = service_state_paths(home)
    shim = [expected[0], state["launcher"], "--state", state["state_file"]]

    unit = render_unit("launchd", config, config_path, environ={}, home=home)
    assert list(unit.program_arguments) == shim
    # The resolved store Python only boots the shim; the daemon module lives in
    # the launcher state, not in the persisted unit.
    assert "hermes_kanban_lanes.cli" not in unit.content
    document = plistlib.loads(unit.content.encode("utf-8"))
    assert document["ProgramArguments"] == shim

    systemd = render_unit("systemd", config, config_path, environ={}, home=home)
    exec_start = next(
        line
        for line in systemd.content.splitlines()
        if line.startswith("ExecStart=")
    )
    assert exec_start.startswith(f"ExecStart={expected[0]} ")
    assert exec_start.endswith(
        f"{state['launcher']} --state {state['state_file']}"
    )


def test_install_fails_closed_without_launcher(tmp_path):
    """Missing/failed/invalid launch commands raise before any unit write."""

    config = _config(tmp_path, launcher=False)
    config_path = _config_path(tmp_path)
    home = tmp_path / "home"
    target = (
        home
        / "Library"
        / "LaunchAgents"
        / "com.hermes-kanban-lanes.daemon.plist"
    )
    binary = _launcher(tmp_path / "hermes")

    missing_argv0 = json.dumps(
        ["/nonexistent/python", "run", "--config", config_path]
    )
    cases = [
        ("missing", None, "service-launcher-missing"),
        (
            "nonzero",
            lambda: _write_launcher(tmp_path / "hermes", exit_code=7),
            "service-launcher-failed:7",
        ),
        (
            "non-json",
            lambda: _write_launcher(
                tmp_path / "hermes", stdout="not json\n"
            ),
            "service-launcher-invalid",
        ),
        (
            "missing-argv0",
            lambda: _write_launcher(
                tmp_path / "hermes", stdout=missing_argv0 + "\n"
            ),
            "service-launcher-invalid",
        ),
        (
            "tail-mismatch",
            lambda: _write_launcher(
                tmp_path / "hermes",
                stdout=json.dumps(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        BOOTSTRAP,
                        "run",
                        "--config",
                        "/elsewhere.json",
                    ]
                )
                + "\n",
            ),
            "service-launcher-invalid",
        ),
    ]
    for name, setup, expected in cases:
        binary.unlink(missing_ok=True)
        if setup is not None:
            setup()
        runner = Recorder()
        with pytest.raises(ServiceError) as info:
            install_service(
                "launchd",
                config,
                config_path,
                runner=runner,
                home=home,
                environ={},
                uid=501,
            )
        assert str(info.value) == expected, name
        assert runner.calls == [], name
        assert not target.exists(), name
        assert not (home / "Library").exists(), name


def test_status_and_uninstall_do_not_resolve_launcher(tmp_path):
    """Identity-only actions keep working with no launcher at all."""

    config = _config(tmp_path, launcher=False)
    config_path = _config_path(tmp_path)
    home = tmp_path / "home"
    target = (
        home
        / "Library"
        / "LaunchAgents"
        / "com.hermes-kanban-lanes.daemon.plist"
    )
    target.parent.mkdir(parents=True)
    target.write_text("installed\n", encoding="utf-8")
    runner = Recorder(returncode=0)

    status = service_status(
        "launchd",
        config,
        config_path,
        runner=runner,
        home=home,
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    assert status["installed"] is True
    assert status["running"] is True
    assert runner.calls == [
        ["launchctl", "print", "gui/501/com.hermes-kanban-lanes.daemon"]
    ]

    runner.calls.clear()
    restarted = restart_service(
        "launchd",
        config,
        config_path,
        runner=runner,
        home=home,
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    assert restarted["ok"] is True
    assert runner.calls == [
        [
            "launchctl",
            "kickstart",
            "-k",
            "gui/501/com.hermes-kanban-lanes.daemon",
        ]
    ]
    assert target.is_file()

    runner.calls.clear()
    removed = uninstall_service(
        "launchd",
        config,
        config_path,
        runner=runner,
        home=home,
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    assert removed["ok"] is True
    assert removed["existed"] is True
    assert runner.calls == [
        ["launchctl", "bootout", "gui/501", str(target)]
    ]
    assert not target.exists()

    # The launch command itself is still missing: no silent fallback exists.
    with pytest.raises(ServiceError) as info:
        resolve_launch_argv(config.hermes_source, "run", "--config", config_path)
    assert str(info.value) == "service-launcher-missing"


# ---------------------------------------------------------------------------
# install / uninstall / status / restart through a fake runner
# ---------------------------------------------------------------------------


def test_install_launchd_argv_and_file(tmp_path):
    config = _config(tmp_path)
    runner = Recorder()
    home = tmp_path / "home"
    _seed_hermes_installs(home)
    result = install_service(
        "launchd",
        config,
        _config_path(tmp_path),
        runner=runner,
        home=home,
        python_executable=PYTHON,
        environ={"PATH": "/usr/bin"},
        uid=501,
    )
    target = home / "Library" / "LaunchAgents" / "com.hermes-kanban-lanes.daemon.plist"
    assert result["ok"] is True
    assert result["unit_path"] == str(target)
    assert runner.calls == [["launchctl", "bootstrap", "gui/501", str(target)]]
    assert result["commands"] == runner.calls
    assert [entry["returncode"] for entry in result["results"]] == [0]
    document = plistlib.loads(target.read_bytes())
    assert document["ProgramArguments"][:2] == [PYTHON, "-m"]
    assert document["EnvironmentVariables"] == {"PATH": "/usr/bin"}


@pytest.mark.skipif(
    sys.platform != "darwin", reason="systemd render-only guard is asserted on macOS"
)
def test_systemd_lifecycle_is_render_only_and_never_calls_systemctl(tmp_path):
    """D2/W7: v0.1 renders systemd units but does not claim their lifecycle."""

    config = _config(tmp_path, {"platform": "systemd"})
    runner = Recorder()
    home = tmp_path / "home"
    for call in (
        install_service,
        uninstall_service,
        service_status,
        restart_service,
    ):
        with pytest.raises(ServiceError) as excinfo:
            call(
                "systemd",
                config,
                _config_path(tmp_path),
                runner=runner,
                home=home,
                python_executable=PYTHON,
                environ={},
                uid=501,
            )
        assert "service-platform-render-only:systemd" in str(excinfo.value)

    assert runner.calls == []
    assert not (home / ".config").exists()
    # render still works on this platform.
    unit = render_unit(
        "systemd",
        config,
        _config_path(tmp_path),
        python_executable=PYTHON,
        environ={},
    )
    assert "ExecStart=" in unit.content


def test_install_failure_is_reported(tmp_path):
    config = _config(tmp_path)
    runner = Recorder(returncode=5)
    _seed_hermes_installs(tmp_path / "home")
    result = install_service(
        "launchd",
        config,
        _config_path(tmp_path),
        runner=runner,
        home=tmp_path / "home",
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    assert result["ok"] is False
    assert result["results"][0]["returncode"] == 5


def test_install_dry_run_writes_nothing_and_runs_nothing(tmp_path):
    config = _config(tmp_path)
    runner = Recorder()
    home = tmp_path / "home"
    result = install_service(
        "launchd",
        config,
        _config_path(tmp_path),
        runner=runner,
        home=home,
        python_executable=PYTHON,
        environ={},
        dry_run=True,
        uid=501,
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["executed"] is False
    assert runner.calls == []
    assert result["commands"] == [
        [
            "launchctl",
            "bootstrap",
            "gui/501",
            str(
                home
                / "Library"
                / "LaunchAgents"
                / "com.hermes-kanban-lanes.daemon.plist"
            ),
        ]
    ]
    # Every planned command is marked unexecuted, never returncode 0.
    assert result["results"] == [
        {"argv": result["commands"][0], "returncode": None, "executed": False}
    ]
    assert not (home / "Library").exists()


def test_uninstall_only_removes_the_unit_and_job(tmp_path):
    config = _config(tmp_path)
    home = tmp_path / "home"
    _seed_hermes_installs(home)
    runner = Recorder()
    install_service(
        "launchd",
        config,
        _config_path(tmp_path),
        runner=runner,
        home=home,
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    target = home / "Library" / "LaunchAgents" / "com.hermes-kanban-lanes.daemon.plist"
    assert target.is_file()

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    evidence = tmp_path / "work" / "runs" / "t" / "r1"
    evidence.mkdir(parents=True)
    (evidence / "run.json").write_text("{}\n", encoding="utf-8")

    runner.calls.clear()
    result = uninstall_service(
        "launchd",
        config,
        _config_path(tmp_path),
        runner=runner,
        home=home,
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    assert result["ok"] is True
    assert result["existed"] is True
    assert runner.calls == [["launchctl", "bootout", "gui/501", str(target)]]
    assert result["commands"] == runner.calls
    assert not target.exists()
    assert (repo / "keep.txt").is_file()
    assert (evidence / "run.json").is_file()


def test_uninstall_not_installed(tmp_path):
    config = _config(tmp_path)
    result = uninstall_service(
        "launchd",
        config,
        _config_path(tmp_path),
        runner=Recorder(),
        home=tmp_path / "home",
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    assert result["ok"] is False
    assert result["existed"] is False


def test_uninstall_dry_run_keeps_the_file(tmp_path):
    config = _config(tmp_path)
    home = tmp_path / "home"
    _seed_hermes_installs(home)
    install_service(
        "launchd",
        config,
        _config_path(tmp_path),
        runner=Recorder(),
        home=home,
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    target = home / "Library" / "LaunchAgents" / "com.hermes-kanban-lanes.daemon.plist"
    result = uninstall_service(
        "launchd",
        config,
        _config_path(tmp_path),
        runner=Recorder(),
        home=home,
        python_executable=PYTHON,
        environ={},
        dry_run=True,
        uid=501,
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["executed"] is False
    assert all(
        entry["executed"] is False and entry["returncode"] is None
        for entry in result["results"]
    )
    assert target.is_file()


def test_status_read_only_launchd(tmp_path):
    config = _config(tmp_path)
    home = tmp_path / "home"
    runner = Recorder(returncode=1)
    result = service_status(
        "launchd",
        config,
        _config_path(tmp_path),
        runner=runner,
        home=home,
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    assert result["installed"] is False
    assert result["running"] is False
    assert result["ok"] is False
    assert runner.calls == [["launchctl", "print", "gui/501/com.hermes-kanban-lanes.daemon"]]
    assert not (home / "Library").exists()


def test_restart_precondition_when_not_installed(tmp_path):
    config = _config(tmp_path)
    runner = Recorder()
    result = restart_service(
        "launchd",
        config,
        _config_path(tmp_path),
        runner=runner,
        home=tmp_path / "home",
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    assert result["ok"] is False
    assert result["precondition"] == "service-not-installed"
    assert runner.calls == []


def test_restart_launchd(tmp_path):
    home = tmp_path / "home"
    _seed_hermes_installs(home)
    launchd = _config(tmp_path)
    install_service(
        "launchd",
        launchd,
        _config_path(tmp_path),
        runner=Recorder(),
        home=home,
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    runner = Recorder()
    result = restart_service(
        "launchd",
        launchd,
        _config_path(tmp_path),
        runner=runner,
        home=home,
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    assert result["ok"] is True
    assert result["precondition"] is None
    assert runner.calls == [
        ["launchctl", "kickstart", "-k", "gui/501/com.hermes-kanban-lanes.daemon"]
    ]


def test_runner_accepts_int_and_object_results(tmp_path):
    """The injectable runner may return an int or an object with returncode."""

    config = _config(tmp_path)
    home = tmp_path / "home"
    _seed_hermes_installs(home)
    install_service(
        "launchd",
        config,
        _config_path(tmp_path),
        runner=lambda argv: 0,
        home=home,
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    result = service_status(
        "launchd",
        config,
        _config_path(tmp_path),
        runner=lambda argv: Result(),
        home=home,
        python_executable=PYTHON,
        environ={},
        uid=501,
    )
    assert result["running"] is True


def test_cli_service_render_and_dry_run_install(tmp_path, monkeypatch, capsys):
    from hermes_kanban_lanes.cli import main

    home = tmp_path / "home"
    home.mkdir()
    _seed_hermes_installs(home)
    monkeypatch.setenv("HOME", str(home))
    config = _config(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
    out = tmp_path / "out"
    assert (
        main(
            [
                "service",
                "render",
                "--platform",
                "launchd",
                "--config",
                str(config_path),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert (out / "com.hermes-kanban-lanes.daemon.plist").is_file()

    assert (
        main(
            [
                "service",
                "install",
                "--platform",
                "launchd",
                "--config",
                str(config_path),
                "--dry-run",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["executed"] is False
    assert payload["commands"][0][:2] == ["launchctl", "bootstrap"]
    assert all(
        entry["returncode"] is None and entry["executed"] is False
        for entry in payload["results"]
    )


@pytest.mark.skipif(
    sys.platform != "darwin", reason="systemd render-only guard is asserted on macOS"
)
def test_cli_systemd_lifecycle_fails_closed_with_guidance(
    tmp_path, monkeypatch, capsys
):
    from hermes_kanban_lanes.cli import main

    home = tmp_path / "home"
    home.mkdir()
    _seed_hermes_installs(home)
    monkeypatch.setenv("HOME", str(home))
    config = _config(tmp_path, {"platform": "systemd"})
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config.to_dict()), encoding="utf-8")

    for action in ("install", "uninstall", "status", "restart"):
        assert (
            main(
                [
                    "service",
                    action,
                    "--platform",
                    "systemd",
                    "--config",
                    str(config_path),
                ]
            )
            == 2
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        payload = json.loads(captured.err)
        assert payload["ok"] is False
        assert payload["reason"] == "service-platform-render-only"
        assert "render" in payload["impact"].lower()
        assert any(
            "service render --platform systemd" in step
            for step in payload["next_steps"]
        )
        # No executed command is reported and the message says none was run.
        assert '"argv"' not in captured.err
        assert "was not invoked" in captured.err
        assert not (tmp_path / ".config").exists()

    # Rendering remains available on this platform.
    out = tmp_path / "rendered"
    assert (
        main(
            [
                "service",
                "render",
                "--platform",
                "systemd",
                "--config",
                str(config_path),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert (out / "hermes-kanban-lanes.service").is_file()


def test_cli_status_not_installed_explains_next_step(tmp_path, capsys, monkeypatch):
    from hermes_kanban_lanes.cli import main

    # The CLI resolves the user unit from $HOME: isolate it so this test never
    # reads (or, through uninstall, removes) the real user's launchd unit.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = _config(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config.to_dict()), encoding="utf-8")

    assert (
        main(
            [
                "service",
                "status",
                "--platform",
                "launchd",
                "--config",
                str(config_path),
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["installed"] is False
    assert payload["unit_path"].startswith(str(tmp_path / "home"))
    assert payload["reason"] == "service-not-installed"
    assert any("service install --platform launchd" in step for step in payload["next_steps"])


def test_cli_uninstall_not_installed_explains_idempotence(tmp_path, capsys, monkeypatch):
    from hermes_kanban_lanes.cli import main

    # Same isolation as the status test: uninstall would otherwise bootout and
    # unlink the real ~/Library/LaunchAgents/<label>.plist for the current user.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = _config(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config.to_dict()), encoding="utf-8")

    assert (
        main(
            [
                "service",
                "uninstall",
                "--platform",
                "launchd",
                "--config",
                str(config_path),
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["existed"] is False
    assert payload["unit_path"].startswith(str(tmp_path / "home"))
    assert payload["reason"] == "service-not-installed"
    assert "uninstalled" in payload["impact"].lower()


# ---------------------------------------------------------------------------
# W14: the launcher heals a generation that does not have the plugin
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_product_wheel(work: Path) -> Path:
    """Build the current product wheel from a private source copy (offline)."""

    source = work / "wheel-source"
    source.mkdir()
    shutil.copy2(REPO_ROOT / "pyproject.toml", source / "pyproject.toml")
    for name in ("README.md", "LICENSE"):
        document = REPO_ROOT / name
        if document.is_file():
            shutil.copy2(document, source / name)
    shutil.copytree(
        REPO_ROOT / "src",
        source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    wheel_dir = work / "wheelhouse"
    wheel_dir.mkdir()
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            ".",
            "-w",
            str(wheel_dir),
        ],
        cwd=str(source),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert build.returncode == 0, f"{build.stdout}\n{build.stderr}"
    wheels = sorted(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def _build_wheel_variant(work: Path, marker: str) -> Path:
    """Build a same-version wheel whose package exports a unique ``marker``."""

    source = work / "variant-source"
    source.mkdir()
    shutil.copy2(REPO_ROOT / "pyproject.toml", source / "pyproject.toml")
    for name in ("README.md", "LICENSE"):
        document = REPO_ROOT / name
        if document.is_file():
            shutil.copy2(document, source / name)
    shutil.copytree(
        REPO_ROOT / "src",
        source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    init = source / "src" / "hermes_kanban_lanes" / "__init__.py"
    init.write_text(
        init.read_text(encoding="utf-8") + f"\nWHEEL_VARIANT = {marker!r}\n",
        encoding="utf-8",
    )
    wheel_dir = work / "variant-wheelhouse"
    wheel_dir.mkdir()
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            ".",
            "-w",
            str(wheel_dir),
        ],
        cwd=str(source),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert build.returncode == 0, f"{build.stdout}\n{build.stderr}"
    wheels = sorted(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def _below_313_python() -> str | None:
    for name in ("python3.12", "python3.11", "python3.10"):
        found = shutil.which(name)
        if found:
            return found
    return None


@pytest.fixture(scope="module")
def product_wheel(tmp_path_factory) -> Path:
    return _build_product_wheel(tmp_path_factory.mktemp("w14-wheel"))


def _generation_venv(home: Path) -> Path:
    """The Hermes-anchored generation layout the launcher only trusts."""

    return (
        home
        / ".hermes"
        / "installs"
        / "install-1"
        / "environments"
        / "env-1"
        / "venv"
    )


def _new_generation(work: Path, *, without_pip: bool = False) -> Path:
    """A generation venv under ``HERMES_HOME/installs/*/environments/*/venv``."""

    venv = _generation_venv(work / "home")
    command = [sys.executable, "-m", "venv"]
    if without_pip:
        command.append("--without-pip")
    command.append(str(venv))
    created = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert created.returncode == 0, created.stderr
    return venv / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )


def _generation_site_packages(generation_python: Path) -> Path:
    result = subprocess.run(
        [
            str(generation_python),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def _write_fake_daemon(
    work: Path, *, probe_payload: dict | None = None
) -> tuple[Path, Path]:
    """A daemon entry that records startup and emulates ``--package-probe``.

    ``probe_payload`` overrides the import-based probe so tests can pin the
    version and import-location guards to a fixed answer.
    """

    daemon = work / "fake_daemon.py"
    sentinel = work / "daemon-started.json"
    probe_file = work / "probe-payload.json"
    if probe_payload is not None:
        probe_file.write_text(json.dumps(probe_payload), encoding="utf-8")
    daemon.write_text(
        "import importlib.metadata as metadata\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        f"SENTINEL = {str(sentinel)!r}\n"
        f"PROBE_FILE = {str(probe_file)!r}\n"
        "if '--package-probe' in sys.argv:\n"
        "    if os.path.isfile(PROBE_FILE):\n"
        "        with open(PROBE_FILE, encoding='utf-8') as handle:\n"
        "            print(json.dumps(json.load(handle)))\n"
        "        raise SystemExit(0)\n"
        "    try:\n"
        "        import hermes_kanban_lanes as package\n"
        "        print(json.dumps({\n"
        "            'version': package.__version__,\n"
        "            'dist_version': metadata.version('hermes-kanban-lanes'),\n"
        "            'file': package.__file__,\n"
        "        }))\n"
        "    except Exception as exc:\n"
        "        print(json.dumps({'ok': False, 'error': type(exc).__name__}))\n"
        "        raise SystemExit(2)\n"
        "    raise SystemExit(0)\n"
        "with open(SENTINEL, 'w', encoding='utf-8') as handle:\n"
        "    json.dump({'argv': sys.argv[1:]}, handle)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    return daemon, sentinel


def _write_generation_launcher(
    source: Path,
    generation_python: Path,
    daemon: Path,
    *,
    site_argv: list[str] | None = None,
) -> Path:
    """A fake ``hermes`` whose runtime command points at ``generation_python``."""

    binary = _launcher(source)
    binary.parent.mkdir(parents=True, exist_ok=True)
    site_parts = (
        site_argv
        if site_argv is not None
        else [str(generation_python), "-m", "site"]
    )
    script = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "module = 'hermes_kanban_lanes.cli'\n"
        "if '--module' in args:\n"
        "    module = args[args.index('--module') + 1]\n"
        "tail = args[args.index('--') + 1:] if '--' in args else []\n"
        "if module == 'site':\n"
        f"    parts = {site_parts!r}\n"
        "else:\n"
        f"    parts = [{str(generation_python)!r}, {str(daemon)!r}] + tail\n"
        "print(json.dumps(parts))\n"
    )
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _run_launcher(
    staged: dict,
    *,
    env: dict | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, staged["launcher"], "--state", staged["state_file"]],
        capture_output=True,
        text=True,
        timeout=240,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_service_state_records_the_hermes_root(
    tmp_path, monkeypatch, product_wheel
):
    """A session profile (``<root>/profiles/<name>``) records its data root."""

    work = tmp_path / "w14-root"
    work.mkdir()
    generation_python = _new_generation(work)
    daemon, sentinel = _write_fake_daemon(work)
    config = _config(tmp_path, launcher=False)
    _write_generation_launcher(tmp_path / "hermes", generation_python, daemon)
    config_path = _config_path(tmp_path)

    hermes_root = work / "home" / ".hermes"
    profile = hermes_root / "profiles" / "probe-profile"
    profile.mkdir(parents=True)
    # A different HOME keeps the fallback candidate free of installs, so only
    # normalising the profile path can produce a usable root.
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setenv("HERMES_HOME", str(profile))

    staged = stage_service_state(config, config_path, wheel_path=product_wheel)
    state = json.loads(
        Path(staged["state_file"]).read_text(encoding="utf-8")
    )
    assert state["hermes_home"] == str(hermes_root)

    result = _run_launcher(staged)
    assert result.returncode == 0, result.stderr
    assert sentinel.is_file()


def test_service_state_requires_a_hermes_root_with_installs(
    tmp_path, monkeypatch
):
    """Without an ``installs`` root, render/install fails instead of recording it."""

    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setenv(
        "HERMES_HOME", str(tmp_path / "elsewhere" / "profiles" / "probe")
    )
    config = _config(tmp_path)

    with pytest.raises(ServiceError, match="service-hermes-home-unresolved"):
        stage_service_state(config, _config_path(tmp_path))


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_launcher_self_heals_missing_plugin_in_new_generation(
    tmp_path, product_wheel
):
    """A generation without HKL is healed offline before the daemon execs."""

    work = tmp_path / "w14-heal"
    work.mkdir()
    generation_python = _new_generation(work)
    daemon, sentinel = _write_fake_daemon(work)
    config = _config(tmp_path, launcher=False)
    _write_generation_launcher(tmp_path / "hermes", generation_python, daemon)
    config_path = _config_path(tmp_path)
    home = work / "home"

    staged = stage_service_state(
        config,
        config_path,
        home=home,
        wheel_path=product_wheel,
        hermes_home=home / ".hermes",
    )
    assert staged["wheel"] is not None
    result = _run_launcher(staged)
    assert result.returncode == 0, result.stderr

    # The daemon startup path was reached (not actually started).
    assert sentinel.is_file()
    assert json.loads(sentinel.read_text(encoding="utf-8"))["argv"] == [
        "run",
        "--config",
        config_path,
    ]

    # The wheel was installed into this generation, offline and without deps.
    probe = subprocess.run(
        [
            str(generation_python),
            "-c",
            "import importlib.metadata as m, hermes_kanban_lanes as p; "
            "print(p.__version__, m.version('hermes-kanban-lanes'), p.__file__)",
        ],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    version, dist_version, module_file = probe.stdout.split()
    assert version == __version__
    assert dist_version == __version__
    assert Path(module_file).resolve().is_relative_to(
        Path(
            subprocess.run(
                [
                    str(generation_python),
                    "-c",
                    "import sysconfig; print(sysconfig.get_paths()['purelib'])",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        ).resolve()
    )

    log = (Path(staged["state_dir"]) / "launcher.log").read_text(encoding="utf-8")
    assert "install rc=0" in log
    assert "--no-index" in log
    assert "--no-deps" in log
    assert "--force-reinstall" in log
    assert not Path(staged["marker"]).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_launcher_fails_closed_without_wheel(tmp_path, product_wheel):
    """A missing staged wheel is a hard, marked failure; the daemon never execs."""

    work = tmp_path / "w14-no-wheel"
    work.mkdir()
    generation_python = _new_generation(work)
    daemon, sentinel = _write_fake_daemon(work)
    config = _config(tmp_path, launcher=False)
    _write_generation_launcher(tmp_path / "hermes", generation_python, daemon)
    config_path = _config_path(tmp_path)
    home = work / "home"

    staged = stage_service_state(
        config,
        config_path,
        home=home,
        wheel_path=product_wheel,
        hermes_home=home / ".hermes",
    )
    assert staged["wheel"] is not None
    Path(staged["wheel"]).unlink()

    result = _run_launcher(staged)
    assert result.returncode != 0
    assert not sentinel.exists()  # no daemon, no fallback interpreter

    marker = json.loads(
        Path(staged["marker"]).read_text(encoding="utf-8")
    )
    assert marker["reason"] == "wheel-missing"
    assert marker["runtime"] == str(generation_python)
    assert marker["generation"]
    assert "wheel-missing" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_launcher_does_not_reinstall_a_current_generation(
    tmp_path, product_wheel
):
    """The first start installs once; later starts never reinstall."""

    work = tmp_path / "w14-current"
    work.mkdir()
    generation_python = _new_generation(work)
    daemon, sentinel = _write_fake_daemon(work)
    config = _config(tmp_path, launcher=False)
    _write_generation_launcher(tmp_path / "hermes", generation_python, daemon)
    config_path = _config_path(tmp_path)
    home = work / "home"

    staged = stage_service_state(
        config,
        config_path,
        home=home,
        wheel_path=product_wheel,
        hermes_home=home / ".hermes",
    )
    for _ in range(2):
        result = _run_launcher(staged)
        assert result.returncode == 0, result.stderr
        assert sentinel.is_file()

    log = (Path(staged["state_dir"]) / "launcher.log").read_text(encoding="utf-8")
    assert "probe rc=0" in log
    assert log.count("install rc=0") == 1
    assert log.count("exec daemon") == 2
    assert not Path(staged["marker"]).exists()



@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_launcher_uv_fallback_without_pip(tmp_path, product_wheel):
    """A generation without pip heals through ``uv pip install --python``.

    ``uv``'s implicit interpreter selection is forced to a Python older than
    the project's ``requires-python``; dropping ``--python`` therefore makes
    the fallback fail.  This pins R1's argv contract to behavior.
    """

    uv = shutil.which("uv")
    incompatible = _below_313_python()
    if uv is None or incompatible is None:
        pytest.skip("uv and a Python below 3.13 are required")

    work = tmp_path / "w14-uv-fallback"
    work.mkdir()
    generation_python = _new_generation(work, without_pip=True)
    pip_check = subprocess.run(
        [str(generation_python), "-m", "pip", "--version"],
        capture_output=True,
        text=True,
    )
    assert pip_check.returncode != 0

    daemon, sentinel = _write_fake_daemon(work)
    config = _config(tmp_path, launcher=False)
    _write_generation_launcher(tmp_path / "hermes", generation_python, daemon)
    config_path = _config_path(tmp_path)
    home = work / "home"
    staged = stage_service_state(
        config,
        config_path,
        home=home,
        wheel_path=product_wheel,
        hermes_home=home / ".hermes",
    )
    assert staged["wheel"] is not None

    # Make ``uv`` discover an incompatible interpreter instead of the
    # generation's own Python when ``--python`` is absent.
    shim = work / "python-shim"
    shim.mkdir()
    for name in ("python", "python3"):
        (shim / name).symlink_to(incompatible)
    environment = dict(os.environ)
    for variable in ("VIRTUAL_ENV", "CONDA_PREFIX", "UV_PYTHON"):
        environment.pop(variable, None)
    environment["PATH"] = os.pathsep.join([str(shim), "/usr/bin", "/bin"])

    result = _run_launcher(staged, env=environment, cwd=work)
    assert result.returncode == 0, result.stderr
    assert sentinel.is_file()
    assert not Path(staged["marker"]).exists()

    probe = subprocess.run(
        [
            str(generation_python),
            "-c",
            "import importlib.metadata as m, hermes_kanban_lanes as p; "
            "print(p.__version__, m.version('hermes-kanban-lanes'))",
        ],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.split() == [__version__, __version__]

    log = (Path(staged["state_dir"]) / "launcher.log").read_text(encoding="utf-8")
    assert "install-uv rc=0" in log
    assert "--python" in log


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_launcher_version_mismatch_after_heal_fails(tmp_path, product_wheel):
    """A probe version mismatch heals once, then still fails closed (rc=3)."""

    work = tmp_path / "w14-mismatch"
    work.mkdir()
    generation_python = _new_generation(work)
    site_packages = _generation_site_packages(generation_python)
    probe_payload = {
        "version": "9.9.9",
        "dist_version": "9.9.9",
        "file": str(site_packages / "hermes_kanban_lanes" / "__init__.py"),
    }
    daemon, sentinel = _write_fake_daemon(work, probe_payload=probe_payload)
    config = _config(tmp_path, launcher=False)
    _write_generation_launcher(tmp_path / "hermes", generation_python, daemon)
    config_path = _config_path(tmp_path)
    home = work / "home"
    staged = stage_service_state(
        config,
        config_path,
        home=home,
        wheel_path=product_wheel,
        hermes_home=home / ".hermes",
    )

    result = _run_launcher(staged)
    assert result.returncode == 3, result.stderr
    assert not sentinel.exists()

    marker = json.loads(Path(staged["marker"]).read_text(encoding="utf-8"))
    assert marker["reason"].startswith("plugin-version-mismatch:")
    log = (Path(staged["state_dir"]) / "launcher.log").read_text(encoding="utf-8")
    assert "install rc=0" in log

    # Deleting either version guard in the launcher turns this successful and
    # the assertion on rc=3 red.


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_launcher_import_outside_generation_fails(tmp_path, product_wheel):
    """An import resolved outside the selected generation is never accepted."""

    work = tmp_path / "w14-outside"
    work.mkdir()
    generation_python = _new_generation(work)
    outside = work / "store" / "site-packages" / "hermes_kanban_lanes" / "__init__.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("", encoding="utf-8")
    probe_payload = {
        "version": __version__,
        "dist_version": __version__,
        "file": str(outside),
    }
    daemon, sentinel = _write_fake_daemon(work, probe_payload=probe_payload)
    config = _config(tmp_path, launcher=False)
    _write_generation_launcher(tmp_path / "hermes", generation_python, daemon)
    config_path = _config_path(tmp_path)
    home = work / "home"
    staged = stage_service_state(
        config,
        config_path,
        home=home,
        wheel_path=product_wheel,
        hermes_home=home / ".hermes",
    )

    result = _run_launcher(staged)
    assert result.returncode == 3, result.stderr
    assert not sentinel.exists()

    marker = json.loads(Path(staged["marker"]).read_text(encoding="utf-8"))
    assert marker["reason"].startswith("plugin-import-outside-generation:")
    assert str(outside) in marker["reason"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_same_version_wheel_change_triggers_reinstall(tmp_path, product_wheel):
    """A different 0.1.0 wheel replaces the installed copy without a bump."""

    work = tmp_path / "w14-same-version"
    work.mkdir()
    generation_python = _new_generation(work)
    daemon, sentinel = _write_fake_daemon(work)
    config = _config(tmp_path, launcher=False)
    _write_generation_launcher(tmp_path / "hermes", generation_python, daemon)
    config_path = _config_path(tmp_path)
    home = work / "home"

    variant = _build_wheel_variant(work, "wheel-b-marker")
    assert variant.name == product_wheel.name

    first = stage_service_state(
        config,
        config_path,
        home=home,
        wheel_path=product_wheel,
        hermes_home=home / ".hermes",
    )
    result = _run_launcher(first)
    assert result.returncode == 0, result.stderr
    assert sentinel.is_file()
    installed = json.loads(
        (Path(first["state_dir"]) / "installed.json").read_text(encoding="utf-8")
    )
    assert installed["wheel_sha256"] == first["wheel_sha256"]
    assert installed["generation"]
    assert installed["installed_at"]

    second = stage_service_state(
        config,
        config_path,
        home=home,
        wheel_path=variant,
        hermes_home=home / ".hermes",
    )
    assert second["wheel_sha256"] != first["wheel_sha256"]
    result = _run_launcher(second)
    assert result.returncode == 0, result.stderr
    assert sentinel.is_file()

    probe = subprocess.run(
        [
            str(generation_python),
            "-c",
            "import hermes_kanban_lanes as p; "
            "print(getattr(p, 'WHEEL_VARIANT', ''))",
        ],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "wheel-b-marker"

    installed = json.loads(
        (Path(second["state_dir"]) / "installed.json").read_text(encoding="utf-8")
    )
    assert installed["wheel_sha256"] == second["wheel_sha256"]
    log = (Path(second["state_dir"]) / "launcher.log").read_text(encoding="utf-8")
    assert "not recorded for this generation; reinstalling" in log


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_generation_anchor_rejects_store_purelib(tmp_path, product_wheel):
    """A store/tool ``site-packages`` first on ``sys.path`` is fail-closed."""

    work = tmp_path / "w14-anchor"
    work.mkdir()
    generation_python = _new_generation(work)
    anchored = _generation_site_packages(generation_python)
    store_purelib = work / "store" / "lib" / "python3.13" / "site-packages"
    store_purelib.mkdir(parents=True)

    fake_site = work / "fake_site.py"
    fake_site.write_text(
        "print('sys.path = ' + repr([\n"
        f"    {str(store_purelib)!r},\n"
        f"    {str(anchored)!r},\n"
        "]))\n",
        encoding="utf-8",
    )
    daemon, sentinel = _write_fake_daemon(work)
    config = _config(tmp_path, launcher=False)
    _write_generation_launcher(
        tmp_path / "hermes",
        generation_python,
        daemon,
        site_argv=[str(generation_python), str(fake_site)],
    )
    config_path = _config_path(tmp_path)
    home = work / "home"
    staged = stage_service_state(
        config,
        config_path,
        home=home,
        wheel_path=product_wheel,
        hermes_home=home / ".hermes",
    )

    result = _run_launcher(staged)
    assert result.returncode != 0
    assert not sentinel.exists()

    marker = json.loads(Path(staged["marker"]).read_text(encoding="utf-8"))
    assert marker["reason"].startswith("generation-unanchored:")
    assert str(store_purelib) in marker["reason"]
    assert marker["generation"] is None
    assert list(store_purelib.iterdir()) == []
    assert not (anchored / "hermes_kanban_lanes").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_install_failure_records_stderr_and_uv_rc(tmp_path):
    """A failed pip+uv install leaves a diagnosable marker and log tail."""

    if shutil.which("uv") is None:
        pytest.skip("uv is required for the fallback diagnostic")

    work = tmp_path / "w14-install-error"
    work.mkdir()
    generation_python = _new_generation(work)
    daemon, sentinel = _write_fake_daemon(work)
    config = _config(tmp_path, launcher=False)
    _write_generation_launcher(tmp_path / "hermes", generation_python, daemon)
    config_path = _config_path(tmp_path)
    home = work / "home"
    corrupt = work / "dist" / "hermes_kanban_lanes-0.2.0-py3-none-any.whl"
    corrupt.parent.mkdir()
    corrupt.write_bytes(b"this is not a wheel")
    staged = stage_service_state(
        config,
        config_path,
        home=home,
        wheel_path=corrupt,
        hermes_home=home / ".hermes",
    )

    result = _run_launcher(staged)
    assert result.returncode == 2, result.stderr
    assert not sentinel.exists()

    marker = json.loads(Path(staged["marker"]).read_text(encoding="utf-8"))
    assert marker["reason"].startswith("wheel-install-failed:")
    assert "uv-rc=" in marker["reason"]
    assert isinstance(marker["install_error"], str) and marker["install_error"]
    assert len(marker["install_error"]) <= 800

    log = (Path(staged["state_dir"]) / "launcher.log").read_text(encoding="utf-8")
    assert "install stderr:" in log
    assert "install-uv stderr:" in log
