import subprocess

import pytest
import anyio
import httpx
from fastapi import FastAPI
from hermes_cli import design_board_view as view
from hermes_cli import design_board_store as store


class _CompatTestClient:
    def __init__(self, app: FastAPI):
        self.app = app

    def get(self, url: str, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs):
        return self._request("PATCH", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs):
        async def _send():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.request(method, url, **kwargs)

        return anyio.run(_send)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(view, "batch_task_facets", lambda ids: {})
    app = FastAPI()
    view.register_design_board_routes(app)
    return _CompatTestClient(app)


def test_create_list_get_card(client):
    r = client.post("/api/design-board/cards", json={"kind": "bug", "title": "Overlap"})
    assert r.status_code == 200
    cid = r.json()["id"]
    cards = client.get("/api/design-board/cards").json()
    assert any(c["id"] == cid for c in cards)
    listed = next(c for c in cards if c["id"] == cid)
    assert listed["derived_status"] is None
    got = client.get(f"/api/design-board/cards/{cid}").json()
    assert got["title"] == "Overlap"
    assert got["derived_status"] is None


def test_list_derives_status_from_linked_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        view, "batch_task_facets",
        lambda ids: {"t_1": {"id": "t_1", "status": "done", "assignee": "coder", "terminal": True}}
        if ids else {},
    )
    app = FastAPI()
    view.register_design_board_routes(app)
    client = _CompatTestClient(app)
    cid = client.post("/api/design-board/cards", json={"kind": "bug", "title": "x"}).json()["id"]
    store.link_task(cid, "t_1")
    cards = client.get("/api/design-board/cards").json()
    assert cards[0]["derived_status"] == "addressed"


def test_list_survives_batch_lookup_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import sqlite3

    def _raise(_ids):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(view, "batch_task_facets", _raise)
    app = FastAPI()
    view.register_design_board_routes(app)
    client = _CompatTestClient(app)
    cid = client.post("/api/design-board/cards", json={"kind": "bug", "title": "x"}).json()["id"]
    store.link_task(cid, "t_1")
    cards = client.get("/api/design-board/cards").json()
    assert len(cards) == 1
    assert cards[0]["derived_status"] is None


def test_get_missing_card_404(client):
    assert client.get("/api/design-board/cards/c_nope").status_code == 404


def test_upload_and_serve_asset(client):
    cid = client.post("/api/design-board/cards", json={"kind": "bug", "title": "x"}).json()["id"]
    up = client.post(f"/api/design-board/cards/{cid}/images",
                     files={"file": ("shot.png", b"\x89PNGbytes", "image/png")})
    assert up.status_code == 200
    name = up.json()["name"]
    served = client.get(f"/api/design-board/cards/{cid}/assets/{name}")
    assert served.status_code == 200
    assert served.content == b"\x89PNGbytes"
    assert served.headers["content-type"].startswith("image/")


def test_asset_traversal_rejected(client):
    cid = client.post("/api/design-board/cards", json={"kind": "bug", "title": "x"}).json()["id"]
    assert client.get(f"/api/design-board/cards/{cid}/assets/..%2f..%2fpasswd").status_code in (400, 404)


def test_add_entry_with_pins(client):
    cid = client.post("/api/design-board/cards", json={"kind": "bug", "title": "x"}).json()["id"]
    r = client.post(f"/api/design-board/cards/{cid}/entries",
                    json={"author": "piet", "kind": "screenshot", "note": "here",
                          "pins": [{"id": "p1", "x": 0.4, "y": 0.6, "note": "gap"}]})
    assert r.status_code == 200
    card = client.get(f"/api/design-board/cards/{cid}").json()
    assert card["entries"][0]["pins"][0]["x"] == 0.4


def test_add_comment_entry(client):
    cid = client.post("/api/design-board/cards", json={"kind": "bug", "title": "x"}).json()["id"]
    r = client.post(f"/api/design-board/cards/{cid}/entries",
                    json={"author": "piet", "kind": "comment", "note": "just a note"})
    assert r.status_code == 200
    card = client.get(f"/api/design-board/cards/{cid}").json()
    assert len(card["entries"]) == 1
    assert card["entries"][0]["kind"] == "comment"
    assert card["entries"][0]["note"] == "just a note"
    assert card["entries"][0]["asset"] is None


def test_patch_status(client):
    cid = client.post("/api/design-board/cards", json={"kind": "bug", "title": "x"}).json()["id"]
    r = client.patch(f"/api/design-board/cards/{cid}", json={"status": "addressed"})
    assert r.status_code == 200
    assert r.json()["status"] == "addressed"
    assert "derived_status" in r.json()
    assert "task_facets" in r.json()


