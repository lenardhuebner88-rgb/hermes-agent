from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from plugins.kanban.dashboard import plugin_api


def test_usage_consumption_endpoint_is_registered_as_observability_edge() -> None:
    owners = plugin_api.route_contract.owner_by_key()

    assert owners[("GET", "/stats/usage-consumption")] == "observability"
    route = next(
        route
        for route in plugin_api.router.routes
        if route.path == "/stats/usage-consumption"
    )
    assert Path(route.endpoint.__code__.co_filename).name == (
        "usage_consumption_routes.py"
    )


def test_usage_consumption_endpoint_passes_window_and_board_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_path = tmp_path / "usage_facts.db"
    board_path = tmp_path / "kanban.db"
    captured: dict[str, Any] = {}

    def fake_payload(path: Path, **kwargs: Any) -> dict[str, Any]:
        captured["path"] = path
        captured.update(kwargs)
        return {"contract": "usage-consumption.v1"}

    monkeypatch.setattr(plugin_api, "_usage_facts_db_path", lambda: usage_path)
    monkeypatch.setattr(plugin_api, "_resolve_board", lambda board: f"resolved-{board}")
    monkeypatch.setattr(
        plugin_api.kanban_db,
        "kanban_db_path",
        lambda *, board: board_path if board == "resolved-review" else Path("wrong"),
    )
    monkeypatch.setattr(plugin_api, "_build_consumption_payload", fake_payload)

    payload = plugin_api.get_usage_consumption(
        board="review", days=90, breakdown="buzz_unit"
    )

    assert payload == {"contract": "usage-consumption.v1"}
    assert captured == {
        "path": usage_path,
        "days": 90,
        "breakdown": "buzz_unit",
        "kanban_path": board_path,
    }


def test_usage_consumption_endpoint_rejects_bad_params() -> None:
    with pytest.raises(Exception) as exc_info:
        plugin_api.get_usage_consumption(days=14)
    assert "14" in str(exc_info.value) or "400" in str(exc_info.value)
    with pytest.raises(Exception):
        plugin_api.get_usage_consumption(breakdown="bogus")
