"""W6 D2: CLI surface, exit codes, JSON contracts and the plugin seam."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_kanban_lanes import __version__, cli, plugin
from hermes_kanban_lanes.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"


def _document(tmp_path: Path) -> dict:
    return {
        "schema_version": 1,
        "hermes_source": str(tmp_path / "hermes"),
        "hermes_commit": "a" * 40,
        "boards": {"test": str(tmp_path / "board.db")},
        "repositories": {
            "primary": {"path": str(tmp_path / "repo"), "trusted": True}
        },
        "executors": {"claude": {"argv": ["claude"]}},
        "work_root": str(tmp_path / "work"),
        "max_concurrent": 1,
    }


def _write_config(tmp_path: Path, document: dict | None = None) -> str:
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document or _document(tmp_path)), encoding="utf-8")
    return str(path)


def _write_fake_launcher(tmp_path: Path) -> None:
    """Minimal Hermes installation-bound launcher for service render tests."""

    binary = tmp_path / "hermes" / ".hermes" / "bin" / "hermes"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "tail = args[args.index('--') + 1:] if '--' in args else []\n"
        f"print(json.dumps([{sys.executable!r}, '-I', '-c', 'bootstrap', *tail]))\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)


def _fake_hermes_source(tmp_path: Path) -> tuple[str, str]:
    """A git checkout whose host import chain needs a missing package."""

    source = tmp_path / "fake-hermes"
    package = source / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "kanban_db.py").write_text(
        "import hkl_missing_host_dependency_xyz\n", encoding="utf-8"
    )
    (package / "kanban_db_connect.py").write_text("", encoding="utf-8")
    (package / "kanban_db_dispatch.py").write_text("", encoding="utf-8")
    git = ["git", "-C", str(source)]
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run([*git, "config", "user.email", "hkl@example.invalid"], check=True)
    subprocess.run([*git, "config", "user.name", "HKL Tests"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "fake host"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return str(source), head


def _document_with_host(tmp_path: Path, source: str, head: str) -> dict:
    document = _document(tmp_path)
    document["hermes_source"] = source
    document["hermes_commit"] = head
    return document


class FakeLock:
    stale_reclaimed = False
    previous = None
    pid = 4242
    version = "test"

    def __init__(self) -> None:
        self.released = 0

    def release(self) -> None:
        self.released += 1


def _patch_daemon(monkeypatch, *, preflight=None, lock=None, tick=None, run=None):
    from hermes_kanban_lanes.runtime import daemon

    monkeypatch.setattr(daemon, "preflight", lambda config: list(preflight or []))
    holder = lock if lock is not None else FakeLock()

    def _acquire(*args, **kwargs):
        if isinstance(holder, Exception):
            raise holder
        return holder

    monkeypatch.setattr(daemon, "acquire_daemon_lock", _acquire)
    monkeypatch.setattr(daemon, "log_lock_event", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_append_log", lambda *a, **k: None)
    if tick is not None:
        monkeypatch.setattr(daemon, "tick", tick)
    if run is not None:
        monkeypatch.setattr(daemon, "run_daemon", run)
    from hermes_kanban_lanes.runtime import retention

    monkeypatch.setattr(retention, "maybe_prune_runs", lambda *a, **k: {})
    return daemon, holder


# ---------------------------------------------------------------------------
# parser and entry point
# ---------------------------------------------------------------------------


def test_version_still_prints_installed_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor"],
        ["tick"],
        ["run"],
        ["service"],
        ["service", "render"],
        ["service", "render", "--platform", "launchd"],
        ["service", "install", "--platform", "windows"],
    ],
)
def test_incomplete_commands_exit_two(argv):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code == 2


def test_new_commands_have_help():
    for command in ("doctor", "tick", "run", "service"):
        with pytest.raises(SystemExit) as excinfo:
            cli.main([command, "--help"])
        assert excinfo.value.code == 0


def test_attach_commands_parses_without_product_version_flag():
    parser = argparse.ArgumentParser(prog="hermes kanban-lanes")
    cli.attach_commands(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])
    args = parser.parse_args(["doctor", "--config", "/tmp/x.json", "--json"])
    assert args.command == "doctor"
    assert args.config_path == "/tmp/x.json"
    assert args.json is True


def test_attach_commands_dispatch_matches_product_main(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    parser = argparse.ArgumentParser(prog="hermes kanban-lanes")
    cli.attach_commands(parser)
    args = parser.parse_args(["validate-config", config_path])
    assert cli.dispatch(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "valid": True,
        "schema_version": 1,
        "kind": "config",
    }


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------


def test_tick_success_prints_one_report_object(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    report = {"schema_version": 1, "tick": 0, "errors": [], "runs": []}
    daemon, lock = _patch_daemon(monkeypatch, tick=lambda config: report)
    assert cli.main(["tick", "--config", config_path, "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == report
    assert captured.err == ""
    assert lock.released == 1


def test_tick_report_with_errors_exits_three(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    report = {"schema_version": 1, "tick": 0, "errors": ["board:test:boom"]}
    _patch_daemon(monkeypatch, tick=lambda config: report)
    assert cli.main(["tick", "--config", config_path, "--json"]) == 3
    captured = capsys.readouterr()
    assert json.loads(captured.out)["errors"] == ["board:test:boom"]
    assert captured.err == ""


def test_tick_preflight_failure_never_acquires_the_lock(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    from hermes_kanban_lanes.runtime import daemon

    monkeypatch.setattr(daemon, "preflight", lambda config: ["board:test:board-missing"])

    def _forbidden(*args, **kwargs):
        raise AssertionError("lock must not be taken when preflight fails")

    monkeypatch.setattr(daemon, "acquire_daemon_lock", _forbidden)
    assert cli.main(["tick", "--config", config_path]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["errors"] == ["board:test:board-missing"]


def test_tick_lock_held_exits_three_with_holder(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    from hermes_kanban_lanes.runtime import daemon

    held = daemon.DaemonLockHeld({"pid": 99, "version": "0.1.0"})
    _patch_daemon(monkeypatch, lock=held)
    assert cli.main(["tick", "--config", config_path]) == 3
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "daemon-lock-held"
    assert payload["holder"] == {"pid": 99, "version": "0.1.0"}


def test_tick_lock_unsupported_exits_two(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    from hermes_kanban_lanes.runtime import daemon

    _patch_daemon(monkeypatch, lock=daemon.DaemonLockUnsupported("x"))
    assert cli.main(["tick", "--config", config_path]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "daemon-lock-unsupported"


def test_validate_config_error_has_reason_impact_next_steps(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    assert cli.main(["validate-config", str(path)]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["valid"] is False
    assert payload["reason"] == "invalid-config"
    assert payload["impact"]
    assert any("validate-config" in step for step in payload["next_steps"])


def test_tick_invalid_config_exits_two_without_preflight(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    from hermes_kanban_lanes.runtime import daemon

    monkeypatch.setattr(
        daemon,
        "preflight",
        lambda config: (_ for _ in ()).throw(AssertionError("no preflight")),
    )
    assert cli.main(["tick", "--config", str(path)]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False
    assert "invalid JSON" in payload["error"]
    assert payload["reason"] == "invalid-config"
    assert payload["impact"]
    assert payload["next_steps"]


def test_tick_missing_host_dependency_is_structured_and_never_locks(
    tmp_path, monkeypatch, capsys
):
    source, head = _fake_hermes_source(tmp_path)
    config_path = _write_config(
        tmp_path, _document_with_host(tmp_path, source, head)
    )
    from hermes_kanban_lanes.runtime import daemon

    monkeypatch.setattr(daemon, "preflight", lambda config: [])

    def _forbidden(*args, **kwargs):
        raise AssertionError("lock must not be taken when the host cannot import")

    monkeypatch.setattr(daemon, "acquire_daemon_lock", _forbidden)
    assert cli.main(["tick", "--config", config_path]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False
    assert payload["reason"] == (
        "host-dependency-missing:hkl_missing_host_dependency_xyz"
    )
    assert "same Python environment" in " ".join(payload["next_steps"])
    assert payload["impact"]


def test_run_missing_host_dependency_matches_tick_diagnostic(
    tmp_path, monkeypatch, capsys
):
    source, head = _fake_hermes_source(tmp_path)
    config_path = _write_config(
        tmp_path, _document_with_host(tmp_path, source, head)
    )
    from hermes_kanban_lanes.runtime import daemon

    monkeypatch.setattr(daemon, "preflight", lambda config: [])
    monkeypatch.setattr(
        daemon,
        "acquire_daemon_lock",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("lock must not be taken")
        ),
    )
    assert cli.main(["run", "--config", config_path]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["reason"] == (
        "host-dependency-missing:hkl_missing_host_dependency_xyz"
    )
    assert payload["impact"]
    assert payload["next_steps"]


def test_host_import_check_passes_with_the_real_host(hermes_source):
    from hermes_kanban_lanes.runtime.service import check_host_imports

    assert check_host_imports(hermes_source)["ok"] is True


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_stops_normally_with_zero(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    report = {"schema_version": 1, "tick": 0, "errors": []}
    _patch_daemon(monkeypatch, run=lambda config, **kwargs: report)
    assert cli.main(["run", "--config", config_path, "--poll-seconds", "0.1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["stopped"] is True
    assert payload["last_report"] == report


def test_run_lock_held_exits_three(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    from hermes_kanban_lanes.runtime import daemon

    _patch_daemon(monkeypatch, lock=daemon.DaemonLockHeld({"pid": 7}))
    assert cli.main(["run", "--config", config_path]) == 3
    assert json.loads(capsys.readouterr().err)["error"] == "daemon-lock-held"


def test_run_fatal_tick_exits_three(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)

    def explode(config, **kwargs):
        raise RuntimeError("boom")

    daemon, lock = _patch_daemon(monkeypatch, run=explode)
    assert cli.main(["run", "--config", config_path]) == 3
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "daemon-fatal"
    assert payload["reason"] == "RuntimeError"
    assert lock.released == 1


def test_run_preflight_failure_exits_two(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    _patch_daemon(monkeypatch, preflight=["hermes-source-missing"])
    assert cli.main(["run", "--config", config_path]) == 2
    assert json.loads(capsys.readouterr().err)["errors"] == ["hermes-source-missing"]


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


def _service_payload(**overrides):
    payload = {
        "ok": True,
        "platform": "launchd",
        "unit_path": "/tmp/unit.plist",
        "commands": [],
        "results": [],
        "installed": True,
        "running": True,
        "precondition": None,
    }
    payload.update(overrides)
    return payload


def test_service_status_exit_codes(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    from hermes_kanban_lanes.runtime import service

    monkeypatch.setattr(
        service, "service_status", lambda *a, **k: _service_payload(installed=False)
    )
    assert (
        cli.main(["service", "status", "--platform", "launchd", "--config", config_path])
        == 2
    )
    assert json.loads(capsys.readouterr().out)["installed"] is False

    monkeypatch.setattr(
        service, "service_status", lambda *a, **k: _service_payload()
    )
    assert (
        cli.main(["service", "status", "--platform", "launchd", "--config", config_path])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_service_uninstall_and_restart_exit_codes(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    from hermes_kanban_lanes.runtime import service

    monkeypatch.setattr(
        service, "uninstall_service", lambda *a, **k: _service_payload(ok=False)
    )
    assert (
        cli.main(
            ["service", "uninstall", "--platform", "launchd", "--config", config_path]
        )
        == 2
    )
    capsys.readouterr()

    monkeypatch.setattr(
        service, "uninstall_service", lambda *a, **k: _service_payload()
    )
    assert (
        cli.main(
            ["service", "uninstall", "--platform", "launchd", "--config", config_path]
        )
        == 0
    )
    capsys.readouterr()

    monkeypatch.setattr(
        service,
        "restart_service",
        lambda *a, **k: _service_payload(
            ok=False, precondition="service-not-installed"
        ),
    )
    assert (
        cli.main(
            ["service", "restart", "--platform", "launchd", "--config", config_path]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().out)["precondition"] == "service-not-installed"

    monkeypatch.setattr(
        service, "restart_service", lambda *a, **k: _service_payload(ok=False)
    )
    assert (
        cli.main(
            ["service", "restart", "--platform", "launchd", "--config", config_path]
        )
        == 2
    )


def test_service_dry_run_is_forwarded(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    from hermes_kanban_lanes.runtime import service

    seen = {}

    def fake_install(platform, config, config_path_arg, **kwargs):
        seen.update(platform=platform, path=config_path_arg, **kwargs)
        return _service_payload(dry_run=True)

    monkeypatch.setattr(service, "install_service", fake_install)
    assert (
        cli.main(
            [
                "service",
                "install",
                "--platform",
                "launchd",
                "--config",
                config_path,
                "--dry-run",
            ]
        )
        == 0
    )
    assert seen["dry_run"] is True
    assert seen["platform"] == "launchd"
    assert seen["path"] == os.path.abspath(config_path)


def test_service_error_exits_two(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    from hermes_kanban_lanes.runtime import service

    def fail(*args, **kwargs):
        raise service.ServiceError("service-config-path-must-be-absolute")

    monkeypatch.setattr(service, "install_service", fail)
    assert (
        cli.main(
            ["service", "install", "--platform", "launchd", "--config", config_path]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "service-config-path-must-be-absolute"


def test_service_oserror_exits_two(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    _write_fake_launcher(tmp_path)
    from hermes_kanban_lanes.runtime import service

    def fail_install(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(service, "install_service", fail_install)
    assert (
        cli.main(
            ["service", "install", "--platform", "launchd", "--config", config_path]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"] == "read-only file system"

    def fail_render(unit, out_dir):
        raise OSError("cannot write unit")

    monkeypatch.setattr(service, "render_to", fail_render)
    out_dir = tmp_path / "out"
    assert (
        cli.main(
            [
                "service",
                "render",
                "--platform",
                "launchd",
                "--config",
                config_path,
                "--out",
                str(out_dir),
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"] == "cannot write unit"


def test_service_render_requires_absolute_config(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path)
    _write_fake_launcher(tmp_path)
    monkeypatch.chdir(tmp_path)
    relative = Path(config_path).name
    assert (
        cli.main(
            [
                "service",
                "render",
                "--platform",
                "launchd",
                "--config",
                relative,
                "--out",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    capsys.readouterr()


# ---------------------------------------------------------------------------
# plugin entry point
# ---------------------------------------------------------------------------


class FakeContext:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def register_cli_command(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))

    def register_hook(self, *args, **kwargs) -> None:  # pragma: no cover
        raise AssertionError("plugin must not register hooks")

    def register_tool(self, *args, **kwargs) -> None:  # pragma: no cover
        raise AssertionError("plugin must not register tools")


def test_register_registers_exactly_one_cli_command():
    ctx = FakeContext()
    plugin.register(ctx)
    assert len(ctx.calls) == 1
    args, kwargs = ctx.calls[0]
    assert args == ("kanban-lanes",)
    assert kwargs["help"] == plugin.COMMAND_HELP
    assert kwargs["description"] == plugin.COMMAND_DESCRIPTION
    assert kwargs["setup_fn"] is plugin.setup_command
    assert kwargs["handler_fn"] is plugin.handle_command


def test_plugin_handler_dispatches_the_same_cli(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    setup = argparse.ArgumentParser(prog="hermes kanban-lanes")
    plugin.setup_command(setup)
    args = setup.parse_args(["validate-config", config_path])
    assert plugin.handle_command(args) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "config"


def test_plugin_module_imports_are_side_effect_free():
    tree = ast.parse((SRC_DIR / "hermes_kanban_lanes" / "plugin.py").read_text())
    relative_cli_imports = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                assert node.module in {"__future__", "typing"}
            else:
                # Only ``from . import cli`` inside the two lazy functions.
                assert node.module in {None, ""}
                assert [alias.name for alias in node.names] == ["cli"]
                relative_cli_imports += 1
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in {"typing", "__future__"}
    assert relative_cli_imports == 2


def test_readme_is_a_runnable_user_guide_without_internal_references():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    required = [
        "3.13",
        "same Python environment",
        "hermes-kanban-lanes --version",
        "validate-config",
        "doctor",
        "tick",
        "service render",
        "service status",
        "JSON",
        "Exit codes",
        "launchd",
        "systemd",
        "render-only",
        "reason",
        "impact",
        "next_steps",
        "retention_days",
        "max_total_bytes",
        "max_log_bytes",
        "keep_failed_runs",
        "log_root",
        "restart",
    ]
    for token in required:
        assert token in readme, token
    for forbidden in ("/Users/operator", "/srv/hermes"):
        assert forbidden not in readme
    assert not re.search(r"\bt_[0-9a-f]{8}\b", readme)


def test_example_config_has_service_and_logging_blocks():
    example = REPO_ROOT / "examples" / "config.json"
    text = example.read_text(encoding="utf-8")
    document = json.loads(text)
    assert "service" in document
    assert "logging" in document
    assert "/srv/" not in text
    # The documented example must be structurally valid.
    config = load_config(str(example))
    assert config.service_options().platform in {"launchd", "systemd", None}
    assert config.logging_options().retention_days >= 1


def test_importing_plugin_does_not_pull_host_or_runtime_modules():
    code = (
        "import sys\n"
        "import hermes_kanban_lanes.plugin\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONPATH"] = str(SRC_DIR)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(SRC_DIR.parent),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    loaded = set(result.stdout.splitlines())
    assert "hermes_kanban_lanes.plugin" in loaded
    assert "hermes_kanban_lanes.cli" not in loaded
    assert "hermes_kanban_lanes.hermes_adapter" not in loaded
    assert "hermes_kanban_lanes.runtime.daemon" not in loaded
