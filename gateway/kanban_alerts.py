"""Kanban alert engine (night-sprint F2) — push statt Pull.

Pure rule evaluation over the kanban board DB. The single notifications
watcher in ``gateway/kanban_watchers.py`` invokes these as rule hooks and owns
the I/O plumbing. Keeping the rules synchronous + connection-in/alerts-out
makes them testable without a gateway.

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
      ``held_critical``, ``deploy_failed``, ``held_red_gate``), plus the
      distinct ``auto_release_hook_crashed`` event kind (a hook crash never
      produced an outcome, so it rides the same rule as an always-alert
      case); ``deployed``/``held_live_test``/``aborted_pre_live_test`` stay
      silent (successes and expected holds).

Rate limit: max one alert per rule per ``cooldown_seconds`` (default 15 min).
A suppressed alert is dropped, not queued — the next qualifying event after
the cooldown alerts again. Alerts go to Discord only (Telegram ist ab).

Send-confirmation cursor gating (A1, 2026-07-06): the monotonic-cursor rules
(``operator_escalation``, ``auto_release_attention``, and since 2026-07-10
``run_failed`` — its run-id cursor is equally irreversible) accept an
optional ``send_fn`` — when given, the cursor for a rule only commits AFTER
``send_fn(alert)`` confirms delivery (truthy, no exception); a failed send
leaves the cursor untouched so the next tick re-fetches and retries the SAME
(possibly grown) batch instead of silently losing it. After
``_MAX_SEND_ATTEMPTS`` consecutive failures for that rule the retry gives up
and writes a digest-backstop entry (default: ``~/.hermes/reports/
kanban-alerts-backstop.log``, same durable-alert-file precedent as
``auto_release._default_notify``) instead — the event is documented, not
lost forever, and the cursor commits then too. ``send_fn``/``backstop_fn``
default to ``None``/the file writer; a caller that omits ``send_fn`` keeps
the PRE-fix eager-advance-before-send behavior unchanged (the production
watcher DOES pass it since the A2 wiring, 2026-07-06 — see
``gateway/kanban_watchers.py::_kanban_notifications_watcher``).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 300       # 5-min tick
DEFAULT_COOLDOWN_SECONDS = 900       # max 1 alert per rule per 15 min
DEFAULT_ERROR_RATE_THRESHOLD = 0.30
DEFAULT_ERROR_RATE_WINDOW_MINUTES = 30
DEFAULT_ERROR_RATE_MIN_RUNS = 5
_SNIPPET_MAX_CHARS = 280
_MAX_FAILURES_LISTED = 5
_OPERATOR_ESCALATION_EVENT = "operator_escalation"
_AUTO_RELEASE_EVENT = "auto_release"
_AUTO_RELEASE_HOOK_CRASHED_EVENT = "auto_release_hook_crashed"
_AUTO_RELEASE_HOOK_CRASHED_EMOJI = "🔴"
# outcome -> emoji, attention-only (deployed / held_live_test /
# aborted_pre_live_test are successes or expected operator-gated holds and
# stay silent — operator time is scarce).
_AUTO_RELEASE_ATTENTION_EMOJI = {
    "rolled_back": "🔴",
    "deploy_failed": "🔴",
    "held_critical": "🟡",
    "held_red_gate": "🟡",
}
_AUTO_RELEASE_DETAIL_MAX_CHARS = 200
# K: bounded consecutive-send-failure retries before a rule's digest-backstop
# takes over (A1, 2026-07-06). At the default 5-min tick interval, 3 tries
# spans ~10-15 min of retrying through a transient Discord blip (rate limit,
# momentary 5xx) — long enough to self-heal, short enough to not spam-hold an
# operator-attention event indefinitely. Same order of magnitude as this
# codebase's other small failure bounds (kanban_db.DEFAULT_FAILURE_LIMIT=2,
# auto_release's CONFLICT_FIXER_MAX_ATTEMPTS=2) rather than an arbitrary pick.
_MAX_SEND_ATTEMPTS = 3
# The two rules whose ``alert["rule"]`` this module already attempts to
# deliver ITSELF via ``send_fn`` (see ``_confirm_or_defer``) when a caller
# passes one. A caller wiring ``send_fn`` in must not also re-send an alert
# whose rule is in this set through its own post-hoc send loop — public so
# ``gateway.kanban_watchers`` can filter on it without duplicating the
# rule-name strings.
SEND_GATED_RULES = frozenset(
    {_OPERATOR_ESCALATION_EVENT, "auto_release_attention", "run_failed"}
)

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
    """Fresh watcher state for a first-ever alert evaluator.

    Production loads this state from disk after the first successful tick, so
    ordinary gateway restarts retain their confirmed cursors. Lazy MAX(id)
    initialization only suppresses history when no durable state exists yet.
    """
    return {
        "last_seen_run_id": None,
        "last_seen_operator_escalation_event_id": None,
        "last_seen_auto_release_event_id": None,
        "last_sent": {},
        # rule -> consecutive confirmed-send-failure count (A1, send-gated
        # cursor rules only; reset to 0 on a confirmed send or a backstop).
        "send_attempts": {},
    }


def load_alert_state(path: str | Path) -> dict:
    """Load durable alert cursors, falling back safely on missing/corrupt data."""
    state = new_alert_state()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return state
    if not isinstance(payload, dict):
        return state
    for key in (
        "last_seen_run_id",
        "last_seen_operator_escalation_event_id",
        "last_seen_auto_release_event_id",
    ):
        value = payload.get(key)
        if value is None or isinstance(value, int):
            state[key] = value
    for key in ("last_sent", "send_attempts"):
        value = payload.get(key)
        if isinstance(value, dict):
            state[key] = value
    return state


def save_alert_state(path: str | Path, state: dict) -> None:
    """Atomically persist confirmed cursors for restart/failover continuity."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp.replace(target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


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


