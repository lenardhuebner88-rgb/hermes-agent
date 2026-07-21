"""Regression tests for Jarvis B3 — /api/pa/search and /api/pa/node.

Covers the normalized API contract from the Jarvis B3 kanban card:
- Response schema for both endpoints (AC-1).
- Search hit IDs equal pa_graph node IDs for the same record (AC-2),
  including unicode/relative vault paths.
- Vault (qmd FTS5), kanban (LIKE) and memory (SessionDB FTS5) sources,
  deterministic dedupe and a global limit (AC-3) — ``limit=1`` must yield
  at most one item even though hits come from multiple sources.
- Per-source error isolation: one failing source still yields HTTP 200 with
  partial results and structured ``errors[]`` (AC-4).
- Node preview body/metadata/connections, 404 for unknown IDs, 400 for
  invalid/traversal IDs, no path leaks (AC-5).
- Short-TTL cache for search responses (AC-6).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import pa_search


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state():
    pa_search._reset_caches()
    yield
    pa_search._reset_caches()


def _make_qmd(tmp_path: Path) -> Path:
    """Create a minimal qmd-style index.sqlite with FTS5."""
    db = tmp_path / "qmd" / "index.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, filepath TEXT, hash TEXT, "
        "created_at TEXT, modified_at TEXT, active INTEGER DEFAULT 1)"
    )
    conn.execute(
        "CREATE TABLE content (id INTEGER PRIMARY KEY, hash TEXT, doc TEXT, title TEXT)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE documents_fts USING fts5(filepath, title, body, content='')"
    )
    conn.execute(
        "INSERT INTO documents (filepath, hash, created_at, modified_at) VALUES (?, ?, ?, ?)",
        ("Zettelkasten/Uni-Konzept.md", "hash-uni-1", "2026-07-21T10:00:00+00:00", "2026-07-21T10:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO content (hash, doc, title) VALUES (?, ?, ?)",
        ("hash-uni-1", "Ein Konzept über Hermes Agent Kanban Orchestrierung und Worktrees.", "Uni-Konzept"),
    )
    conn.execute(
        "INSERT INTO documents_fts (rowid, filepath, title, body) VALUES (?, ?, ?, ?)",
        (1, "Zettelkasten/Uni-Konzept.md", "Uni-Konzept", "Ein Konzept über Hermes Agent Kanban Orchestrierung und Worktrees."),
    )
    conn.commit()
    conn.close()
    return db


def _make_kanban(tmp_path: Path) -> Path:
    """Create a minimal kanban DB with a tasks table (no FTS5 — LIKE fallback)."""
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, "
        "status TEXT NOT NULL, board TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, body, status, board, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("t_abcd1234", "Jarvis B3 Kanban Orchestrierung", "Body über Orchestrierung und Worktrees.", "done", "default", 1784640000),
    )
    conn.commit()
    conn.close()
    return db


class _FakeSessionDB:
    """Minimal SessionDB stand-in with messages_fts5."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fts5_search(self, query, trigram=False, limit=20, offset=0, role_filter=None):
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        table = "messages_fts_trigram" if trigram else "messages_fts"
        try:
            rows = con.execute(
                f"SELECT m.id, m.role, m.content, m.tool_name, m.ts, "
                f"snippet({table}, 0, '**', '**', '…', 32) AS snippet "
                f"FROM {table} f JOIN messages m ON m.id = f.rowid "
                f"WHERE {table} MATCH ? LIMIT ?",
                (query, limit),
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]


class _FakeSessionDBCtx:
    """pa_graph-compatible DB context: (path, profile) + __enter__→sqlite3 conn."""

    def __init__(self, db_path: Path, profile: str = "coder"):
        self.db_path = db_path
        self.profile = profile

    def __enter__(self):
        self._con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        self._con.row_factory = sqlite3.Row
        return self._con

    def __exit__(self, *exc):
        self._con.close()
        return False


