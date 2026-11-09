"""Hermes pip-plugin entry point (W6 D1).

The plugin is deliberately minimal: :func:`register` registers exactly one CLI
command, ``kanban-lanes``, through the host's ``PluginContext``.  It registers
no hook, tool, provider or filesystem/network access, and importing this module
has no side effects.  The argparse tree lives in :mod:`hermes_kanban_lanes.cli`
and is attached lazily so importing the plugin never imports
``hermes_adapter`` or ``runtime.daemon``.

Installation is opt-in on the host side (``hermes plugins enable
hermes-kanban-lanes``); the standalone ``hermes-kanban-lanes`` entry point keeps
working whether or not the plugin is enabled.
"""

from __future__ import annotations

from typing import Any

COMMAND_NAME = "kanban-lanes"
COMMAND_HELP = "Validate, diagnose and run Hermes Kanban lanes."
COMMAND_DESCRIPTION = (
    "The same CLI as hermes-kanban-lanes: validate-config, validate-card, "
    "doctor, tick, run and service render|install|uninstall|status|restart."
)


def setup_command(parser: Any) -> None:
    """Attach the shared argparse tree to the host's command parser."""

    from . import cli

    cli.attach_commands(parser)


def handle_command(args: Any) -> int:
    """Dispatch the parsed command through the product CLI."""

    from . import cli

    return cli.dispatch(args)


def register(ctx: Any) -> None:
    """Register the single ``kanban-lanes`` CLI command; nothing else."""

    ctx.register_cli_command(
        COMMAND_NAME,
        help=COMMAND_HELP,
        setup_fn=setup_command,
        handler_fn=handle_command,
        description=COMMAND_DESCRIPTION,
    )


__all__ = [
    "COMMAND_DESCRIPTION",
    "COMMAND_HELP",
    "COMMAND_NAME",
    "handle_command",
    "register",
    "setup_command",
]
