"""Regression tests for Jarvis B3 — ``/api/pa/search`` and ``/api/pa/node``.

Covers the normalized API contract from the B3 kanban card:

* AC-1 response schema for both endpoints.
* AC-2 ``search.items[].id`` is byte-identical to the matching
  ``pa_graph`` node id, including unicode and relative vault paths.
* AC-3 vault/kanban/memory hits, deterministic dedupe and a global limit
  (``limit=1`` must not leak a second item through source interleaving).
* AC-4 per-source error *and* timeout isolation → HTTP 200 with partial
  results plus a structured ``errors[]``.
* AC-5 node preview body/metadata/connections, unknown → 404,
  traversal/absolute → 400, no absolute path or secret leaks.
* AC-6 short-TTL response cache.

The fixtures mirror the **live** schemas (verified 2026-07-21): qmd
``documents(collection, path, title, hash, active, modified_at)`` +
``content(hash, doc)`` + ``documents_fts(filepath, title, body)`` joined on
``documents.id == documents_fts.rowid``, and a kanban ``tasks`` table without
any FTS index.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import pa_graph, pa_search


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_caches():
    pa_search.invalidate_search_cache()
    pa_graph.invalidate_graph_cache()
    yield
    pa_search.invalidate_search_cache()
    pa_graph.invalidate_graph_cache()


# Vault notes: (relative path on disk, title, body). Note the deliberate
# mixed case / spaces — `_normalize_vault_path` folds those away, so the id
# and the on-disk path differ.
_NOTES: tuple[tuple[str, str, str], ...] = (
    (
        "00-Canon/Uni-Konzept.md",
        "Uni-Konzept",
        "# Uni-Konzept\n\nEin Konzept über Hermes Orchestrierung.\nSiehe [[Beta-Notiz]].\n",
    ),
    (
        "00-Canon/Beta-Notiz.md",
        "Beta-Notiz",
        "# Beta-Notiz\n\nOrchestrierung im Detail.\n",
    ),
    (
        "00-Canon/Übungs Notiz Größe.md",
        "Übungs Notiz Größe",
        "# Übungs Notiz Größe\n\nUnicode-Inhalt zur Orchestrierung.\n",
    ),
)


# Live quirk (regression guard): qmd rewrites paths when indexing — leading
# underscores are stripped from directories and "_" becomes "-" in filenames.
# The id therefore has NO counterpart on disk, so a reverse lookup that only
# joins/walks the filesystem 404s on every note under `_agents/`, `_coordination/`.
_UNDERSCORE_NOTE_DISK = "_agents/_coordination/2026-07-19_2240_kimi_check-in.md"
_UNDERSCORE_NOTE_INDEXED = "agents/coordination/2026-07-19-2240-kimi-check-in.md"
_UNDERSCORE_NOTE_TITLE = "Check-IN — Jarvis Orchestrierung"
_UNDERSCORE_NOTE_BODY = f"# {_UNDERSCORE_NOTE_TITLE}\n\nCheck-IN zur Orchestrierung.\n"


def _make_vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for relative, _title, body in _NOTES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    underscored = root / _UNDERSCORE_NOTE_DISK
    underscored.parent.mkdir(parents=True, exist_ok=True)
    underscored.write_text(_UNDERSCORE_NOTE_BODY, encoding="utf-8")
    return root


def _make_qmd(tmp_path: Path) -> Path:
    """qmd-shaped index.sqlite — same tables/columns as the live index."""
    db = tmp_path / "qmd" / "index.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, collection TEXT, path TEXT, title TEXT,
            hash TEXT, created_at TEXT, modified_at TEXT, active INTEGER DEFAULT 1
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT, created_at TEXT);
        CREATE VIRTUAL TABLE documents_fts USING fts5(filepath, title, body);
        """
    )
    for index, (relative, title, body) in enumerate(_NOTES, start=1):
        digest = f"hash-{index}"
        conn.execute(
            "INSERT INTO documents (id, collection, path, title, hash, created_at, "
            "modified_at, active) VALUES (?, 'vault', ?, ?, ?, ?, ?, 1)",
            (index, relative, title, digest, "2026-07-21", f"2026-07-2{index}"),
        )
        conn.execute(
            "INSERT INTO content (hash, doc, created_at) VALUES (?, ?, ?)",
            (digest, body, "2026-07-21"),
        )
        # Live quirk: documents_fts.filepath carries a `vault/` prefix that
        # documents.path does NOT — ids must come from documents.path.
        conn.execute(
            "INSERT INTO documents_fts (rowid, filepath, title, body) VALUES (?, ?, ?, ?)",
            (index, f"vault/{relative}", title, body),
        )
    # Indexed under the qmd-rewritten path; on disk it lives at
    # `_agents/_coordination/...` with underscores.
    underscore_index = len(_NOTES) + 1
    conn.execute(
        "INSERT INTO documents (id, collection, path, title, hash, created_at, "
        "modified_at, active) VALUES (?, 'vault', ?, ?, 'hash-underscore', ?, ?, 1)",
        (
            underscore_index,
            _UNDERSCORE_NOTE_INDEXED,
            _UNDERSCORE_NOTE_TITLE,
            "2026-07-21",
            "2026-07-21",
        ),
    )
    conn.execute(
        "INSERT INTO content (hash, doc, created_at) VALUES ('hash-underscore', ?, ?)",
        (_UNDERSCORE_NOTE_BODY, "2026-07-21"),
    )
    conn.execute(
        "INSERT INTO documents_fts (rowid, filepath, title, body) VALUES (?, ?, ?, ?)",
        (
            underscore_index,
            f"vault/{_UNDERSCORE_NOTE_INDEXED}",
            _UNDERSCORE_NOTE_TITLE,
            _UNDERSCORE_NOTE_BODY,
        ),
    )
    conn.commit()
    conn.close()
    return db