def test_patch_invalid_status_returns_400(client):
    cid = client.post("/api/design-board/cards", json={"kind": "bug", "title": "x"}).json()["id"]
    r = client.patch(f"/api/design-board/cards/{cid}", json={"status": "invalid"})
    assert r.status_code == 400


def test_list_and_detail_carry_kanban_ok(client):
    cid = client.post("/api/design-board/cards", json={"kind": "bug", "title": "x"}).json()["id"]
    cards = client.get("/api/design-board/cards").json()
    assert cards[0]["kanban_ok"] is True
    card = client.get(f"/api/design-board/cards/{cid}").json()
    assert card["kanban_ok"] is True


def test_list_reports_kanban_ok_false_on_lookup_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import sqlite3

    def _raise(_ids):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(view, "batch_task_facets", _raise)
    app = FastAPI()
    view.register_design_board_routes(app)
    client = _CompatTestClient(app)
    client.post("/api/design-board/cards", json={"kind": "bug", "title": "x"})
    cards = client.get("/api/design-board/cards").json()
    assert cards[0]["kanban_ok"] is False


# Fixture mirrors the shape of a real card harvested from
# ~/.hermes/design-board/cards/c_abfa284a/card.json (values copied, not
# synthesized — same key layout, same pin structure incl. empty notes).
_REAL_CARD_ENTRY = {
    "author": "piet", "kind": "screenshot",
    "note": "Navigation  - front- colors ",
    "pins": [
        {"id": "p1", "x": 0.8121597096188747, "y": 0.802750487750848, "note": ""},
        {"id": "p2", "x": 0.7431941923774955, "y": 0.6312017798774212, "note": "overlaps puls chip"},
    ],
}


def test_promote_creates_task_and_links(client, monkeypatch):
    cid = client.post("/api/design-board/cards",
                      json={"kind": "bug", "title": "Design an Fleet immernoch nicht gefixt"}).json()["id"]
    client.post(f"/api/design-board/cards/{cid}/entries", json=_REAL_CARD_ENTRY)

    from hermes_cli import design_board_cli

    monkeypatch.setattr(design_board_cli.kanban_db, "connect_closing", lambda *a, **k: _FakeCtx())
    calls = []

    def fake_create_task(conn, *, title, body, assignee=None, idempotency_key=None, **kw):
        calls.append(idempotency_key)
        return "t_promoted1"

    monkeypatch.setattr(design_board_cli.kanban_db, "create_task", fake_create_task)

    r = client.post(f"/api/design-board/cards/{cid}/promote")
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == "t_promoted1"
    assert body["card"]["linked_tasks"] == ["t_promoted1"]
    assert "kanban_ok" in body["card"]
    assert calls == [f"design-board:{cid}"]

    # idempotency: a second promote on an already-linked card is rejected
    r2 = client.post(f"/api/design-board/cards/{cid}/promote")
    assert r2.status_code == 409


def test_promote_missing_card_404(client):
    assert client.post("/api/design-board/cards/c_nope/promote").status_code == 404


