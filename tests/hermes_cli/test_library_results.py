"""Bibliothek — Ergebnisse: /api/library/results{,/item}.

Fixture-DB gebaut aus dem live geschöpften Schema-Dump
(``fixtures/kanban_schema.sql``, ``.schema tasks``/``.schema task_runs`` von
``~/.hermes/kanban.db``) plus 4 verbatim von live kopierten Task-Zeilen samt
ihrer echten ``task_runs`` (``fixtures/kanban_results_rows.json``, geerntet
2026-07-09 via ``SELECT ... FROM tasks WHERE status='done' AND result IS NOT
NULL``) — keine synthetischen Ergebnistexte. Ein zusätzlicher
Kontroll-Datensatz (offener Task ohne Ergebnis) wird PRO TEST direkt
eingefügt, um den ``status='done'``-Filter gegen etwas Falsches zu beweisen.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import library_results as lr
from hermes_cli import web_server

FIXTURES_DIR = Path(__file__).parent / "fixtures"
_SCHEMA_SQL = (FIXTURES_DIR / "kanban_schema.sql").read_text(encoding="utf-8")
_ROWS = json.loads((FIXTURES_DIR / "kanban_results_rows.json").read_text(encoding="utf-8"))

# Real ids/titles from the harvested fixture rows, for assertions below.
T_KIMI = "t_fe62854e"          # kind=code, assignee/profile=coder, no verdict on latest run
T_SCOUT = "t_10cda708"         # kind=None, profile=scout, very long markdown result
T_RELEASE_GATE = "t_5f17034e"  # kind=code, profile=premium, 8 runs incl. APPROVED/REQUEST_CHANGES
T_DESIGN_BOARD = "t_7387e51e"  # kind=code, profile=coder, oldest completed_at


def _build_fixture_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SCHEMA_SQL)
        for t in _ROWS["tasks"]:
            conn.execute(
                "INSERT INTO tasks (id, title, body, assignee, status, created_at, "
                "completed_at, result, kind, tenant) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    t["id"], t["title"], t["body"], t["assignee"], t["status"],
                    t["created_at"], t["completed_at"], t["result"], t["kind"],
                    t["tenant"],
                ),
            )
        for r in _ROWS["runs"]:
            conn.execute(
                "INSERT INTO task_runs (id, task_id, profile, status, started_at, "
                "ended_at, outcome, verdict, cost_usd, input_tokens, output_tokens, "
                "summary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r["id"], r["task_id"], r["profile"], r["status"], r["started_at"],
                    r["ended_at"], r["outcome"], r["verdict"], r["cost_usd"],
                    r["input_tokens"], r["output_tokens"], r["summary"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_control_task(
    db_path: Path, task_id: str, *, status: str, result: str | None,
) -> None:
    """Insert a NON-fixture control row (e.g. still-open task) to prove the
    ``status='done' AND result IS NOT NULL`` filter actually excludes it."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, completed_at, "
            "result, kind) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, "Control: noch offener Task", "coder", status, 1, None, result, "code"),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "kanban.db"
    _build_fixture_db(path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    return path


@pytest.fixture
def client(db_path):
    """Loopback-TestClient against the real app stack (route wiring + gate)."""
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app, base_url="http://127.0.0.1:9119")
    yield test_client
    web_server.app.state.bound_host = prev_host
    web_server.app.state.auth_required = prev_required


HEADERS = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}


# ---------------------------------------------------------------------------
# (a) list = only status='done' with non-empty result, newest completed_at first
# ---------------------------------------------------------------------------

def test_list_requires_session_token(client):
    assert client.get("/api/library/results").status_code == 401


def test_list_returns_only_done_with_result_newest_first(client, db_path):
    _insert_control_task(db_path, "t_control_open", status="open", result=None)
    _insert_control_task(db_path, "t_control_done_no_result", status="done", result=None)

    res = client.get("/api/library/results", headers=HEADERS)
    assert res.status_code == 200
    payload = res.json()
    ids = [item["id"] for item in payload["items"]]

    # Newest completed_at first, among exactly the 4 real done+result rows.
    assert ids == [T_KIMI, T_SCOUT, T_RELEASE_GATE, T_DESIGN_BOARD]
    assert payload["total"] == 4
    assert "t_control_open" not in ids
    assert "t_control_done_no_result" not in ids


def test_list_item_shape_matches_real_row(client):
    res = client.get("/api/library/results?limit=1", headers=HEADERS)
    item = res.json()["items"][0]
    assert item["id"] == T_KIMI
    assert item["title"] == "Implement Kimi Usage analog zu Claude/ChatGPT im Statistik-Tab"
    assert item["kind"] == "code"
    assert item["profile"] == "coder"
    assert item["completed_at"] == "2026-07-09T08:31:00Z"
    # The latest task_runs row for t_fe62854e (id 6465) carries no verdict —
    # the final closing run rarely does; real behaviour, not invented.
    assert item["verdict"] is None
    assert item["outcome"] == "completed"
    assert item["run_count"] == 5
    assert "result_md" not in item  # full body only via the item endpoint


# ---------------------------------------------------------------------------
# (b) result_summary truncates at <=280 chars on a word boundary; full text
#     only via the item endpoint.
# ---------------------------------------------------------------------------

