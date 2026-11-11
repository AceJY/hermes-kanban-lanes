"""W14: stale ``*.dist-info`` metadata must never stall a service upgrade.

``pip install --target`` overwrites the package directory but never removes an
older ``*.dist-info``.  A second upgrade therefore leaves several versions in
one generation and ``importlib.metadata`` may resolve any of them.  These tests
pin the doctor-side guard; the launcher's cleanup and probe guards live in
``test_w6_service.py``.
"""

from __future__ import annotations

from hermes_kanban_lanes import __version__, cli


def test_doctor_fails_and_prints_exact_cleanup_for_ambiguous_dist_info(
    tmp_path, monkeypatch, make_config, board, make_repo, fake_executor
):
    """Doctor rejects several HKL dist-info dirs and prints the cleanup argv."""

    generation = tmp_path / "site-packages"
    generation.mkdir()
    stale = generation / "hermes_kanban_lanes-0.2.0.dist-info"
    current = generation / f"hermes_kanban_lanes-{__version__}.dist-info"
    stale.mkdir()
    current.mkdir()

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_generation_paths", lambda: [str(generation)])
    monkeypatch.setattr(cli, "_distribution_version", lambda: __version__)

    config = make_config(
        board_path=board.board_path,
        repo_path=make_repo(),
        executor_argv=[fake_executor("")],
    )
    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    check = {entry["name"]: entry for entry in report["checks"]}["plugin-runtime"]

    assert check["ok"] is False
    detail = check["detail"]
    assert "plugin-runtime-dist-info-ambiguous" in detail
    assert stale.name in detail
    assert "cleanup: rm -rf " in detail
    assert str(stale) in detail
    assert report["ok"] is False

    # doctor is read-only: it reports the command but never runs it.
    assert stale.is_dir()
    assert current.is_dir()


def test_doctor_cleanup_keeps_newest_when_version_lookup_is_stale(
    tmp_path, monkeypatch, make_config, board, make_repo, fake_executor
):
    """The production incident: ``__version__`` resolved an old directory.

    With 0.2.0/0.2.1/0.2.3 metadata in one generation,
    ``importlib.metadata.version`` returned 0.2.1.  The cleanup command must
    keep the newest directory (the one the last upgrade wrote) instead of
    deleting 0.2.3 and leaving the stale 0.2.1 behind.
    """

    generation = tmp_path / "site-packages"
    generation.mkdir()
    entries = {}
    for version in ("0.2.0", "0.2.1", "0.2.3"):
        entry = generation / f"hermes_kanban_lanes-{version}.dist-info"
        entry.mkdir()
        entries[version] = entry

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_generation_paths", lambda: [str(generation)])
    monkeypatch.setattr(cli, "_distribution_version", lambda: "0.2.1")
    # Reproduce the ambiguous lookup the incident hit.
    monkeypatch.setattr(cli, "__version__", "0.2.1")

    config = make_config(
        board_path=board.board_path,
        repo_path=make_repo(),
        executor_argv=[fake_executor("")],
    )
    report = cli.doctor_report(config, str(tmp_path / "config.json"))
    check = {entry["name"]: entry for entry in report["checks"]}["plugin-runtime"]

    assert check["ok"] is False
    detail = check["detail"]
    assert "plugin-runtime-dist-info-ambiguous" in detail
    assert f"keep={entries['0.2.3'].name}" in detail

    cleanup = detail.split("cleanup: ", 1)[1].split(";", 1)[0]
    assert str(entries["0.2.0"]) in cleanup
    assert str(entries["0.2.1"]) in cleanup
    assert str(entries["0.2.3"]) not in cleanup
    for entry in entries.values():
        assert entry.is_dir()
