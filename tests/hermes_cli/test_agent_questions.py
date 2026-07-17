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

# VERBATIM long prompt: question + 8 long option labels, total length > 600.
# overview() would have cut at [-600:] and lost option 1 + question text;
# direct capture_pane must keep all 8 options for ingest/recheck fingerprint match.
_FIXTURE_OVER_600_SELECT = (
    "  Which of the following deployment and release strategies should we apply "
    "to the production Kubernetes cluster for the multi-region customer-facing "
    "API gateway during the next scheduled maintenance window this quarter?\n"
    "  ❯ 1. Rolling update with progressive health checks, automated rollback on "
    "SLO burn-rate alerts, and staggered pod disruption budgets across zones\n"
    "    2. Blue-green swap with full traffic drain validation, canary smoke on "
    "the green pool, and instant DNS cutover only after synthetic checks pass\n"
    "    3. Canary five percent traffic for thirty minutes with error-budget "
    "guardrails, then automatic promotion in ten-percent steps to one hundred\n"
    "    4. Recreate all pods in a controlled wave with pre-flight capacity "
    "checks, temporary replica boost, and post-wave readiness gate enforcement\n"
    "    5. Shadow traffic mirror of production requests into a dark cluster "
    "for parity comparison without user impact before any live cutover begins\n"
    "    6. Manual stepwise promote with operator approval gates between each "
    "region and an explicit hold-point after the first region succeeds cleanly\n"
    "    7. Abort and hold the current production release unchanged while the "
    "incident bridge investigates residual risk from the previous deploy attempt\n"
    "    8. Roll back to the previous known-good release tag immediately and "
    "freeze further deploys until a postmortem action item is fully completed\n"
)
assert len(_FIXTURE_OVER_600_SELECT) > 600


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


def test_parse_stale_select_above_fresh_yn_is_not_picked() -> None:
    """A scrolled-up, already-answered select block above a fresh bottom y/n
    question must not be parsed/fingerprinted as the standing question."""
    stale_then_yn = (
        "  Do you want to proceed?\n"
        "  ❯ 1. Yes\n"
        "    2. No, and tell Claude what to do differently\n"
        "  chosen: 1\n"
        "  running build step 1 …\n"
        "  running build step 2 …\n"
        "  running build step 3 …\n"
        "  running build step 4 …\n"
        "  running build step 5 …\n"
        "  build done.\n"
        "Allow network access for this tool? (y/n)\n"
    )
    parsed = aq.parse_question(stale_then_yn)
    assert parsed is not None
    assert parsed["options"] == [
        {"nr": "y", "label": "yes"},
        {"nr": "n", "label": "no"},
    ]
    assert "Allow network access" in parsed["question_text"]
    assert "Do you want to proceed?" not in parsed["region"]

    # And the fingerprint differs from the stale select prompt's fingerprint.
    stale_parsed = aq.parse_question(_FIXTURE_CLAUDE_SELECT)
    assert stale_parsed is not None
    fp_yn = aq.compute_fingerprint("%s", parsed["region"])
    fp_stale = aq.compute_fingerprint("%s", stale_parsed["region"])
    assert fp_yn != fp_stale


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


def test_supersede_and_insert_single_transaction(qdb: Path) -> None:
    """Supersede+insert happen atomically; repeat with same fp is idempotent."""
    aq.insert_question_event(
        session="work",
        window="claude",
        pane_id="%a",
        fingerprint="fp-old",
        question_text="Old?",
        db_path=qdb,
    )
    n_super, new_id = aq.supersede_and_insert(
        session="work",
        window="claude",
        pane_id="%a",
        fingerprint="fp-new",
        question_text="New?",
        db_path=qdb,
    )
    assert n_super == 1
    assert isinstance(new_id, int)
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    assert opens[0]["fingerprint"] == "fp-new"
    assert len(aq.list_question_events(status="superseded", db_path=qdb)) == 1

    # Same fingerprint again: nothing superseded, insert ignored.
    n_super2, new_id2 = aq.supersede_and_insert(
        session="work",
        window="claude",
        pane_id="%a",
        fingerprint="fp-new",
        question_text="New?",
        db_path=qdb,
    )
    assert n_super2 == 0
    assert new_id2 is None
    assert len(aq.list_question_events(status="open", db_path=qdb)) == 1


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
# Ingestor with stubbed list_windows + capture_pane (not overview)
# ---------------------------------------------------------------------------


class _StubService:
    """Stub matching the ingest path: list_windows() + capture_pane().

    ``windows`` entries are plain dicts (same shape as TmuxWindow.to_dict())
    with an extra ``tail`` field used as the capture_pane return value.
    Optional ``capture_errors`` pane_ids raise on capture (per-pane skip).
    ``extra_captures`` / ``capture_raises`` cover cross-session pane ids that
    are not listed in list_windows (I2 hook expiry).
    """

    def __init__(
        self,
        windows: list[dict[str, Any]],
        now: float | None = None,
        *,
        capture_errors: set[str] | None = None,
        extra_captures: dict[str, str] | None = None,
        capture_raises: dict[str, BaseException] | None = None,
    ) -> None:
        self._windows = windows
        self._now = now if now is not None else time.time()
        self._capture_errors = set(capture_errors or ())
        self._extra_captures = dict(extra_captures or {})
        self._capture_raises = dict(capture_raises or {})
        self.capture_starts: list[int] = []

    def list_windows(self, session: str | None = None) -> list[dict[str, Any]]:
        wins = list(self._windows)
        if session is not None:
            wins = [w for w in wins if w.get("session") == session]
        return wins

    def capture_pane(self, pane_id: str, *, start: int = -25) -> str:
        self.capture_starts.append(int(start))
        if pane_id in self._capture_raises:
            raise self._capture_raises[pane_id]
        if pane_id in self._capture_errors:
            raise RuntimeError(f"capture failed for {pane_id}")
        if pane_id in self._extra_captures:
            return str(self._extra_captures[pane_id])
        for w in self._windows:
            if str(w.get("pane_id")) == pane_id:
                return str(w.get("tail") or "")
        return ""

    def send_keys_to_pane(self, pane_id: str, text: str, *, enter: bool = False) -> None:
        return None