def _default_send_backstop(entry: dict) -> None:
    """Durable fallback once a rule's send keeps failing (``_MAX_SEND_ATTEMPTS``
    consecutive tries): append to a log file so the event is documented
    instead of silently dropped. Same precedent as ``auto_release``'s
    ``_default_notify`` (``~/.hermes/reports/*.log``, fail-soft, UTC stamp) —
    a distinct file so the two backstops never interleave/clobber."""
    try:
        import datetime as _dt
        from pathlib import Path

        reports = Path.home() / ".hermes" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rule = entry.get("rule", "?")
        attempts = entry.get("attempts", "?")
        text = " ".join(str(entry.get("text", "")).split())
        with open(
            reports / "kanban-alerts-backstop.log", "a", encoding="utf-8",
        ) as fh:
            fh.write(f"{stamp} rule={rule} attempts={attempts} {text}\n")
    except Exception:
        pass


def _confirm_or_defer(
    state: dict,
    rule: str,
    cursor_key: str,
    new_cursor: int,
    alert: Optional[dict],
    send_fn: Optional[Callable[[dict], bool]],
    max_send_attempts: int,
    backstop_fn: Optional[Callable[[dict], None]],
) -> Optional[dict]:
    """Send-confirmation cursor gate shared by the two event-cursor rules.

    No-op / legacy path — ``alert is None`` (a silent batch: rows were found
    but none needed a push) or ``send_fn is None`` (caller did not opt into
    gating): commit ``state[cursor_key] = new_cursor`` unconditionally,
    exactly like before this fix (F2's original eager advance).

    Send-gated path (``send_fn`` given and there IS an alert): the cursor
    only commits after ``send_fn(alert)`` confirms delivery (truthy, no
    exception). A failed send leaves the cursor at its old value, so the next
    tick re-fetches and retries the SAME (possibly grown) batch instead of
    losing it. After ``max_send_attempts`` consecutive failures for this rule,
    stop retrying: write a backstop entry (default :func:`_default_send_backstop`)
    and commit the cursor anyway — documented, not lost forever.
    """
    if alert is None or send_fn is None:
        state[cursor_key] = new_cursor
        return alert
    attempts_by_rule = state.setdefault("send_attempts", {})
    # Codex review 2026-07-06 finding 2: the retry budget is keyed by BATCH
    # IDENTITY (rule + new_cursor), not by rule alone. new_cursor = max(id)
    # over the fetched batch, so when a NEW event arrives mid-retry the
    # batch identity changes and the counter resets — otherwise the new
    # event would inherit the old batch's failures and could be backstopped
    # after a single actual Discord attempt.
    entry = attempts_by_rule.get(rule)
    if not isinstance(entry, dict) or entry.get("cursor") != new_cursor:
        entry = {"cursor": new_cursor, "n": 0}
        attempts_by_rule[rule] = entry
    try:
        sent_ok = bool(send_fn(alert))
    except Exception:
        sent_ok = False
    if sent_ok:
        attempts_by_rule.pop(rule, None)
        state[cursor_key] = new_cursor
        return alert
    entry["n"] += 1
    if entry["n"] < max_send_attempts:
        return None  # deferred — cursor stays put, retry next tick
    (backstop_fn or _default_send_backstop)(
        {"rule": rule, "text": alert["text"], "attempts": entry["n"]}
    )
    attempts_by_rule.pop(rule, None)
    state[cursor_key] = new_cursor  # documented via backstop, not lost
    return None