def test_promote_reports_kanban_unavailable(client, monkeypatch):
    cid = client.post("/api/design-board/cards", json={"kind": "bug", "title": "x"}).json()["id"]
    from hermes_cli import design_board_cli
    import sqlite3

    def _raise(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(design_board_cli.kanban_db, "connect_closing", _raise)
    r = client.post(f"/api/design-board/cards/{cid}/promote")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "kanban_unavailable"


def test_promote_blank_title_returns_400(client, monkeypatch):
    cid = client.post("/api/design-board/cards", json={"kind": "bug", "title": ""}).json()["id"]
    from hermes_cli import design_board_cli

    def _raise(*a, **k):
        raise ValueError("title is required")

    monkeypatch.setattr(design_board_cli.kanban_db, "connect_closing", lambda *a, **k: _FakeCtx())
    monkeypatch.setattr(design_board_cli.kanban_db, "create_task", _raise)
    r = client.post(f"/api/design-board/cards/{cid}/promote")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_card"


class _FakeCtx:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def _stub_render(monkeypatch, png_bytes: bytes = b"PNGDATA"):
    """Replace the real chromium render so add_mockup runs without a browser."""
    from hermes_cli import design_board_cli

    def _fake(html_path, png_path, **kw):
        with open(png_path, "wb") as fh:
            fh.write(png_bytes)

    monkeypatch.setattr(design_board_cli, "render_html_to_png", _fake)


def test_upload_mockup_creates_html_entry_and_serves_asset(client, monkeypatch):
    _stub_render(monkeypatch)
    cid = client.post("/api/design-board/cards", json={"kind": "mockup", "title": "Hero"}).json()["id"]
    up = client.post(
        f"/api/design-board/cards/{cid}/mockups",
        files={"file": ("hero.html", b"<h1>hi</h1>", "text/html")},
        data={"note": "hero mockup"},
    )
    assert up.status_code == 200
    card = client.get(f"/api/design-board/cards/{cid}").json()
    entry = card["entries"][-1]
    assert entry["kind"] == "mockup_html"
    assert entry["note"] == "hero mockup"
    assert entry["html"] and entry["html"].endswith(".html")
    assert entry["asset"] and entry["asset"].endswith(".png")
    # the stored HTML asset is served back verbatim (drives the live iframe)
    html_name = entry["html"].split("/")[-1]
    served = client.get(f"/api/design-board/cards/{cid}/assets/{html_name}")
    assert served.status_code == 200
    assert served.content == b"<h1>hi</h1>"
    # and the rendered PNG sibling is served too
    png_name = entry["asset"].split("/")[-1]
    assert client.get(f"/api/design-board/cards/{cid}/assets/{png_name}").status_code == 200


def test_upload_mockup_forces_html_extension(client, monkeypatch):
    _stub_render(monkeypatch)
    cid = client.post("/api/design-board/cards", json={"kind": "mockup", "title": "x"}).json()["id"]
    up = client.post(
        f"/api/design-board/cards/{cid}/mockups",
        files={"file": ("noext", b"<h1/>", "application/octet-stream")},
    )
    assert up.status_code == 200
    entry = client.get(f"/api/design-board/cards/{cid}").json()["entries"][-1]
    assert entry["html"].endswith(".html")


def test_upload_mockup_missing_card_404(client):
    r = client.post(
        "/api/design-board/cards/c_nope/mockups",
        files={"file": ("m.html", b"<h1/>", "text/html")},
    )
    assert r.status_code == 404


def test_upload_mockup_too_large_returns_413(client, monkeypatch):
    _stub_render(monkeypatch)
    monkeypatch.setattr(view, "_MAX_HTML_BYTES", 8)
    cid = client.post("/api/design-board/cards", json={"kind": "mockup", "title": "x"}).json()["id"]
    r = client.post(
        f"/api/design-board/cards/{cid}/mockups",
        files={"file": ("big.html", b"<h1>far too long for the tiny cap</h1>", "text/html")},
    )
    assert r.status_code == 413
    assert r.json()["detail"]["error"] == "file_too_large"


def test_upload_mockup_render_unavailable_returns_502(client, monkeypatch):
    from hermes_cli import design_board_cli

    def _missing(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "chromium-shot")

    monkeypatch.setattr(design_board_cli, "render_html_to_png", _missing)
    cid = client.post("/api/design-board/cards", json={"kind": "mockup", "title": "x"}).json()["id"]
    r = client.post(
        f"/api/design-board/cards/{cid}/mockups",
        files={"file": ("m.html", b"<h1/>", "text/html")},
    )
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "render_unavailable"


def test_upload_mockup_render_failed_returns_502(client, monkeypatch):
    from hermes_cli import design_board_cli

    def _boom(*a, **k):
        raise RuntimeError("chromium render failed: b'boom'")

    monkeypatch.setattr(design_board_cli, "render_html_to_png", _boom)
    cid = client.post("/api/design-board/cards", json={"kind": "mockup", "title": "x"}).json()["id"]
    r = client.post(
        f"/api/design-board/cards/{cid}/mockups",
        files={"file": ("m.html", b"<h1/>", "text/html")},
    )
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "render_failed"


def test_upload_mockup_render_timeout_returns_504(client, monkeypatch):
    from hermes_cli import design_board_cli

    def _slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="chromium-shot", timeout=60)

    monkeypatch.setattr(design_board_cli, "render_html_to_png", _slow)
    cid = client.post("/api/design-board/cards", json={"kind": "mockup", "title": "x"}).json()["id"]
    r = client.post(
        f"/api/design-board/cards/{cid}/mockups",
        files={"file": ("m.html", b"<h1/>", "text/html")},
    )
    assert r.status_code == 504
    assert r.json()["detail"]["error"] == "render_timeout"
