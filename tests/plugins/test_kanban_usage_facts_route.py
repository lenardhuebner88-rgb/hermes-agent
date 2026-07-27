from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from plugins.kanban.dashboard import plugin_api


def test_usage_facts_endpoint_is_registered_as_observability_edge() -> None:
    owners = plugin_api.route_contract.owner_by_key()

    assert owners[("GET", "/stats/usage-facts")] == "observability"
    route = next(
        route
        for route in plugin_api.router.routes
        if route.path == "/stats/usage-facts"
    )
    assert Path(route.endpoint.__code__.co_filename).name == (
        "usage_facts_routes.py"
    )


def test_usage_facts_endpoint_passes_board_and_filters_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_path = tmp_path / "usage_facts.db"
    board_path = tmp_path / "kanban.db"
    hermes_home = tmp_path / "hermes"
    captured: dict[str, Any] = {}

    def fake_payload(path: Path, **kwargs: Any) -> dict[str, Any]:
        captured["path"] = path
        captured.update(kwargs)
        return {"contract_version": "usage-facts.v1"}

    monkeypatch.setattr(
        plugin_api,
        "_usage_facts_db_path",
        lambda: usage_path,
    )
    monkeypatch.setattr(
        plugin_api,
        "_usage_get_hermes_home",
        lambda: hermes_home,
    )
    monkeypatch.setattr(
        plugin_api,
        "_resolve_board",
        lambda board: f"resolved-{board}",
    )
    monkeypatch.setattr(
        plugin_api.kanban_db,
        "kanban_db_path",
        lambda *, board: board_path
        if board == "resolved-review"
        else Path("wrong"),
    )
    monkeypatch.setattr(
        plugin_api,
        "_build_usage_facts_payload",
        fake_payload,
    )

    payload = plugin_api.get_usage_facts_stats(
        board="review",
        origin=["claude_code", "codex_cli"],
        captured_from="2026-07-01T00:00:00Z",
        captured_to="2026-08-01T00:00:00Z",
    )

    assert payload == {"contract_version": "usage-facts.v1"}
    assert captured == {
        "path": usage_path,
        "kanban_path": board_path,
        "profiles_root": hermes_home / "profiles",
        "origins": ["claude_code", "codex_cli"],
        "captured_from": "2026-07-01T00:00:00Z",
        "captured_to": "2026-08-01T00:00:00Z",
    }
