"""Bibliothek-Routen (Härtung c): FastAPI-Wiring über den echten App-Stack.

Die Funktions-Tests (test_library_view.py) decken Adapter/Redaction ab;
hier läuft der TestClient gegen ``web_server.app`` und beweist, dass die
Routen wirklich montiert sind, das Session-Gate greift und die
Validierungs-Grenzen (limit, Kategorie, Traversal-IDs) als 4xx ankommen.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import library_view as lv
from hermes_cli import web_server


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Loopback-TestClient mit isoliertem HERMES_HOME (kein Live-Lesesaal)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    lv._cron_parse_cache.clear()
    lv._cron_dir_cache.clear()
    kb.init_db()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.auth_required = False
    yield TestClient(web_server.app, base_url="http://127.0.0.1:9119")
    web_server.app.state.bound_host = prev_host
    web_server.app.state.auth_required = prev_required


HEADERS = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}


def test_items_requires_session_token(client):
    assert client.get("/api/library/items").status_code == 401
    res = client.get("/api/library/items", headers=HEADERS)
    assert res.status_code == 200
    payload = res.json()
    assert set(payload) >= {"items", "count", "truncated", "categories"}
    assert payload["categories"] == list(lv.CATEGORIES)


def test_items_limit_bounds_are_validated(client):
    assert client.get("/api/library/items?limit=0", headers=HEADERS).status_code == 422
    assert client.get("/api/library/items?limit=500", headers=HEADERS).status_code == 422
    assert client.get("/api/library/items?limit=abc", headers=HEADERS).status_code == 422
    assert client.get("/api/library/items?limit=200", headers=HEADERS).status_code == 200


def test_items_unknown_category_is_400(client):
    res = client.get("/api/library/items?category=quatsch", headers=HEADERS)
    assert res.status_code == 400
    assert client.get(
        "/api/library/items?category=news", headers=HEADERS,
    ).status_code == 200


def test_item_traversal_ids_are_400_not_5xx(client):
    for evil in (
        "../../../etc/passwd",
        "cron::main::16dd6ac01fc0::../../jobs.json",
        "cron::main::../secrets::2026-06-10_07-31-09.md",
        "deliverable::t_x::../../../etc/passwd.md",
    ):
        res = client.get("/api/library/item", params={"id": evil}, headers=HEADERS)
        assert res.status_code == 400, evil


def test_item_unknown_id_is_404_and_gate_applies(client):
    assert client.get(
        "/api/library/item", params={"id": "research::t_00000000"},
    ).status_code == 401
    res = client.get(
        "/api/library/item", params={"id": "research::t_00000000"}, headers=HEADERS,
    )
    assert res.status_code == 404
