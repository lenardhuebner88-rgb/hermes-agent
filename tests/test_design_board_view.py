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
    monkeypatch.setattr(view, "task_facets", lambda ids: [])
    app = FastAPI()
    view.register_design_board_routes(app)
    return _CompatTestClient(app)


def test_create_list_get_card(client):
    r = client.post("/api/design-board/cards", json={"kind": "bug", "title": "Overlap"})
    assert r.status_code == 200
    cid = r.json()["id"]
    assert any(c["id"] == cid for c in client.get("/api/design-board/cards").json())
    got = client.get(f"/api/design-board/cards/{cid}").json()
    assert got["title"] == "Overlap"
    assert got["derived_status"] is None


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