def _make_kanban(tmp_path: Path) -> Path:
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, "
        "status TEXT NOT NULL, assignee TEXT, created_by TEXT, priority INTEGER, "
        "created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, body, status, assignee, created_by, priority, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "t_abcd1234",
            "Jarvis B3 Orchestrierung",
            "Body über Orchestrierung und Worktrees.",
            "done",
            "coder",
            "piet",
            5,
            1784640000,
        ),
    )
    conn.commit()
    conn.close()
    return db


def _make_memories(tmp_path: Path) -> Path:
    root = tmp_path / "memories"
    root.mkdir()
    (root / "project_orchestrierung.md").write_text(
        "# Orchestrierung\n\nNotiz über Orchestrierung der Worker.\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point both pa_search and pa_graph at the same temp brain."""
    vault = _make_vault(tmp_path)
    qmd = _make_qmd(tmp_path)
    kanban = _make_kanban(tmp_path)
    memories = _make_memories(tmp_path)
    roots = (("memsearch", memories),)

    for module in (pa_graph, pa_search):
        monkeypatch.setattr(module, "_vault_root", lambda v=vault: v, raising=False)
        monkeypatch.setattr(module, "_qmd_index_path", lambda q=qmd: q, raising=False)
        monkeypatch.setattr(
            module, "_kanban_location", lambda k=kanban: (k, "default"), raising=False
        )
        monkeypatch.setattr(module, "_memory_roots", lambda r=roots: r, raising=False)
    # Keep the graph's remaining collectors off the developer's real machine.
    monkeypatch.setattr(pa_graph, "_projects_db_path", lambda: tmp_path / "projects.db")
    monkeypatch.setattr(pa_graph, "_receipts_root", lambda: tmp_path / "no-receipts")

    return {"vault": vault, "qmd": qmd, "kanban": kanban, "memories": memories, "tmp": tmp_path}


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    pa_search.register_pa_search_routes(app)
    return TestClient(app, raise_server_exceptions=False)


def _graph_ids() -> set[str]:
    graph = pa_graph.build_graph(force_refresh=True)
    return {str(node["id"]) for node in graph["nodes"]}


# ---------------------------------------------------------------------------
# AC-1 — response schema
# ---------------------------------------------------------------------------


def test_search_response_schema(wired):
    response = _client().get("/api/pa/search", params={"q": "Orchestrierung"})
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"query", "items", "took_ms", "errors"}
    assert data["query"] == "Orchestrierung"
    assert isinstance(data["took_ms"], int) and data["took_ms"] >= 0
    assert data["errors"] == []
    assert data["items"], "expected hits across the three sources"
    for item in data["items"]:
        assert set(item) == {
            "id", "ref", "title", "cluster", "snippet", "score",
            "kind", "source", "in_graph", "meta",
        }
        assert item["source"] in {"vault", "kanban", "memory"}
        assert isinstance(item["in_graph"], bool)
        assert isinstance(item["meta"], dict)


