"""Read-only Prompt-Schmiede catalog endpoint.

Serves the static curated prompt catalog (hermes_cli/promptforge_catalog.py)
under GET /api/promptforge/catalog. No state, no mutation, no auth call needed —
the blanket auth_middleware already gates /api/ paths not in PUBLIC_API_PATHS,
and this catalog is intentionally NOT public.
"""
from __future__ import annotations

from typing import Any

from hermes_cli.promptforge_catalog import PROMPTFORGE_CATALOG


def register_promptforge_routes(app: Any) -> None:
    @app.get("/api/promptforge/catalog")
    async def get_promptforge_catalog() -> dict[str, Any]:
        return PROMPTFORGE_CATALOG
