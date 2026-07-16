"""Tests for hermes_cli.agent_questions (Frage-Assistent P0a).

Parser fixtures are VERBATIM capture-style strings (same style as
test_agent_terminals._FIXTURE_F); whitespace is load-bearing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from collections.abc import Generator
from typing import Any

import pytest

from hermes_cli import agent_questions as aq
from hermes_cli.agent_terminals import TmuxAgentSessionService


# ----- Verbatim capture fixtures (do not "clean up" whitespace) -------------

# Same string as tests/hermes_cli/test_agent_terminals.py::_FIXTURE_F
_FIXTURE_CLAUDE_SELECT = (
    "  Do you want to proceed?\n"
    "  ❯ 1. Yes\n"
    "    2. No, and tell Claude what to do differently"
)

_FIXTURE_YN = "Allow network access for this tool? (y/n)\n"

_FIXTURE_BARE_Q = "What is the deployment target?\n"

_FIXTURE_NOT_QUESTION = (
    "• Working (6m 27s • esc to interrupt) · 1 background terminal running"
)

_FIXTURE_CLAUDE_SELECT_V2 = (
    "  Do you want to proceed?\n"
    "  ❯ 1. Yes\n"
    "    2. No\n"
    "    3. Something else entirely"
)

# Long claude-code-style select: question + ≥6 options; total block > 8 lines so
# last-8-only parsing would drop the question and/or early options.
_FIXTURE_LONG_SELECT = (
    "  Choose a deployment strategy for production?\n"
    "  ❯ 1. Rolling update with health checks\n"
    "    2. Blue-green swap\n"
    "    3. Canary 5 percent\n"
    "    4. Recreate all pods\n"
    "    5. Shadow traffic mirror\n"
    "    6. Manual stepwise promote\n"
    "    7. Abort and hold current\n"
    "    8. Roll back previous release\n"
)

_FIXTURE_LONG_SELECT_CURSOR3 = (
    "  Choose a deployment strategy for production?\n"
    "    1. Rolling update with health checks\n"
    "    2. Blue-green swap\n"
    "  ❯ 3. Canary 5 percent\n"
    "    4. Recreate all pods\n"
    "    5. Shadow traffic mirror\n"
    "    6. Manual stepwise promote\n"
    "    7. Abort and hold current\n"
    "    8. Roll back previous release\n"
)

_FIXTURE_LONG_SELECT_LABEL_CHANGE = (
    "  Choose a deployment strategy for production?\n"
    "  ❯ 1. Rolling update with health checks\n"
    "    2. Blue-green swap NOW\n"
    "    3. Canary 5 percent\n"
    "    4. Recreate all pods\n"
    "    5. Shadow traffic mirror\n"
    "    6. Manual stepwise promote\n"
    "    7. Abort and hold current\n"
    "    8. Roll back previous release\n"
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_claude_select_prompt_recommended_option() -> None:
    parsed = aq.parse_question(_FIXTURE_CLAUDE_SELECT)
    assert parsed is not None
    assert parsed["question_text"] == "Do you want to proceed?"
    assert len(parsed["options"]) == 2
    assert parsed["options"][0] == {"nr": 1, "label": "Yes", "recommended": True}
    assert parsed["options"][1]["nr"] == 2
    assert parsed["options"][1]["label"] == "No, and tell Claude what to do differently"
    assert parsed["options"][1]["recommended"] is False


def test_parse_yn_prompt() -> None:
    parsed = aq.parse_question(_FIXTURE_YN)
    assert parsed is not None
    assert "Allow network access" in parsed["question_text"]
    assert parsed["options"] == [
        {"nr": "y", "label": "yes"},
        {"nr": "n", "label": "no"},
    ]


def test_parse_bare_question_no_options() -> None:
    parsed = aq.parse_question(_FIXTURE_BARE_Q)
    assert parsed is not None
    assert parsed["question_text"].endswith("?")
    assert parsed["options"] == []


def test_parse_non_question_returns_none() -> None:
    assert aq.parse_question(_FIXTURE_NOT_QUESTION) is None
    assert aq.parse_question("") is None
    assert aq.parse_question("hello world\nplain line") is None


def test_fingerprint_stable_and_pane_scoped() -> None:
    p = aq.parse_question(_FIXTURE_CLAUDE_SELECT)
    assert p is not None
    fp1 = aq.compute_fingerprint("%1", p["region"])
    fp2 = aq.compute_fingerprint("%1", p["region"])
    fp_other = aq.compute_fingerprint("%2", p["region"])
    assert fp1 == fp2
    assert len(fp1) == 64
    assert fp1 != fp_other


def test_parse_long_select_full_options_and_marker_insensitive_fp() -> None:
    """F4/F6: long option block from full tail; cursor marker must not churn fp."""
    parsed = aq.parse_question(_FIXTURE_LONG_SELECT)
    assert parsed is not None
    assert parsed["question_text"] == "Choose a deployment strategy for production?"
    assert len(parsed["options"]) == 8
    assert parsed["options"][0] == {
        "nr": 1,
        "label": "Rolling update with health checks",
        "recommended": True,
    }
    assert parsed["options"][7]["nr"] == 8
    assert parsed["options"][7]["label"] == "Roll back previous release"
    assert parsed["options"][7]["recommended"] is False
    # Semantic region has no cursor markers / leading spaces.
    assert "❯" not in parsed["region"]
    assert "›" not in parsed["region"]
    assert parsed["region"].startswith("Choose a deployment strategy for production?")

    fp_cursor1 = aq.compute_fingerprint("%long", parsed["region"])
    parsed_c3 = aq.parse_question(_FIXTURE_LONG_SELECT_CURSOR3)
    assert parsed_c3 is not None
    assert len(parsed_c3["options"]) == 8
    assert parsed_c3["options"][2]["recommended"] is True
    assert parsed_c3["options"][0]["recommended"] is False
    fp_cursor3 = aq.compute_fingerprint("%long", parsed_c3["region"])
    assert fp_cursor1 == fp_cursor3

    parsed_label = aq.parse_question(_FIXTURE_LONG_SELECT_LABEL_CHANGE)
    assert parsed_label is not None
    fp_label = aq.compute_fingerprint("%long", parsed_label["region"])
    assert fp_label != fp_cursor1


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@pytest.fixture
def qdb(tmp_path: Path) -> Path:
    return tmp_path / "question_events.db"


def test_store_schema_init_idempotent_and_list_filter(qdb: Path) -> None:
    conn1 = aq.connect(db_path=qdb)
    conn1.close()
    conn2 = aq.connect(db_path=qdb)
    conn2.close()

    aq.insert_question_event(
        session="work",
        window="claude",
        pane_id="%1",
        fingerprint="fp-open",
        question_text="Open one?",
        options=[{"nr": 1, "label": "Yes", "recommended": True}],
        kind="claude",
        db_path=qdb,
    )
    # Expire the first open event, then insert a fresh one for status filter.
    assert aq.expire_open_events({"%1"}, db_path=qdb) == 1

    aq.insert_question_event(
        session="work",
        window="claude",
        pane_id="%2",
        fingerprint="fp-open-2",
        question_text="Still open?",
        db_path=qdb,
    )

    opens = aq.list_question_events(status="open", db_path=qdb)
    expired = aq.list_question_events(status="expired", db_path=qdb)
    assert len(opens) == 1
    assert opens[0]["question_text"] == "Still open?"
    assert opens[0]["options"] == []
    assert len(expired) == 1
    assert expired[0]["fingerprint"] == "fp-open"
    assert isinstance(opens[0]["options"], list)


def test_store_options_json_roundtrip(qdb: Path) -> None:
    opts = [
        {"nr": 1, "label": "Yes", "recommended": True},
        {"nr": 2, "label": "No", "recommended": False},
    ]
    aq.insert_question_event(
        session="work",
        window="claude",
        pane_id="%9",
        fingerprint="fp-opts",
        question_text="Proceed?",
        options=opts,
        db_path=qdb,
    )
    rows = aq.list_question_events(status="open", db_path=qdb)
    assert rows[0]["options"] == opts


def test_store_unique_open_pane_fingerprint_idempotent(qdb: Path) -> None:
    """F2/F6: partial unique index → second insert same pane+fp returns None."""
    first = aq.insert_question_event(
        session="work",
        window="claude",
        pane_id="%u",
        fingerprint="fp-unique",
        question_text="Unique?",
        options=[{"nr": 1, "label": "Yes", "recommended": True}],
        db_path=qdb,
    )
    assert first is not None
    second = aq.insert_question_event(
        session="work",
        window="claude",
        pane_id="%u",
        fingerprint="fp-unique",
        question_text="Unique again?",
        db_path=qdb,
    )
    assert second is None
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    assert opens[0]["question_text"] == "Unique?"

    assert aq.expire_open_events({"%u"}, db_path=qdb) == 1
    third = aq.insert_question_event(
        session="work",
        window="claude",
        pane_id="%u",
        fingerprint="fp-unique",
        question_text="Unique reopened?",
        db_path=qdb,
    )
    assert third is not None
    opens2 = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens2) == 1
    assert opens2[0]["question_text"] == "Unique reopened?"


# ---------------------------------------------------------------------------
# Ingestor with stubbed overview
# ---------------------------------------------------------------------------


class _StubService:
    def __init__(self, windows: list[dict[str, Any]], now: float | None = None) -> None:
        self._windows = windows
        self._now = now if now is not None else time.time()

    def overview(self, *, tail_lines: int = 10) -> dict[str, Any]:
        return {"now": int(self._now), "windows": list(self._windows)}


def _frage_window(
    *,
    pane_id: str = "%1",
    tail: str = _FIXTURE_CLAUDE_SELECT,
    activity: float | None = None,
    now: float | None = None,
    session: str = "work",
    window: str = "claude",
    command: str = "claude",
    state: str = "frage",
) -> dict[str, Any]:
    t = time.time() if now is None else float(now)
    if activity is None:
        activity = t - 10.0  # age ~10s > 3s threshold
    return {
        "session": session,
        "window": window,
        "active": True,
        "pane_id": pane_id,
        "pid": 4242,
        "command": command,
        "cwd": "/tmp/proj",
        "dead": False,
        "activity": int(activity),
        "managed": True,
        "tail": tail,
        "state": state,
        "state_source": "heuristic",
    }


def test_ingestor_stability_then_create_and_idempotent(qdb: Path) -> None:
    now = 1_700_000_000.0
    win = _frage_window(now=now, activity=now - 10)
    service = _StubService([win], now=now)
    ing = aq.QuestionScrapeIngestor(
        db_path=qdb,
        service_factory=lambda: service,
        now=lambda: now,
    )

    s1 = ing.poll_once()
    assert s1["created"] == 0
    assert s1["pending"] >= 1
    assert aq.list_question_events(status="open", db_path=qdb) == []

    s2 = ing.poll_once()
    assert s2["created"] == 1
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    assert opens[0]["question_text"] == "Do you want to proceed?"
    assert len(opens[0]["options"]) == 2
    assert opens[0]["options"][0]["recommended"] is True
    assert opens[0]["kind"] == "claude"
    assert opens[0]["pane_id"] == "%1"

    s3 = ing.poll_once()
    assert s3["created"] == 0
    assert s3["idempotent"] == 1
    assert len(aq.list_question_events(status="open", db_path=qdb)) == 1


def test_ingestor_skips_when_activity_age_too_fresh(qdb: Path) -> None:
    now = 1_700_000_100.0
    win = _frage_window(now=now, activity=now - 1)  # age = 1s <= 3
    service = _StubService([win], now=now)
    ing = aq.QuestionScrapeIngestor(
        db_path=qdb,
        service_factory=lambda: service,
        now=lambda: now,
    )
    assert ing.poll_once()["created"] == 0
    s2 = ing.poll_once()
    assert s2["created"] == 0
    assert s2["skipped_age"] >= 1
    assert aq.list_question_events(status="open", db_path=qdb) == []


def test_ingestor_fingerprint_change_supersedes(qdb: Path) -> None:
    """F3: supersede only after the new fingerprint is stable (poll 2 of change)."""
    now = 1_700_000_200.0
    win_a = _frage_window(tail=_FIXTURE_CLAUDE_SELECT, now=now, activity=now - 10)
    service = _StubService([win_a], now=now)
    ing = aq.QuestionScrapeIngestor(
        db_path=qdb,
        service_factory=lambda: service,
        now=lambda: now,
    )
    ing.poll_once()
    ing.poll_once()
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    old_fp = opens[0]["fingerprint"]

    win_b = _frage_window(tail=_FIXTURE_CLAUDE_SELECT_V2, now=now, activity=now - 10)
    service._windows = [win_b]

    s_change = ing.poll_once()  # first observation of new fp: old still open
    assert s_change["superseded"] == 0
    assert s_change["created"] == 0
    opens_mid = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens_mid) == 1
    assert opens_mid[0]["fingerprint"] == old_fp
    assert aq.list_question_events(status="superseded", db_path=qdb) == []

    s_stable = ing.poll_once()  # second poll with same new fp → supersede + create
    assert s_stable["superseded"] >= 1
    assert s_stable["created"] == 1
    supers = aq.list_question_events(status="superseded", db_path=qdb)
    assert any(r["fingerprint"] == old_fp for r in supers)
    opens2 = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens2) == 1
    assert opens2[0]["fingerprint"] != old_fp
    assert "Something else" in opens2[0]["options"][2]["label"]


def test_ingestor_expires_when_frage_disappears(qdb: Path) -> None:
    """F1: two-poll expiry — state flip to idle needs a second poll to expire."""
    now = 1_700_000_300.0
    win = _frage_window(now=now, activity=now - 10)
    service = _StubService([win], now=now)
    ing = aq.QuestionScrapeIngestor(
        db_path=qdb,
        service_factory=lambda: service,
        now=lambda: now,
    )
    ing.poll_once()
    ing.poll_once()
    assert len(aq.list_question_events(status="open", db_path=qdb)) == 1

    # Pane still listed but no longer in frage state
    gone = dict(win)
    gone["state"] = "idle"
    gone["tail"] = _FIXTURE_NOT_QUESTION
    service._windows = [gone]

    s1 = ing.poll_once()
    assert s1["expired"] == 0
    assert len(aq.list_question_events(status="open", db_path=qdb)) == 1

    s2 = ing.poll_once()
    assert s2["expired"] >= 1
    assert aq.list_question_events(status="open", db_path=qdb) == []
    assert len(aq.list_question_events(status="expired", db_path=qdb)) == 1


def test_ingestor_empty_snapshot_skips_expiry(qdb: Path) -> None:
    """F1/F6: empty overview must not expire open events (transient tmux fail)."""
    now = 1_700_000_350.0
    win = _frage_window(now=now, activity=now - 10)
    service = _StubService([win], now=now)
    ing = aq.QuestionScrapeIngestor(
        db_path=qdb,
        service_factory=lambda: service,
        now=lambda: now,
    )
    ing.poll_once()
    ing.poll_once()
    assert len(aq.list_question_events(status="open", db_path=qdb)) == 1

    service._windows = []
    s = ing.poll_once()
    assert s["windows"] == 0
    assert s["skipped_expiry_empty_snapshot"] == 1
    assert s["expired"] == 0
    assert len(aq.list_question_events(status="open", db_path=qdb)) == 1


def test_ingestor_ignores_non_work_session(qdb: Path) -> None:
    now = 1_700_000_400.0
    win = _frage_window(session="other", now=now, activity=now - 10)
    service = _StubService([win], now=now)
    ing = aq.QuestionScrapeIngestor(
        db_path=qdb,
        service_factory=lambda: service,
        now=lambda: now,
    )
    ing.poll_once()
    ing.poll_once()
    assert aq.list_question_events(status="open", db_path=qdb) == []


def test_ingestor_filters_only_work_when_mixed(qdb: Path) -> None:
    now = 1_700_000_450.0
    work = _frage_window(session="work", pane_id="%w", now=now, activity=now - 10)
    other = _frage_window(session="other", pane_id="%o", now=now, activity=now - 10)
    service = _StubService([work, other], now=now)
    ing = aq.QuestionScrapeIngestor(
        db_path=qdb,
        service_factory=lambda: service,
        now=lambda: now,
    )
    ing.poll_once()
    ing.poll_once()
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    assert opens[0]["pane_id"] == "%w"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def test_endpoint_options_json_corruption_falls_back_to_empty(qdb: Path) -> None:
    """Corrupt options_json must decode to [] in the endpoint payload."""
    from hermes_cli.sqlite_util import write_txn

    aq.insert_question_event(
        session="work",
        window="claude",
        pane_id="%e",
        fingerprint="fp-ep",
        question_text="Ship it?",
        options=[{"nr": 1, "label": "Yes", "recommended": True}],
        db_path=qdb,
    )
    # Corrupt options_json path: write junk then ensure decode falls back to []
    with aq.connect_closing(db_path=qdb) as conn:
        with write_txn(conn):
            conn.execute(
                "UPDATE question_events SET options_json = ? WHERE fingerprint = ?",
                ("NOT-JSON", "fp-ep"),
            )
    payload = {"questions": aq.list_question_events(status="open", db_path=qdb)}
    assert len(payload["questions"]) == 1
    assert payload["questions"][0]["options"] == []


def test_endpoint_real_route_get_agent_questions(qdb: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the REAL web_server route (same pattern as
    test_web_server_agent_terminals.py: TestClient without lifespan)."""
    from fastapi.testclient import TestClient

    import hermes_cli.web_server as web_server

    # Point the store at the isolated test DB for the route's default db_path.
    monkeypatch.setattr(aq, "question_events_db_path", lambda: qdb)

    aq.insert_question_event(
        session="work",
        window="claude",
        pane_id="%api",
        fingerprint="fp-api",
        question_text="Approve deploy?",
        options=[{"nr": 1, "label": "Yes", "recommended": True}],
        db_path=qdb,
    )

    client = TestClient(web_server.app)
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    resp = client.get(
        "/api/agent-questions", params={"status": "open", "limit": 10}, headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "questions" in body
    assert body["questions"][0]["question_text"] == "Approve deploy?"
    assert body["questions"][0]["options"][0]["recommended"] is True


# ---------------------------------------------------------------------------
# Real tmux E2E
# ---------------------------------------------------------------------------

pytestmark_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is required"
)


