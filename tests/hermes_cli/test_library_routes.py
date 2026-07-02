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
    test_client = TestClient(web_server.app, base_url="http://127.0.0.1:9119")
    test_client._hermes_test_root = tmp_path  # type: ignore[attr-defined]
    yield test_client
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


def test_items_offset_is_validated_and_wired(client):
    assert client.get("/api/library/items?offset=-1", headers=HEADERS).status_code == 422
    assert client.get("/api/library/items?offset=abc", headers=HEADERS).status_code == 422
    res = client.get("/api/library/items?offset=0", headers=HEADERS)
    assert res.status_code == 200
    payload = res.json()
    assert set(payload) >= {"has_more"}
    assert payload["has_more"] is False  # leere Bibliothek im isolierten Test-Home


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


def test_knowledge_route_exposes_vault_plans_metadata(client):
    vault_root = client._hermes_test_root / "vault" / "03-Agents"  # type: ignore[attr-defined]
    plan_path = vault_root / "Hermes" / "plans" / "2026-07-01-dashboard-refresh.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        """---
created: 2026-07-01
owner: Hermes
type: implementation
status: active
---
# Dashboard Refresh

Widgets härten.
""",
        encoding="utf-8",
    )

    assert client.get("/api/library/knowledge").status_code == 401
    res = client.get("/api/library/knowledge", headers=HEADERS)
    assert res.status_code == 200
    payload = res.json()
    collection = next(item for item in payload["collections"] if item["id"] == "vault-plans")
    assert collection["title"] == "Vault Plans"
    assert collection["accent"] == "rose"
    assert collection["icon"] == "Newspaper"
    assert len(collection["docs"]) == 1
    doc = collection["docs"][0]
    assert doc["title"] == "Dashboard Refresh"
    assert doc["created"] == "2026-07-01"
    assert doc["owner"] == "Hermes"
    assert doc["type"] == "implementation"
    assert doc["status"] == "active"

    detail = client.get("/api/library/knowledge/doc", params={"id": doc["id"]}, headers=HEADERS)
    assert detail.status_code == 200
    detail_doc = detail.json()
    assert detail_doc["source_ref"] == doc["source_ref"]
    assert detail_doc["source_ref"].endswith("Hermes/plans/2026-07-01-dashboard-refresh.md")
    assert detail_doc["created"] == "2026-07-01"
    assert detail_doc["owner"] == "Hermes"
    assert detail_doc["type"] == "implementation"
    assert detail_doc["status"] == "active"


def test_saved_search_routes_create_list_update_delete(client):
    assert client.get("/api/library/saved-searches").status_code == 401

    created = client.post(
        "/api/library/saved-searches",
        headers=HEADERS,
        json={
            "name": "KI Modelle täglich",
            "query": "frontier model releases",
            "topic_tags": ["KI-Modelle"],
            "person_tags": ["Piet"],
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["name"] == "KI Modelle täglich"
    assert body["query"] == "frontier model releases"

    listed = client.get("/api/library/saved-searches", headers=HEADERS)
    assert listed.status_code == 200
    assert [s["name"] for s in listed.json()["items"]] == ["KI Modelle täglich"]

    patched = client.patch(
        f"/api/library/saved-searches/{body['id']}",
        headers=HEADERS,
        json={"name": "KI Modelle Woche", "query": "open weights"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "KI Modelle Woche"
    assert patched.json()["query"] == "open weights"

    deleted = client.delete(f"/api/library/saved-searches/{body['id']}", headers=HEADERS)
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert client.get("/api/library/saved-searches", headers=HEADERS).json()["items"] == []


def test_topic_routes_list_follow_unfollow_demo_seed(client):
    listed = client.get("/api/library/topics", headers=HEADERS)
    assert listed.status_code == 200
    topics = {topic["title"]: topic for topic in listed.json()["items"]}
    topic = topics["KI-Modelle"]
    assert topic["seeded"] is True
    assert topic["followed"] is False

    followed = client.post(f"/api/library/topics/{topic['id']}/follow", headers=HEADERS)
    assert followed.status_code == 200
    assert followed.json()["followed"] is True
    assert followed.json()["subscribed"] is True

    unfollowed = client.delete(f"/api/library/topics/{topic['id']}/follow", headers=HEADERS)
    assert unfollowed.status_code == 200
    assert unfollowed.json()["followed"] is False
    assert unfollowed.json()["subscribed"] is False
