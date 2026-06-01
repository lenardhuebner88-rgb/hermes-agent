"""Tests for the read-only Orchestrator backlog endpoints
(``GET /api/orchestration/backlog`` and detail by id).

The endpoint parses the orchestration workspace's ``backlog/*.md`` frontmatter
(Backlog.md schema: status/priority/dependsOn/planGate/created) from the working
tree. These tests assert the parse/counts/shape logic and the route contract
against tmp fixtures (no real backlog, no live server).
"""

import asyncio
import json

import pytest

from hermes_cli.orchestration_backlog_view import (
    _parse_bool,
    _parse_depends_on,
    _parse_frontmatter,
    _read_items_sync,
)


def _write(dir_, name, **fm):
    lines = ["---"]
    for key, value in fm.items():
        lines.append(f"{key}: {value}")
    lines += ["---", "", "## Ziel", "", "body mit --- als Trennlinie"]
    (dir_ / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _asgi_get(app, path):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    await app(scope, receive, send)

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return status, json.loads(body.decode("utf-8"))


def test_parse_frontmatter_keeps_colon_values_and_ignores_body_rules():
    text = "---\nid: f-x\ntitle: Foo: Bar\ngate: cd web && tsc\n---\n# Body\n\n---\n"
    fm = _parse_frontmatter(text)
    assert fm["title"] == "Foo: Bar"
    assert fm["gate"] == "cd web && tsc"


def test_parse_frontmatter_missing_or_unterminated():
    assert _parse_frontmatter("# kein Frontmatter") == {}
    assert _parse_frontmatter("---\nid: x\nkein Ende") == {}


def test_parse_depends_on():
    assert _parse_depends_on("[a, b]") == ["a", "b"]
    assert _parse_depends_on("[]") == []
    assert _parse_depends_on("[ f-one ]") == ["f-one"]
    assert _parse_depends_on(None) == []
    assert _parse_depends_on("[a, , b,]") == ["a", "b"]
    assert _parse_depends_on("bare") == ["bare"]


def test_parse_bool():
    assert _parse_bool("true") is True
    assert _parse_bool("True") is True
    assert _parse_bool("false") is False
    assert _parse_bool("") is False
    assert _parse_bool(None) is False


def test_read_items_counts_and_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_BACKLOG_DIR", str(tmp_path))
    monkeypatch.delenv("ORCHESTRATION_BACKLOG_REF", raising=False)
    _write(tmp_path, "f-a.md", id="f-a", title="A", status="doing",
           priority="high", dependsOn="[]", planGate="true", created="2026-06-01")
    _write(tmp_path, "f-b.md", id="f-b", title="B", status="todo",
           priority="medium", dependsOn="[f-a]", planGate="false", created="2026-05-30")
    _write(tmp_path, "f-c.md", id="f-c", title="C", status="done",
           priority="low", dependsOn="[f-a, f-b]", planGate="false", created="2026-05-29")
    # README has no frontmatter → must be dropped by the parser.
    (tmp_path / "README.md").write_text("# Backlog\n\nnur Prosa\n", encoding="utf-8")

    out = _read_items_sync(0)

    assert out["schema"] == "orchestration-backlog-v1"
    assert out["source"]["count"] == 3  # README dropped
    assert out["source"]["ref"] == "fs:working-tree"
    assert out["counts"]["doing"] == 1
    assert out["counts"]["todo"] == 1
    assert out["counts"]["done"] == 1
    assert out["counts"]["backlog"] == 0

    by_id = {it["id"]: it for it in out["items"]}
    assert set(by_id) == {"f-a", "f-b", "f-c"}
    assert by_id["f-a"]["planGate"] is True
    assert by_id["f-b"]["planGate"] is False
    assert by_id["f-b"]["dependsOn"] == ["f-a"]
    assert by_id["f-c"]["dependsOn"] == ["f-a", "f-b"]
    assert by_id["f-a"]["priority"] == "high"
    assert by_id["f-a"]["created"] == "2026-06-01"


def test_read_items_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_BACKLOG_DIR", str(tmp_path / "nope"))
    monkeypatch.delenv("ORCHESTRATION_BACKLOG_REF", raising=False)
    out = _read_items_sync(0)
    assert out["items"] == []
    assert out["counts"]["doing"] == 0
    assert out["source"]["ref"] == "missing"
    assert out["error"]


def test_route_returns_json(tmp_path, monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from fastapi import FastAPI

    from hermes_cli.orchestration_backlog_view import register_orchestration_backlog_routes

    monkeypatch.setenv("ORCHESTRATION_BACKLOG_DIR", str(tmp_path))
    monkeypatch.delenv("ORCHESTRATION_BACKLOG_REF", raising=False)
    _write(tmp_path, "f-a.md", id="f-a", title="A", status="review",
           priority="high", dependsOn="[]", planGate="true", created="2026-06-01")

    app = FastAPI()
    register_orchestration_backlog_routes(app)
    client = TestClient(app)

    r = client.get("/api/orchestration/backlog")
    assert r.status_code == 200
    data = r.json()
    assert data["schema"] == "orchestration-backlog-v1"
    assert data["source"]["count"] == 1
    assert data["items"][0]["id"] == "f-a"
    assert data["items"][0]["title"] == "A"
    assert data["items"][0]["status"] == "review"


def test_detail_route_returns_item_body_gate_and_root(tmp_path, monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from fastapi import FastAPI

    from hermes_cli.orchestration_backlog_view import register_orchestration_backlog_routes

    monkeypatch.setenv("ORCHESTRATION_BACKLOG_DIR", str(tmp_path))
    monkeypatch.delenv("ORCHESTRATION_BACKLOG_REF", raising=False)
    _write(tmp_path, "f-a.md", id="f-a", title="A", status="review",
           priority="high", dependsOn="[f-b, f-c]", planGate="true",
           gate="venv/bin/python -m pytest -q", root="/tmp/project",
           created="2026-06-01")

    app = FastAPI()
    register_orchestration_backlog_routes(app)
    client = TestClient(app)

    r = client.get("/api/orchestration/backlog/f-a")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "f-a"
    assert data["title"] == "A"
    assert data["dependsOn"] == ["f-b", "f-c"]
    assert data["planGate"] is True
    assert data["gate"] == "venv/bin/python -m pytest -q"
    assert data["root"] == "/tmp/project"
    assert data["body"].strip()


def test_detail_route_rejects_traversal_ids(tmp_path, monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from fastapi import FastAPI

    from hermes_cli.orchestration_backlog_view import register_orchestration_backlog_routes

    monkeypatch.setenv("ORCHESTRATION_BACKLOG_DIR", str(tmp_path))
    monkeypatch.delenv("ORCHESTRATION_BACKLOG_REF", raising=False)

    app = FastAPI()
    register_orchestration_backlog_routes(app)
    client = TestClient(app)

    encoded = client.get("/api/orchestration/backlog/..%2f..%2fetc%2fpasswd")
    assert encoded.status_code == 200
    assert "error" in encoded.json()
    assert "root:" not in encoded.text

    status, raw = asyncio.run(_asgi_get(app, "/api/orchestration/backlog/../../etc/passwd"))
    assert status == 200
    assert "error" in raw
    assert "root:" not in json.dumps(raw)


def test_detail_route_empty_and_unknown_id_return_error(tmp_path, monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from fastapi import FastAPI

    from hermes_cli.orchestration_backlog_view import register_orchestration_backlog_routes

    monkeypatch.setenv("ORCHESTRATION_BACKLOG_DIR", str(tmp_path))
    monkeypatch.delenv("ORCHESTRATION_BACKLOG_REF", raising=False)

    app = FastAPI()
    register_orchestration_backlog_routes(app)
    client = TestClient(app)

    empty = client.get("/api/orchestration/backlog/")
    assert empty.status_code == 200
    assert "error" in empty.json()

    unknown = client.get("/api/orchestration/backlog/not-real")
    assert unknown.status_code == 200
    assert "error" in unknown.json()