def _make_state_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, profile TEXT, "
        "role TEXT, content TEXT, tool_name TEXT, tool_calls TEXT, tool_call_id TEXT, ts INTEGER)"
    )
    conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")
    conn.execute("CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tokenize='trigram')")
    conn.execute(
        "INSERT INTO messages (id, session_id, profile, role, content, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (7, "20260721_100000_abcdef", "coder", "user", "Wie orchestriert man Kanban Worktrees in Hermes?", 1784640000),
    )
    conn.execute("INSERT INTO messages_fts (rowid, content) VALUES (?, ?)", (7, "Wie orchestriert man Kanban Worktrees in Hermes?"))
    conn.execute("INSERT INTO messages_fts_trigram (rowid, content) VALUES (?, ?)", (7, "Wie orchestriert man Kanban Worktrees in Hermes?"))
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Wire all three search sources plus pa_graph into temp paths."""
    qmd_db = _make_qmd(tmp_path)
    kanban_db = _make_kanban(tmp_path)
    state_db = _make_state_db(tmp_path)

    monkeypatch.setattr(pa_search, "_qmd_db_path", lambda: qmd_db)
    monkeypatch.setattr(pa_search, "_kanban_location", lambda: (kanban_db, "default"))
    monkeypatch.setattr(pa_search, "_SessionDB", lambda profile=None: _FakeSessionDB(state_db))
    return {"qmd": qmd_db, "kanban": kanban_db, "state": state_db, "tmp": tmp_path}


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    pa_search.register_pa_search_routes(app)
    return TestClient(app)


# ---------------------------------------------------------------------------
# AC-1: response schema
# ---------------------------------------------------------------------------


def test_search_response_schema(wired):
    resp = _client().get("/api/pa/search", params={"q": "Kanban"})
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {"query", "items", "took_ms", "errors"}
    assert data["query"] == "Kanban"
    assert isinstance(data["took_ms"], int) and data["took_ms"] >= 0
    assert isinstance(data["errors"], list)
    assert data["items"], "expected at least one hit across the three sources"
    item = data["items"][0]
    assert set(item.keys()) == {
        "id", "ref", "title", "cluster", "snippet", "score", "kind", "source",
        "in_graph", "meta",
    }
    assert item["source"] in {"vault", "kanban", "memory"}


def test_node_response_schema(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    note = vault / "Zettelkasten" / "Uni-Konzept.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Uni-Konzept\n\nBody über Orchestrierung.\n", encoding="utf-8")
    monkeypatch.setattr(pa_search, "_vault_root", lambda: vault)

    resp = _client().get("/api/pa/node", params={"id": "vault:zettelkasten/uni-konzept.md"})
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {"id", "title", "cluster", "body", "metadata", "connections", "source"}
    assert data["id"] == "vault:zettelkasten/uni-konzept.md"
    assert data["title"] == "Uni-Konzept"
    assert data["cluster"] == "vault"
    assert "Orchestrierung" in data["body"]
    assert data["source"] == "vault"
    assert isinstance(data["connections"], list)
    conn = data["connections"][0]
    assert set(conn.keys()) == {"id", "title", "cluster", "kind", "direction", "label"}


# ---------------------------------------------------------------------------
# AC-2: ID parity with pa_graph
# ---------------------------------------------------------------------------


def test_search_hit_id_matches_graph_node_id(wired, monkeypatch):
    from hermes_cli import pa_graph

    monkeypatch.setattr(pa_graph, "_qmd_db_path", lambda: wired["qmd"])
    graph = pa_graph._build_uncached(include_receipts=False, include_memories=False)
    graph_ids = {n["id"] for n in graph["nodes"]}
    assert "vault:zettelkasten/uni-konzept.md" in graph_ids

    data = _client().get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    vault_hits = [i for i in data["items"] if i["source"] == "vault"]
    assert vault_hits, "expected a vault hit"
    for hit in vault_hits:
        assert hit["id"] in graph_ids
        assert hit["in_graph"] is True


def test_kanban_and_memory_ids_match_graph(wired, monkeypatch):
    from hermes_cli import pa_graph

    monkeypatch.setattr(pa_graph, "_kanban_location", lambda: (wired["kanban"], "default"))
    monkeypatch.setattr(
        pa_graph, "_session_db_contexts", lambda limit=None: [_FakeSessionDBCtx(wired["state"])]
    )
    graph = pa_graph._build_uncached(include_vault=False, include_receipts=False)
    graph_ids = {n["id"] for n in graph["nodes"]}
    data = _client().get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    for hit in data["items"]:
        if hit["source"] == "kanban":
            assert hit["id"] == f"task:t_abcd1234"
            assert hit["id"] in graph_ids
        if hit["source"] == "memory":
            assert hit["id"].startswith("session:msg:7:")
            assert hit["id"] in graph_ids


# ---------------------------------------------------------------------------
# AC-3: sources, dedupe, global limit
# ---------------------------------------------------------------------------


def test_all_three_sources_represented(wired):
    data = _client().get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    sources = {i["source"] for i in data["items"]}
    assert {"vault", "kanban", "memory"} <= sources


def test_limit_one_returns_exactly_one_item(wired):
    data = _client().get("/api/pa/search", params={"q": "Orchestrierung", "limit": 1}).json()
    assert len(data["items"]) <= 1


def test_global_limit_enforced(wired):
    data = _client().get("/api/pa/search", params={"q": "Orchestrierung", "limit": 2}).json()
    assert len(data["items"]) <= 2


def test_dedupe_deterministic(wired):
    # Same underlying record reachable via two sources must collapse to one item.
    first = _client().get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    second = _client().get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    assert [i["id"] for i in first["items"]] == [i["id"] for i in second["items"]]
    assert len({i["id"] for i in first["items"]}) == len(first["items"])


# ---------------------------------------------------------------------------
# AC-4: per-source error isolation
# ---------------------------------------------------------------------------


def test_missing_qmd_db_still_200_with_errors(wired, tmp_path, monkeypatch):
    monkeypatch.setattr(pa_search, "_qmd_db_path", lambda: tmp_path / "nope" / "index.sqlite")
    resp = _client().get("/api/pa/search", params={"q": "Orchestrierung"})
    assert resp.status_code == 200
    data = resp.json()
    assert any(e["source"] == "vault" for e in data["errors"])
    # kanban + memory hits still present
    sources = {i["source"] for i in data["items"]}
    assert "kanban" in sources or "memory" in sources


def test_kanban_error_isolated(wired, monkeypatch):
    def _boom():
        raise RuntimeError("kanban kaputt")

    monkeypatch.setattr(pa_search, "_search_kanban", _boom)
    resp = _client().get("/api/pa/search", params={"q": "Orchestrierung"})
    assert resp.status_code == 200
    data = resp.json()
    assert any(e["source"] == "kanban" for e in data["errors"])
    assert any(i["source"] == "vault" for i in data["items"])


def test_empty_query_returns_empty_items(wired):
    resp = _client().get("/api/pa/search", params={"q": "   "})
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []


# ---------------------------------------------------------------------------
# AC-5: node preview, 404/400, path safety
# ---------------------------------------------------------------------------


def test_node_unknown_id_404(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(pa_search, "_vault_root", lambda: vault)
    resp = _client().get("/api/pa/node", params={"id": "vault:does/not-exist.md"})
    assert resp.status_code == 404


def test_node_traversal_id_400(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(pa_search, "_vault_root", lambda: vault)
    resp = _client().get("/api/pa/node", params={"id": "vault:../outside.md"})
    assert resp.status_code == 400


def test_node_absolute_path_400(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(pa_search, "_vault_root", lambda: vault)
    resp = _client().get("/api/pa/node", params={"id": "vault:/etc/passwd"})
    assert resp.status_code == 400


def test_node_garbage_id_400():
    resp = _client().get("/api/pa/node", params={"id": "not-a-node"})
    assert resp.status_code == 400


def test_node_no_absolute_path_leak(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    note = vault / "Zettelkasten" / "Uni-Konzept.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Uni-Konzept\n\nBody.\n", encoding="utf-8")
    monkeypatch.setattr(pa_search, "_vault_root", lambda: vault)
    resp = _client().get("/api/pa/node", params={"id": "vault:zettelkasten/uni-konzept.md"})
    assert resp.status_code == 200
    assert str(vault) not in resp.text


def test_node_kanban_task(tmp_path, monkeypatch):
    kanban_db = _make_kanban(tmp_path)
    monkeypatch.setattr(pa_search, "_kanban_location", lambda: (kanban_db, "default"))
    resp = _client().get("/api/pa/node", params={"id": "task:t_abcd1234"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Jarvis B3 Kanban Orchestrierung"
    assert data["cluster"] == "kanban"
    assert data["source"] == "kanban"
    assert "Orchestrierung" in data["body"]


def test_node_session_message(tmp_path, monkeypatch):
    state_db = _make_state_db(tmp_path)
    monkeypatch.setattr(pa_search, "_SessionDB", lambda profile=None: _FakeSessionDB(state_db))
    node_id = "session:msg:7:20260721_100000_abcdef:coder"
    resp = _client().get("/api/pa/node", params={"id": node_id})
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "memory"
    assert data["cluster"] == "sessions"
    assert "Kanban Worktrees" in data["body"]


def test_node_unicode_vault_path(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    note = vault / "Zettelkasten" / "Übungs-Notiz Größe.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Übungs-Notiz Größe\n\nUnicode-Inhalt.\n", encoding="utf-8")
    monkeypatch.setattr(pa_search, "_vault_root", lambda: vault)
    node_id = "vault:zettelkasten/übungs-notiz größe.md"
    resp = _client().get("/api/pa/node", params={"id": node_id})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Übungs-Notiz Größe"
    assert "Unicode-Inhalt" in data["body"]


def test_node_connections_include_edges(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    note_a = vault / "Zettelkasten" / "Alpha.md"
    note_b = vault / "Zettelkasten" / "Beta.md"
    note_a.parent.mkdir(parents=True)
    note_a.write_text("# Alpha\n\nLink nach [[Beta]].\n", encoding="utf-8")
    note_b.write_text("# Beta\n\nBody B.\n", encoding="utf-8")
    monkeypatch.setattr(pa_search, "_vault_root", lambda: vault)
    resp = _client().get("/api/pa/node", params={"id": "vault:zettelkasten/alpha.md"})
    assert resp.status_code == 200
    data = resp.json()
    conn_ids = {c["id"] for c in data["connections"]}
    assert "vault:zettelkasten/beta.md" in conn_ids
    beta_conn = next(c for c in data["connections"] if c["id"] == "vault:zettelkasten/beta.md")
    assert beta_conn["direction"] == "out"
    assert beta_conn["title"] == "Beta"


# ---------------------------------------------------------------------------
# AC-6: caching
# ---------------------------------------------------------------------------


def test_search_cache_reuses_response(wired, monkeypatch):
    calls = {"n": 0}
    real = pa_search._search_vault

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(pa_search, "_search_vault", _counting)
    client = _client()
    client.get("/api/pa/search", params={"q": "Orchestrierung"})
    client.get("/api/pa/search", params={"q": "Orchestrierung"})
    assert calls["n"] == 1
    # Different query → fresh lookup
    client.get("/api/pa/search", params={"q": "Worktrees"})
    assert calls["n"] == 2
