"""Tests for the read-only Family-Organizer backlog endpoint
(``GET /api/family-organizer/backlog``).

The endpoint parses the family-organizer repo's ``backlog/items/*.md`` frontmatter
contract from disk. These tests assert the parse/counts/stale logic and the route
contract against tmp fixtures (no real repo, no live server).
"""

import asyncio
import datetime as dt
import json

import pytest

from hermes_cli.family_organizer_view import (
    _extract_excerpt,
    _parse_frontmatter,
    _read_items_sync,
    _read_sources_from_git,
    _updated_epoch,
)


def _write(dir_, name, body=None, **fm):
    lines = ["---"]
    for key, value in fm.items():
        lines.append(f"{key}: {value}")
    if body is not None:
        lines += ["---", "", body]
    else:
        lines += ["---", "", "# Kontext", "", "body mit --- als Trennlinie"]
    (dir_ / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _source_text(**fm):
    lines = ["---"]
    for key, value in fm.items():
        lines.append(f"{key}: {value}")
    lines += ["---", "", "# Kontext", "", "detail body"]
    return "\n".join(lines) + "\n"


def _detail_client(monkeypatch, sources):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from fastapi import FastAPI

    from hermes_cli import family_organizer_view

    monkeypatch.setattr(family_organizer_view, "_read_sources_from_git", lambda base: sources)

    def fail_fs(_base):
        raise AssertionError("detail endpoint should use git source fixture")

    monkeypatch.setattr(family_organizer_view, "_read_sources_from_fs", fail_fs)

    app = FastAPI()
    family_organizer_view.register_backlog_routes(app)
    return TestClient(app)


async def _asgi_get(app, path):
    """Drive the ASGI app with a literal path, bypassing the client's URL
    dot-segment normalization so an un-encoded ``../..`` actually reaches the
    ``{item_id:path}`` route and exercises the real hardening."""
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


def test_git_source_none_for_non_repo(tmp_path):
    # A plain tmp dir is not a git repo → git read returns None → caller falls back to FS.
    assert _read_sources_from_git(tmp_path) is None


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

    assert out["schema"] == "fo-backlog-v2"
    assert out["source"]["count"] == 3
    assert out["source"]["ref"].startswith("fs:")  # tmp dir is not a git repo → working-tree fallback
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


def test_read_items_contract_health_preserves_drift_and_quality_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    _write(tmp_path, "0001-drift.md", id="0001", title="A drift item", status="readyish",
           owner="nobody", risk="urgent", area="lists", updated="2026-05-20",
           body="## Kontext\n\nOhne klare Kriterien.")
    _write(tmp_path, "0002-ready.md", id="0002", title="A ready item", status="next",
           owner="unassigned", risk="high", area="db", updated="2026-06-01",
           body="## Akzeptanzkriterien\n\n- Gate ist gruen.\n\n## Next Action\n\nImplementieren.")
    _write(tmp_path, "0003-stale.md", id="0003", title="A stale item", status="in_progress",
           owner="codex", risk="medium", area="process", updated="2026-05-20",
           body="## Kontext\n\nBegonnen, aber ohne neuen Beleg.")

    now = int(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc).timestamp())
    out = _read_items_sync(now)

    by_id = {it["id"]: it for it in out["items"]}
    assert by_id["0001"]["status"] == "readyish"
    assert by_id["0001"]["risk"] == "urgent"
    assert by_id["0001"]["owner"] == "nobody"

    health = out["contract_health"]
    assert health["source_count"] == 3
    assert health["counted_sum"] == 2
    assert health["unknown_statuses"] == [{"status": "readyish", "count": 1, "ids": ["0001"]}]
    assert health["invalid_risk_count"] == 1
    assert health["invalid_owner_count"] == 1
    assert health["unowned_count"] == 1
    assert health["stale_count"] == 1
    assert health["missing_acceptance_count"] == 2
    assert health["missing_next_action_count"] == 2


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
    assert data["schema"] == "fo-backlog-v2"
    assert data["source"]["count"] == 1
    assert data["items"][0]["id"] == "0001"
    assert data["items"][0]["title"] == "A"