def _frage_window(
    *,
    pane_id: str = "%1",
    tail: str = _FIXTURE_CLAUDE_SELECT,
    activity: float | None = None,
    now: float | None = None,
    session: str = "work",
    window: str = "claude",
    command: str = "claude",
    dead: bool = False,
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
        "dead": dead,
        "activity": int(activity),
        "managed": True,
        "tail": tail,
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
    """F1: two-poll expiry — capture no longer classifies as frage → expire."""
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

    # Pane still listed but capture no longer classifies as frage
    gone = dict(win)
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
    """F1/F6: empty list_windows must not expire open events (transient tmux fail)."""
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


def test_ingestor_persistently_empty_snapshots_eventually_expire(qdb: Path) -> None:
    """Truly gone windows (>= 3 consecutive empty list_windows) must not leave
    events open forever; expiry stays two-poll confirmed on top."""
    now = 1_700_000_360.0
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
    s1 = ing.poll_once()  # empty #1: skipped
    s2 = ing.poll_once()  # empty #2: skipped
    assert s1["skipped_expiry_empty_snapshot"] == 1
    assert s2["skipped_expiry_empty_snapshot"] == 1
    assert len(aq.list_question_events(status="open", db_path=qdb)) == 1

    s3 = ing.poll_once()  # empty #3: expiry pass runs → candidate pending
    assert s3["skipped_expiry_empty_snapshot"] == 0
    assert len(aq.list_question_events(status="open", db_path=qdb)) == 1

    s4 = ing.poll_once()  # empty #4: two-poll confirmed → expired
    assert s4["expired"] == 1
    assert aq.list_question_events(status="open", db_path=qdb) == []


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


def test_ingestor_answer_cooldown_blocks_reinsert_then_allows(qdb: Path) -> None:
    """R1: answered pane+fp within 60s must not open a duplicate; after 61s may."""
    now = 1_700_000_500.0
    win = _frage_window(now=now, activity=now - 10)
    service = _StubService([win], now=now)
    clock = {"t": now}

    def _now() -> float:
        return float(clock["t"])

    ing = aq.QuestionScrapeIngestor(
        db_path=qdb,
        service_factory=lambda: service,
        now=_now,
    )
    ing.poll_once()
    assert ing.poll_once()["created"] == 1
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    eid = int(opens[0]["id"])
    fp = opens[0]["fingerprint"]
    pane_id = opens[0]["pane_id"]

    # Claim as answered at current clock (same standing capture remains).
    assert aq._claim_event(
        eid, answer="1", answered_by="operator", db_path=qdb, now=clock["t"]
    )
    assert aq.list_question_events(status="open", db_path=qdb) == []

    s_cd1 = ing.poll_once()
    s_cd2 = ing.poll_once()
    assert s_cd1["created"] == 0
    assert s_cd2["created"] == 0
    assert s_cd1["cooldown"] + s_cd2["cooldown"] >= 1
    assert aq.list_question_events(status="open", db_path=qdb) == []
    assert aq.recently_answered(pane_id, fp, db_path=qdb, now=clock["t"]) is True

    # After 61s the same standing question may reappear (send was ineffective).
    clock["t"] = now + 61.0
    # activity still aged relative to the new clock
    service._windows = [
        _frage_window(now=clock["t"], activity=clock["t"] - 10, pane_id=pane_id)
    ]
    # Pending still holds the same fp from cooldown polls → may create on first
    # post-cooldown tick (already stable); second poll is idempotent either way.
    s_late1 = ing.poll_once()
    s_late2 = ing.poll_once()
    assert s_late1["cooldown"] == 0
    assert s_late1["created"] + s_late2["created"] == 1, (
        f"expected re-create after cooldown: {s_late1=} {s_late2=}"
    )
    opens2 = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens2) == 1
    assert opens2[0]["fingerprint"] == fp
    assert opens2[0]["id"] != eid


def test_ingestor_long_prompt_over_600_keeps_all_options_and_recheck_matches(
    qdb: Path,
) -> None:
    """R2: ingest keeps all 8 options (>600 chars); recheck fp matches → not superseded."""
    assert len(_FIXTURE_OVER_600_SELECT) > 600
    now = 1_700_000_550.0
    win = _frage_window(
        pane_id="%long600",
        tail=_FIXTURE_OVER_600_SELECT,
        now=now,
        activity=now - 10,
    )
    service = _StubService([win], now=now)
    ing = aq.QuestionScrapeIngestor(
        db_path=qdb,
        service_factory=lambda: service,
        now=lambda: now,
    )
    assert ing.poll_once()["created"] == 0
    assert ing.poll_once()["created"] == 1
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    event = opens[0]
    assert len(event["options"]) == 8
    assert event["options"][0]["nr"] == 1
    assert "Rolling update" in event["options"][0]["label"]
    assert event["options"][7]["nr"] == 8
    assert "Roll back" in event["options"][7]["label"]
    assert "multi-region" in event["question_text"]

    # Recheck fingerprint over the same stub capture must match stored fp.
    recheck_fp = aq._recheck_fingerprint(service, "%long600")
    assert recheck_fp == event["fingerprint"]
    # capture_pane start must be -25 for both ingest and recheck
    assert all(s == -25 for s in service.capture_starts)

    svc_ans = _RecordingService([_FIXTURE_OVER_600_SELECT, ""])
    result = aq.answer_question(
        int(event["id"]),
        "1",
        db_path=qdb,
        service=svc_ans,
        verify_delay_s=0,
        sleep=lambda _s: None,
    )
    assert result["ok"] is True, result
    assert result.get("reason") != "superseded"
    assert aq.list_question_events(status="superseded", db_path=qdb) == []
    answered = aq.list_question_events(status="answered", db_path=qdb)
    assert len(answered) == 1
    assert answered[0]["answer"] == "1"


def test_ingestor_capture_error_skips_pane_counts_and_continues(qdb: Path) -> None:
    """R2: one pane capture failure must not abort the poll; summary counts it."""
    now = 1_700_000_600.0
    bad = _frage_window(pane_id="%bad", now=now, activity=now - 10)
    good = _frage_window(pane_id="%good", now=now, activity=now - 10)
    service = _StubService([bad, good], now=now, capture_errors={"%bad"})
    ing = aq.QuestionScrapeIngestor(
        db_path=qdb,
        service_factory=lambda: service,
        now=lambda: now,
    )
    s1 = ing.poll_once()
    assert s1["capture_errors"] >= 1
    assert s1["created"] == 0
    s2 = ing.poll_once()
    assert s2["capture_errors"] >= 1
    assert s2["created"] == 1
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    assert opens[0]["pane_id"] == "%good"


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


