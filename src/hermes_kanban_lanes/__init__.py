"""Hermes Kanban Lanes W1 package.

W1 provides strict data, configuration and card contracts plus a read-only
validation CLI.  It deliberately contains no execution kernel: importing this
package never starts a process, opens a board/database, or touches the network.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _distribution_version

_DISTRIBUTION = "hermes-kanban-lanes"

try:
    __version__ = _distribution_version(_DISTRIBUTION)
except PackageNotFoundError:
    # A source checkout that has not been built/installed.  The real version is
    # defined only in pyproject.toml, so this deliberately is a sentinel rather
    # than a duplicated version string.
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