def test_detail_route_returns_body_from_git_source(monkeypatch):
    client = _detail_client(monkeypatch, [
        ("0001-a.md", _source_text(id="0001", title="A", status="done",
                                    owner="claude", risk="low", area="kitchen",
                                    updated="2026-06-01", result="x")),
    ])

    r = client.get("/api/family-organizer/backlog/0001")

    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "0001"
    assert data["title"] == "A"
    assert data["status"] == "done"
    assert data["owner"] == "claude"
    assert data["risk"] == "low"
    assert data["area"] == "kitchen"
    assert data["updated"] == "2026-06-01"
    assert data["result"] == "x"
    assert data["body"].strip()


def test_detail_route_resolves_four_digit_prefix_filename(monkeypatch):
    client = _detail_client(monkeypatch, [
        ("0007-something.md", _source_text(id="0007", title="Resolved by prefix",
                                           status="next", owner="hermes",
                                           risk="medium", area="lists",
                                           updated="2026-06-01")),
    ])

    r = client.get("/api/family-organizer/backlog/0007")

    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "0007"
    assert data["title"] == "Resolved by prefix"
    assert "error" not in data


def test_detail_route_returns_product_manager_sections(monkeypatch):
    text = """---
id: 0042
title: Structured drawer item
status: next
owner: claude
risk: medium
area: lists
updated: 2026-06-01
---

## Decision / Why now

Diese Arbeit reduziert Queue-Reibung.

## Akzeptanzkriterien

- Tabelle zeigt den naechsten Schritt.
- Drawer zeigt Belege.

## Current Evidence / Last Proof

Commit abc123, gate gruen.

## Blockers

- Keine.

## Next Action

Implementierung in Hermes vorbereiten.

Siehe [Runbook](https://example.invalid/runbook).
"""
    client = _detail_client(monkeypatch, [("backlog/items/0042-structured.md", text)])

    r = client.get("/api/family-organizer/backlog/0042")

    assert r.status_code == 200
    data = r.json()
    assert data["source_path"] == "backlog/items/0042-structured.md"
    assert data["source_ref"] == "git:origin/main"
    assert data["decision"] == ["Diese Arbeit reduziert Queue-Reibung."]
    assert data["acceptance_criteria"] == ["Tabelle zeigt den naechsten Schritt.", "Drawer zeigt Belege."]
    assert data["proofs"] == ["Commit abc123, gate gruen."]
    assert data["blockers"] == ["Keine."]
    assert data["next_action"] == "Implementierung in Hermes vorbereiten."
    assert data["links"] == [{"label": "Runbook", "href": "https://example.invalid/runbook"}]


def test_detail_route_rejects_traversal_ids(monkeypatch):
    client = _detail_client(monkeypatch, [])

    # Encoded ``%2f`` is preserved by the client and reaches the {item_id:path}
    # route, so the id validation (rejecting ``..``/``/``) is what answers here.
    encoded = client.get("/api/family-organizer/backlog/..%2f..%2fetc%2fpasswd")
    assert encoded.status_code == 200
    assert encoded.json()["error"]
    assert "etc" not in encoded.text or "passwd" not in encoded.text

    # A raw, un-encoded ``../..`` would be normalized away by the HTTP client, so
    # drive the ASGI app directly with the literal path to hit the real route.
    status, raw = asyncio.run(
        _asgi_get(client.app, "/api/family-organizer/backlog/../../etc/passwd")
    )
    assert status == 200
    assert raw["error"]
    assert "passwd" not in json.dumps(raw)


def test_extract_excerpt_strips_markdown_markers():
    text = "---\nid: 0001\n---\n\n## Heading\n\nErster Satz hier.\n"
    assert _extract_excerpt(text) == "Heading"

    text_plain = "---\nid: 0001\n---\n\nPlain line.\n"
    assert _extract_excerpt(text_plain) == "Plain line."

    text_long = "---\nid: 0001\n---\n\n" + "x" * 200 + "\n"
    assert len(_extract_excerpt(text_long)) <= 140

    text_empty = "---\nid: 0001\n---\n\n"
    assert _extract_excerpt(text_empty) == ""


