from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import hermes_cli.pa_chat as pa
import hermes_cli.pa_graph as graph


def _create_qmd_index(path: Path) -> None:
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE content (
            hash TEXT PRIMARY KEY,
            doc TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            path TEXT NOT NULL,
            title TEXT NOT NULL,
            hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            modified_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(collection, path)
        );
        """
    )
    docs = [
        (
            "h1",
            "00-canon/vision.md",
            "Vision",
            "# Vision\n\n[[03-projects/alpha]]\n[Demo](agents/skills/demo.md)\n",
            "2026-07-19T18:00:00Z",
        ),
        (
            "h2",
            "03-projects/alpha.md",
            "Alpha",
            "# Alpha\n\n[[Vision]]\n",
            "2026-07-19T17:00:00Z",
        ),
        (
            "h3",
            "agents/skills/demo.md",
            "Demo Skill",
            "# Demo Skill\n",
            "2026-07-19T16:00:00Z",
        ),
        (
            "h4",
            "03-agents/codex/notes/live.md",
            "Codex live",
            "# Codex live\n",
            "2026-07-19T15:00:00Z",
        ),
        # Receipts have a dedicated source and must not be duplicated by qmd.
        (
            "h5",
            "03-agents/codex/receipts/duplicate.md",
            "Duplicate receipt",
            "# Duplicate receipt\n",
            "2026-07-19T19:00:00Z",
        ),
    ]
    for doc_hash, doc_path, title, body, modified_at in docs:
        conn.execute(
            "INSERT INTO content(hash, doc, created_at) VALUES (?, ?, ?)",
            (doc_hash, body, modified_at),
        )
        conn.execute(
            "INSERT INTO documents(collection, path, title, hash, created_at, "
            "modified_at, active) VALUES ('vault', ?, ?, ?, ?, ?, 1)",
            (doc_path, title, doc_hash, modified_at, modified_at),
        )
    conn.commit()
    conn.close()


def _create_projects_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE projects ("
        "id TEXT PRIMARY KEY, slug TEXT NOT NULL, name TEXT NOT NULL, "
        "board_slug TEXT, created_at INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO projects VALUES ('p_alpha', 'alpha', 'Alpha Project', "
        "'default', 100, 0)"
    )
    conn.commit()
    conn.close()


def _create_kanban_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            last_heartbeat_at INTEGER,
            project_id TEXT
        );
        CREATE TABLE task_links (
            parent_id TEXT NOT NULL,
            child_id TEXT NOT NULL,
            PRIMARY KEY(parent_id, child_id)
        );
        INSERT INTO tasks VALUES (
            't_parent', 'Parent task', 'running', 100, 110, NULL, 120, 'p_alpha'
        );
        INSERT INTO tasks VALUES (
            't_child', 'Child task', 'ready', 90, NULL, NULL, NULL, 'p_alpha'
        );
        INSERT INTO task_links VALUES ('t_parent', 't_child');
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def isolated_graph_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    vault = tmp_path / "vault"
    qmd_index = home / ".cache" / "qmd" / "index.sqlite"
    kanban_db = hermes_home / "kanban.db"
    projects_db = hermes_home / "projects.db"
    memories = home / ".memsearch" / "shared" / "memory"
    receipts = vault / "03-Agents"
    hermes_home.mkdir(parents=True)
    memories.mkdir(parents=True)
    (receipts / "Codex" / "receipts").mkdir(parents=True)
    (vault / "00-Canon").mkdir(parents=True)
    (vault / "00-Canon" / "fallback.md").write_text(
        "# Fallback\n\n[[other]]\n", encoding="utf-8"
    )
    (vault / "00-Canon" / "other.md").write_text("# Other\n", encoding="utf-8")
    (receipts / "Codex" / "receipts" / "done-receipt.md").write_text(
        "# Done Receipt\n\nEvidence.\n", encoding="utf-8"
    )
    (memories / "2026-07-18.md").write_text("# Previous\n", encoding="utf-8")
    (memories / "2026-07-19.md").write_text("# Current\n", encoding="utf-8")
    _create_qmd_index(qmd_index)
    _create_kanban_db(kanban_db)
    _create_projects_db(projects_db)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(pa, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(graph, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(graph, "_qmd_index_path", lambda: qmd_index)
    monkeypatch.setattr(graph, "_vault_root", lambda: vault)
    monkeypatch.setattr(graph, "_receipts_root", lambda: receipts)
    monkeypatch.setattr(graph, "_memory_roots", lambda: (("memsearch", memories),))
    monkeypatch.setattr(graph, "_projects_db_path", lambda: projects_db)
    monkeypatch.setattr(graph, "_kanban_location", lambda: (kanban_db, "default"))
    graph.invalidate_graph_cache()
    yield {
        "home": home,
        "hermes_home": hermes_home,
        "vault": vault,
        "qmd": qmd_index,
        "kanban": kanban_db,
        "projects": projects_db,
        "memories": memories,
        "receipts": receipts,
    }
    graph.invalidate_graph_cache()


def _part_node(source: str) -> dict[str, Any]:
    return {
        "id": f"{source}:one",
        "label": source,
        "cluster": "canon",
        "kind": "doc",
        "weight": 0.5,
    }


def test_live_schema_matches_frontend_mock_shape_and_sources(
    isolated_graph_sources: dict[str, Path],
) -> None:
    payload = graph.build_graph(force_refresh=True)

    assert set(payload) == {
        "schema",
        "source",
        "layout",
        "generated_at",
        "refresh",
        "clusters",
        "nodes",
        "edges",
        "errors",
    }
    assert payload["schema"] == "pa-graph/v1"
    assert payload["source"] == "live"
    assert payload["layout"] == "precomputed-viewbox-1280x820"
    assert payload["refresh"] == {
        "interval_s": 30,
        "cache_ttl_s": 60,
        "invalidation": "ttl-or-process-restart",
        "on_error": "empty-live-data + frontend-mock-fallback",
    }
    assert payload["errors"] == []
    assert {cluster["id"] for cluster in payload["clusters"]} == {
        "canon",
        "projekte",
        "agenten",
        "skills",
        "memories",
        "receipts",
        "archiv",
    }
    assert {cluster["color"] for cluster in payload["clusters"]} == {
        "#38d8ff",
        "#3ddc97",
        "#ffb347",
        "#5b8cff",
        "#b78cff",
        "#ff7ab8",
        "#5a6f8f",
    }
    for node in payload["nodes"]:
        assert {"id", "label", "cluster", "kind", "weight", "x", "y"} <= set(node)
        assert isinstance(node["label"], str)
        assert 0.2 <= node["weight"] <= 1.0
    for edge in payload["edges"]:
        assert set(edge) == {"from", "to", "kind"}

    node_ids = {node["id"] for node in payload["nodes"]}
    edge_kinds = {edge["kind"] for edge in payload["edges"]}
    assert "vault:03-agents/codex/receipts/duplicate.md" not in node_ids
    assert {
        "vault:00-canon/vision.md",
        "vault:03-projects/alpha.md",
        "project:p_alpha",
        "task:t_parent",
        "task:t_child",
        "agent:codex",
        "receipt:codex/done-receipt.md",
        "memory:memsearch:2026-07-19.md",
    } <= node_ids
    assert {
        "wikilink",
        "markdown-link",
        "project-task",
        "task-link",
        "receipt",
        "previous-memory",
    } <= edge_kinds


@pytest.mark.parametrize(
    ("path", "cluster"),
    [
        ("00-Canon/vision.md", "canon"),
        ("03-Projects/alpha.md", "projekte"),
        ("03-Agents/Codex/note.md", "agenten"),
        ("_agents/skills/demo.md", "skills"),
        ("01-Daily/2026-07-19.md", "memories"),
        ("03-Agents/Codex/receipts/x.md", "receipts"),
        ("09-Archive/old.md", "archiv"),
    ],
)
def test_top_level_vault_cluster_mapping(path: str, cluster: str) -> None:
    assert graph._cluster_for_vault_path(path) == cluster


def test_qmd_failure_uses_bounded_vault_fallback(
    isolated_graph_sources: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = isolated_graph_sources["home"] / "missing-qmd.sqlite"
    monkeypatch.setattr(graph, "_qmd_index_path", lambda: missing)

    part = graph._collect_vault()

    assert {node["id"] for node in part.nodes} == {
        "vault:00-canon/fallback.md",
        "vault:00-canon/other.md",
    }
    assert part.errors[0]["source"] == "qmd"
    assert "bounded Vault fallback active" in part.errors[0]["error"]


def test_source_and_global_caps_are_hard(monkeypatch: pytest.MonkeyPatch) -> None:
    vault_rows = [
        {"path": f"00-canon/note-{index}.md", "title": str(index), "doc": ""}
        for index in range(graph.VAULT_NODE_LIMIT + 75)
    ]
    assert len(graph._vault_part_from_rows(vault_rows).nodes) == graph.VAULT_NODE_LIMIT

    oversized = graph.GraphPart(
        nodes=[
            {
                "id": f"node:{index}",
                "label": str(index),
                "cluster": "canon",
                "kind": "doc",
                "weight": 0.3,
            }
            for index in range(graph.MAX_NODES + 75)
        ],
        edges=[
            {"from": f"node:{index}", "to": f"node:{index + 1}", "kind": "link"}
            for index in range(graph.MAX_NODES + 74)
        ],
    )
    monkeypatch.setattr(graph, "_collect_vault", lambda: oversized)
    monkeypatch.setattr(graph, "_collect_kanban", graph.GraphPart)
    monkeypatch.setattr(graph, "_collect_receipts", graph.GraphPart)
    monkeypatch.setattr(graph, "_collect_memories", graph.GraphPart)

    payload = graph.build_graph(force_refresh=True)

    assert len(payload["nodes"]) == graph.MAX_NODES
    assert len(payload["edges"]) == graph.MAX_NODES - 1
    assert len(payload["edges"]) <= graph.MAX_EDGES


@pytest.mark.parametrize(
    ("broken", "error_source"),
    [
        ("_collect_vault", "vault"),
        ("_collect_kanban", "kanban"),
        ("_collect_receipts", "receipts"),
        ("_collect_memories", "memories"),
    ],
)
def test_each_source_failure_is_isolated(
    monkeypatch: pytest.MonkeyPatch, broken: str, error_source: str
) -> None:
    collectors = {
        "_collect_vault": "vault",
        "_collect_kanban": "kanban",
        "_collect_receipts": "receipts",
        "_collect_memories": "memories",
    }
    for name, source in collectors.items():
        if name == broken:
            def fail(source_name: str = source) -> graph.GraphPart:
                raise RuntimeError(f"{source_name} unavailable")

            monkeypatch.setattr(graph, name, fail)
        else:
            monkeypatch.setattr(
                graph,
                name,
                lambda source_name=source: graph.GraphPart(nodes=[_part_node(source_name)]),
            )

    payload = graph.build_graph(force_refresh=True)

    assert len(payload["nodes"]) == 3
    assert payload["errors"] == [
        {"source": error_source, "error": f"{error_source} unavailable"}
    ]


def test_cache_reuses_snapshot_until_ttl_and_returns_deep_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = {"value": 100.0}
    calls = {"count": 0}

    def counted() -> graph.GraphPart:
        calls["count"] += 1
        return graph.GraphPart(nodes=[_part_node("vault")])

    monkeypatch.setattr(graph, "_clock", lambda: now["value"])
    monkeypatch.setattr(graph, "_collect_vault", counted)
    monkeypatch.setattr(graph, "_collect_kanban", graph.GraphPart)
    monkeypatch.setattr(graph, "_collect_receipts", graph.GraphPart)
    monkeypatch.setattr(graph, "_collect_memories", graph.GraphPart)
    graph.invalidate_graph_cache()

    first = graph.build_graph()
    first["nodes"].clear()
    now["value"] = 159.9
    second = graph.build_graph()
    now["value"] = 160.0
    third = graph.build_graph()

    assert len(second["nodes"]) == 1
    assert len(third["nodes"]) == 1
    assert calls["count"] == 2


def test_total_failure_endpoint_returns_200_empty_graph_and_uses_executor(
    isolated_graph_sources: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "_collect_vault",
        "_collect_kanban",
        "_collect_receipts",
        "_collect_memories",
    ):
        def fail(source_name: str = name.removeprefix("_collect_")) -> graph.GraphPart:
            raise RuntimeError(f"{source_name} down")

        monkeypatch.setattr(graph, name, fail)
    graph.invalidate_graph_cache()
    threads: dict[str, int] = {}
    real_build = graph.build_graph

    def recording_build() -> dict[str, Any]:
        threads["worker"] = threading.get_ident()
        return real_build()

    monkeypatch.setattr(graph, "build_graph", recording_build)
    app = FastAPI()

    @app.middleware("http")
    async def record_loop_thread(request: Request, call_next: Any) -> Any:
        threads["loop"] = threading.get_ident()
        return await call_next(request)

    pa.register_pa_routes(app)
    with TestClient(app) as client:
        response = client.get("/api/pa/graph")

    assert response.status_code == 200
    payload = response.json()
    assert payload["nodes"] == []
    assert payload["edges"] == []
    assert {entry["source"] for entry in payload["errors"]} == {
        "vault",
        "kanban",
        "receipts",
        "memories",
    }
    assert threads["worker"] != threads["loop"]


def test_real_web_server_registers_graph_as_authenticated_non_public_route(
    isolated_graph_sources: dict[str, Path],
) -> None:
    code = """
import json
from hermes_cli.web_server import _PUBLIC_API_PATHS, app
paths = {getattr(route, 'path', '') for route in app.routes}
path = '/api/pa/graph'
print(json.dumps({'registered': path in paths, 'public': path in _PUBLIC_API_PATHS}))
"""
    env = os.environ.copy()
    env["HOME"] = str(isolated_graph_sources["home"])
    env["HERMES_HOME"] = str(isolated_graph_sources["hermes_home"])
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "registered": True,
        "public": False,
    }