def test_node_response_schema(wired):
    _graph_ids()  # warm the graph cache so connections resolve
    response = _client().get(
        "/api/pa/node", params={"id": "vault:00-canon/uni-konzept.md"}
    )
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {
        "id", "title", "cluster", "body", "metadata", "connections", "source",
    }
    assert data["id"] == "vault:00-canon/uni-konzept.md"
    assert data["title"] == "Uni-Konzept"
    assert data["cluster"] == "canon"
    assert data["source"] == "vault"
    assert "Orchestrierung" in data["body"]
    assert isinstance(data["metadata"], dict)
    for connection in data["connections"]:
        assert set(connection) == {"id", "title", "cluster", "kind", "direction", "label"}
        assert connection["direction"] in {"in", "out", "both"}


# ---------------------------------------------------------------------------
# AC-2 — id parity with pa_graph
# ---------------------------------------------------------------------------


def test_vault_hit_ids_match_graph_node_ids(wired):
    graph_ids = _graph_ids()
    assert "vault:00-canon/uni-konzept.md" in graph_ids

    data = _client().get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    vault_hits = [item for item in data["items"] if item["source"] == "vault"]
    assert vault_hits, "expected a vault hit"
    for hit in vault_hits:
        assert hit["id"] in graph_ids
        assert hit["in_graph"] is True
        # Relative vault path — no `vault/` FTS prefix, no absolute path.
        assert not hit["id"].startswith("vault:vault/")
        assert str(wired["vault"]) not in hit["id"]


def test_unicode_vault_path_id_parity(wired):
    graph_ids = _graph_ids()
    expected = pa_graph._vault_node("00-Canon/Übungs Notiz Größe.md", "x", 0)["id"]
    # str.casefold() is deliberately lossy beyond lowercasing: "ß" folds to "ss",
    # so the canonical id is "grösse", not "größe". pa_graph owns this mapping —
    # the search must reuse it verbatim rather than re-deriving ids (AC-2).
    assert expected == "vault:00-canon/übungs-notiz-grösse.md"
    assert expected in graph_ids

    data = _client().get("/api/pa/search", params={"q": "Unicode-Inhalt"}).json()
    ids = {item["id"] for item in data["items"]}
    assert expected in ids


def test_kanban_and_memory_ids_match_graph(wired):
    graph_ids = _graph_ids()
    data = _client().get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    by_source = {item["source"]: item for item in data["items"]}

    assert by_source["kanban"]["id"] == "task:t_abcd1234"
    assert by_source["kanban"]["id"] in graph_ids
    assert by_source["memory"]["id"] == "memory:memsearch:project_orchestrierung.md"
    assert by_source["memory"]["id"] in graph_ids


def test_in_graph_false_when_graph_cache_is_cold(wired):
    """`in_graph` must never trigger a multi-second graph rebuild."""
    data = _client().get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    assert data["items"]
    assert all(item["in_graph"] is False for item in data["items"])


# ---------------------------------------------------------------------------
# AC-3 — sources, dedupe, global limit
# ---------------------------------------------------------------------------


def test_all_three_sources_represented(wired):
    data = _client().get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    assert {"vault", "kanban", "memory"} <= {item["source"] for item in data["items"]}


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_global_limit_enforced_across_sources(wired, limit):
    data = _client().get(
        "/api/pa/search", params={"q": "Orchestrierung", "limit": limit}
    ).json()
    assert len(data["items"]) <= limit


def test_dedupe_and_deterministic_order(wired):
    client = _client()
    first = client.get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    pa_search.invalidate_search_cache()
    second = client.get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    ids = [item["id"] for item in first["items"]]
    assert ids == [item["id"] for item in second["items"]]
    assert len(set(ids)) == len(ids)


def test_duplicate_id_across_sources_collapses(wired, monkeypatch):
    """A record surfacing from two sources must yield exactly one item."""
    clash = {
        "id": "vault:00-canon/uni-konzept.md",
        "ref": "x", "title": "clash", "cluster": "canon", "snippet": "",
        "score": 1.0, "kind": "task", "source": "kanban", "meta": {},
    }
    monkeypatch.setattr(pa_search, "_search_kanban", lambda q, limit: [clash])
    data = _client().get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    ids = [item["id"] for item in data["items"]]
    assert ids.count("vault:00-canon/uni-konzept.md") == 1