# ---------------------------------------------------------------------------
# Answer path (P0b)
# ---------------------------------------------------------------------------


class _RecordingService:
    """Stub tmux service: scripted captures + records send_keys_to_pane calls."""

    def __init__(
        self,
        captures: list[str] | Exception | list[Any],
        *,
        recheck_raises: Exception | None = None,
        verify_raises: Exception | None = None,
    ) -> None:
        # captures: sequence of pane texts returned by successive capture_pane calls
        if isinstance(captures, Exception):
            self._captures: list[Any] = [captures]
        else:
            self._captures = list(captures)
        self.recheck_raises = recheck_raises
        self.verify_raises = verify_raises
        self.sent: list[dict[str, Any]] = []
        self._capture_i = 0

    def capture_pane(self, pane_id: str, *, start: int = -50) -> str:
        if self._capture_i == 0 and self.recheck_raises is not None:
            raise self.recheck_raises
        if self._capture_i >= 1 and self.verify_raises is not None:
            raise self.verify_raises
        if self._capture_i >= len(self._captures):
            # default: empty pane (question gone)
            return ""
        item = self._captures[self._capture_i]
        self._capture_i += 1
        if isinstance(item, Exception):
            raise item
        return str(item)

    def send_keys_to_pane(self, pane_id: str, text: str, *, enter: bool = False) -> None:
        self.sent.append({"pane_id": pane_id, "text": text, "enter": enter})


def _insert_open_select(
    qdb: Path,
    *,
    pane_id: str = "%42",
    kind: str = "claude",
    options: list[dict[str, Any]] | None = None,
    fingerprint: str | None = None,
    question_text: str = "Do you want to proceed?",
    fixture: str = _FIXTURE_CLAUDE_SELECT,
) -> tuple[int, str, str]:
    """Insert open select event; return (id, pane_id, fingerprint matching fixture)."""
    parsed = aq.parse_question(fixture)
    assert parsed is not None
    fp = fingerprint or aq.compute_fingerprint(pane_id, parsed["region"])
    opts = options if options is not None else parsed["options"]
    eid = aq.insert_question_event(
        session="work",
        window="claude",
        pane_id=pane_id,
        fingerprint=fp,
        question_text=question_text,
        options=opts,
        kind=kind,
        db_path=qdb,
    )
    assert eid is not None
    return int(eid), pane_id, fp


def test_answer_claim_double_click_safe(qdb: Path) -> None:
    eid, pane_id, fp = _insert_open_select(qdb)
    parsed = aq.parse_question(_FIXTURE_CLAUDE_SELECT)
    assert parsed is not None
    # recheck matches, then verify: question gone
    svc = _RecordingService([_FIXTURE_CLAUDE_SELECT, ""])
    r1 = aq.answer_question(eid, "1", db_path=qdb, service=svc, verify_delay_s=0, sleep=lambda _s: None)
    assert r1["ok"] is True
    assert r1["verified"] is True
    r2 = aq.answer_question(eid, "1", db_path=qdb, service=svc, verify_delay_s=0, sleep=lambda _s: None)
    assert r2 == {"ok": False, "reason": "not-open"}
    answered = aq.list_question_events(status="answered", db_path=qdb)
    assert len(answered) == 1
    assert answered[0]["answer"] == "1"
    assert len(svc.sent) == 1


def test_answer_recheck_mismatch_supersedes_no_send(qdb: Path) -> None:
    eid, _pane, _fp = _insert_open_select(qdb, fingerprint="fp-stale-mismatch")
    # Recheck sees a *different* standing prompt → supersede, no send
    svc = _RecordingService([_FIXTURE_CLAUDE_SELECT_V2])
    result = aq.answer_question(
        eid, "1", db_path=qdb, service=svc, verify_delay_s=0, sleep=lambda _s: None
    )
    assert result == {"ok": False, "reason": "superseded"}
    assert svc.sent == []
    assert aq.list_question_events(status="open", db_path=qdb) == []
    supers = aq.list_question_events(status="superseded", db_path=qdb)
    assert len(supers) == 1
    assert supers[0]["id"] == eid


def test_answer_recheck_capture_error_rolls_back_to_open(qdb: Path) -> None:
    eid, _pane, _fp = _insert_open_select(qdb)
    svc = _RecordingService([], recheck_raises=RuntimeError("tmux capture failed"))
    result = aq.answer_question(
        eid, "1", db_path=qdb, service=svc, verify_delay_s=0, sleep=lambda _s: None
    )
    assert result == {"ok": False, "reason": "recheck-failed"}
    assert svc.sent == []
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    assert opens[0]["id"] == eid
    assert opens[0].get("answer") is None


def test_answer_dialect_claude_select_and_default_yn(qdb: Path) -> None:
    # claude select → digit, enter=False
    eid_c, pane_c, _fp = _insert_open_select(qdb, pane_id="%c1", kind="claude")
    svc_c = _RecordingService([_FIXTURE_CLAUDE_SELECT, ""])
    r_c = aq.answer_question(
        eid_c, "1", db_path=qdb, service=svc_c, verify_delay_s=0, sleep=lambda _s: None
    )
    assert r_c["ok"] is True
    assert svc_c.sent == [{"pane_id": pane_c, "text": "1", "enter": False}]

    # default yn → "y" + Enter
    parsed_yn = aq.parse_question(_FIXTURE_YN)
    assert parsed_yn is not None
    pane_y = "%y1"
    fp_y = aq.compute_fingerprint(pane_y, parsed_yn["region"])
    eid_y = aq.insert_question_event(
        session="work",
        window="claude",
        pane_id=pane_y,
        fingerprint=fp_y,
        question_text=parsed_yn["question_text"],
        options=parsed_yn["options"],
        kind="unknown",  # falls through to default dialect
        db_path=qdb,
    )
    assert eid_y is not None
    svc_y = _RecordingService([_FIXTURE_YN, ""])
    r_y = aq.answer_question(
        int(eid_y), "y", db_path=qdb, service=svc_y, verify_delay_s=0, sleep=lambda _s: None
    )
    assert r_y["ok"] is True
    assert svc_y.sent == [{"pane_id": pane_y, "text": "y", "enter": True}]


