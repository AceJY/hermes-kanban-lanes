"""W6 D6: unit rendering and user-level lifecycle through an injected runner."""

from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path

import pytest

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
    service_status,
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
    """The unit carries the launcher's argv, never the installer's Python."""

    config = _config(tmp_path, launcher=False)
    _write_launcher(
        tmp_path / "hermes",
        prefix=["-I", "-c", BOOTSTRAP],
        interpreter=sys.executable,
    )
    config_path = _config_path(tmp_path)

    expected = [sys.executable, "-I", "-c", BOOTSTRAP, "run", "--config", config_path]
    assert (
        resolve_launch_argv(
            config.hermes_source, "run", "--config", config_path
        )
        == expected
    )

    unit = render_unit("launchd", config, config_path, environ={})
    assert list(unit.program_arguments) == expected
    assert unit.program_arguments[:4] == (
        sys.executable,
        "-I",
        "-c",
        BOOTSTRAP,
    )
    assert unit.program_arguments[-3:] == ("run", "--config", config_path)
    document = plistlib.loads(unit.content.encode("utf-8"))
    assert document["ProgramArguments"] == expected

    systemd = render_unit("systemd", config, config_path, environ={})
    exec_start = next(
        line
        for line in systemd.content.splitlines()
        if line.startswith("ExecStart=")
    )
    assert exec_start.startswith(
        f"ExecStart={sys.executable} -I -c "
    )
    assert exec_start.endswith(f"run --config {config_path}")


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


def test_cli_service_render_and_dry_run_install(tmp_path, capsys):
    from hermes_kanban_lanes.cli import main

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
def test_cli_systemd_lifecycle_fails_closed_with_guidance(tmp_path, capsys):
    from hermes_kanban_lanes.cli import main

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
