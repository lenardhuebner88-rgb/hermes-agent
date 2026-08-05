"""Tests for the deck's read routes.

The property under test is mostly *degradation*: this endpoint exists so a phone
makes one request instead of four, and the whole point collapses if one weak
sub-source can blank the other three. So every section gets its own failure test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.deck_pulse import DeckPulseService, _trim_events

FIXTURE = Path(__file__).parent / "fixtures" / "buzz_agent_journal_activity.jsonl"


class FakeResult:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class FakeRunner:
    """Answers the journal query with the real fixture, everything else emptily."""

    def __init__(self, journal: str = "", journal_returncode: int = 0) -> None:
        self.journal = journal
        self.journal_returncode = journal_returncode

    def run(self, args: list[str], *, timeout: float | None = None) -> FakeResult:
        if args and args[0] == "journalctl":
            return FakeResult(self.journal, self.journal_returncode)
        return FakeResult("", 0)


class FakeControl:
    """Stands in for `BuzzModelControlService` with two known agents."""

    def __init__(self, runner: FakeRunner, *, raises: bool = False) -> None:
        self.runner = runner
        self._raises = raises

    def agents_payload(self) -> dict:
        if self._raises:
            raise RuntimeError("systemctl exploded")
        return {
            "agents": [
                {"stem": "claude", "display_name": "Claude", "active_state": "active"},
                {"stem": "grok", "display_name": "Grok", "active_state": "active"},
            ],
            "heartbeat_window_seconds": 3600,
            "heartbeat_recent_seconds": 300,
            "heartbeat_error": None,
        }


def make_service(
    *,
    journal: str = "",
    journal_returncode: int = 0,
    control_raises: bool = False,
    kanban=None,
) -> DeckPulseService:
    runner = FakeRunner(journal, journal_returncode)
    return DeckPulseService(
        control=FakeControl(runner, raises=control_raises),
        kanban_module=kanban if kanban is not None else _WorkingKanban(),
        clock=lambda: 1_800_000_000.0,
    )


class _WorkingKanban:
    @staticmethod
    def list_active_workers(*, board=None):
        return {"workers": [{"run_id": 7, "task_title": "Puls bauen"}], "count": 1}

    @staticmethod
    def get_dispatch_holds(*, board=None):
        return {"holds": [{"task_id": "T-1", "reason": "freigabe"}]}

    @staticmethod
    def get_live_events(*, board=None, limit=20, since_id=None):
        return {"events": [{"id": i, "kind": "completed"} for i in range(50)]}


@pytest.fixture(scope="module")
def journal() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# --- activity -------------------------------------------------------------


def test_activity_attaches_a_timeline_to_each_agent(journal: str) -> None:
    payload = make_service(journal=journal).activity_payload()
    agents = {agent["stem"]: agent for agent in payload["agents"]}
    assert agents["claude"]["activity"]["calls_in_window"] > 0
    # An agent the journal never mentioned gets an empty timeline, not a null —
    # "made no calls" is a measured answer.
    assert agents["grok"]["activity"]["calls_in_window"] == 0
    assert agents["grok"]["activity"]["latest"] is None
    assert payload["activity_error"] is None
    assert payload["activity_diagnostics"]["suspicious"] is False


def test_activity_keeps_the_existing_heartbeat_fields(journal: str) -> None:
    """The counter and the timeline are different claims; neither replaces the other."""
    payload = make_service(journal=journal).activity_payload()
    assert payload["heartbeat_window_seconds"] == 3600
    assert "activity_window_seconds" in payload


def test_activity_is_null_when_the_journal_cannot_be_read() -> None:
    payload = make_service(journal="", journal_returncode=1).activity_payload()
    assert payload["activity_error"] == "journal_unavailable"
    for agent in payload["agents"]:
        # Null, not an empty timeline: an unreadable journal must not render as
        # a quiet agent. This is the single most important line in the file.
        assert agent["activity"] is None


def test_activity_limit_is_clamped(journal: str) -> None:
    payload = make_service(journal=journal).activity_payload(limit=9_999)
    for agent in payload["agents"]:
        if agent["activity"]:
            assert len(agent["activity"]["recent"]) <= 60


# --- the bundle -----------------------------------------------------------


def test_pulse_carries_every_section(journal: str) -> None:
    payload = make_service(journal=journal).pulse_payload(board="default")
    assert payload["board"] == "default"
    for name in ("agents", "workers", "holds", "events"):
        section = payload[name]
        assert section["error"] is None, name
        assert section["data"] is not None, name
        assert section["as_of"] == payload["as_of"]


def test_pulse_says_so_when_the_kanban_plugin_is_not_mounted(journal: str) -> None:
    """No injected module and none in `sys.modules` — the plugin is simply absent."""
    assert "hermes_dashboard_plugin_kanban" not in sys.modules
    service = DeckPulseService(
        control=FakeControl(FakeRunner(journal)),
        kanban_module=None,
        clock=lambda: 1_800_000_000.0,
    )
    payload = service.pulse_payload()
    assert payload["agents"]["data"] is not None
    for name in ("workers", "holds", "events"):
        assert payload[name]["error"] == "kanban_unavailable"
        assert payload[name]["data"] is None


def test_pulse_names_a_renamed_route_instead_of_going_quiet(journal: str) -> None:
    """An upstream rename is a different problem from an outage, and says so."""
    payload = make_service(journal=journal, kanban=_HalfKanban()).pulse_payload()
    assert payload["workers"]["error"] is None
    assert payload["holds"]["error"] == "route_missing:get_dispatch_holds"


class _HalfKanban:
    list_active_workers = staticmethod(_WorkingKanban.list_active_workers)
    get_live_events = staticmethod(_WorkingKanban.get_live_events)


def test_one_exploding_section_does_not_sink_the_others(journal: str) -> None:
    payload = make_service(journal=journal, kanban=_ExplodingWorkers()).pulse_payload()
    assert payload["workers"]["data"] is None
    assert "RuntimeError" in payload["workers"]["error"]
    assert payload["holds"]["data"] is not None
    assert payload["events"]["data"] is not None
    assert payload["agents"]["data"] is not None


class _ExplodingWorkers:
    @staticmethod
    def list_active_workers(*, board=None):
        raise RuntimeError("board is locked")

    get_dispatch_holds = staticmethod(_WorkingKanban.get_dispatch_holds)
    get_live_events = staticmethod(_WorkingKanban.get_live_events)


def test_a_broken_agent_source_does_not_sink_kanban() -> None:
    payload = make_service(control_raises=True).pulse_payload()
    assert payload["agents"]["data"] is None
    assert "RuntimeError" in payload["agents"]["error"]
    assert payload["workers"]["data"] is not None


def test_pulse_trims_the_event_ticker(journal: str) -> None:
    payload = make_service(journal=journal).pulse_payload()
    assert len(payload["events"]["data"]["events"]) == 20


@pytest.mark.parametrize(
    ("raw", "expected_len"),
    [
        ({"events": list(range(50))}, 20),
        ({"items": list(range(50))}, 20),
        (list(range(50)), 20),
    ],
)
def test_trim_events_handles_every_shape(raw, expected_len: int) -> None:
    trimmed = _trim_events(raw)
    values = trimmed if isinstance(trimmed, list) else next(
        v for v in trimmed.values() if isinstance(v, list)
    )
    assert len(values) == expected_len


def test_trim_events_leaves_an_unknown_shape_alone() -> None:
    assert _trim_events({"error": "nope"}) == {"error": "nope"}
    assert _trim_events(None) is None


# --- route surface --------------------------------------------------------


def test_routes_are_registered_on_the_dashboard() -> None:
    """The routes must exist before the SPA catch-all, or they return HTML."""
    from fastapi import FastAPI

    from hermes_cli.deck_pulse import register_deck_pulse_routes

    app = FastAPI()
    register_deck_pulse_routes(app, service_factory=lambda: make_service())
    paths = {route.path for route in app.routes}
    assert "/api/buzz/agents/activity" in paths
    assert "/api/deck/pulse" in paths


def test_the_routes_answer_json(journal: str) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hermes_cli.deck_pulse import register_deck_pulse_routes

    app = FastAPI()
    register_deck_pulse_routes(app, service_factory=lambda: make_service(journal=journal))
    client = TestClient(app)

    response = client.get("/api/deck/pulse?board=default&limit=5")
    assert response.status_code == 200
    body = response.json()
    assert body["board"] == "default"
    assert body["agents"]["data"]["agents"]

    response = client.get("/api/buzz/agents/activity?limit=3")
    assert response.status_code == 200
    assert len(response.json()["agents"]) == 2

    # A limit outside the contract is rejected, not silently clamped by FastAPI.
    assert client.get("/api/buzz/agents/activity?limit=0").status_code == 422


def test_no_secret_survives_into_the_payload(journal: str) -> None:
    """End-to-end proof of the masking rule, at the boundary that matters."""
    leaky = json.dumps(
        {
            "_SYSTEMD_USER_UNIT": "buzz-agent@claude.service",
            "__REALTIME_TIMESTAMP": "1800000000000000",
            "MESSAGE": "tool_call: terminal: curl -H 'Authorization: Bearer supersecretvalue' (execute)",
        }
    )
    payload = make_service(journal=leaky).activity_payload()
    assert "supersecretvalue" not in json.dumps(payload)
