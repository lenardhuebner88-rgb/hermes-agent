"""Bundled consumers for the generic Kanban lifecycle hook contract.

The Kanban core knows only this registry.  Project integrations remain at the
edge and register through the public plugin hook API without reaching into the
plugin manager's private state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable


logger = logging.getLogger(__name__)


def _register(name: str, callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception as exc:  # pragma: no cover - defensive edge isolation
        logger.debug("bundled kanban lifecycle consumer %s failed: %s", name, exc)


def register_bundled_consumers() -> None:
    """Register every bundled lifecycle observer idempotently."""
    from hermes_cli.design_board_kanban import (
        register_lifecycle_hooks as register_design_board_hooks,
    )
    from plugins.kanban.dashboard.plugin_api import register_push_lifecycle_hooks
    from plugins.kanban.family_organizer import (
        register_lifecycle_hooks as register_family_organizer_hooks,
    )

    for name, callback in (
        ("dashboard-push", register_push_lifecycle_hooks),
        ("design-board", register_design_board_hooks),
        ("family-organizer", register_family_organizer_hooks),
    ):
        _register(name, callback)
