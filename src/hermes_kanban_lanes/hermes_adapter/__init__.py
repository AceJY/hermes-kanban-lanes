"""The single host-compatibility boundary.

This package is the **only** place in the product source tree allowed to import
``hermes_cli`` / ``hermes_agent`` / ``hermes`` or ``sqlite3``.  Every other
product module talks to the board through :class:`BoardAdapter`.

The boundary is a compatibility boundary, not a permission boundary: importing
it does not prove the caller owns a run.  Every write still carries a
``RunIdentity`` and the run/claim fence is re-checked inside the same
``BEGIN IMMEDIATE`` transaction that performs the side effect.
"""

from __future__ import annotations

from .board import BoardAdapter, BoardAdapterError, RunIdentityError
from .intents import IntentError, apply_intent, ensure_receipts, validate_intent

__all__ = [
    "BoardAdapter",
    "BoardAdapterError",
    "IntentError",
    "RunIdentityError",
    "apply_intent",
    "ensure_receipts",
    "validate_intent",
]