def test_result_summary_truncates_on_word_boundary(client):
    scout_row = next(t for t in _ROWS["tasks"] if t["id"] == T_SCOUT)
    full_flat = " ".join(scout_row["result"].split())
    assert len(full_flat) > 280  # precondition: this fixture row IS long

    res = client.get("/api/library/results", headers=HEADERS)
    item = next(i for i in res.json()["items"] if i["id"] == T_SCOUT)
    summary = item["result_summary"]

    assert len(summary) <= 280
    assert full_flat.startswith(summary)
    # The char right after the cut in the flattened original is either
    # nothing or a space — i.e. we never sliced through a word.
    assert full_flat[len(summary):len(summary) + 1] in ("", " ")


def test_item_endpoint_returns_full_untruncated_result(client):
    res = client.get("/api/library/results/item", params={"id": T_SCOUT}, headers=HEADERS)
    assert res.status_code == 200
    payload = res.json()
    scout_row = next(t for t in _ROWS["tasks"] if t["id"] == T_SCOUT)
    assert payload["result_md"] == scout_row["result"]
    assert len(payload["result_md"]) > 280
    assert isinstance(payload["runs"], list)
    assert len(payload["runs"]) == 1
    assert isinstance(payload["artifacts"], list)


def test_item_endpoint_unknown_id_is_404(client):
    res = client.get("/api/library/results/item", params={"id": "t_nope"}, headers=HEADERS)
    assert res.status_code == 404


def test_item_endpoint_traversal_id_is_400(client):
    res = client.get(
        "/api/library/results/item", params={"id": "../../etc/passwd"}, headers=HEADERS,
    )
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# (c) filters kind=/q= narrow the result set correctly.
# ---------------------------------------------------------------------------

def test_kind_filter(client):
    res = client.get("/api/library/results?kind=code", headers=HEADERS)
    ids = {item["id"] for item in res.json()["items"]}
    assert ids == {T_KIMI, T_RELEASE_GATE, T_DESIGN_BOARD}  # T_SCOUT has kind=None


def test_q_filter_matches_title(client):
    res = client.get("/api/library/results?q=Kimi", headers=HEADERS)
    ids = {item["id"] for item in res.json()["items"]}
    assert ids == {T_KIMI}


def test_q_filter_matches_result_body(client):
    res = client.get("/api/library/results?q=board_slug_for_conn", headers=HEADERS)
    ids = {item["id"] for item in res.json()["items"]}
    assert ids == set()  # that string lives only in run summaries, not in result/title
    res2 = client.get("/api/library/results?q=Reviewer%20NEEDS_REVISION", headers=HEADERS)
    ids2 = {item["id"] for item in res2.json()["items"]}
    assert ids2 == {T_RELEASE_GATE}


def test_verdict_filter(client):
    # None of the 4 fixture tasks' LATEST run carries a verdict — this is
    # real board behaviour (final closing run supersedes the review runs).
    res = client.get("/api/library/results?verdict=APPROVED", headers=HEADERS)
    assert res.json()["items"] == []


def test_profile_filter(client):
    res = client.get("/api/library/results?profile=scout", headers=HEADERS)
    ids = {item["id"] for item in res.json()["items"]}
    assert ids == {T_SCOUT}


# ---------------------------------------------------------------------------
# (d) format=md returns a clean markdown digest, no HTML.
# ---------------------------------------------------------------------------

def test_format_md_returns_markdown_digest(client):
    res = client.get("/api/library/results?format=md&limit=3", headers=HEADERS)
    assert res.status_code == 200
    assert "markdown" in res.headers["content-type"]
    body = res.text
    # No HTML tags — the real fixture text legitimately contains literal
    # angle brackets as placeholder syntax (e.g. "<slug>", "<task_id>"),
    # so this checks for actual tag patterns, not a blanket "<" ban.
    assert not re.search(r"</?(?:html|body|div|span|script|style|br|img|a\s)", body, re.IGNORECASE)
    assert "Implement Kimi Usage analog zu Claude/ChatGPT im Statistik-Tab" in body
    assert f"## Implement Kimi Usage analog zu Claude/ChatGPT im Statistik-Tab — {T_KIMI}" in body
    # Full result body present verbatim, not the 280-char summary.
    kimi_row = next(t for t in _ROWS["tasks"] if t["id"] == T_KIMI)
    assert kimi_row["result"] in body


def test_format_md_requires_session_token_too(client):
    assert client.get("/api/library/results?format=md").status_code == 401


# ---------------------------------------------------------------------------
# (e) the DB is opened strictly read-only.
# ---------------------------------------------------------------------------

def test_db_is_opened_read_only(client, monkeypatch):
    seen_uris: list[str] = []
    real_connect = sqlite3.connect

    def _spy_connect(uri_or_path, *args, **kwargs):
        if kwargs.get("uri"):
            seen_uris.append(uri_or_path)
        return real_connect(uri_or_path, *args, **kwargs)

    monkeypatch.setattr(lr.sqlite3, "connect", _spy_connect)

    res = client.get("/api/library/results", headers=HEADERS)
    assert res.status_code == 200
    assert seen_uris, "expected at least one uri=True sqlite3.connect call"
    assert all("mode=ro" in u for u in seen_uris)
