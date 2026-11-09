"""Hermes host import-chain probe (W6 D5/D6).

The service unit launches the daemon with the Hermes installation-bound
launch command, so the interpreter that will run the daemon may be the PM
store Python rather than the interpreter used by the installer.  This module
must therefore be runnable with that exact launch command and with the plain
product interpreter:

    python -m hermes_kanban_lanes.runtime.host_probe <hermes_source>

It prints exactly one ``HKL-HOST-PROBE:`` JSON line so both ``doctor`` and
``check_host_imports`` share one implementation and one output format.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from typing import Any

HOST_PROBE_PREFIX = "HKL-HOST-PROBE:"


def _product_root() -> str:
    """``src`` (or the installed parent) containing ``hermes_kanban_lanes``."""

    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def check_host(source: str, product_root: str | None = None) -> dict[str, Any]:
    """Import the host chain from ``source`` and report one result dict.

    A missing ``hermes_source`` is not this check's failure: the existing
    ``host-hermes``/preflight checks report it with their own guidance.
    """

    resolved = os.path.realpath(source)
    root = product_root or _product_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    result: dict[str, Any] = {"ok": True}
    try:
        board = importlib.import_module(
            "hermes_kanban_lanes.hermes_adapter.board"
        )
        board._load_host(resolved)
    except ModuleNotFoundError as exc:
        result = {
            "ok": False,
            "code": "host-dependency-missing",
            "module": exc.name or "",
        }
    except Exception as exc:  # noqa: BLE001 - the host import surface is opaque
        message = str(exc)
        if "outside the configured" in message:
            result = {
                "ok": False,
                "code": "host-import-outside-source",
                "module": "",
            }
        else:
            result = {
                "ok": False,
                "code": "host-import-failed",
                "module": type(exc).__name__,
            }
    return result


def parse_probe_output(output: str) -> dict[str, Any] | None:
    """Return the first ``HKL-HOST-PROBE:`` JSON object in ``output``."""

    for line in (output or "").splitlines():
        if not line.startswith(HOST_PROBE_PREFIX):
            continue
        try:
            payload = json.loads(line[len(HOST_PROBE_PREFIX):])
        except ValueError:
            return None
        if isinstance(payload, dict):
            return payload
        return None
    return None


def main(argv: list[str] | None = None) -> int:
    """CLI entry: probe ``argv[1]`` (the Hermes source) and print one line."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        result: dict[str, Any] = {
            "ok": False,
            "code": "host-probe-usage",
            "module": "",
        }
    else:
        result = check_host(arguments[0])
    print(HOST_PROBE_PREFIX + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())


__all__ = [
    "HOST_PROBE_PREFIX",
    "main",
    "parse_probe_output",
    "check_host",
]