@pytest.fixture
def tmux_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TmuxAgentSessionService, None, None]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    socket = tmp_path / "tmux.sock"
    service = TmuxAgentSessionService(socket_path=socket, hermes_home=home)
    yield service
    subprocess.run(
        ["tmux", "-S", str(socket), "kill-server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytestmark_tmux
def test_real_tmux_e2e_stable_open_then_expire(
    tmp_path: Path,
    tmux_service: TmuxAgentSessionService,
) -> None:
    db_path = tmp_path / "home" / "question_events.db"
    prompt_file = tmp_path / "select_prompt.txt"
    # Write verbatim fixture for cat into the pane
    prompt_file.write_text(_FIXTURE_CLAUDE_SELECT, encoding="utf-8")

    # Isolated work session: print fixture then sleep so activity ages
    tmux_service._run(
        "new-session",
        "-d",
        "-s",
        "work",
        "-n",
        "claude",
        "sh",
        "-c",
        f"cat {prompt_file}; sleep 120",
    )
    # Companion non-frage window: killing the only window empties the overview,
    # and F1 skips expiry on empty snapshots (indistinguishable from tmux failure).
    tmux_service._run(
        "new-window",
        "-t",
        "work",
        "-n",
        "idle",
        "sh",
        "-c",
        "sleep 120",
    )
    # Let tmux settle and activity age past the 3s threshold
    time.sleep(4.0)

    overview = tmux_service.overview(tail_lines=25)
    work_windows = [w for w in overview["windows"] if w.get("session") == "work"]
    assert work_windows, f"expected work windows, got {overview}"
    # At least one should classify as frage given the fixture
    frage = [w for w in work_windows if w.get("state") == "frage"]
    assert frage, f"expected frage state from fixture; windows={work_windows}"

    ing = aq.QuestionScrapeIngestor(
        db_path=db_path,
        service_factory=lambda: tmux_service,
    )
    s1 = ing.poll_once()
    assert s1["created"] == 0  # first poll: pending only
    s2 = ing.poll_once()
    assert s2["created"] == 1, f"expected create on second stable poll: {s1=} {s2=}"

    opens = aq.list_question_events(status="open", db_path=db_path)
    assert len(opens) == 1
    assert opens[0]["question_text"] == "Do you want to proceed?"
    assert len(opens[0]["options"]) == 2
    assert opens[0]["options"][0]["recommended"] is True
    assert opens[0]["session"] == "work"

    # Third identical poll: idempotent
    s3 = ing.poll_once()
    assert s3["created"] == 0
    assert s3["idempotent"] >= 1
    assert len(aq.list_question_events(status="open", db_path=db_path)) == 1

    # Remove the frage pane → two-poll expire (F1)
    tmux_service._run("kill-window", "-t", "work:claude")
    s4 = ing.poll_once()
    assert s4["expired"] == 0
    assert len(aq.list_question_events(status="open", db_path=db_path)) == 1
    s5 = ing.poll_once()
    assert s5["expired"] >= 1
    assert aq.list_question_events(status="open", db_path=db_path) == []
    assert len(aq.list_question_events(status="expired", db_path=db_path)) == 1


def test_start_poller_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_AGENT_QUESTIONS_POLL", "0")
    aq.stop_poller()
    assert aq.start_poller(interval_s=60.0) is False


def test_start_poller_skipped_under_pytest_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifespan runs inside `with TestClient(app)` across the suite — without
    the pytest guard a real poller would scrape live tmux and, after fixture
    teardown, write into the live $HERMES_HOME store."""
    monkeypatch.delenv("HERMES_AGENT_QUESTIONS_POLL", raising=False)
    aq.stop_poller()
    assert aq.start_poller(interval_s=60.0) is False


def test_start_poller_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_AGENT_QUESTIONS_POLL", "1")  # force past pytest guard
    aq.stop_poller()

    class _QuietIngestor(aq.QuestionScrapeIngestor):
        def poll_once(self):
            return {"created": 0}

    monkeypatch.setattr(aq, "QuestionScrapeIngestor", _QuietIngestor)
    assert aq.start_poller(interval_s=60.0, db_path=tmp_path / "q.db") is True
    assert aq.start_poller(interval_s=60.0, db_path=tmp_path / "q.db") is False
    aq.stop_poller()
