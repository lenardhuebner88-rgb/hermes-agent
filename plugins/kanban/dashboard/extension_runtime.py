"""Loader contract for dashboard route extension modules."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any


def load_api_extension(
    path: Path,
    api_context: dict[str, Any],
    *,
    extension_name: str,
) -> ModuleType:
    """Execute one route module inside an explicit parent API context.

    Dashboard plugin tests intentionally load ``plugin_api.py`` under an
    isolated module name. Executing into that module's dictionary avoids
    importing the canonical module again (and the circular second router that
    would create). It also preserves the historical monkeypatch contract:
    extension handlers and helpers resolve globals through ``plugin_api``.
    """
    parent_name = str(api_context.get("__name__") or "kanban_dashboard")
    module_name = f"{parent_name}.{extension_name}"
    inherited_names = frozenset(
        name for name in api_context if not name.startswith("__")
    )
    api_context["_API_CONTEXT_NAMES"] = inherited_names
    try:
        source = path.read_text(encoding="utf-8")
        exec(compile(source, str(path), "exec"), api_context)
        exports = tuple(api_context.pop("__all__"))
    finally:
        api_context.pop("_API_CONTEXT_NAMES", None)

    module = ModuleType(module_name)
    module.__file__ = str(path)
    module.__all__ = exports
    for name in exports:
        setattr(module, name, api_context[name])
    return module
