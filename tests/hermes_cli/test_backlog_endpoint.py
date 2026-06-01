"""Tests for the read-only Family-Organizer backlog endpoint
(``GET /api/family-organizer/backlog``).

The endpoint parses the family-organizer repo's ``backlog/items/*.md`` frontmatter
contract from disk. These tests assert the parse/counts/stale logic and the route
contract against tmp fixtures (no real repo, no live server).
"""

import datetime as dt

import pytest

from hermes_cli.family_organizer_view import (
    _parse_frontmatter,
    _read_items_sync,
    _updated_epoch,
)


def _write(dir_, name, **fm):
    lines = ["---"]
    for key, value in fm.items():
        lines.append(f"{key}: {value}")
    lines += ["---", "", "# Kontext", "", "body mit --- als Trennlinie"]
    (dir_ / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_frontmatter_keeps_colon_values_and_ignores_body_rules():
    text = "---\nid: 0001\ntitle: Foo: Bar\nresult: a; b: c\n---\n# Body\n\n---\n"
    fm = _parse_frontmatter(text)
    assert fm["title"] == "Foo: Bar"
    assert fm["result"] == "a; b: c"


def test_parse_frontmatter_missing_or_unterminated():
    assert _parse_frontmatter("# kein Frontmatter") == {}
    assert _parse_frontmatter("---\nid: 1\nkein Ende") == {}


def test_updated_epoch():
    assert _updated_epoch("2026-06-01") is not None
    assert _updated_epoch("kein-datum") is None
    assert _updated_epoch(None) is None


def test_read_items_counts_stale_and_id_from_filename(tmp_path, monkeypatch):
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    _write(tmp_path, "0001-a.md", id="0001", title="A", status="done",
           owner="claude", risk="low", area="kitchen", updated="2026-06-01",
           result="auf main")
    _write(tmp_path, "0002-b.md", id="0002", title="B", status="later",
           owner="unassigned", risk="medium", area="lists", updated="2026-05-30")
    _write(tmp_path, "0003-c.md", id="0003", title="C", status="in_progress",
           owner="hermes", risk="high", area="process", updated="2000-01-01")

    now = int(dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc).timestamp())
    out = _read_items_sync(now)

    assert out["schema"] == "fo-backlog-v1"
    assert out["source"]["count"] == 3
    assert out["counts"]["done"] == 1
    assert out["counts"]["later"] == 1
    assert out["counts"]["in_progress"] == 1

    by_id = {it["id"]: it for it in out["items"]}
    # id comes from the filename prefix, not the YAML (which would coerce 0001→1)
    assert set(by_id) == {"0001", "0002", "0003"}
    assert by_id["0003"]["stale"] is True   # in_progress + ancient updated
    assert by_id["0001"]["stale"] is False  # done is never stale
    assert by_id["0001"]["area"] == "kitchen"
    assert by_id["0001"]["result"] == "auf main"


def test_read_items_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path / "nope"))
    out = _read_items_sync(0)
    assert out["items"] == []
    assert out["counts"]["done"] == 0
    assert out["error"]


def test_route_returns_json(tmp_path, monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from fastapi import FastAPI

    from hermes_cli.family_organizer_view import register_backlog_routes

    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    _write(tmp_path, "0001-a.md", id="0001", title="A", status="done",
           owner="claude", risk="low", area="kitchen", updated="2026-06-01",
           result="x")

    app = FastAPI()
    register_backlog_routes(app)
    client = TestClient(app)

    r = client.get("/api/family-organizer/backlog")
    assert r.status_code == 200
    data = r.json()
    assert data["schema"] == "fo-backlog-v1"
    assert data["source"]["count"] == 1
    assert data["items"][0]["id"] == "0001"
    assert data["items"][0]["title"] == "A"