# ---------------------------------------------------------------------------
# AC-4 — per-source error and timeout isolation
# ---------------------------------------------------------------------------


def test_missing_qmd_index_still_200_with_errors(wired, monkeypatch):
    monkeypatch.setattr(pa_search, "_qmd_index_path", lambda: wired["tmp"] / "gone.sqlite")
    response = _client().get("/api/pa/search", params={"q": "Orchestrierung"})
    assert response.status_code == 200
    data = response.json()
    assert [error["source"] for error in data["errors"]] == ["vault"]
    assert {"kanban", "memory"} <= {item["source"] for item in data["items"]}


def test_raising_source_isolated(wired, monkeypatch):
    def _boom(query, limit):
        raise RuntimeError("kanban kaputt")

    monkeypatch.setattr(pa_search, "_search_kanban", _boom)
    response = _client().get("/api/pa/search", params={"q": "Orchestrierung"})
    assert response.status_code == 200
    data = response.json()
    assert any(
        error["source"] == "kanban" and "kanban kaputt" in error["error"]
        for error in data["errors"]
    )
    assert any(item["source"] == "vault" for item in data["items"])


def test_hanging_source_times_out_without_5xx(wired, monkeypatch):
    def _hang(query, limit):
        time.sleep(30)

    monkeypatch.setattr(pa_search, "_search_memory", _hang)
    monkeypatch.setattr(pa_search, "SOURCE_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()
    response = _client().get("/api/pa/search", params={"q": "Orchestrierung"})
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert elapsed < 10, "handler must not block on the hung source"
    data = response.json()
    assert any(
        error["source"] == "memory" and "timed out" in error["error"]
        for error in data["errors"]
    )
    assert any(item["source"] == "vault" for item in data["items"])


@pytest.mark.parametrize("query", ["", "   ", "a"])
def test_blank_or_too_short_query_returns_empty_items(wired, query):
    response = _client().get("/api/pa/search", params={"q": query})
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["errors"] == []


# ---------------------------------------------------------------------------
# AC-5 — node preview: content, 404/400, path + secret safety
# ---------------------------------------------------------------------------


def test_node_unicode_vault_path_resolves_lossy_id(wired):
    """The id is case-folded with dashes (and "ß"→"ss"); disk has spaces + caps."""
    response = _client().get(
        "/api/pa/node", params={"id": "vault:00-canon/übungs-notiz-grösse.md"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Übungs Notiz Größe"
    assert "Unicode-Inhalt" in data["body"]


def test_node_resolves_id_whose_spelling_only_exists_in_the_index(wired):
    """Regression: qmd strips leading "_" and folds "_"->"-" when indexing.

    The id is therefore `vault:agents/coordination/...` while the note lives at
    `_agents/_coordination/2026-07-19_2240_...` — a filesystem-only reverse
    lookup 404s. Live this silently hid every check-in note under `_agents/`.
    """
    response = _client().get(
        "/api/pa/node", params={"id": f"vault:{_UNDERSCORE_NOTE_INDEXED}"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == _UNDERSCORE_NOTE_TITLE
    assert "Check-IN zur Orchestrierung" in data["body"]
    assert data["source"] == "vault"
    # Still no absolute filesystem path in the payload.
    assert str(wired["vault"]) not in json.dumps(data)


def test_node_resolves_via_index_when_the_vault_walk_cannot_help(wired, monkeypatch):
    """The live vault holds ~90k notes against a 5k walk cap.

    A fixture vault is small enough that the bounded walk always finds the file,
    which hides the real defect — so pin the live condition explicitly: with the
    walk exhausted, the indexed copy must still answer.
    """
    monkeypatch.setattr(pa_search, "_scan_vault_for", lambda root, normalized: None)
    response = _client().get(
        "/api/pa/node", params={"id": f"vault:{_UNDERSCORE_NOTE_INDEXED}"}
    )
    assert response.status_code == 200
    assert response.json()["title"] == _UNDERSCORE_NOTE_TITLE


def test_node_does_not_walk_the_vault_for_indexed_notes(wired, monkeypatch):
    """Cost guard: an indexed note must resolve without the full-vault walk."""
    calls: list[str] = []
    monkeypatch.setattr(
        pa_search,
        "_scan_vault_for",
        lambda root, normalized: calls.append(normalized) or None,
    )
    response = _client().get(
        "/api/pa/node", params={"id": f"vault:{_UNDERSCORE_NOTE_INDEXED}"}
    )
    assert response.status_code == 200
    assert calls == [], f"indexed note triggered a vault walk: {calls}"


def test_every_search_hit_is_retrievable_as_a_node(wired):
    """Round-trip invariant: an id the search hands out must resolve.

    This is the invariant the live smoke broke — search returned ids that node
    preview answered with 404.
    """
    items = _client().get("/api/pa/search", params={"q": "Orchestrierung"}).json()["items"]
    assert items
    for item in items:
        response = _client().get("/api/pa/node", params={"id": item["id"]})
        assert response.status_code == 200, f"{item['id']} ({item['source']}) -> {response.status_code}"
        assert response.json()["id"] == item["id"]


def test_node_connections_come_from_graph_edges(wired):
    _graph_ids()  # warm cache — Uni-Konzept wikilinks to Beta-Notiz
    data = _client().get(
        "/api/pa/node", params={"id": "vault:00-canon/uni-konzept.md"}
    ).json()
    connections = {c["id"]: c for c in data["connections"]}
    assert "vault:00-canon/beta-notiz.md" in connections
    beta = connections["vault:00-canon/beta-notiz.md"]
    assert beta["direction"] == "out"
    assert beta["title"] == "Beta-Notiz"
    assert beta["cluster"] == "canon"


def test_node_without_graph_cache_has_empty_connections(wired):
    data = _client().get(
        "/api/pa/node", params={"id": "vault:00-canon/uni-konzept.md"}
    ).json()
    assert data["connections"] == []


def test_node_task_preview(wired):
    data = _client().get("/api/pa/node", params={"id": "task:t_abcd1234"}).json()
    assert data["id"] == "task:t_abcd1234"
    assert data["title"] == "Jarvis B3 Orchestrierung"
    assert data["cluster"] == "projekte"
    assert data["source"] == "kanban"
    assert "Orchestrierung" in data["body"]
    assert data["metadata"]["status"] == "done"
    assert data["metadata"]["assignee"] == "coder"


def test_node_memory_preview(wired):
    data = _client().get(
        "/api/pa/node", params={"id": "memory:memsearch:project_orchestrierung.md"}
    ).json()
    assert data["source"] == "memory"
    assert data["cluster"] == "memories"
    assert data["title"] == "Orchestrierung"
    assert "Worker" in data["body"]


def test_node_memory_preview_uppercase_filename(wired):
    """Case-folded ids must still resolve to their real (mixed-case) file.

    Regression: ``pa_graph`` mints memory ids as ``relative.casefold()``, so the
    live notes ``~/.hermes/memories/MEMORY.md`` and ``USER.md`` become
    ``memory:hermes:memory.md`` / ``memory:hermes:user.md``. Joining that folded
    string straight onto a case-sensitive filesystem missed the file and made
    every mixed-case memory node — i.e. both real Hermes memories — 404 in the
    preview, even though search and the graph happily listed them.
    """
    (wired["memories"] / "MEMORY-Archiv.md").write_text(
        "# Archiv\n\nÄltere Orchestrierung-Notizen.\n", encoding="utf-8"
    )
    node_id = "memory:memsearch:memory-archiv.md"
    assert node_id in _graph_ids(), "graph mints the case-folded id"

    hit = next(
        item
        for item in _client()
        .get("/api/pa/search", params={"q": "Orchestrierung", "limit": 50})
        .json()["items"]
        if item["id"] == node_id
    )
    assert hit["source"] == "memory"

    data = _client().get("/api/pa/node", params={"id": node_id}).json()
    assert data["source"] == "memory"
    assert data["title"] == "Archiv"
    assert "Orchestrierung" in data["body"]
    assert data["metadata"]["ref"] == "memory://memsearch/MEMORY-Archiv.md"
    assert str(wired["memories"]) not in _client().get(
        "/api/pa/node", params={"id": node_id}
    ).text


def test_node_memory_case_fold_cannot_escape_root(wired):
    """The case-insensitive fallback must not become a traversal hole."""
    (wired["tmp"] / "OUTSIDE.md").write_text("TOPSECRET", encoding="utf-8")
    for node_id in (
        "memory:memsearch:../outside.md",
        "memory:memsearch:../OUTSIDE.md",
    ):
        response = _client().get("/api/pa/node", params={"id": node_id})
        assert response.status_code == 400
        assert "TOPSECRET" not in response.text


@pytest.mark.parametrize(
    "node_id",
    [
        "vault:00-canon/does-not-exist.md",
        "task:t_missing",
        "memory:memsearch:nope.md",
    ],
)
def test_node_unknown_id_404(wired, node_id):
    assert _client().get("/api/pa/node", params={"id": node_id}).status_code == 404


@pytest.mark.parametrize(
    "node_id",
    [
        "vault:../outside.md",
        "vault:00-canon/../../outside.md",
        "vault:/etc/passwd",
        "vault:~/secrets.md",
        "vault:00-canon/note.txt",
        "memory:memsearch:../../outside.md",
        "memory:memsearch:/etc/passwd",
        "memory:unknown-root:note.md",
        "task:../../etc/passwd",
        "not-a-node",
        "",
    ],
)
def test_node_invalid_or_traversal_id_400(wired, node_id):
    assert _client().get("/api/pa/node", params={"id": node_id}).status_code == 400


def test_node_traversal_cannot_read_outside_vault(wired):
    (wired["tmp"] / "outside.md").write_text("TOPSECRET", encoding="utf-8")
    response = _client().get("/api/pa/node", params={"id": "vault:../outside.md"})
    assert response.status_code == 400
    assert "TOPSECRET" not in response.text


def test_node_symlink_escape_rejected(wired):
    (wired["tmp"] / "outside.md").write_text("TOPSECRET", encoding="utf-8")
    link = wired["vault"] / "00-Canon" / "escape.md"
    link.symlink_to(wired["tmp"] / "outside.md")
    response = _client().get("/api/pa/node", params={"id": "vault:00-canon/escape.md"})
    assert response.status_code == 404
    assert "TOPSECRET" not in response.text


def test_node_never_leaks_absolute_paths(wired):
    response = _client().get(
        "/api/pa/node", params={"id": "vault:00-canon/uni-konzept.md"}
    )
    assert response.status_code == 200
    assert str(wired["vault"]) not in response.text
    assert str(wired["tmp"]) not in response.text


def test_secrets_are_redacted_from_body_and_snippet(wired):
    secret = wired["vault"] / "00-Canon" / "Creds.md"
    secret.write_text(
        "# Creds\n\nOrchestrierung token: sk-abcdef0123456789xyz\n", encoding="utf-8"
    )
    response = _client().get("/api/pa/node", params={"id": "vault:00-canon/creds.md"})
    assert response.status_code == 200
    assert "sk-abcdef0123456789xyz" not in response.text
    assert "[redacted]" in response.json()["body"]


# ---------------------------------------------------------------------------
# AC-6 — caching
# ---------------------------------------------------------------------------


def test_search_cache_reuses_response(wired, monkeypatch):
    calls = {"n": 0}
    real = pa_search._search_vault

    def _counting(query, limit):
        calls["n"] += 1
        return real(query, limit)

    monkeypatch.setattr(pa_search, "_search_vault", _counting)
    client = _client()
    first = client.get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    second = client.get("/api/pa/search", params={"q": "Orchestrierung"}).json()
    assert calls["n"] == 1, "second identical query must be served from cache"
    assert [i["id"] for i in first["items"]] == [i["id"] for i in second["items"]]

    client.get("/api/pa/search", params={"q": "Worktrees"})
    assert calls["n"] == 2, "a different query must miss the cache"


def test_cache_invalidation_forces_refetch(wired, monkeypatch):
    calls = {"n": 0}
    real = pa_search._search_vault

    def _counting(query, limit):
        calls["n"] += 1
        return real(query, limit)

    monkeypatch.setattr(pa_search, "_search_vault", _counting)
    client = _client()
    client.get("/api/pa/search", params={"q": "Orchestrierung"})
    pa_search.invalidate_search_cache()
    client.get("/api/pa/search", params={"q": "Orchestrierung"})
    assert calls["n"] == 2


def test_cached_response_is_not_mutable_by_callers(wired):
    first = pa_search.search("Orchestrierung")
    first["items"].clear()
    second = pa_search.search("Orchestrierung")
    assert second["items"], "cache must hand out copies, not shared state"
