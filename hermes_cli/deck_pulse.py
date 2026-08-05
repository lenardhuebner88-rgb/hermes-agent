"""Fork-owned: the one payload the Hermes Deck phone app polls.

Two routes live here.

``GET /api/buzz/agents/activity`` answers *what* each Buzz agent is doing, using
:mod:`hermes_cli.buzz_agent_tool_calls`. ``GET /api/deck/pulse`` bundles that
together with the Kanban board's running workers, its dispatch holds and its live event
ticker, so a phone on a tail-net makes **one** request per tick instead of four.

Subscription budgets are deliberately *not* in the bundle. ``/api/account-usage``
is an async handler inside ``web_server`` — importing it here would close an
import cycle — and the numbers move on the scale of hours, so tying them to an
eight-second tick would buy nothing. The app keeps its existing separate call at
its own, slower cadence.

Bundling has a failure mode that this module treats as the main design problem:
a single sub-source that raises would otherwise take the whole payload with it,
and the operator would see an empty screen for a reason that has nothing to do
with the thing they were looking at. So every section carries its own ``as_of``
and its own ``error``, is built inside its own guard, and a section that fails
degrades to ``null`` beside three sections that still say something true.

The Kanban half is reached in-process through the already-mounted plugin module
rather than over HTTP. A loopback request would have to re-authenticate against
the dashboard's own session gate from inside the dashboard, and the plugin is
either mounted in this process or it is not — which is a state worth reporting
plainly (``kanban_unavailable``) instead of disguising as a network error.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

from fastapi import FastAPI, Query

from hermes_cli.agent_session_facts import SessionFactsService
from hermes_cli.buzz_agent_tool_calls import (
    DEFAULT_ACTIVITY_WINDOW_SECONDS,
    DEFAULT_CALLS_PER_AGENT,
    ActivityReader,
)
from hermes_cli.buzz_model_control import BuzzModelControlService

# The name `_mount_plugin_api_routes` gives the kanban plugin's backend module.
KANBAN_MODULE = "hermes_dashboard_plugin_kanban"

MAX_CALLS_PER_AGENT = 60
MAX_LIVE_EVENTS = 20


class DeckPulseService:
    """Assembles the phone payload. Every section is independently fallible."""

    def __init__(
        self,
        *,
        control: BuzzModelControlService | None = None,
        activity_window_seconds: int = DEFAULT_ACTIVITY_WINDOW_SECONDS,
        activity_ttl_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
        kanban_module: Any = None,
        session_facts: SessionFactsService | None = None,
    ) -> None:
        self._control = control
        self.activity_window_seconds = activity_window_seconds
        self._clock = clock
        self._kanban_module = kanban_module
        self._reader: ActivityReader | None = None
        self._activity_ttl_seconds = activity_ttl_seconds
        self._session_facts = session_facts

    # -- wiring ------------------------------------------------------------

    @property
    def control(self) -> BuzzModelControlService:
        if self._control is None:
            self._control = BuzzModelControlService()
        return self._control

    @property
    def session_facts(self) -> SessionFactsService:
        if self._session_facts is None:
            # Shares the control service's runner rather than spawning its own
            # `SubprocessRunner` — same reasoning as `reader` below: one
            # journal-reading strategy per process, and tests that fake the
            # control runner get a faked steering query for free.
            self._session_facts = SessionFactsService(clock=self._clock, runner=self.control.runner)
        return self._session_facts

    @property
    def reader(self) -> ActivityReader:
        if self._reader is None:
            self._reader = ActivityReader(
                self.control.runner,
                window_seconds=self.activity_window_seconds,
                ttl_seconds=self._activity_ttl_seconds,
                clock=self._clock,
            )
        return self._reader

    def _kanban(self) -> Any | None:
        """The mounted plugin module, or ``None`` when it is not in this process."""
        if self._kanban_module is not None:
            return self._kanban_module
        return sys.modules.get(KANBAN_MODULE)

    # -- sections ----------------------------------------------------------

    def activity_payload(self, *, limit: int = DEFAULT_CALLS_PER_AGENT) -> dict[str, Any]:
        """Per-agent tool-call timelines, merged onto the existing agent summaries.

        The heartbeat counters stay in the payload. They are a different claim
        from the timeline — a count over an hour against a list over half of one —
        and a client that wants "has this agent done anything at all today" is
        still better served by the counter than by the tail of a short window.
        """
        limit = max(1, min(int(limit), MAX_CALLS_PER_AGENT))
        now = self._clock()
        agents = self.control.agents_payload()
        stems = [str(agent.get("stem")) for agent in agents.get("agents", []) if agent.get("stem")]
        timelines, diagnostics, error = self.reader.read(stems)

        for agent in agents.get("agents", []):
            timeline = timelines.get(str(agent.get("stem")))
            if timeline is None or error is not None:
                # Null, never an empty list: "the journal could not be read" and
                # "this agent made no calls" must not render the same way.
                agent["activity"] = None
            else:
                agent["activity"] = timeline.as_payload(now=now, limit=limit)
            agent["session"] = self._session_payload(str(agent.get("stem")), now=now)

        agents["activity_window_seconds"] = self.activity_window_seconds
        agents["activity_error"] = error
        agents["activity_diagnostics"] = diagnostics.as_payload()
        agents["as_of"] = _iso(now)
        return agents

    def _session_payload(self, stem: str, *, now: float) -> dict[str, Any] | None:
        """Session facts for one agent. A read failure degrades this one
        agent's ``session`` to ``null``, the same as a missing timeline —
        it must never take the rest of the bundle down with it."""
        if not stem or stem == "None":
            return None
        try:
            facts = self.session_facts.facts_for_stem(stem)
        except Exception:  # noqa: BLE001 — one bad stem must not sink the rest
            return None
        return facts.as_payload(now=now)

    def _kanban_section(
        self, name: str, call: Callable[[Any], Any]
    ) -> tuple[Any, str | None]:
        module = self._kanban()
        if module is None:
            return None, "kanban_unavailable"
        function = getattr(module, name, None)
        if function is None:
            # The plugin is mounted but does not carry this route — an upstream
            # rename, not an outage. Saying which is the whole point.
            return None, f"route_missing:{name}"
        try:
            return call(function), None
        except Exception as exc:  # noqa: BLE001 — one bad section must not sink three good ones
            return None, f"{type(exc).__name__}: {exc}"[:200]

    def workers_payload(self, *, board: str | None) -> tuple[Any, str | None]:
        return self._kanban_section(
            "list_active_workers", lambda fn: fn(board=board)
        )

    def holds_payload(self, *, board: str | None) -> tuple[Any, str | None]:
        return self._kanban_section("get_dispatch_holds", lambda fn: fn(board=board))

    def events_payload(
        self, *, board: str | None, since_id: int | None
    ) -> tuple[Any, str | None]:
        return self._kanban_section(
            "get_live_events",
            lambda fn: fn(board=board, limit=MAX_LIVE_EVENTS, since_id=since_id),
        )

    # -- the bundle --------------------------------------------------------

    def pulse_payload(
        self,
        *,
        board: str | None = None,
        limit: int = DEFAULT_CALLS_PER_AGENT,
        since_id: int | None = None,
    ) -> dict[str, Any]:
        now = self._clock()
        stamp = _iso(now)

        try:
            agents = self.activity_payload(limit=limit)
            agents_error = None
        except Exception as exc:  # noqa: BLE001
            agents, agents_error = None, f"{type(exc).__name__}: {exc}"[:200]

        workers, workers_error = self.workers_payload(board=board)
        holds, holds_error = self.holds_payload(board=board)
        events, events_error = self.events_payload(board=board, since_id=since_id)

        return {
            "as_of": stamp,
            # Named, not implied. The Kanban half is board-scoped and there is
            # more than one board; a payload that stays silent about which one
            # it read invites the client to present it as the whole system.
            "board": board,
            "agents": _section(agents, agents_error, stamp),
            "workers": _section(workers, workers_error, stamp),
            "holds": _section(holds, holds_error, stamp),
            "events": _section(_trim_events(events), events_error, stamp),
        }


def _section(data: Any, error: str | None, stamp: str) -> dict[str, Any]:
    return {"data": data, "error": error, "as_of": stamp}


def _trim_events(events: Any) -> Any:
    """The ticker is unbounded upstream; a phone needs the newest handful."""
    if isinstance(events, dict):
        for key in ("events", "items", "rows"):
            value = events.get(key)
            if isinstance(value, list):
                trimmed = dict(events)
                trimmed[key] = value[:MAX_LIVE_EVENTS]
                return trimmed
    if isinstance(events, list):
        return events[:MAX_LIVE_EVENTS]
    return events


def _iso(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch_seconds))


_default_service: DeckPulseService | None = None


def _get_default_service() -> DeckPulseService:
    global _default_service
    if _default_service is None:
        _default_service = DeckPulseService()
    return _default_service


def register_deck_pulse_routes(
    app: FastAPI,
    *,
    service_factory: Callable[[], DeckPulseService] = _get_default_service,
) -> None:
    """Register the deck's read routes before the SPA catch-all."""

    @app.get("/api/buzz/agents/activity")
    def buzz_agent_activity(
        limit: int = Query(DEFAULT_CALLS_PER_AGENT, ge=1, le=MAX_CALLS_PER_AGENT),
    ) -> dict[str, Any]:
        return service_factory().activity_payload(limit=limit)

    @app.get("/api/deck/pulse")
    def deck_pulse(
        board: str | None = Query(None),
        limit: int = Query(DEFAULT_CALLS_PER_AGENT, ge=1, le=MAX_CALLS_PER_AGENT),
        since_id: int | None = Query(None),
    ) -> dict[str, Any]:
        return service_factory().pulse_payload(board=board, limit=limit, since_id=since_id)