def test_answer_invalid_option_stays_open(qdb: Path) -> None:
    eid, _pane, _fp = _insert_open_select(qdb)
    svc = _RecordingService([_FIXTURE_CLAUDE_SELECT])
    result = aq.answer_question(
        eid, "7", db_path=qdb, service=svc, verify_delay_s=0, sleep=lambda _s: None
    )
    assert result["ok"] is False
    assert result["reason"] == "invalid-option"
    assert svc.sent == []
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    assert opens[0]["id"] == eid


def test_answer_verify_true_and_false(qdb: Path) -> None:
    # verified True: question gone after send
    eid1, _p1, _f1 = _insert_open_select(qdb, pane_id="%v1")
    svc_ok = _RecordingService([_FIXTURE_CLAUDE_SELECT, ""])
    r_ok = aq.answer_question(
        eid1, "1", db_path=qdb, service=svc_ok, verify_delay_s=0, sleep=lambda _s: None
    )
    assert r_ok["ok"] is True
    assert r_ok["verified"] is True
    assert r_ok["latency_s"] >= 0
    row1 = aq.list_question_events(status="answered", db_path=qdb)[0]
    assert row1["answer_verified"] == 1
    assert row1["latency_s"] is not None and row1["latency_s"] >= 0

    # verified False: same prompt still standing
    eid2, _p2, _f2 = _insert_open_select(qdb, pane_id="%v2")
    svc_still = _RecordingService([_FIXTURE_CLAUDE_SELECT, _FIXTURE_CLAUDE_SELECT])
    r_still = aq.answer_question(
        eid2, "1", db_path=qdb, service=svc_still, verify_delay_s=0, sleep=lambda _s: None
    )
    assert r_still["ok"] is True
    assert r_still["verified"] is False
    rows = [e for e in aq.list_question_events(status="answered", db_path=qdb) if e["id"] == eid2]
    assert len(rows) == 1
    assert rows[0]["answer_verified"] == 0


def test_answer_free_text_not_supported(qdb: Path) -> None:
    eid = aq.insert_question_event(
        session="work",
        window="claude",
        pane_id="%ft",
        fingerprint="fp-ft",
        question_text="What is the deployment target?",
        options=[],
        kind="claude",
        db_path=qdb,
    )
    assert eid is not None
    svc = _RecordingService([])
    result = aq.answer_question(
        int(eid), "prod", db_path=qdb, service=svc, verify_delay_s=0, sleep=lambda _s: None
    )
    assert result == {"ok": False, "reason": "free-text-not-supported"}
    assert svc.sent == []


