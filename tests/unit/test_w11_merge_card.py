"""W11: the merge executor must load Hermes-native dev cards from run.json.

Hermes-native development cards never write ``cards/<task_id>.json``; their
only durable declaration is the ``card`` snapshot on the run.  The merge path
must therefore fall back to the dev task's newest run snapshot, while an
explicit card file keeps priority.  Anything missing, malformed,
non-implementation or borrowed from another task fails closed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from hermes_kanban_lanes.card_contract import CardSpec
from hermes_kanban_lanes.config import (
    Executor,
    LaneConfig,
    MergeConfig,
    Repository,
    ReviewConfig,
)
from hermes_kanban_lanes.contracts import RunIdentity
from hermes_kanban_lanes.runtime.release import _load_card, release_once


def _config(tmp_path: Path) -> LaneConfig:
    return LaneConfig(
        schema_version=1,
        hermes_source=str(tmp_path),
        hermes_commit="a" * 40,
        boards={"test": str(tmp_path / "board.db")},
        repositories={
            "primary": Repository(path=str(tmp_path / "repo"), trusted=True)
        },
        executors={"claude": Executor(argv=(sys.executable,))},
        work_root=str(tmp_path / "work"),
        max_concurrent=1,
        review=ReviewConfig(merge=MergeConfig(enabled=True)),
    )


def _card(
    task_id: str,
    *,
    allow: tuple[str, ...] = ("RESULT.txt",),
    card_kind: str = "implementation",
    closure_for: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "card_kind": card_kind,
        "lineage": task_id,
        "closure_for": closure_for,
        "executor": "claude",
        "repository": "primary",
        "allow": list(allow),
        "commands": [{"argv": [sys.executable, "-c", "pass"], "cwd": "."}],
        "risk": "normal",
        "artifacts": [],
    }


def _write_run(
    work_root: Path,
    task_id: str,
    run_id: int,
    card,
    *,
    suffix: str = "snapshot",
) -> Path:
    run_dir = work_root / "runs" / task_id / f"{run_id}-{suffix}"
    run_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "identity": {
            "schema_version": 1,
            "task_id": task_id,
            "run_id": run_id,
            "claim_lock": "test",
        },
        "card": card,
        "started_at": run_id,
        "finished_at": run_id,
    }
    (run_dir / "run.json").write_text(json.dumps(document), encoding="utf-8")
    return run_dir


class _Adapter:
    """The slice of ``BoardAdapter`` used before a card can be merged."""

    def __init__(self, identity: RunIdentity):
        self._identity = identity
        self.blocked: list[tuple[str, str]] = []

    def identity(self, task_id: str):
        return self._identity if task_id == self._identity.task_id else None

    def block(self, identity: RunIdentity, reason: str, kind: str) -> bool:
        self.blocked.append((reason, kind))
        return True

    def task_row(self, task_id: str):
        return {"id": task_id}

    def run_rows(self, task_id: str):
        return []

    def review_runs(self, task_id: str):
        return []


def _run_release(config: LaneConfig, dev_task_id: str) -> dict:
    identity = RunIdentity(
        schema_version=1,
        task_id="t_merge",
        run_id=9,
        claim_lock="test",
    )
    adapter = _Adapter(identity)
    card = CardSpec.from_dict(
        _card("t_merge", card_kind="merge", closure_for=dev_task_id), config
    )
    return release_once(
        config=config,
        adapter=adapter,
        card=card,
        identity=identity,
        work_root=config.work_root,
    )


def test_run_snapshot_fallback(tmp_path):
    """No explicit JSON: the merge path must use the run's card snapshot."""

    config = _config(tmp_path)
    work_root = Path(config.work_root)
    _write_run(work_root, "t_native", 7, _card("t_native"))
    assert not (work_root / "cards" / "t_native.json").exists()

    document, card = _load_card(work_root, config, "t_native")

    assert card is not None
    assert card.card_kind == "implementation"
    assert card.lineage == "t_native"
    assert document is not None and document["lineage"] == "t_native"

    outcome = _run_release(config, "t_native")

    assert outcome["reason"] != "dev-card-missing"
    # Loaded past the guard, stopped only because the dev task has no review.
    assert outcome["reason"] == "review-missing"


def test_explicit_card_wins(tmp_path):
    """A valid ``cards/<id>.json`` wins over a newer, different snapshot."""

    config = _config(tmp_path)
    work_root = Path(config.work_root)
    _write_run(work_root, "t_both", 7, _card("t_both", allow=("SNAPSHOT.txt",)))
    cards_dir = work_root / "cards"
    cards_dir.mkdir(parents=True)
    (cards_dir / "t_both.json").write_text(
        json.dumps(_card("t_both", allow=("EXPLICIT.txt",))), encoding="utf-8"
    )

    document, card = _load_card(work_root, config, "t_both")

    assert document is not None and document["allow"] == ["EXPLICIT.txt"]
    assert card is not None and card.allow == ("EXPLICIT.txt",)


@pytest.mark.parametrize(
    "broken",
    ["bad-json", "non-implementation", "no-run-json"],
)
def test_invalid_snapshot_fails_closed(tmp_path, broken):
    """Unusable snapshots never guess: the merge blocks with a clear reason."""

    config = _config(tmp_path)
    work_root = Path(config.work_root)
    if broken == "bad-json":
        run_dir = work_root / "runs" / "t_bad" / "3-broken"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text("{not-json", encoding="utf-8")
    elif broken == "non-implementation":
        _write_run(
            work_root,
            "t_bad",
            3,
            _card("t_bad", card_kind="merge", closure_for="t_bad"),
        )
    else:
        (work_root / "runs" / "t_bad" / "3-empty").mkdir(parents=True)

    document, card = _load_card(work_root, config, "t_bad")
    assert card is None
    assert isinstance(document, dict)

    outcome = _run_release(config, "t_bad")

    assert outcome["action"] == "block"
    assert outcome["reason"] == "dev-card-invalid"


def test_no_card_and_no_run_still_missing(tmp_path):
    config = _config(tmp_path)

    assert _load_card(config.work_root, config, "t_absent") == (None, None)

    outcome = _run_release(config, "t_absent")

    assert outcome["action"] == "block"
    assert outcome["reason"] == "dev-card-missing"


def test_latest_run_snapshot_wins(tmp_path):
    """Older snapshots must not override the newest run's declaration."""

    config = _config(tmp_path)
    work_root = Path(config.work_root)
    _write_run(work_root, "t_multi", 1, _card("t_multi", allow=("OLD.txt",)))
    _write_run(work_root, "t_multi", 2, _card("t_multi", allow=("NEW.txt",)))

    _, card = _load_card(work_root, config, "t_multi")

    assert card is not None and card.allow == ("NEW.txt",)