def _rule_operator_escalation(
    conn: sqlite3.Connection,
    acfg: dict,
    state: dict,
    now: int,
    *,
    send_fn: Optional[Callable[[dict], bool]] = None,
    max_send_attempts: int = _MAX_SEND_ATTEMPTS,
    backstop_fn: Optional[Callable[[dict], None]] = None,
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
    new_cursor = max(int(r["id"]) for r in rows)

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
    return _confirm_or_defer(
        state, _OPERATOR_ESCALATION_EVENT,
        "last_seen_operator_escalation_event_id", new_cursor,
        alert, send_fn, max_send_attempts, backstop_fn,
    )


def _rule_auto_release_attention(
    conn: sqlite3.Connection,
    acfg: dict,
    state: dict,
    now: int,
    *,
    send_fn: Optional[Callable[[dict], bool]] = None,
    max_send_attempts: int = _MAX_SEND_ATTEMPTS,
    backstop_fn: Optional[Callable[[dict], None]] = None,
) -> Optional[dict]:
    """``auto_release`` task events that need operator attention, plus the
    distinct ``auto_release_hook_crashed`` event kind (A1, 2026-07-06).

    Same cursor/dedupe shape as ``_rule_operator_escalation`` (event-id
    cursor, not a time cooldown — each event alerts exactly once). Only
    ``rolled_back`` / ``held_critical`` / ``deploy_failed`` / ``held_red_gate``
    outcomes push; ``deployed`` / ``held_live_test`` / ``aborted_pre_live_test``
    stay silent. ``auto_release_hook_crashed`` never carries an ``outcome``
    (a hook crash produced no verdict at all) so it always pushes, fixed 🔴.
    """
    del now
    last_seen = state.get("last_seen_auto_release_event_id")
    if last_seen is None:
        row = conn.execute(
            "SELECT MAX(id) AS m FROM task_events WHERE kind IN (?, ?)",
            (_AUTO_RELEASE_EVENT, _AUTO_RELEASE_HOOK_CRASHED_EVENT),
        ).fetchone()
        state["last_seen_auto_release_event_id"] = (
            int(row["m"]) if row and row["m"] is not None else 0
        )
        return None

    rows = conn.execute(
        "SELECT id, task_id, kind, payload FROM task_events "
        "WHERE id > ? AND kind IN (?, ?) ORDER BY id ASC",
        (int(last_seen), _AUTO_RELEASE_EVENT, _AUTO_RELEASE_HOOK_CRASHED_EVENT),
    ).fetchall()
    if not rows:
        return None
    new_cursor = max(int(r["id"]) for r in rows)

    lines = []
    for r in rows:
        payload = _payload_dict(r["payload"])
        if r["kind"] == _AUTO_RELEASE_HOOK_CRASHED_EVENT:
            # No "outcome" — a hook crash never produced a verdict at all —
            # so this always alerts, fixed emoji, no outcome-emoji lookup.
            error = _snippet(payload.get("error")) or "kein Fehlertext"
            lines.append(
                f"{_AUTO_RELEASE_HOOK_CRASHED_EMOJI} **Auto-Release Hook "
                f"Crashed** (`{r['task_id']}`) — {error}"
            )
            continue
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
        if outcome == "rolled_back" and payload.get("rollback_ok"):
            # rollback_dashboard.sh leaves the live checkout DETACHED on the
            # anchor by design — the next human/agent must return it.
            lines.append(
                "⚠️ Live-Checkout ist DETACHED auf dem Anchor — nach Triage: "
                "`git checkout main` in ~/.hermes/hermes-agent."
            )
    alert = None
    if lines:
        lines.append("Details: /control Fleet → Plan-Tab, Auto-Release-Kachel.")
        alert = {"rule": "auto_release_attention", "text": "\n".join(lines)}
        # Operator-attention alert: prefer the escalation channel (same
        # routing as operator_escalation) so it delivers even when only that
        # channel is set.
        if acfg.get("escalation_channel_id"):
            alert["channel_id"] = acfg["escalation_channel_id"]
    return _confirm_or_defer(
        state, "auto_release_attention", "last_seen_auto_release_event_id",
        new_cursor, alert, send_fn, max_send_attempts, backstop_fn,
    )


def _rule_run_failed(
    conn: sqlite3.Connection,
    acfg: dict,
    state: dict,
    now: int,
    *,
    send_fn: Optional[Callable[[dict], bool]] = None,
    max_send_attempts: int = _MAX_SEND_ATTEMPTS,
    backstop_fn: Optional[Callable[[dict], None]] = None,
) -> Optional[dict]:
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
    if not failures:
        # Silent batch — nothing to deliver, eager cursor commit is safe.
        state["last_seen_run_id"] = max_id
        return None
    # A deferred batch (send failed last tick, cursor NOT advanced) must
    # bypass the cooldown: _cooldown_ok stamped last_sent on the failed
    # attempt, so without the bypass the retry tick would fall into the
    # suppression branch below and eagerly commit the cursor — losing the
    # very batch the send gate deferred.
    _attempts = state.get("send_attempts")
    _pending_retry = isinstance(_attempts, dict) and "run_failed" in _attempts
    if not _pending_retry and not _cooldown_ok(
        state, "run_failed", now, acfg["cooldown_seconds"]
    ):
        # Cooldown suppression is deliberate anti-spam (pre-fix semantics):
        # these failures are dropped by design, so the cursor advances.
        state["last_seen_run_id"] = max_id
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
    alert = {"rule": "run_failed", "text": "\n".join(lines)}
    # Send-confirmation cursor gate (same as the two event-cursor rules):
    # the run-id cursor is MONOTONIC — advancing it before a confirmed send
    # meant one failed/ratelimited Discord delivery lost those specific run
    # failures forever (not just delayed).  Without send_fn (legacy caller)
    # _confirm_or_defer commits eagerly, exactly like pre-fix.
    return _confirm_or_defer(
        state, "run_failed", "last_seen_run_id",
        max_id, alert, send_fn, max_send_attempts, backstop_fn,
    )


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
    send_fn: Optional[Callable[[dict], bool]] = None,
    max_send_attempts: int = _MAX_SEND_ATTEMPTS,
    backstop_fn: Optional[Callable[[dict], None]] = None,
) -> list[dict]:
    """Run all rules against the board; returns ``[{rule, text}, ...]``.

    Mutates ``state`` (run cursor + per-rule rate-limit stamps). Each rule is
    individually fail-soft — one broken rule never blocks the others.

    ``send_fn`` (A1, 2026-07-06; extended to ``run_failed`` 2026-07-10):
    when given, it gates the MONOTONIC-cursor rules (``operator_escalation``,
    ``auto_release_attention``, ``run_failed`` — see ``_confirm_or_defer``
    and ``SEND_GATED_RULES``) on a confirmed send instead of advancing the
    cursor eagerly. ``error_rate``/``daily_cost`` stay ungated (no cursor —
    window-based, the condition re-fires after cooldown, so a dropped send
    is bounded, not permanent). The production watcher
    (``gateway.kanban_watchers._kanban_notifications_watcher``) DOES pass
    ``send_fn`` and filters ``SEND_GATED_RULES`` out of its post-hoc send
    loop; a caller that omits ``send_fn`` keeps the pre-fix
    eager-advance-before-send behavior.
    """
    ts = int(now if now is not None else time.time())
    alerts: list[dict] = []
    for rule_fn in (_rule_error_rate, _rule_daily_cost):
        try:
            alert = rule_fn(conn, acfg, state, ts)
        except Exception:
            # A broken rule must not kill the watcher tick, but dying
            # silently means e.g. a schema drift disables the rule forever
            # with zero trace — leave a breadcrumb.
            logger.warning(
                "kanban alerts: rule %s failed", rule_fn.__name__, exc_info=True
            )
            continue
        if alert is not None:
            alerts.append(alert)
    for rule_fn in (_rule_run_failed, _rule_operator_escalation, _rule_auto_release_attention):
        try:
            alert = rule_fn(
                conn, acfg, state, ts,
                send_fn=send_fn,
                max_send_attempts=max_send_attempts,
                backstop_fn=backstop_fn,
            )
        except Exception:
            logger.warning(
                "kanban alerts: rule %s failed", rule_fn.__name__, exc_info=True
            )
            continue
        if alert is not None:
            alerts.append(alert)
    return alerts