def test_endpoint_answer_success_and_conflict(
    qdb: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    import hermes_cli.web_server as web_server

    monkeypatch.setattr(aq, "question_events_db_path", lambda: qdb)

    eid, _pane_id, _fp = _insert_open_select(qdb, pane_id="%api-ans")
    svc = _RecordingService([_FIXTURE_CLAUDE_SELECT, ""])
    real_answer = aq.answer_question

    def _answer(event_id, answer, **kwargs):
        # Route does not pass db_path/service; inject isolated store + stub.
        return real_answer(
            event_id,
            answer,
            answered_by=kwargs.get("answered_by", "operator"),
            db_path=qdb,
            service=svc,
            verify_delay_s=0,
            sleep=lambda _s: None,
        )

    monkeypatch.setattr(aq, "answer_question", _answer)

    client = TestClient(web_server.app)
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}

    ok = client.post(
        f"/api/agent-questions/{eid}/answer",
        json={"answer": "1"},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["ok"] is True
    assert body["verified"] is True
    assert svc.sent and svc.sent[0]["text"] == "1"

    conflict = client.post(
        f"/api/agent-questions/{eid}/answer",
        json={"answer": "1"},
        headers=headers,
    )
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    assert detail["reason"] == "not-open"


@pytestmark_tmux
def test_real_tmux_e2e_answer_chosen(
    tmp_path: Path,
    tmux_service: TmuxAgentSessionService,
) -> None:
    """Full claim→recheck→send→verify path against a real pane.

    Pane runs: cat fixture; read answer; clear; echo CHOSEN:$x
    Ingestor creates the event; answer_question sends "1"+Enter (codex dialect).
    """
    db_path = tmp_path / "home" / "question_events.db"
    prompt_file = tmp_path / "select_prompt.txt"
    prompt_file.write_text(_FIXTURE_CLAUDE_SELECT, encoding="utf-8")

    tmux_service._run(
        "new-session",
        "-d",
        "-s",
        "work",
        "-n",
        "claude",
        "sh",
        "-c",
        f"cat {prompt_file}; read -r x; clear; echo \"CHOSEN:$x\"; sleep 120",
    )
    # Companion window so overview is never empty after kill paths (not used here).
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
    time.sleep(4.0)

    overview = tmux_service.overview(tail_lines=25)
    frage = [
        w
        for w in overview["windows"]
        if w.get("session") == "work" and w.get("state") == "frage"
    ]
    assert frage, f"expected frage state; windows={overview.get('windows')}"
    pane_id = str(frage[0]["pane_id"])

    ing = aq.QuestionScrapeIngestor(
        db_path=db_path,
        service_factory=lambda: tmux_service,
    )
    s1 = ing.poll_once()
    assert s1["created"] == 0
    s2 = ing.poll_once()
    assert s2["created"] == 1, f"expected create on second stable poll: {s1=} {s2=}"

    opens = aq.list_question_events(status="open", db_path=db_path)
    assert len(opens) == 1
    event_id = int(opens[0]["id"])

    # sh needs Enter; force codex dialect (select enter=True).
    with aq.connect_closing(db_path=db_path) as conn:
        from hermes_cli.sqlite_util import write_txn

        with write_txn(conn):
            conn.execute(
                "UPDATE question_events SET kind = ? WHERE id = ?",
                ("codex", event_id),
            )

    result = aq.answer_question(
        event_id,
        "1",
        db_path=db_path,
        service=tmux_service,
        verify_delay_s=0.8,
    )
    assert result["ok"] is True, result
    assert result["verified"] is True, result

    # Pane should show the chosen value after clear+echo.
    deadline = time.time() + 5.0
    chosen_seen = False
    while time.time() < deadline:
        cap = tmux_service.capture_pane(pane_id, start=-30)
        if "CHOSEN:1" in cap:
            chosen_seen = True
            break
        time.sleep(0.2)
    assert chosen_seen, f"expected CHOSEN:1 in pane; last={cap!r}"

    answered = aq.list_question_events(status="answered", db_path=db_path)
    assert len(answered) == 1
    assert answered[0]["answer"] == "1"
    assert answered[0]["answer_verified"] == 1
    assert answered[0]["latency_s"] is not None


# ---------------------------------------------------------------------------
# I2 — Hook-source ingest / resolve / merge / expiry
# ---------------------------------------------------------------------------

# VERBATIM PreToolUse payload (measured live 2026-07-17 session frage-i2).
_REAL_PRETOOLUSE_PAYLOAD: dict[str, Any] = {
    "session_id": "cb26fba6-6802-4569-8bf9-3443b914f3bd",
    "transcript_path": (
        "/home/piet/.claude/projects/-home-piet--hermes-hermes-agent/"
        "cb26fba6-6802-4569-8bf9-3443b914f3bd.jsonl"
    ),
    "cwd": "/home/piet/.hermes/hermes-agent",
    "prompt_id": "f31b5f64-2a2e-48a0-8c57-421bad03d8d6",
    "permission_mode": "bypassPermissions",
    "effort": {"level": "medium"},
    "hook_event_name": "PreToolUse",
    "tool_name": "AskUserQuestion",
    "tool_input": {
        "questions": [
            {
                "question": "Which deployment strategy should we use?",
                "header": "Strategy",
                "options": [
                    {
                        "label": "Rolling update (Recommended)",
                        "description": "Zero downtime",
                    },
                    {"label": "Blue-green", "description": "Instant rollback"},
                    {"label": "Canary", "description": "Gradual rollout"},
                ],
                "multiSelect": False,
            }
        ]
    },
    "tool_use_id": "toolu_01E5FVqcuqBPvRCWXLgrn3Sw",
}

# Store-near shape the hook script POSTs (labels stripped of "(Recommended)",
# recommended flag set; nr 1..3). Built from the real payload above.
_HOOK_STORE_OPTIONS: list[dict[str, Any]] = [
    {"nr": 1, "label": "Rolling update", "recommended": True},
    {"nr": 2, "label": "Blue-green", "recommended": False},
    {"nr": 3, "label": "Canary", "recommended": False},
]

_HOOK_QUESTION_TEXT = _REAL_PRETOOLUSE_PAYLOAD["tool_input"]["questions"][0]["question"]
_HOOK_KEY = _REAL_PRETOOLUSE_PAYLOAD["tool_use_id"]
_HOOK_ACTION_CONTEXT = "AskUserQuestion: Strategy"


def _hook_store_event(
    *,
    pane_id: str = "%80",
    session: str = "work",
    window: str = "claude",
    hook_key: str | None = _HOOK_KEY,
) -> dict[str, Any]:
    return {
        "pane_id": pane_id,
        "session": session,
        "window": window,
        "kind": "claude",
        "cwd": _REAL_PRETOOLUSE_PAYLOAD["cwd"],
        "question_text": _HOOK_QUESTION_TEXT,
        "options": list(_HOOK_STORE_OPTIONS),
        "action_context": _HOOK_ACTION_CONTEXT,
        "hook_key": hook_key,
    }


# P0 schema without I2 columns — migration fixture.
_P0_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS question_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    updated_ts    TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'scrape',
    session       TEXT NOT NULL,
    window        TEXT NOT NULL,
    pane_id       TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    kind          TEXT,
    cwd           TEXT,
    question_text TEXT NOT NULL,
    options_json  TEXT NOT NULL DEFAULT '[]',
    class         TEXT NOT NULL DEFAULT 'unknown',
    status        TEXT NOT NULL DEFAULT 'open',
    answered_by   TEXT,
    answer        TEXT,
    latency_s     REAL,
    answer_verified INTEGER,
    override      INTEGER NOT NULL DEFAULT 0
);
"""


def test_i2_migration_adds_columns_preserves_old_events(tmp_path: Path) -> None:
    """DB with P0 schema (no action_context/hook_key) → connect migrates; old rows readable."""
    import sqlite3

    qdb = tmp_path / "legacy_q.db"
    conn = sqlite3.connect(str(qdb))
    conn.executescript(_P0_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO question_events ("
        "ts, updated_ts, source, session, window, pane_id, fingerprint, "
        "question_text, options_json, class, status, override"
        ") VALUES ("
        "'2026-07-17T00:00:00.000000Z', '2026-07-17T00:00:00.000000Z', "
        "'scrape', 'work', 'claude', '%legacy', 'fp-legacy', "
        "'Legacy open?', '[]', 'unknown', 'open', 0"
        ")"
    )
    conn.commit()
    conn.close()

    aq._INITIALIZED_PATHS.discard(str(qdb.resolve()))
    with aq.connect_closing(db_path=qdb) as c2:
        cols = {str(r[1]) for r in c2.execute("PRAGMA table_info(question_events)")}
    assert "action_context" in cols
    assert "hook_key" in cols

    rows = aq.list_question_events(status="open", db_path=qdb)
    assert len(rows) == 1
    assert rows[0]["question_text"] == "Legacy open?"
    assert rows[0]["fingerprint"] == "fp-legacy"
    assert rows[0].get("action_context") is None
    assert rows[0].get("hook_key") is None


def test_i2_ingest_hook_happy_path_real_options(qdb: Path) -> None:
    result = aq.ingest_hook_event(_hook_store_event(), db_path=qdb)
    assert result["ok"] is True
    assert isinstance(result["id"], int)
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    ev = opens[0]
    assert ev["id"] == result["id"]
    assert ev["source"] == "hook"
    assert ev["question_text"] == _HOOK_QUESTION_TEXT
    assert ev["options"] == _HOOK_STORE_OPTIONS
    assert ev["options"][0]["label"] == "Rolling update"
    assert ev["options"][0]["recommended"] is True
    assert "(Recommended)" not in ev["options"][0]["label"]
    assert ev["action_context"] == _HOOK_ACTION_CONTEXT
    assert ev["hook_key"] == _HOOK_KEY
    assert ev["kind"] == "claude"
    assert ev["pane_id"] == "%80"
    assert str(ev["fingerprint"]).startswith("hook:")


def test_i2_ingest_invalid_payload_writes_nothing(qdb: Path) -> None:
    bad = _hook_store_event(pane_id="not-a-pane")
    assert aq.ingest_hook_event(bad, db_path=qdb) == {
        "ok": False,
        "reason": "invalid-payload",
    }
    assert aq.list_question_events(status="open", db_path=qdb) == []

    bad2 = _hook_store_event()
    bad2["question_text"] = "   "
    assert aq.ingest_hook_event(bad2, db_path=qdb)["ok"] is False
    assert aq.list_question_events(status="open", db_path=qdb) == []

    bad3 = _hook_store_event()
    bad3["options"] = "not-a-list"  # type: ignore[assignment]
    assert aq.ingest_hook_event(bad3, db_path=qdb)["reason"] == "invalid-payload"


def test_i2_merge_scrape_then_hook_supersedes(qdb: Path) -> None:
    """Open scrape event on pane → hook ingest same pane → exactly one open, source=hook."""
    now = 1_700_001_000.0
    win = _frage_window(pane_id="%80", now=now, activity=now - 10)
    service = _StubService([win], now=now)
    ing = aq.QuestionScrapeIngestor(
        db_path=qdb, service_factory=lambda: service, now=lambda: now
    )
    ing.poll_once()
    ing.poll_once()
    scrape_opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(scrape_opens) == 1
    assert scrape_opens[0]["source"] == "scrape"
    scrape_id = scrape_opens[0]["id"]

    result = aq.ingest_hook_event(_hook_store_event(pane_id="%80"), db_path=qdb)
    assert result["ok"] is True
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    assert opens[0]["source"] == "hook"
    assert opens[0]["id"] == result["id"]
    supers = aq.list_question_events(status="superseded", db_path=qdb)
    assert any(r["id"] == scrape_id for r in supers)


def test_i2_skip_hook_authoritative_blocks_scrape_insert(qdb: Path) -> None:
    """Open hook event → scrape ingestor with parseable prompt same pane → no second event."""
    result = aq.ingest_hook_event(_hook_store_event(pane_id="%1"), db_path=qdb)
    assert result["ok"] is True
    assert len(aq.list_question_events(status="open", db_path=qdb)) == 1

    now = 1_700_001_100.0
    # Capture still contains the hook question_text (so hook stays standing)
    # PLUS a parseable select block that would otherwise create a scrape event.
    dual_tail = (
        f"  {_HOOK_QUESTION_TEXT}\n"
        f"{_FIXTURE_CLAUDE_SELECT}"
    )
    win = _frage_window(
        pane_id="%1",
        tail=dual_tail,
        now=now,
        activity=now - 10,
    )
    service = _StubService([win], now=now)
    ing = aq.QuestionScrapeIngestor(
        db_path=qdb, service_factory=lambda: service, now=lambda: now
    )
    s1 = ing.poll_once()
    s2 = ing.poll_once()
    assert s1["skipped_hook_authoritative"] >= 1
    assert s2["skipped_hook_authoritative"] >= 1
    assert s1["created"] == 0
    assert s2["created"] == 0
    opens = aq.list_question_events(status="open", db_path=qdb)
    assert len(opens) == 1
    assert opens[0]["source"] == "hook"
    assert opens[0]["id"] == result["id"]
    assert opens[0]["question_text"] == _HOOK_QUESTION_TEXT


def test_i2_ingest_idempotent_same_hook_key(qdb: Path) -> None:
    r1 = aq.ingest_hook_event(_hook_store_event(), db_path=qdb)
    r2 = aq.ingest_hook_event(_hook_store_event(), db_path=qdb)
    assert r1["ok"] is True and "id" in r1
    assert r2 == {"ok": True, "deduped": True, "id": r1["id"]}
    assert len(aq.list_question_events(status="open", db_path=qdb)) == 1


def test_i2_ingest_dedup_survives_closed_event(qdb: Path) -> None:
    """A re-POST of the same hook_key must NOT resurrect a closed event.

    tool_use_ids are globally unique — after the dashboard (or resolve)
    closed the event, a duplicate PreToolUse POST is a dedup no-op, not a
    fresh open event (Kimi-Lens I2 #1).
    """
    r1 = aq.ingest_hook_event(_hook_store_event(), db_path=qdb)
    eid = int(r1["id"])
    resolved = aq.resolve_hook_event(
        str(_hook_store_event()["hook_key"]), "Rolling update", db_path=qdb
    )
    assert resolved["resolved"] is True
    r2 = aq.ingest_hook_event(_hook_store_event(), db_path=qdb)
    assert r2 == {"ok": True, "deduped": True, "id": eid}
    assert aq.list_question_events(status="open", db_path=qdb) == []


def test_i2_answer_recheck_requires_option_labels(qdb: Path) -> None:
    """Same question re-asked with DIFFERENT options → answer refused.

    The recheck needle (first 80 chars) matches, but the old event's option
    labels are gone from the capture — sending a digit would map to the
    wrong option (Kimi-Lens I2 #2). Claim must roll to superseded, nothing sent.
    """
    result = aq.ingest_hook_event(_hook_store_event(pane_id="%43"), db_path=qdb)
    eid = int(result["id"])
    # Pane now shows the SAME question with different options.
    reasked = (
        f"  {_HOOK_QUESTION_TEXT}\n"
        "  1. Big-bang cutover\n"
        "  2. Shadow traffic\n"
    )
    svc = _RecordingService([reasked, reasked])
    r = aq.answer_question(
        eid, "1", db_path=qdb, service=svc, verify_delay_s=0, sleep=lambda _s: None
    )
    assert r == {"ok": False, "reason": "superseded"}
    assert svc.sent == []


def test_i2_resolve_hook_event_and_noop(qdb: Path) -> None:
    ing = aq.ingest_hook_event(_hook_store_event(), db_path=qdb, now=1_700_001_200.0)
    assert ing["ok"] is True
    eid = int(ing["id"])

    r1 = aq.resolve_hook_event(
        _HOOK_KEY, "Rolling update", db_path=qdb, now=1_700_001_210.0
    )
    assert r1["ok"] is True
    assert r1["resolved"] is True
    assert r1["id"] == eid
    assert r1["latency_s"] >= 0

    answered = aq.list_question_events(status="answered", db_path=qdb)
    assert len(answered) == 1
    assert answered[0]["answered_by"] == "terminal"
    assert answered[0]["answer"] == "Rolling update"
    assert answered[0]["latency_s"] is not None
    assert answered[0]["status"] == "answered"

    # Double resolve → resolved:False no-op
    r2 = aq.resolve_hook_event(_HOOK_KEY, "x", db_path=qdb)
    assert r2 == {"ok": True, "resolved": False}

    # Resolve after dashboard answer path: fresh hook, claim via answer, then resolve no-op
    aq.ingest_hook_event(
        _hook_store_event(pane_id="%81", hook_key="toolu_other"),
        db_path=qdb,
    )
    opens = [e for e in aq.list_question_events(status="open", db_path=qdb)]
    assert len(opens) == 1
    dash_id = int(opens[0]["id"])
    # claim open→answered as dashboard would
    assert aq._claim_event(
        dash_id, answer="1", answered_by="operator", db_path=qdb
    )
    r3 = aq.resolve_hook_event("toolu_other", "1", db_path=qdb)
    assert r3 == {"ok": True, "resolved": False}


def test_i2_expiry_cross_session_hook_three_cases(qdb: Path) -> None:
    """Open hook event, pane NOT in work scan; capture (a) present (b) gone (c) transient."""
    # --- (a) question text present → stays open over 2 polls ---
    aq.ingest_hook_event(_hook_store_event(pane_id="%90"), db_path=qdb)
    now = 1_700_001_300.0
    # Non-empty list_windows so empty-snapshot guard does not skip expiry,
    # but the hook pane is NOT in the work scan.
    other = _frage_window(
        pane_id="%other",
        tail=_FIXTURE_NOT_QUESTION,
        now=now,
        activity=now - 10,
        session="work",
        window="idle",
    )
    service = _StubService(
        [other],
        now=now,
        extra_captures={
            "%90": (
                "Some noise\n"
                f"  {_HOOK_QUESTION_TEXT}\n"
                "  1. Rolling update\n"
                "  2. Blue-green\n"
            )
        },
    )
    ing = aq.QuestionScrapeIngestor(
        db_path=qdb, service_factory=lambda: service, now=lambda: now
    )
    s1 = ing.poll_once()
    s2 = ing.poll_once()
    assert s1["cross_session_checked"] >= 1
    assert s2["cross_session_checked"] >= 1
    assert s1["expired"] == 0
    assert s2["expired"] == 0
    assert len(aq.list_question_events(status="open", db_path=qdb)) == 1

    # --- (b) text gone → after TWO polls expired ---
    service._extra_captures["%90"] = "Working… no question here\n"
    s3 = ing.poll_once()
    assert s3["expired"] == 0
    assert len(aq.list_question_events(status="open", db_path=qdb)) == 1
    s4 = ing.poll_once()
    assert s4["expired"] >= 1
    assert aq.list_question_events(status="open", db_path=qdb) == []
    assert len(aq.list_question_events(status="expired", db_path=qdb)) == 1

    # --- (c) capture throws transient → no strike ---
    aq.ingest_hook_event(
        _hook_store_event(pane_id="%91", hook_key="toolu_transient"),
        db_path=qdb,
    )
    service2 = _StubService(
        [other],
        now=now,
        capture_raises={"%91": RuntimeError("transient socket busy")},
    )
    ing2 = aq.QuestionScrapeIngestor(
        db_path=qdb, service_factory=lambda: service2, now=lambda: now
    )
    t1 = ing2.poll_once()
    t2 = ing2.poll_once()
    t3 = ing2.poll_once()
    assert t1["expired"] == 0
    assert t2["expired"] == 0
    assert t3["expired"] == 0
    opens = [
        e
        for e in aq.list_question_events(status="open", db_path=qdb)
        if e["pane_id"] == "%91"
    ]
    assert len(opens) == 1

    # Gone error DOES strike (and two-poll expires)
    service2._capture_raises["%91"] = RuntimeError("can't find pane %91")
    g1 = ing2.poll_once()
    assert g1["expired"] == 0
    g2 = ing2.poll_once()
    assert g2["expired"] >= 1
    assert not any(
        e["pane_id"] == "%91"
        for e in aq.list_question_events(status="open", db_path=qdb)
    )


def test_i2_endpoint_ingest_and_resolve(
    qdb: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    import hermes_cli.web_server as web_server

    monkeypatch.setattr(aq, "question_events_db_path", lambda: qdb)
    client = TestClient(web_server.app)
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}

    ok = client.post(
        "/api/agent-questions/ingest",
        json=_hook_store_event(),
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["ok"] is True
    assert isinstance(body["id"], int)

    bad = client.post(
        "/api/agent-questions/ingest",
        json=_hook_store_event(pane_id="bad"),
        headers=headers,
    )
    assert bad.status_code == 400, bad.text

    resolved = client.post(
        "/api/agent-questions/resolve",
        json={"hook_key": _HOOK_KEY, "answer": "1"},
        headers=headers,
    )
    assert resolved.status_code == 200, resolved.text
    rbody = resolved.json()
    assert rbody["ok"] is True
    assert rbody["resolved"] is True

    noop = client.post(
        "/api/agent-questions/resolve",
        json={"hook_key": _HOOK_KEY, "answer": "1"},
        headers=headers,
    )
    assert noop.status_code == 200
    assert noop.json() == {"ok": True, "resolved": False}


def test_i2_answer_question_on_hook_event_option_indices(qdb: Path) -> None:
    """answer_question works on a hook event (option nr indices)."""
    result = aq.ingest_hook_event(_hook_store_event(pane_id="%42"), db_path=qdb)
    assert result["ok"] is True
    eid = int(result["id"])
    # Capture still contains the question text (hook recheck path).
    present = (
        f"  {_HOOK_QUESTION_TEXT}\n"
        "  1. Rolling update\n"
        "  2. Blue-green\n"
        "  3. Canary\n"
    )
    svc = _RecordingService([present, ""])
    r = aq.answer_question(
        eid, "1", db_path=qdb, service=svc, verify_delay_s=0, sleep=lambda _s: None
    )
    assert r["ok"] is True
    assert r["verified"] is True
    assert svc.sent == [{"pane_id": "%42", "text": "1", "enter": False}]
    answered = aq.list_question_events(status="answered", db_path=qdb)
    assert len(answered) == 1
    assert answered[0]["answer"] == "1"
    assert answered[0]["source"] == "hook"


# ---------------------------------------------------------------------------
# I3 Mini #3 — hook verify false-negative (CC echo)
# ---------------------------------------------------------------------------


def test_i3_hook_answer_verified_when_question_text_still_visible(qdb: Path) -> None:
    """Hook event answered via dashboard; CC still echoes question text → verified True.

    Pre-I3 the text-disappear check marked these false (false-negative).
    """
    result = aq.ingest_hook_event(_hook_store_event(pane_id="%77"), db_path=qdb)
    assert result["ok"] is True
    eid = int(result["id"])
    # Both recheck (pre-send) and post-send verify still show the full prompt.
    still_there = (
        f"  {_HOOK_QUESTION_TEXT}\n"
        "  1. Rolling update\n"
        "  2. Blue-green\n"
        "  3. Canary\n"
    )
    svc = _RecordingService([still_there, still_there])
    r = aq.answer_question(
        eid, "1", db_path=qdb, service=svc, verify_delay_s=0, sleep=lambda _s: None
    )
    assert r["ok"] is True
    assert r["verified"] is True
    answered = aq.list_question_events(status="answered", db_path=qdb)
    assert len(answered) == 1
    assert answered[0]["answer_verified"] == 1


def test_i3_resolve_hook_event_sets_verified(qdb: Path) -> None:
    """PostToolUse resolve-signal stamps answer_verified=1."""
    result = aq.ingest_hook_event(
        _hook_store_event(pane_id="%78", hook_key="hk-resolve-verified"),
        db_path=qdb,
    )
    assert result["ok"] is True
    r = aq.resolve_hook_event("hk-resolve-verified", "1", db_path=qdb)
    assert r["ok"] is True
    assert r["resolved"] is True
    assert r.get("verified") is True
    answered = aq.list_question_events(status="answered", db_path=qdb)
    assert len(answered) == 1
    assert answered[0]["answer_verified"] == 1


# ---------------------------------------------------------------------------
# I3 Mini #4 — GC / housekeeping
# ---------------------------------------------------------------------------


def test_i3_prune_old_closed_events(qdb: Path) -> None:
    """expired/superseded/answered with updated_ts > 14d → DELETE; open kept."""
    t_old = time.time() - (20 * 86400)
    t_recent = time.time() - (2 * 86400)
    t_now = time.time()

    # Old answered → pruned
    _, id_old_ans = aq.supersede_and_insert(
        session="s",
        window="w",
        pane_id="%90",
        fingerprint="fp-old-ans",
        question_text="old answered?",
        options=[{"nr": 1, "label": "y"}],
        db_path=qdb,
        now=t_old,
    )
    assert id_old_ans is not None
    from hermes_cli.sqlite_util import write_txn

    iso_old = aq._iso_now(t_old)
    with aq.connect_closing(db_path=qdb) as conn:
        with write_txn(conn):
            conn.execute(
                "UPDATE question_events SET status = 'answered', updated_ts = ? WHERE id = ?",
                (iso_old, id_old_ans),
            )
            # Old expired
            conn.execute(
                "INSERT INTO question_events ("
                "ts, updated_ts, source, session, window, pane_id, fingerprint, "
                "question_text, options_json, class, status, override"
                ") VALUES (?, ?, 'scrape', 's', 'w', '%91', 'fp-old-exp', "
                "'old expired?', '[]', 'unknown', 'expired', 0)",
                (iso_old, iso_old),
            )
            # Old superseded
            conn.execute(
                "INSERT INTO question_events ("
                "ts, updated_ts, source, session, window, pane_id, fingerprint, "
                "question_text, options_json, class, status, override"
                ") VALUES (?, ?, 'scrape', 's', 'w', '%92', 'fp-old-sup', "
                "'old super?', '[]', 'unknown', 'superseded', 0)",
                (iso_old, iso_old),
            )
            # Recent answered — kept
            iso_recent = aq._iso_now(t_recent)
            conn.execute(
                "INSERT INTO question_events ("
                "ts, updated_ts, source, session, window, pane_id, fingerprint, "
                "question_text, options_json, class, status, override"
                ") VALUES (?, ?, 'scrape', 's', 'w', '%93', 'fp-recent', "
                "'recent answered?', '[]', 'unknown', 'answered', 0)",
                (iso_recent, iso_recent),
            )

    # Open event (must survive regardless of age)
    _, id_open = aq.supersede_and_insert(
        session="s",
        window="w",
        pane_id="%94",
        fingerprint="fp-open",
        question_text="still open?",
        options=[{"nr": 1, "label": "y"}],
        db_path=qdb,
        now=t_old,
    )
    assert id_open is not None

    deleted = aq.prune_old_events(db_path=qdb, now=t_now, max_age_days=14)
    assert deleted == 3
    remaining = aq.list_question_events(status="", limit=50, db_path=qdb)
    statuses = {int(e["id"]): e["status"] for e in remaining}
    assert id_open in statuses and statuses[id_open] == "open"
    # recent answered still present
    assert any(e["status"] == "answered" and "recent" in e["question_text"] for e in remaining)
    assert not any("old answered" in e["question_text"] for e in remaining)
    assert not any(e["status"] == "expired" for e in remaining)
    assert not any(e["status"] == "superseded" for e in remaining)


def test_i3_prune_bak_files(tmp_path: Path) -> None:
    """Only question_events.db.bak-* older than 14d are removed."""
    home = tmp_path / "h"
    home.mkdir()
    db = home / "question_events.db"
    db.write_text("")
    old_bak = home / "question_events.db.bak-20260101"
    new_bak = home / "question_events.db.bak-20260701"
    other = home / "other.db.bak-old"
    old_bak.write_text("x")
    new_bak.write_text("y")
    other.write_text("z")
    now = time.time()
    import os

    os.utime(old_bak, (now - 20 * 86400, now - 20 * 86400))
    os.utime(new_bak, (now - 2 * 86400, now - 2 * 86400))
    os.utime(other, (now - 20 * 86400, now - 20 * 86400))

    removed = aq.prune_bak_files(db_path=db, now=now, max_age_days=14)
    assert removed == 1
    assert not old_bak.exists()
    assert new_bak.exists()
    assert other.exists()


def test_i3_resolve_hook_event_empty_answer_not_verified(qdb: Path) -> None:
    """PostToolUse with empty answers (Esc-Abbruch) closes but must NOT stamp
    verified — closing is correct, the verification claim is not (review m5)."""
    ing = aq.ingest_hook_event(_hook_store_event(), db_path=qdb, now=1_700_001_200.0)
    eid = int(ing["id"])

    r = aq.resolve_hook_event(_HOOK_KEY, "", db_path=qdb, now=1_700_001_210.0)
    assert r["ok"] is True and r["resolved"] is True and r["id"] == eid
    assert r["verified"] is False

    answered = aq.list_question_events(status="answered", db_path=qdb)
    assert len(answered) == 1
    assert not answered[0]["answer_verified"]
