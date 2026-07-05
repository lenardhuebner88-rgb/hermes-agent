"""Kanban alert engine (night-sprint F2) — push statt Pull.

Pure rule evaluation over the kanban board DB; the asyncio watcher lives in
``gateway/kanban_watchers.py`` (``_kanban_alerts_watcher``) and only does the
plumbing (config gate, tick loop, Discord adapter send). Keeping the rules
synchronous + connection-in/alerts-out makes them testable without a gateway.

Rules (config: ``kanban.alerts:`` in the ROOT config.yaml, additive):

  (a) ``run_failed``  — newly ended failed/blocked runs since the last tick,
      reported with task title + profile + error snippet.
  (b) ``error_rate``  — failure rate above a threshold (default >30%) over a
      rolling window (default 30 min), given a minimum sample size.
  (c) ``daily_cost``  — rolling-24h cost (K17 ``task_runs.cost_usd`` stamps,
      ``started_at`` window — same semantics as the C1 budget gate) above a
      configured USD threshold. No threshold configured = rule off.
  (d) ``auto_release_attention`` — ``auto_release`` task events (Subsystem C3)
      whose outcome needs operator attention (``rolled_back``,
      ``held_critical``, ``deploy_failed``); ``deployed``/``held_live_test``/
      ``aborted_pre_live_test`` stay silent (successes and expected holds).

Rate limit: max one alert per rule per ``cooldown_seconds`` (default 15 min).
A suppressed alert is dropped, not queued — the next qualifying event after
the cooldown alerts again. Alerts go to Discord only (Telegram ist ab).
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

DEFAULT_INTERVAL_SECONDS = 300       # 5-min tick
DEFAULT_COOLDOWN_SECONDS = 900       # max 1 alert per rule per 15 min
DEFAULT_ERROR_RATE_THRESHOLD = 0.30
DEFAULT_ERROR_RATE_WINDOW_MINUTES = 30
DEFAULT_ERROR_RATE_MIN_RUNS = 5
_SNIPPET_MAX_CHARS = 280
_MAX_FAILURES_LISTED = 5
_OPERATOR_ESCALATION_EVENT = "operator_escalation"
_AUTO_RELEASE_EVENT = "auto_release"
# outcome -> emoji, attention-only (deployed / held_live_test /
# aborted_pre_live_test are successes or expected operator-gated holds and
# stay silent — operator time is scarce).
_AUTO_RELEASE_ATTENTION_EMOJI = {
    "rolled_back": "🔴",
    "deploy_failed": "🔴",
    "held_critical": "🟡",
}
_AUTO_RELEASE_DETAIL_MAX_CHARS = 200

# Terminal failure classification. status covers the run row's own state;
# outcome covers the dispatcher's verdict (same family the reliability stats
# use — kanban_db._RELIABILITY_FAIL_OUTCOMES plus the explicit blocked/failed
# pair the plan names).
FAIL_STATUSES = frozenset({"failed", "blocked", "crashed", "timed_out"})
FAIL_OUTCOMES = frozenset({
    "blocked", "crashed", "timed_out", "spawn_failed", "gave_up",
    "iteration_budget_exhausted",
})


def load_alerts_config(cfg: Any) -> dict:
    """Normalize ``kanban.alerts`` from a loaded root config (fail-soft).

    ``enabled`` defaults to **False** so only the gateway whose config
    explicitly opts in alerts (the research gateway etc. stay silent).
    ``channel_id`` falls back to ``kanban.reporting_channel_id`` so an
    existing reporting route can be reused without new wiring.
    """
    kanban_cfg = cfg.get("kanban") if isinstance(cfg, dict) else None
    kanban_cfg = kanban_cfg if isinstance(kanban_cfg, dict) else {}
    raw = kanban_cfg.get("alerts")
    raw = raw if isinstance(raw, dict) else {}

    def _num(key: str, default: float, cast=float) -> float:
        try:
            return cast(raw.get(key, default))
        except (TypeError, ValueError):
            return default

    channel_id = str(raw.get("channel_id") or "").strip()
    if not channel_id:
        for fallback_key in ("reporting_channel_id", "reporting_discord_channel_id"):
            fallback = str(kanban_cfg.get(fallback_key) or "").strip()
            if fallback:
                channel_id = fallback
                break
    escalation_channel_id = str(raw.get("escalation_channel_id") or "").strip()
    if not escalation_channel_id:
        escalation_channel_id = channel_id

    threshold = raw.get("daily_cost_threshold_usd")
    try:
        daily_cost_threshold = float(threshold) if threshold is not None else None
    except (TypeError, ValueError):
        daily_cost_threshold = None

    return {
        "enabled": bool(raw.get("enabled", False)),
        "channel_id": channel_id or None,
        "escalation_channel_id": escalation_channel_id or None,
        "thread_id": str(raw.get("thread_id") or "").strip() or None,
        "interval_seconds": max(30.0, _num("interval_seconds", DEFAULT_INTERVAL_SECONDS)),
        "cooldown_seconds": max(0.0, _num("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)),
        "error_rate_threshold": _num("error_rate_threshold", DEFAULT_ERROR_RATE_THRESHOLD),
        "error_rate_window_minutes": max(1, int(_num("error_rate_window_minutes", DEFAULT_ERROR_RATE_WINDOW_MINUTES, int))),
        "error_rate_min_runs": max(1, int(_num("error_rate_min_runs", DEFAULT_ERROR_RATE_MIN_RUNS, int))),
        "daily_cost_threshold_usd": daily_cost_threshold,
    }


def new_alert_state() -> dict:
    """Fresh watcher state. ``last_seen_run_id`` is lazily initialized to the
    current MAX(id) on the first tick so a gateway (re)start never replays
    historic failures as fresh alerts."""
    return {
        "last_seen_run_id": None,
        "last_seen_operator_escalation_event_id": None,
        "last_seen_auto_release_event_id": None,
        "last_sent": {},
    }


def _cooldown_ok(state: dict, rule: str, now: int, cooldown_seconds: float) -> bool:
    """True (and stamps the rule) when the rule may alert now."""
    last = (state.get("last_sent") or {}).get(rule)
    if last is not None and (now - last) < cooldown_seconds:
        return False
    state.setdefault("last_sent", {})[rule] = now
    return True


def _snippet(text: Optional[str]) -> str:
    s = " ".join(str(text or "").split())
    if len(s) > _SNIPPET_MAX_CHARS:
        s = s[: _SNIPPET_MAX_CHARS - 1] + "…"
    return s


def _is_failure(status: Optional[str], outcome: Optional[str]) -> bool:
    return (
        (status or "").strip().lower() in FAIL_STATUSES
        or (outcome or "").strip().lower() in FAIL_OUTCOMES
    )


def _payload_dict(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _rule_operator_escalation(
    conn: sqlite3.Connection,
    acfg: dict,
    state: dict,
    now: int,
) -> Optional[dict]:
    del now
    last_seen = state.get("last_seen_operator_escalation_event_id")
    if last_seen is None:
        row = conn.execute(
            "SELECT MAX(id) AS m FROM task_events WHERE kind = ?",
            (_OPERATOR_ESCALATION_EVENT,),
        ).fetchone()
        state["last_seen_operator_escalation_event_id"] = (
            int(row["m"]) if row and row["m"] is not None else 0
        )
        return None

    rows = conn.execute(
        "SELECT e.id, e.task_id, e.payload, t.title "
        "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
        "WHERE e.id > ? AND e.kind = ? "
        "ORDER BY e.id ASC",
        (int(last_seen), _OPERATOR_ESCALATION_EVENT),
    ).fetchall()
    if not rows:
        return None
    state["last_seen_operator_escalation_event_id"] = max(int(r["id"]) for r in rows)

    lines = [
        f"🚨 **Kanban operator escalation:** {len(rows)} task(s) need human action"
    ]
    for r in rows[:_MAX_FAILURES_LISTED]:
        payload = _payload_dict(r["payload"])
        task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        title = _snippet(task.get("title") or r["title"]) or "(ohne Titel)"
        task_id = task.get("id") or r["task_id"]
        why = _snippet(payload.get("why_now")) or "retry ladder exhausted"
        action = _snippet(payload.get("recommended_human_action"))
        line = f"• **{title}** (`{task_id}`) — {why}"
        if action:
            line += f"; action: {action}"
        lines.append(line)
    if len(rows) > _MAX_FAILURES_LISTED:
        lines.append(f"… und {len(rows) - _MAX_FAILURES_LISTED} weitere")
    alert = {"rule": _OPERATOR_ESCALATION_EVENT, "text": "\n".join(lines)}
    if acfg.get("escalation_channel_id"):
        alert["channel_id"] = acfg["escalation_channel_id"]
    return alert


def _rule_auto_release_attention(
    conn: sqlite3.Connection,
    acfg: dict,
    state: dict,
    now: int,
) -> Optional[dict]:
    """``auto_release`` task events that need operator attention.

    Same cursor/dedupe shape as ``_rule_operator_escalation`` (event-id
    cursor, not a time cooldown — each event alerts exactly once). Only
    ``rolled_back`` / ``held_critical`` / ``deploy_failed`` outcomes push;
    ``deployed`` / ``held_live_test`` / ``aborted_pre_live_test`` stay silent.
    """
    del now
    last_seen = state.get("last_seen_auto_release_event_id")
    if last_seen is None:
        row = conn.execute(
            "SELECT MAX(id) AS m FROM task_events WHERE kind = ?",
            (_AUTO_RELEASE_EVENT,),
        ).fetchone()
        state["last_seen_auto_release_event_id"] = (
            int(row["m"]) if row and row["m"] is not None else 0
        )
        return None

    rows = conn.execute(
        "SELECT id, task_id, payload FROM task_events "
        "WHERE id > ? AND kind = ? ORDER BY id ASC",
        (int(last_seen), _AUTO_RELEASE_EVENT),
    ).fetchall()
    if not rows:
        return None
    state["last_seen_auto_release_event_id"] = max(int(r["id"]) for r in rows)

    lines = []
    for r in rows:
        payload = _payload_dict(r["payload"])
        outcome = str(payload.get("outcome") or "").strip()
        emoji = _AUTO_RELEASE_ATTENTION_EMOJI.get(outcome)
        if emoji is None:
            continue  # deployed / held_live_test / aborted_pre_live_test — no push
        detail = " ".join(str(payload.get("detail") or "").split())
        if len(detail) > _AUTO_RELEASE_DETAIL_MAX_CHARS:
            detail = detail[: _AUTO_RELEASE_DETAIL_MAX_CHARS - 1] + "…"
        line = f"{emoji} **Auto-Release {outcome}** (`{r['task_id']}`)"
        if detail:
            line += f" — {detail}"
        lines.append(line)
    if not lines:
        return None
    lines.append("Details: /control Fleet → Plan-Tab, Auto-Release-Kachel.")
    alert = {"rule": "auto_release_attention", "text": "\n".join(lines)}
    # Operator-attention alert: prefer the escalation channel (same routing as
    # operator_escalation) so it delivers even when only that channel is set.
    if acfg.get("escalation_channel_id"):
        alert["channel_id"] = acfg["escalation_channel_id"]
    return alert


def _rule_run_failed(conn: sqlite3.Connection, acfg: dict, state: dict, now: int) -> Optional[dict]:
    last_seen = state.get("last_seen_run_id")
    if last_seen is None:
        row = conn.execute("SELECT MAX(id) AS m FROM task_runs").fetchone()
        state["last_seen_run_id"] = int(row["m"]) if row and row["m"] is not None else 0
        return None
    rows = conn.execute(
        "SELECT r.id, r.profile, r.status, r.outcome, r.error, r.summary, "
        "       t.title "
        "FROM task_runs r LEFT JOIN tasks t ON t.id = r.task_id "
        "WHERE r.id > ? AND r.ended_at IS NOT NULL "
        "ORDER BY r.id ASC",
        (int(last_seen),),
    ).fetchall()
    failures = []
    max_id = int(last_seen)
    for r in rows:
        max_id = max(max_id, int(r["id"]))
        if _is_failure(r["status"], r["outcome"]):
            failures.append(r)
    state["last_seen_run_id"] = max_id
    if not failures:
        return None
    if not _cooldown_ok(state, "run_failed", now, acfg["cooldown_seconds"]):
        return None
    lines = [f"🔴 **Kanban-Alert:** {len(failures)} Run(s) failed/blocked"]
    for r in failures[:_MAX_FAILURES_LISTED]:
        title = _snippet(r["title"]) or "(ohne Titel)"
        profile = (r["profile"] or "?").strip() or "?"
        kind = (r["status"] or "").strip().lower() or (r["outcome"] or "?").strip().lower()
        err = _snippet(r["error"]) or _snippet(r["summary"]) or "kein Fehlertext"
        lines.append(f"• **{title}** ({profile}) — {kind}: {err}")
    if len(failures) > _MAX_FAILURES_LISTED:
        lines.append(f"… und {len(failures) - _MAX_FAILURES_LISTED} weitere")
    return {"rule": "run_failed", "text": "\n".join(lines)}


def _rule_error_rate(conn: sqlite3.Connection, acfg: dict, state: dict, now: int) -> Optional[dict]:
    window_start = now - acfg["error_rate_window_minutes"] * 60
    status_ph = ",".join("?" for _ in FAIL_STATUSES)
    outcome_ph = ",".join("?" for _ in FAIL_OUTCOMES)
    row = conn.execute(
        f"SELECT COUNT(*) AS total, "
        f"  SUM(CASE WHEN lower(trim(coalesce(status, ''))) IN ({status_ph}) "
        f"        OR lower(trim(coalesce(outcome, ''))) IN ({outcome_ph}) "
        f"      THEN 1 ELSE 0 END) AS failed "
        f"FROM task_runs WHERE ended_at IS NOT NULL AND ended_at >= ?",
        (*FAIL_STATUSES, *FAIL_OUTCOMES, window_start),
    ).fetchone()
    total = int(row["total"] or 0)
    failed = int(row["failed"] or 0)
    if total < acfg["error_rate_min_runs"]:
        return None
    rate = failed / total
    if rate <= acfg["error_rate_threshold"]:
        return None
    if not _cooldown_ok(state, "error_rate", now, acfg["cooldown_seconds"]):
        return None
    return {
        "rule": "error_rate",
        "text": (
            f"🟠 **Kanban-Alert:** Fehlerrate {rate:.0%} "
            f"({failed}/{total} Runs) in den letzten "
            f"{acfg['error_rate_window_minutes']} min — Schwelle "
            f"{acfg['error_rate_threshold']:.0%}."
        ),
    }


def _rule_daily_cost(conn: sqlite3.Connection, acfg: dict, state: dict, now: int) -> Optional[dict]:
    threshold = acfg.get("daily_cost_threshold_usd")
    if threshold is None:
        return None
    # started_at window — deliberately the same semantics as the C1 budget
    # gate so "alert" and "dispatch hold" describe the same number.
    try:
        row = conn.execute(
            "SELECT SUM(cost_usd) AS c FROM task_runs WHERE started_at >= ?",
            (now - 86400,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # pre-K5a DB without cost_usd — rule silently off
    cost = float(row["c"]) if row and row["c"] is not None else 0.0
    if cost <= threshold:
        return None
    if not _cooldown_ok(state, "daily_cost", now, acfg["cooldown_seconds"]):
        return None
    return {
        "rule": "daily_cost",
        "text": (
            f"🟡 **Kanban-Alert:** Tageskosten ${cost:.2f} über Schwelle "
            f"${threshold:.2f} (rollierende 24 h, K17-Stamps)."
        ),
    }


def evaluate_alerts(
    conn: sqlite3.Connection,
    acfg: dict,
    state: dict,
    *,
    now: Optional[int] = None,
) -> list[dict]:
    """Run all rules against the board; returns ``[{rule, text}, ...]``.

    Mutates ``state`` (run cursor + per-rule rate-limit stamps). Each rule is
    individually fail-soft — one broken rule never blocks the others.
    """
    ts = int(now if now is not None else time.time())
    alerts: list[dict] = []
    for rule_fn in (
        _rule_run_failed,
        _rule_error_rate,
        _rule_daily_cost,
        _rule_operator_escalation,
        _rule_auto_release_attention,
    ):
        try:
            alert = rule_fn(conn, acfg, state, ts)
        except Exception:
            continue
        if alert is not None:
            alerts.append(alert)
    return alerts
