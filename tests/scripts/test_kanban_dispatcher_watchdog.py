"""Tests for the kanban dispatcher heartbeat watchdog (AC3-dispatcher-watchdog).

The watchdog reads ``~/.hermes/state/kanban_dispatcher_heartbeat.json`` (written by
``gateway/kanban_watchers.py`` via ``write_kanban_dispatcher_heartbeat``) and emits at
most ONE Discord alert per calendar day when the heartbeat is stale or unhealthy.
Alert-only — never restarts anything.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from scripts import kanban_dispatcher_watchdog as wd


# A fixed "now" so date buckets are deterministic. 2026-06-18 12:00:00 UTC.
NOW = dt.datetime(2026, 6, 18, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()
NEXT_DAY = dt.datetime(2026, 6, 19, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()


def _write_heartbeat(path: Path, *, last_tick_at: float, tick_health: str = "ok") -> None:
    payload = {
        "last_tick_at": int(last_tick_at),
        "tick_health": tick_health,
        "last_green_gate_at": int(last_tick_at) if tick_health == "ok" else None,
        "counts": {"self_healed_today": 0, "parked_open": 0, "open_escalations": 0, "stranded": 0},
        "boards": [],
    }
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "heartbeat": tmp_path / "kanban_dispatcher_heartbeat.json",
        "state": tmp_path / "kanban_dispatcher_watchdog_state.json",
    }


@pytest.fixture
def captured_posts(monkeypatch) -> list[str]:
    """Replace the network Discord call with an in-memory recorder."""
    posts: list[str] = []

    def _fake_post(body: str, channel: str) -> dict:
        posts.append(body)
        return {"result": "sent", "message_id": f"msg-{len(posts)}"}

    monkeypatch.setattr(wd, "_post_discord", _fake_post)
    return posts


def _run(paths: dict[str, Path], *, now: float, **kw) -> dict:
    return wd.run(
        heartbeat_path=paths["heartbeat"],
        state_path=paths["state"],
        now=now,
        **kw,
    )


def test_fresh_healthy_heartbeat_does_not_alert(paths, captured_posts):
    _write_heartbeat(paths["heartbeat"], last_tick_at=NOW - 60, tick_health="ok")

    result = _run(paths, now=NOW)

    assert result["action"] == "noop"
    assert captured_posts == []


def test_stale_heartbeat_alerts_exactly_once_per_day(paths, captured_posts):
    # Heartbeat last ticked 20 minutes ago (> 15 min default threshold).
    _write_heartbeat(paths["heartbeat"], last_tick_at=NOW - 20 * 60, tick_health="ok")

    first = _run(paths, now=NOW)
    second = _run(paths, now=NOW + 30)  # same calendar day, still stale

    assert first["action"] == "alert_emitted"
    assert first["alert_reason"] == "stale"
    assert second["action"] == "noop"
    assert second["reason"] == "already_alerted_today"
    assert len(captured_posts) == 1


def test_unhealthy_tick_health_alerts_even_when_fresh(paths, captured_posts):
    # Fresh tick (1 min ago) but the dispatcher reported a bad tick.
    _write_heartbeat(paths["heartbeat"], last_tick_at=NOW - 60, tick_health="degraded")

    first = _run(paths, now=NOW)
    second = _run(paths, now=NOW + 30)

    assert first["action"] == "alert_emitted"
    assert first["alert_reason"] == "tick_health"
    assert second["action"] == "noop"
    assert len(captured_posts) == 1


def test_new_calendar_day_alerts_again(paths, captured_posts):
    _write_heartbeat(paths["heartbeat"], last_tick_at=NOW - 20 * 60, tick_health="ok")
    _run(paths, now=NOW)

    # Next day, heartbeat still stale relative to the new "now".
    _write_heartbeat(paths["heartbeat"], last_tick_at=NEXT_DAY - 20 * 60, tick_health="ok")
    third = _run(paths, now=NEXT_DAY)

    assert third["action"] == "alert_emitted"
    assert len(captured_posts) == 2


def test_missing_heartbeat_alerts(paths, captured_posts):
    # No heartbeat file at all → dispatcher never wrote one → alert.
    result = _run(paths, now=NOW)

    assert result["action"] == "alert_emitted"
    assert result["alert_reason"] == "heartbeat_missing"
    assert len(captured_posts) == 1


def test_dry_run_never_posts(paths, captured_posts):
    _write_heartbeat(paths["heartbeat"], last_tick_at=NOW - 20 * 60, tick_health="ok")

    result = _run(paths, now=NOW, dry_run=True)

    assert result["action"] == "alert_would_have_fired"
    assert captured_posts == []
    # Dry-run must NOT persist the alert bucket, so a real run can still fire.
    assert not paths["state"].exists()


def test_evaluate_classifies_states():
    fresh = {"last_tick_at": int(NOW - 60), "tick_health": "ok"}
    healthy, reason, _ = wd.evaluate(fresh, now=NOW, stale_after_seconds=15 * 60)
    assert healthy is True and reason == "ok"

    stale = {"last_tick_at": int(NOW - 16 * 60), "tick_health": "ok"}
    healthy, reason, _ = wd.evaluate(stale, now=NOW, stale_after_seconds=15 * 60)
    assert healthy is False and reason == "stale"

    bad = {"last_tick_at": int(NOW - 60), "tick_health": "error"}
    healthy, reason, _ = wd.evaluate(bad, now=NOW, stale_after_seconds=15 * 60)
    assert healthy is False and reason == "tick_health"


def test_default_hermes_root_collapses_profile_path():
    """The watchdog must read from where the dispatcher writes.

    The writer uses ``get_default_hermes_root()``, which collapses a
    profile-scoped ``HERMES_HOME`` (``<root>/profiles/<name>``) back to
    ``<root>``. If the watchdog read the raw ``HERMES_HOME`` it would look in
    ``<root>/profiles/<name>/state`` and falsely cry "heartbeat missing"
    whenever it runs inside a profile env.
    """
    native = Path("/home/piet/.hermes")

    # Unset → native root.
    assert wd._default_hermes_root(native=native, env="") == native

    # Profile-scoped HERMES_HOME under the root → collapses to the root.
    assert (
        wd._default_hermes_root(native=native, env="/home/piet/.hermes/profiles/verifier")
        == native
    )

    # Root itself → native root.
    assert wd._default_hermes_root(native=native, env="/home/piet/.hermes") == native

    # Docker/custom profile layout outside the native root → grandparent.
    assert (
        wd._default_hermes_root(native=native, env="/opt/data/profiles/coder")
        == Path("/opt/data")
    )

    # Custom root outside ~/.hermes, not a profile → itself.
    assert (
        wd._default_hermes_root(native=native, env="/opt/data") == Path("/opt/data")
    )


def test_alert_send_failure_does_not_persist_bucket(paths, monkeypatch):
    """If Discord rejects the post, we must retry on the next run, not swallow it."""
    _write_heartbeat(paths["heartbeat"], last_tick_at=NOW - 20 * 60, tick_health="ok")
    monkeypatch.setattr(
        wd, "_post_discord", lambda body, channel: {"result": "error", "error": "http_500"}
    )

    result = _run(paths, now=NOW)

    assert result["action"] == "alert_send_failed"
    assert not paths["state"].exists()


# ---------------------------------------------------------------------------
# Classifier + formatter boundaries
# ---------------------------------------------------------------------------

def test_evaluate_zero_last_tick_is_invalid_not_stale():
    """A heartbeat with last_tick_at=0 never ticked — that is INVALID, not
    a 1970-vintage stale tick (which would mis-age the alert)."""
    healthy, reason, detail = wd.evaluate({"last_tick_at": 0}, now=1000.0)
    assert healthy is False
    assert reason == "heartbeat_invalid"
    assert detail == {"heartbeat_age_s": None}


def test_evaluate_age_exactly_at_threshold_is_still_healthy():
    """The staleness boundary is strict: at exactly the threshold the
    heartbeat is still fresh (age > threshold, not >=)."""
    heartbeat = {"last_tick_at": 900, "tick_health": "ok"}
    healthy, reason, _detail = wd.evaluate(
        heartbeat, now=1000.0, stale_after_seconds=100
    )
    assert healthy is True
    assert reason == "ok"


def test_format_alert_names_missing_and_invalid_reasons_distinctly():
    """Each reason renders its own line — an 'invalid' alert that reads
    'missing' sends the operator looking for the wrong failure."""
    missing = wd._format_alert(
        "heartbeat_missing", {"heartbeat_age_s": None}, stale_after_seconds=900
    )
    invalid = wd._format_alert(
        "heartbeat_invalid", {"heartbeat_age_s": None}, stale_after_seconds=900
    )
    assert "missing" in missing
    assert "unreadable" in invalid
    assert "unreadable" not in missing
    assert "missing" not in invalid

    stale = wd._format_alert(
        "stale",
        {"heartbeat_age_s": 950.0, "tick_health": "ok", "last_tick_at": 50},
        stale_after_seconds=900,
    )
    assert "stale" in stale
    assert "threshold 15 min" in stale


def test_read_token_preserves_equals_signs_inside_the_token(tmp_path, monkeypatch):
    """Bot tokens can contain '=' — the parser must split on the FIRST '='
    only and keep the rest verbatim, or the token is silently mangled."""
    env = tmp_path / ".env"
    env.write_text("DISCORD_BOT_TOKEN=ab=cd\n", encoding="utf-8")
    monkeypatch.setattr(wd, "ENV_FILE", env)
    assert wd._read_token() == "ab=cd"


def test_post_discord_caps_body_at_exactly_2000_chars(tmp_path, monkeypatch):
    """Discord rejects >2000 chars — the watchdog must slice to exactly
    2000, never 2001 (which would turn every long alert into http_400)."""
    env = tmp_path / ".env"
    env.write_text("DISCORD_BOT_TOKEN=tok\n", encoding="utf-8")
    monkeypatch.setattr(wd, "ENV_FILE", env)

    captured = {}

    class _Resp:
        def read(self):
            return b'{"id": "123"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        captured["content"] = json.loads(req.data.decode("utf-8"))["content"]
        return _Resp()

    monkeypatch.setattr(wd.urllib.request, "urlopen", fake_urlopen)

    result = wd._post_discord("x" * 3000)

    assert result["result"] == "sent"
    assert len(captured["content"]) == 2000


# ---------------------------------------------------------------------------
# Second pass: staleness constant + state serialization
# ---------------------------------------------------------------------------

def test_save_state_keeps_non_ascii_raw(tmp_path: Path):
    """State persists raw UTF-8 — escaped \\u sequences would make the
    state file unreadable to humans diffing it during an incident."""
    path = tmp_path / "state" / "watchdog.json"
    wd._save_state(path, {"note": "prüfung läuft"})
    raw = path.read_text(encoding="utf-8")
    assert "prüfung läuft" in raw
    assert json.loads(raw) == {"note": "prüfung läuft"}
