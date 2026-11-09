"""W6 D7/W6-08: two local wheels install, upgrade and roll back offline.

Every wheel is built from a private copy of ``pyproject.toml`` + ``src/``; the
installation venv is created outside the repository and receives no
``PYTHONPATH``, no editable install and no network access (``--no-index``).

A pure wheel venv has the product but not the Hermes host dependency set, so
``doctor`` must fail closed there with a structured host-dependency diagnosis.
The supported install shares the host's Python environment; after that pure
failure is asserted, the test exposes the host interpreter's site-packages in
the venv and exercises the install/upgrade/rollback doctor cycle green.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

OLD_VERSION = "0.1.0"
NEW_VERSION = "0.2.0"
BROKEN_VERSION = "0.3.0"


def _clean_env(home: Path) -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    } | {"HOME": str(home)}


def _run(command, *, cwd: Path, env: dict[str, str], timeout: float = 180):
    return subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _build_wheel(work: Path, version: str, *, break_import: bool = False) -> Path:
    source = work / f"source-{version}"
    source.mkdir()
    shutil.copy2(REPO_ROOT / "pyproject.toml", source / "pyproject.toml")
    # The public metadata reads README.md and ships LICENSE; a wheel build from
    # the private copy needs both just like the real build.
    for name in ("README.md", "LICENSE"):
        document = REPO_ROOT / name
        if document.is_file():
            shutil.copy2(document, source / name)
    shutil.copytree(
        REPO_ROOT / "src",
        source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    pyproject = source / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert f'version = "{OLD_VERSION}"' in text
    pyproject.write_text(
        text.replace(f'version = "{OLD_VERSION}"', f'version = "{version}"'),
        encoding="utf-8",
    )
    if break_import:
        init = source / "src" / "hermes_kanban_lanes" / "__init__.py"
        init.write_text(
            'raise RuntimeError("broken wheel import")\n', encoding="utf-8"
        )

    wheel_dir = work / f"wheel-{version}"
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


def _install(venv_python: Path, wheel: Path, *, cwd: Path, env: dict[str, str]):
    return _run(
        [
            venv_python,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--force-reinstall",
            wheel,
        ],
        cwd=cwd,
        env=env,
    )


def _entry(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "hermes-kanban-lanes"


def _inject_host_dependencies(
    venv_python: Path, *, cwd: Path, env: dict[str, str]
) -> None:
    """Expose the host interpreter's site-packages inside the wheel venv.

    The supported install uses the same Python environment as the Hermes host.
    The pure wheel venv deliberately does not, so this mirrors the supported
    deployment only after the pure-venv failure is asserted.
    """

    site = _run(
        [
            venv_python,
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        cwd=cwd,
        env=env,
    )
    assert site.returncode == 0, site.stderr
    host_site = sysconfig.get_paths()["purelib"]
    assert (Path(host_site) / "ruamel").is_dir(), host_site
    pth = Path(site.stdout.strip()) / "hkl-host-dependencies.pth"
    pth.write_text(host_site + "\n", encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv layout")
def test_wheel_install_upgrade_rollback(
    tmp_path, hermes_source, hermes_commit, make_repo
):
    from hermes_kanban_lanes.hermes_adapter.board import BoardAdapter

    work = tmp_path / "upgrade"
    work.mkdir()
    outside = work / "outside"
    outside.mkdir()
    home = work / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-ant-upgrade-secret-123456"}}),
        encoding="utf-8",
    )
    env = _clean_env(home)

    wheel_old = _build_wheel(work, OLD_VERSION)
    wheel_new = _build_wheel(work, NEW_VERSION)
    wheel_broken = _build_wheel(work, BROKEN_VERSION, break_import=True)

    venv_dir = work / "venv"
    created = _run([sys.executable, "-m", "venv", venv_dir], cwd=work, env=env)
    assert created.returncode == 0, created.stderr
    venv_python = venv_dir / "bin" / "python"
    entry = _entry(venv_dir)

    # -- lane inputs (temp board/repo/config; never the real board) --------
    board_path = work / "board.db"
    BoardAdapter.create_board(board_path, hermes_source).close()
    repo = make_repo("upgrade-repo")
    executor = work / "fake-claude"
    executor.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "fake-claude 1.0"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    executor.chmod(0o755)
    work_root = work / "lane-work"
    work_root.mkdir()
    config_path = work / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "hermes_source": hermes_source,
                "hermes_commit": hermes_commit,
                "boards": {"upgrade": str(board_path)},
                "repositories": {
                    "primary": {"path": str(repo), "trusted": True}
                },
                "executors": {"claude": {"argv": [str(executor)]}},
                "work_root": str(work_root),
                "max_concurrent": 1,
            }
        ),
        encoding="utf-8",
    )
    config_before = config_path.read_bytes()
    board_before = board_path.read_bytes()

    # -- install old ------------------------------------------------------
    installed = _install(venv_python, wheel_old, cwd=outside, env=env)
    assert installed.returncode == 0, installed.stderr
    version = _run([entry, "--version"], cwd=outside, env=env)
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == OLD_VERSION

    # -- pure wheel venv: doctor fails closed with a structured diagnosis ---
    doctor = _run(
        [entry, "doctor", "--config", config_path, "--json"], cwd=outside, env=env
    )
    assert doctor.returncode == 2, doctor.stderr
    assert doctor.stderr == ""
    failure = json.loads(doctor.stdout)
    assert failure["ok"] is False
    assert failure["reason"].startswith("host-dependency-missing:")
    assert failure["impact"]
    assert failure["next_steps"]
    assert any(
        "same Python environment" in step for step in failure["next_steps"]
    ), failure["next_steps"]
    assert "sk-ant-upgrade-secret-123456" not in doctor.stdout

    # -- supported install: host dependency set visible to the same venv ----
    _inject_host_dependencies(venv_python, cwd=outside, env=env)
    doctor = _run(
        [entry, "doctor", "--config", config_path, "--json"], cwd=outside, env=env
    )
    assert doctor.returncode == 0, doctor.stderr
    payload = json.loads(doctor.stdout)
    assert payload["ok"] is True
    assert payload["version"] == OLD_VERSION
    assert "sk-ant-upgrade-secret-123456" not in doctor.stdout

    probe = _run(
        [
            venv_python,
            "-c",
            "import json, hermes_kanban_lanes as p; "
            "print(json.dumps({'file': p.__file__, 'version': p.__version__}))",
        ],
        cwd=outside,
        env=env,
    )
    assert probe.returncode == 0, probe.stderr
    installed_probe = json.loads(probe.stdout)
    assert installed_probe["version"] == OLD_VERSION
    assert Path(installed_probe["file"]).resolve().is_relative_to(venv_dir.resolve())

    # -- upgrade ----------------------------------------------------------
    upgraded = _install(venv_python, wheel_new, cwd=outside, env=env)
    assert upgraded.returncode == 0, upgraded.stderr
    version = _run([entry, "--version"], cwd=outside, env=env)
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == NEW_VERSION
    doctor = _run(
        [entry, "doctor", "--config", config_path, "--json"], cwd=outside, env=env
    )
    assert doctor.returncode == 0, doctor.stderr
    assert json.loads(doctor.stdout)["version"] == NEW_VERSION

    # -- broken candidate: install succeeds, import fails ------------------
    broken = _install(venv_python, wheel_broken, cwd=outside, env=env)
    assert broken.returncode == 0, broken.stderr
    failed = _run([entry, "--version"], cwd=outside, env=env)
    assert failed.returncode != 0
    assert "broken wheel import" in (failed.stderr + failed.stdout)

    # -- rollback to the kept old wheel ------------------------------------
    rolled_back = _install(venv_python, wheel_old, cwd=outside, env=env)
    assert rolled_back.returncode == 0, rolled_back.stderr
    version = _run([entry, "--version"], cwd=outside, env=env)
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == OLD_VERSION
    doctor = _run(
        [entry, "doctor", "--config", config_path, "--json"], cwd=outside, env=env
    )
    assert doctor.returncode == 0, doctor.stderr
    assert json.loads(doctor.stdout)["ok"] is True

    # Config and board bytes never changed during any install/doctor cycle.
    assert config_path.read_bytes() == config_before
    assert board_path.read_bytes() == board_before