def test_read_items_excerpt_present(tmp_path, monkeypatch):
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    _write(tmp_path, "0001-a.md", body="## Ziel\n\nBeschreibung des Tasks.",
           id="0001", title="A", status="now", owner="claude",
           risk="low", area="kitchen", updated="2026-06-01")
    now = int(dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc).timestamp())
    out = _read_items_sync(now)
    item = out["items"][0]
    assert "excerpt" in item
    assert item["excerpt"] == "Ziel"


def test_route_excerpt_in_list_payload(tmp_path, monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from fastapi import FastAPI
    from hermes_cli.family_organizer_view import register_backlog_routes

    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    _write(tmp_path, "0001-a.md", body="Excerpt-Satz.", id="0001", title="A",
           status="now", owner="claude", risk="low", area="kitchen",
           updated="2026-06-01")
    app = FastAPI()
    register_backlog_routes(app)
    r = TestClient(app).get("/api/family-organizer/backlog")
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert "excerpt" in item
    assert item["excerpt"] == "Excerpt-Satz."


def test_detail_route_empty_and_unknown_id_return_error(monkeypatch):
    client = _detail_client(monkeypatch, [])

    blank = client.get("/api/family-organizer/backlog/%20%20")
    unknown = client.get("/api/family-organizer/backlog/9999")

    assert blank.status_code == 200
    assert unknown.status_code == 200
    assert blank.json()["error"]
    assert unknown.json()["error"]


# --- v2 per-item facts (age_days / freshness / quality_issues / readiness) -----------

_READY_BODY = "## Akzeptanzkriterien\n\n- Gate ist gruen.\n\n## Next Action\n\nUmsetzen."


def test_read_items_age_and_freshness_buckets(tmp_path, monkeypatch):
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    _write(tmp_path, "0001-fresh.md", id="0001", title="Fresh active task", status="next",
           owner="claude", risk="low", area="kitchen", updated="2026-06-10")
    _write(tmp_path, "0002-aging.md", id="0002", title="Aging active task", status="next",
           owner="claude", risk="low", area="kitchen", updated="2026-06-05")
    _write(tmp_path, "0003-stale.md", id="0003", title="Stale running task", status="in_progress",
           owner="claude", risk="low", area="kitchen", updated="2026-05-20")
    # no `updated` field at all → unparseable → no_proof / age_days None
    _write(tmp_path, "0004-noproof.md", id="0004", title="No proof task", status="next",
           owner="claude", risk="low", area="kitchen")

    now = int(dt.datetime(2026, 6, 10, tzinfo=dt.timezone.utc).timestamp())
    by_id = {it["id"]: it for it in _read_items_sync(now)["items"]}

    assert by_id["0001"]["age_days"] == 0
    assert by_id["0001"]["freshness"] == "fresh"
    assert by_id["0002"]["age_days"] == 5
    assert by_id["0002"]["freshness"] == "aging"  # >3 days, not yet stale
    assert by_id["0003"]["freshness"] == "stale"  # in_progress + ancient
    assert by_id["0003"]["stale"] is True
    assert by_id["0004"]["age_days"] is None
    assert by_id["0004"]["freshness"] == "no_proof"


def test_read_items_quality_issues_taxonomy(tmp_path, monkeypatch):
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    big_body = "## Kontext\n\n" + "\n".join(f"- punkt {i}" for i in range(12))
    _write(tmp_path, "0001-big.md", id="0001", title="A well-scoped larger task title",
           status="next", owner="claude", risk="low", area="kitchen",
           updated="2026-06-10", body=big_body)
    _write(tmp_path, "0002-ready.md", id="0002", title="A nicely scoped ready task",
           status="next", owner="claude", risk="low", area="kitchen",
           updated="2026-06-10", body=_READY_BODY)
    _write(tmp_path, "0003-weak.md", id="0003", title="fix", status="next",
           owner="unassigned", risk="low", area="kitchen",
           updated="2026-06-10", body=_READY_BODY)

    now = int(dt.datetime(2026, 6, 10, tzinfo=dt.timezone.utc).timestamp())
    by_id = {it["id"]: it for it in _read_items_sync(now)["items"]}

    def codes(item_id):
        return {q["code"] for q in by_id[item_id]["quality_issues"]}

    assert "large_scope" in codes("0001")     # 12 bullets ≥ scope threshold
    assert codes("0002") == set()             # acceptance + next action + good title/owner
    assert "large_scope" not in codes("0002")
    assert "weak_title" in codes("0003")      # title "fix"
    assert "unclear_owner" in codes("0003")   # owner unassigned

    severities = {q["code"]: q["severity"] for q in by_id["0003"]["quality_issues"]}
    assert severities["unclear_owner"] == "risk"
    assert severities["weak_title"] == "warn"


def test_read_items_readiness_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    _write(tmp_path, "0001-drift.md", id="0001", title="Drift status item", status="bogus",
           owner="claude", risk="low", area="kitchen", updated="2026-06-10", body=_READY_BODY)
    _write(tmp_path, "0002-blocked.md", id="0002", title="Blocked but documented", status="blocked",
           owner="claude", risk="low", area="kitchen", updated="2026-06-10", body=_READY_BODY)
    _write(tmp_path, "0003-groom.md", id="0003", title="Needs grooming item", status="next",
           owner="claude", risk="low", area="kitchen", updated="2026-06-10",
           body="## Kontext\n\nNur Kontext, keine Kriterien.")
    _write(tmp_path, "0004-ready.md", id="0004", title="A ready scoped task", status="next",
           owner="claude", risk="low", area="kitchen", updated="2026-06-10", body=_READY_BODY)

    now = int(dt.datetime(2026, 6, 10, tzinfo=dt.timezone.utc).timestamp())
    by_id = {it["id"]: it for it in _read_items_sync(now)["items"]}

    assert by_id["0001"]["readiness"] == "drift"            # unknown status
    assert by_id["0002"]["readiness"] == "blocked"          # blocked dominates
    assert by_id["0003"]["readiness"] == "needs_grooming"   # risk-severity issues
    assert by_id["0004"]["readiness"] == "ready"            # complete + clean


def test_detail_route_includes_v2_facts(monkeypatch):
    client = _detail_client(monkeypatch, [
        ("0001-a.md", _source_text(id="0001", title="A detail facts item", status="next",
                                   owner="unassigned", risk="high", area="db",
                                   updated="2026-06-01")),
    ])

    data = client.get("/api/family-organizer/backlog/0001").json()

    assert "age_days" in data
    assert data["freshness"] in {"fresh", "aging", "stale", "no_proof"}
    assert data["readiness"] in {"ready", "needs_grooming", "blocked", "drift"}
    assert isinstance(data["quality_issues"], list)
    # owner unassigned → unclear_owner (now-independent, so deterministic to assert)
    assert any(q["code"] == "unclear_owner" for q in data["quality_issues"])


def test_read_items_deterministic_for_fixed_now(tmp_path, monkeypatch):
    monkeypatch.setenv("FAMILY_ORGANIZER_BACKLOG_DIR", str(tmp_path))
    _write(tmp_path, "0001-a.md", id="0001", title="First task title here", status="next",
           owner="claude", risk="low", area="kitchen", updated="2026-06-05", body=_READY_BODY)
    _write(tmp_path, "0002-b.md", id="0002", title="Second running task", status="in_progress",
           owner="hermes", risk="high", area="db", updated="2026-05-20")

    now = int(dt.datetime(2026, 6, 10, tzinfo=dt.timezone.utc).timestamp())
    assert _read_items_sync(now) == _read_items_sync(now)
