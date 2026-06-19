"""Kanban board watcher methods for GatewayRunner.

Extracted verbatim from ``gateway/run.py`` (god-file decomposition Phase 3).
These are the background-loop methods that subscribe to kanban boards, deliver
notifications/artifacts, and drive the multi-agent dispatcher. They use only
``self`` state, so they live on a mixin that ``GatewayRunner`` inherits — the
``self._kanban_*`` call sites resolve identically via the MRO, making this a
behavior-neutral move that lifts ~1,000 LOC out of run.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")

_COMPLETED_HANDOFF_LIMIT = 1600

_REPORTING_ROUTE_KINDS = {
    "received", "completed", "blocked", "gave_up", "tree_stalled_flush",
}
_NORMAL_REPORT_KINDS = {"received", "completed", "tree_stalled_flush"}


def _clean_completed_handoff(text: str, *, limit: int = _COMPLETED_HANDOFF_LIMIT) -> str:
    """Return a Discord-sized, result-focused completion handoff body."""
    lines = [line.rstrip() for line in str(text or "").strip().splitlines()]
    cleaned: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        cleaned.append(line)
        previous_blank = blank
    handoff = "\n".join(cleaned).strip()
    if len(handoff) <= limit:
        return handoff
    return handoff[: max(0, limit - 12)].rstrip() + " … [gekürzt]"


def _completed_handoff_text(event: Any, task: Any, run: Any = None) -> str:
    """Prefer the full run summary, falling back to legacy compact fields."""
    if run is not None and getattr(run, "summary", None):
        return _clean_completed_handoff(str(run.summary))
    if getattr(event, "payload", None) and event.payload.get("summary"):
        return _clean_completed_handoff(str(event.payload["summary"]))
    if task and getattr(task, "result", None):
        return _clean_completed_handoff(str(task.result))
    return ""


def _first_nonempty_line(text: str, *, limit: int = 180) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _completed_kurzfazit(text: str) -> str:
    short = _first_nonempty_line(text) or "abgeschlossen"
    if short.lower().startswith("kurzfazit:"):
        short = short.split(":", 1)[1].strip() or "abgeschlossen"
    return short


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _format_duration_seconds(started_at: Any, ended_at: Any) -> str:
    try:
        if started_at is None or ended_at is None:
            return ""
        seconds = int(ended_at) - int(started_at)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def _run_metadata(run: Any) -> dict[str, Any]:
    metadata = getattr(run, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


_COMPLETED_VERDICT_TOKENS = {
    "APPROVED": "APPROVED",
    "REQUEST_CHANGES": "REQUEST_CHANGES",
    "NEEDS_REVISION": "REQUEST_CHANGES",
}


def _completed_verdict_line(run: Any) -> str:
    """Compact 'Verdict + Verifikation' line, or '' when there is no verifier signal.

    Mirrors the dashboard's verdict normalisation (plugins/kanban/dashboard/
    plugin_api.py ``_normalize_verifier_verdict``): prefer ``metadata['verdict']``,
    else parse the leading token of the run summary. Only emits a line for a
    recognised verifier verdict so non-gated tasks stay quiet.
    """
    metadata = _run_metadata(run)
    raw = metadata.get("verdict")
    if not isinstance(raw, str) or not raw.strip():
        summary = str(getattr(run, "summary", "") or "")
        first = next((line.strip() for line in summary.splitlines() if line.strip()), "")
        raw = first.split("—", 1)[0].split(":", 1)[0].strip() if first else ""
    token = str(raw or "").strip().upper().replace(" ", "_").replace("-", "_")
    verdict = _COMPLETED_VERDICT_TOKENS.get(token)
    if not verdict:
        return ""
    state = "Verifier-approved" if verdict == "APPROVED" else "Änderungen angefordert"
    return f"Verdict: {verdict} | Verifikation: {state}"


def _dashboard_link(kanban_cfg: Optional[dict], task_id: str) -> str:
    """Build a clickable /control deep-link to the task, config-driven.

    Uses ``kanban.dashboard_url`` (e.g. the tailnet ``https://…:9443/control``)
    when set so Discord links are externally reachable; falls back to the local
    loopback dashboard otherwise. ``?focus=`` is honoured by the Backlog view.
    """
    base = ""
    if isinstance(kanban_cfg, dict):
        base = _string_config_value(kanban_cfg, "dashboard_url", "dashboard_base_url")
    base = (base or "http://127.0.0.1:9119/control").rstrip("/")
    if task_id and task_id != "<unknown>":
        return f"{base}/backlog?focus={task_id}"
    return base


def _format_completed_report(
    event: Any,
    task: Any,
    run: Any,
    *,
    board: Optional[str] = None,
    kanban_cfg: Optional[dict] = None,
) -> str:
    """Compact completion report (K1 de-flood).

    Replaces the prior multi-section template (highlights / artifacts /
    open-points lists) that made completed reports *grow* — audit finding D4.
    The result body still surfaces in full; artifact paths are delivered as
    native uploads separately, so dropping the list sections loses no signal.
    Shape: ✅-header / Kurzfazit / Task id+title / Status (+Verdict) / Ergebnis
    body / Dashboard link.
    """
    task_id = getattr(event, "task_id", None) or getattr(task, "id", "") or "<unknown>"
    title = (getattr(task, "title", None) or task_id)[:160]
    status = getattr(task, "status", None) or "done"
    profile = getattr(task, "assignee", None) or getattr(run, "profile", None) or "unbekannt"
    handoff = _completed_handoff_text(event, task, run)
    short = _completed_kurzfazit(handoff)

    status_line = f"Status: {status} | Profil: {profile}"
    if board:
        status_line += f" | Board: {board}"

    lines = [
        "✅ Hermes Report — Task abgeschlossen",
        f"Kurzfazit: {short}",
        f"Task: {task_id} — {title}",
        status_line,
    ]
    verdict_line = _completed_verdict_line(run)
    if verdict_line:
        lines.append(verdict_line)
    lines.extend([
        "Ergebnis:",
        handoff or "kein Ergebnistext hinterlegt",
        f"Dashboard: {_dashboard_link(kanban_cfg, task_id)}",
    ])
    duration = _format_duration_seconds(
        getattr(run, "started_at", None), getattr(run, "ended_at", None),
    )
    if duration:
        lines.append(f"Laufzeit: {duration}")
    return _clean_completed_handoff("\n".join(lines), limit=1900)


# ── K2: per-tree completed-report aggregation ───────────────────────────────
# In a decomposed graph, ``task_links(parent_id, child_id)`` means "child waits
# for parent". The orchestrator ROOT is linked as the child of every leaf, so
# it is the *sink* that completes LAST, after all its transitive parents (the
# work tasks) are terminal. We therefore: suppress the per-child completed
# report of every interior work node (it has children depending on it) and emit
# ONE consolidated report when the sink/root completes. Standalone tasks (no
# links) are untouched. Failure kinds (blocked/gave_up/crashed/timed_out) keep
# their per-task pings — only the ``completed`` success path is rolled up.
_MAX_TREE_SUBTASKS_SHOWN = 15


def _collect_tree_members(conn: Any, root_id: str, _kb: Any) -> list[str]:
    """Return all transitive parents (work tasks) of a sink/root, cycle-safe."""
    seen: set[str] = set()
    members: list[str] = []
    stack = list(_kb.parent_ids(conn, root_id))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        members.append(pid)
        stack.extend(_kb.parent_ids(conn, pid))
    return members


def _tree_member_detail(conn: Any, member_id: str, _kb: Any) -> Optional[dict[str, Any]]:
    task = _kb.get_task(conn, member_id)
    if task is None:
        return None
    title = (getattr(task, "title", None) or member_id)[:80]
    status = getattr(task, "status", None) or "?"
    summary = ""
    try:
        for run in reversed(_kb.list_runs(conn, member_id)):
            if getattr(run, "summary", None):
                summary = str(run.summary)
                break
    except Exception:
        summary = ""
    if not summary:
        summary = str(getattr(task, "result", "") or "")
    return {
        "id": member_id,
        "title": title,
        "status": status,
        "kurz": _completed_kurzfazit(summary) if summary.strip() else "",
        "created_at": getattr(task, "created_at", 0) or 0,
    }


def _format_tree_completed_report(
    task: Any,
    run: Any,
    event: Any,
    members: list[dict[str, Any]],
    *,
    board: Optional[str] = None,
    kanban_cfg: Optional[dict] = None,
) -> str:
    """One consolidated completion report for a whole decomposed tree (K2)."""
    root_id = getattr(task, "id", None) or getattr(event, "task_id", None) or "<unknown>"
    root_title = (getattr(task, "title", None) or root_id)[:160]
    root_status = getattr(task, "status", None) or "done"
    profile = getattr(task, "assignee", None) or getattr(run, "profile", None) or "unbekannt"
    handoff = _completed_handoff_text(event, task, run)
    short = _completed_kurzfazit(handoff)

    total = len(members)
    done = sum(1 for m in members if m["status"] in {"done", "archived"})
    status_line = f"Status: {root_status} | Profil: {profile}"
    if board:
        status_line += f" | Board: {board}"

    lines = [
        f"✅ Hermes Report — Auftrag abgeschlossen ({total} Teilaufgaben)",
        f"Kurzfazit: {short}",
        f"Auftrag: {root_id} — {root_title}",
        status_line,
    ]
    verdict_line = _completed_verdict_line(run)
    if verdict_line:
        lines.append(verdict_line)
    if members:
        lines.append(f"Teilaufgaben ({done}/{total} erledigt):")
        for m in members[:_MAX_TREE_SUBTASKS_SHOWN]:
            if m["status"] in {"done", "archived"}:
                mark = "✅"
            elif m["status"] == "blocked":
                mark = "⏸"
            else:
                mark = "•"
            suffix = f": {m['kurz']}" if m["kurz"] else ""
            lines.append(f"- {mark} {m['id']} {m['title']}{suffix}")
        if total > _MAX_TREE_SUBTASKS_SHOWN:
            lines.append(f"- … +{total - _MAX_TREE_SUBTASKS_SHOWN} weitere")
    lines.extend([
        "Ergebnis:",
        handoff or "kein Ergebnistext hinterlegt",
        f"Dashboard: {_dashboard_link(kanban_cfg, root_id)}",
    ])
    return _clean_completed_handoff("\n".join(lines), limit=1900)


# ── F1 (S2): flush suppressed child-successes for abandoned/stalled roots ────
# K2 suppresses an interior work node's ``completed`` report and rolls it into
# the ONE report the sink/root emits when it completes. But if a root NEVER
# becomes terminal — a member is sticky-blocked or the circuit-breaker gave_up,
# so the sink can never go ``done`` — those already-consumed child successes are
# swallowed forever (no emitter fires). F1 detects such a stalled root and
# flushes the suppressed successes as ONE trailing ``tree_stalled_flush`` report.
# Emitted at most once per root (dedup marker = the event itself) and only while
# the root is non-terminal; if the root later completes, K2's consolidated
# report fires normally and no flush is added (we never flush a terminal root).
def _member_is_dead_end(conn: Any, member_id: str, now: int, threshold_s: float) -> bool:
    """True when a tree member is a permanent dead-end that strands its root.

    Either (a) sticky-blocked (worker/operator ``kanban_block``, not a
    recoverable circuit-breaker) with the newest ``blocked`` event at least
    ``threshold_s`` old, or (b) a ``gave_up`` circuit-breaker event at least
    ``threshold_s`` old. Read-only; fail-soft (any hiccup → not-dead-end).
    """
    from hermes_cli import kanban_db as _kb
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (member_id,)
        ).fetchone()
    except Exception:
        return False
    if row is None:
        return False
    if row["status"] == "blocked":
        try:
            if _kb._has_sticky_block(conn, member_id):
                last = conn.execute(
                    "SELECT MAX(created_at) FROM task_events "
                    "WHERE task_id = ? AND kind = 'blocked'",
                    (member_id,),
                ).fetchone()[0]
                if last is not None and (now - int(last)) >= threshold_s:
                    return True
        except Exception:
            pass
    try:
        last_gu = conn.execute(
            "SELECT MAX(created_at) FROM task_events "
            "WHERE task_id = ? AND kind = 'gave_up'",
            (member_id,),
        ).fetchone()[0]
    except Exception:
        last_gu = None
    return last_gu is not None and (now - int(last_gu)) >= threshold_s


def _format_tree_stall_flush_report(
    conn: Any,
    root_id: str,
    members: list[dict[str, Any]],
    _kb: Any,
    *,
    kanban_cfg: Optional[dict] = None,
) -> str:
    """One trailing report for a stalled root's suppressed child-successes (F1)."""
    root_task = _kb.get_task(conn, root_id)
    root_title = (getattr(root_task, "title", None) or root_id)[:160]
    total = len(members)
    done = sum(1 for m in members if m["status"] in {"done", "archived"})
    lines = [
        f"⚠️ Hermes Report — Auftrag steckt fest "
        f"({done}/{total} Teilaufgaben fertig, Root nicht abgeschlossen)",
        f"Auftrag: {root_id} — {root_title}",
        "Bereits erledigte (bisher unterdrückte) Teilaufgaben:",
    ]
    for m in members[:_MAX_TREE_SUBTASKS_SHOWN]:
        if m["status"] in {"done", "archived"}:
            mark = "✅"
        elif m["status"] == "blocked":
            mark = "⏸"
        elif m["status"] == "gave_up":
            mark = "✖"
        else:
            mark = "•"
        suffix = f": {m['kurz']}" if m["kurz"] else ""
        lines.append(f"- {mark} {m['id']} {m['title']}{suffix}")
    if total > _MAX_TREE_SUBTASKS_SHOWN:
        lines.append(f"- … +{total - _MAX_TREE_SUBTASKS_SHOWN} weitere")
    lines.append(f"Dashboard: {_dashboard_link(kanban_cfg, root_id)}")
    return _clean_completed_handoff("\n".join(lines), limit=1900)


def _flush_stalled_trees_for_board(
    conn: Any, _kb: Any, kanban_cfg: dict, *, now: Optional[int] = None,
) -> int:
    """Emit a one-time ``tree_stalled_flush`` event on each stalled sink/root.

    A candidate is a *subscribed* task that is a sink (has parents, no
    children), is non-terminal, was not flushed before, has at least one
    long dead-end member (sticky-blocked / gave_up past the threshold) AND at
    least one suppressed success (``done``/``archived`` member). Returns the
    number of flush events emitted. Fully fail-soft per root.
    """
    now = int(now if now is not None else time.time())
    hours = float(kanban_cfg.get("descendants_blocked_parent_hours", 24) or 0)
    threshold_s = hours * 3600.0
    emitted = 0
    try:
        candidates = [
            r["task_id"]
            for r in conn.execute(
                "SELECT DISTINCT task_id FROM kanban_notify_subs"
            )
        ]
    except Exception:
        return 0
    for root_id in candidates:
        try:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (root_id,)
            ).fetchone()
            if row is None or row["status"] in {"done", "archived"}:
                continue  # only stalled (non-terminal) roots
            if _kb.child_ids(conn, root_id):
                continue  # not a sink — interior node, K2 handles it
            if not _kb.parent_ids(conn, root_id):
                continue  # standalone task — no tree to flush
            if conn.execute(
                "SELECT 1 FROM task_events "
                "WHERE task_id = ? AND kind = 'tree_stalled_flush' LIMIT 1",
                (root_id,),
            ).fetchone():
                continue  # already flushed once — no double-post
            members = _collect_tree_members(conn, root_id, _kb)
            if not any(
                _member_is_dead_end(conn, mid, now, threshold_s) for mid in members
            ):
                continue  # root could still progress — leave to K2
            details: list[dict[str, Any]] = []
            for mid in members:
                detail = _tree_member_detail(conn, mid, _kb)
                if detail is not None:
                    details.append(detail)
            if not any(d["status"] in {"done", "archived"} for d in details):
                continue  # nothing suppressed to surface
            details.sort(key=lambda m: (m["created_at"], m["id"]))
            text = _format_tree_stall_flush_report(
                conn, root_id, details, _kb, kanban_cfg=kanban_cfg,
            )
            _kb.add_event(
                conn, root_id, "tree_stalled_flush",
                {
                    "summary": text,
                    "suppressed": [
                        d["id"] for d in details
                        if d["status"] in {"done", "archived"}
                    ],
                },
            )
            emitted += 1
        except Exception:
            logger.debug(
                "kanban notifier: stall-flush sweep failed for %s",
                root_id, exc_info=True,
            )
            continue
    return emitted


def _format_received_report(event: Any, task: Any, *, board: Optional[str] = None) -> str:
    task_id = getattr(event, "task_id", None) or getattr(task, "id", "") or "<unknown>"
    title = (getattr(task, "title", None) or task_id)[:160]
    profile = getattr(task, "assignee", None) or "unbekannt"
    raw_payload = getattr(event, "payload", None)
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    summary = str(payload.get("summary") or payload.get("message") or "eingegangen").strip()
    source = str(payload.get("source") or "").strip()
    lines = [
        "📥 Hermes Report — Task eingegangen",
        "Kurzfazit: Neuer Auftrag eingegangen.",
        f"Task: {task_id} — {title}",
        f"Status: eingegangen | Profil: {profile}",
        f"Eingang: {summary[:500]}",
        "Nächster Schritt: Dispatcher/Assignee übernimmt den Auftrag.",
    ]
    if source:
        lines.append(f"Quelle: {source[:120]}")
    if board:
        lines.append(f"Board: {board}")
    return "\n".join(lines)


def _string_config_value(config: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = config.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _resolve_report_delivery_target(
    *,
    sub: dict,
    kind: str,
    kanban_cfg: dict[str, Any],
) -> Optional[tuple[str, str]]:
    """Return chat/thread target for a report event, or None to fail closed.

    Discord kanban reports can be centrally routed to ``kanban.reporting_channel_id``
    so originating operator/orchestrator channels stop receiving normal task reports.
    Planned-retry crash/timeout events are intentionally not in the reporting
    route set; the final attention event for repeated failures is ``gave_up``.
    If the only known target is explicitly configured as the orchestrator channel and
    no reporting channel is configured, normal result reports fail closed instead of
    silently falling back to the orchestrator.
    """
    chat_id = str(sub.get("chat_id") or "")
    thread_id = str(sub.get("thread_id") or "")
    platform = str(sub.get("platform") or "").lower()
    if platform != "discord" or kind not in _REPORTING_ROUTE_KINDS:
        return chat_id, thread_id

    reporting_channel_id = _string_config_value(
        kanban_cfg,
        "reporting_channel_id",
        "reporting_discord_channel_id",
        "reporting_channel",
    )
    reporting_thread_id = _string_config_value(
        kanban_cfg,
        "reporting_thread_id",
        "reporting_discord_thread_id",
    )
    if reporting_channel_id:
        return reporting_channel_id, reporting_thread_id or thread_id

    orchestrator_channel_id = _string_config_value(
        kanban_cfg,
        "orchestrator_channel_id",
        "orchestrator_discord_channel_id",
    )
    if kind in _NORMAL_REPORT_KINDS and orchestrator_channel_id and chat_id == orchestrator_channel_id:
        return None
    return chat_id, thread_id


def _gave_up_message(task_id: str, tag: str, payload: Optional[dict]) -> str:
    trigger = str((payload or {}).get("trigger_outcome") or "").strip().lower()
    if trigger == "timed_out":
        reason = "after repeated worker timed out outcomes"
    elif trigger in {"iteration_budget_exhausted", "budget_exhausted"}:
        reason = "after repeated iteration-budget exhaustion"
    elif trigger in {"spawn_failed", "spawn_failure"}:
        reason = "after repeated spawn failures"
    elif trigger:
        reason = f"after repeated {trigger.replace('_', ' ')} failures"
    else:
        reason = "after repeated worker failures"
    err = ""
    if payload and payload.get("error"):
        err = f"\n{str(payload['error'])[:200]}"
    return f"✖ {tag}Kanban {task_id} gave up {reason}{err}"


# K12: where auto vault-receipts land when a task reaches terminal ``done``.
# The env override is REQUIRED so tests can point the write at a tmp dir; in
# production it falls back to the shared Hermes vault receipts dir.
_AUTO_RECEIPT_DEFAULT_DIR = "/home/piet/vault/03-Agents/Hermes/receipts/auto"


def _write_auto_receipt(task: Any, *, board_slug: Optional[str] = None, summary: Optional[str] = None) -> None:
    """K12: write a tiny Markdown receipt for a task that reached ``done``.

    FAIL-SOFT by contract: a missing/unwritable vault dir or any other error
    is swallowed (logged at debug) so the notifier tick continues uninterrupted
    and delivery behaviour never changes. One file per task at
    ``<base>/<task_id>.md`` — overwriting is fine since the terminal delivery
    fires once. Filesystem only; never touches an adapter.
    """
    try:
        base = os.environ.get("HERMES_AUTO_RECEIPT_DIR") or _AUTO_RECEIPT_DEFAULT_DIR
        base_path = Path(base)
        base_path.mkdir(parents=True, exist_ok=True)

        task_id = getattr(task, "id", None) or "<unknown>"
        title = getattr(task, "title", None) or task_id
        assignee = getattr(task, "assignee", None) or "unbekannt"
        status = getattr(task, "status", None) or "done"
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S")
        body = (summary or "").strip()

        lines = [
            "---",
            "kind: auto-receipt",
            f"task_id: {task_id}",
            f"title: {title}",
            f"assignee: {assignee}",
            f"status: {status}",
            f"board: {board_slug or ''}",
            f"completed_at: {ts}",
            "---",
            "",
            f"# {title}",
            "",
            f"Task `{task_id}` reached terminal **{status}** "
            f"(assignee: {assignee}, board: {board_slug or '—'}) at {ts}.",
        ]
        if body:
            lines += ["", "## Ergebnis", "", body]
        lines.append("")

        (base_path / f"{task_id}.md").write_text("\n".join(lines), encoding="utf-8")
    except Exception as exc:
        logger.debug("auto-receipt write failed: %s", exc, exc_info=True)


class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver report events to users.

        For each subscription row, fetches ``task_events`` newer than the
        stored cursor with kind in the report set (``received``, ``completed``,
        ``blocked``, ``gave_up``, ``crashed``, ``timed_out``). Sends one
        message per new event to ``(platform, chat_id, thread_id)``,
        then advances the cursor. When a task reaches a terminal state
        (``completed`` / ``archived``), the subscription is removed.

        Runs in the gateway event loop; all SQLite work is pushed to a
        thread via ``asyncio.to_thread`` so the loop never blocks on the
        WAL lock. Failures in one tick don't stop subsequent ticks.

        **Multi-board:** iterates every board discovered on disk per
        tick. Subscriptions live inside each board's own DB and cannot
        cross boards, so delivery semantics are unchanged — this is
        purely a fan-out of the single-DB poll.
        """
        # Gate: only the dispatch-owning gateway opens kanban DBs for notifier polling.
        # Non-dispatch gateways have no subscriptions to deliver — all kanban state lives
        # in the dispatch owner's per-board DBs. This prevents N-gateway -shm contention.
        # TODO: gate per-board when per-board dispatcher_owner tracking lands.
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban notifier: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban notifier: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban notifier: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban notifier: disabled via config kanban.dispatch_in_gateway=false"
            )
            return
        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        REPORT_KINDS = ("received", "completed", "blocked", "gave_up", "crashed", "timed_out", "tree_stalled_flush")
        # Subscriptions are removed only when the task reaches a truly final
        # status (done / archived). We used to also unsub on any terminal
        # event kind (gave_up / crashed / timed_out / blocked), but that
        # silently dropped the user out of the loop whenever the dispatcher
        # respawned the task: a worker that crashes, gets reclaimed, runs
        # again, and crashes a second time would only notify on the first
        # crash because the subscription was deleted after the first event.
        # Same shape as the reblock-after-unblock cycle that PR #22941
        # fixed for `blocked`. Keeping the subscription alive until the
        # task is genuinely done lets the cursor (advanced atomically by
        # claim_unseen_events_for_sub) handle dedup, and any retry-loop
        # event reaches the user.
        # Per-subscription send-failure counter. Adapter.send raising
        # means the chat is dead (deleted, bot kicked, etc.) — after N
        # consecutive send failures the sub is dropped so we don't spin
        # against a dead chat every 5 seconds forever.
        MAX_SEND_FAILURES = 3
        sub_fail_counts: dict[tuple, int] = getattr(
            self, "_kanban_sub_fail_counts", {}
        )
        self._kanban_sub_fail_counts = sub_fail_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None)
        if not notifier_profile:
            notifier_profile = self._active_profile_name()
            self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        while self._running:
            try:
                # F1: before collecting deliveries, flush any stalled root's
                # suppressed child-successes as a one-time trailing report.
                # Fully fail-soft — a sweep hiccup must never stop the tick.
                try:
                    await asyncio.to_thread(
                        self._kanban_flush_stalled_trees, kanban_cfg, _kb,
                    )
                except Exception:
                    logger.debug(
                        "kanban notifier: stall-flush sweep tick failed",
                        exc_info=True,
                    )

                def _collect():
                    deliveries: list[dict] = []
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    if not active_platforms:
                        logger.debug("kanban notifier: no connected adapters; skipping tick")
                        return deliveries

                    # Enumerate every board on disk, but poll each resolved DB
                    # path once. Multiple slugs can point at the same DB when
                    # HERMES_KANBAN_DB pins the board path; without this guard
                    # one gateway could collect the same subscription/event
                    # more than once before advancing the cursor.
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: set[str] = set()
                    for board_meta in boards:
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(_kb.kanban_db_path(slug).resolve())
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            logger.debug(
                                "kanban notifier: skipping duplicate board slug %s for DB %s",
                                slug, resolved_db_path,
                            )
                            continue
                        seen_db_paths.add(resolved_db_path)
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
                            continue
                        try:
                            # `connect()` runs the schema + idempotent migration
                            # on first open per process, so an explicit
                            # `init_db()` here would be redundant. Worse:
                            # `init_db()` deliberately busts the per-process
                            # cache and re-runs the migration on a *second*
                            # connection, which races the first and used to
                            # log a benign but noisy `duplicate column name`
                            # traceback (and intermittent "database is locked"
                            # — issue #21378) on every gateway start against
                            # a legacy DB. `_add_column_if_missing` now
                            # tolerates that race, but we still skip the
                            # redundant call to avoid the wasted work.
                            subs = _kb.list_notify_subs(conn)
                            if not subs:
                                logger.debug("kanban notifier: board %s has no subscriptions", slug)
                            for sub in subs:
                                owner_profile = sub.get("notifier_profile") or None
                                if owner_profile and owner_profile != notifier_profile:
                                    logger.debug(
                                        "kanban notifier: subscription for %s owned by profile %s; current profile %s skipping",
                                        sub.get("task_id"), owner_profile, notifier_profile,
                                    )
                                    continue
                                platform = (sub.get("platform") or "").lower()
                                if platform not in active_platforms:
                                    logger.debug(
                                        "kanban notifier: subscription for %s on %s skipped; adapter not connected",
                                        sub.get("task_id"), platform or "<missing>",
                                    )
                                    continue
                                old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(
                                    conn,
                                    task_id=sub["task_id"],
                                    platform=sub["platform"],
                                    chat_id=sub["chat_id"],
                                    thread_id=sub.get("thread_id") or "",
                                    kinds=REPORT_KINDS,
                                )
                                if not events:
                                    continue
                                task = _kb.get_task(conn, sub["task_id"])
                                runs_by_event_id: dict[int, Any] = {}
                                for ev in events:
                                    if ev.kind != "completed" or ev.run_id is None:
                                        continue
                                    try:
                                        run = _kb.get_run(conn, ev.run_id)
                                    except Exception:
                                        run = None
                                    if run is not None:
                                        runs_by_event_id[int(ev.id)] = run
                                logger.debug(
                                    "kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s",
                                    len(events), sub["task_id"], slug, old_cursor, cursor,
                                )
                                deliveries.append({
                                    "sub": sub,
                                    "old_cursor": old_cursor,
                                    "cursor": cursor,
                                    "events": events,
                                    "task": task,
                                    "runs_by_event_id": runs_by_event_id,
                                    "board": slug,
                                })
                        finally:
                            conn.close()
                    return deliveries

                deliveries = await asyncio.to_thread(_collect)
                attempted_event_targets: set[tuple[str, str, int, str, str, str]] = set()
                for d in deliveries:
                    sub = d["sub"]
                    task = d["task"]
                    board_slug = d.get("board")
                    platform_str = (sub["platform"] or "").lower()
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # Unknown platform string; skip and advance cursor so
                        # we don't replay forever.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    adapter = self.adapters.get(plat)
                    if adapter is None:
                        logger.debug(
                            "kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                            platform_str, sub["task_id"],
                        )
                        await asyncio.to_thread(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    runs_by_event_id = d.get("runs_by_event_id") or {}
                    # Track the cursor up to the last *successfully* delivered
                    # event so that a mid-batch send failure only rewinds to
                    # the failed event, not the start of the whole batch
                    # (FINDING #6 — duplicate reports on partial-batch failure).
                    delivered_cursor: int = d.get("old_cursor", 0)
                    for ev in d["events"]:
                        kind = ev.kind
                        # Identity prefix: attribute terminal pings to the
                        # worker that did the work. Makes fleets (where one
                        # chat subscribes to many tasks) legible at a glance.
                        who = (task.assignee if task and task.assignee else None)
                        tag = f"@{who} " if who else ""
                        if kind == "completed":
                            # K2: per-tree aggregation. An interior work node's
                            # completion is rolled up into its root's single
                            # consolidated report; the sink/root emits that one
                            # report; standalone tasks fall through to the
                            # normal per-task report.
                            _root_run = runs_by_event_id.get(int(ev.id))
                            tree_decision = await asyncio.to_thread(
                                self._kanban_tree_completion,
                                task, _root_run, ev, board_slug, kanban_cfg,
                            )
                            if tree_decision == "suppress":
                                # Don't send per-child. The cursor still
                                # advances (and the sub unsubs if terminal) via
                                # the for-else below — the event is consumed.
                                logger.debug(
                                    "kanban notifier: suppressing per-child "
                                    "completed for %s (rolled into tree root)",
                                    sub["task_id"],
                                )
                                continue
                            if isinstance(tree_decision, str):
                                msg = tree_decision  # consolidated root report
                            else:
                                # Prefer a structured, human-readable result
                                # report over the legacy one-line done ping.
                                msg = _format_completed_report(
                                    ev,
                                    task,
                                    _root_run,
                                    board=board_slug,
                                    kanban_cfg=kanban_cfg,
                                )
                        elif kind == "tree_stalled_flush":
                            # F1: trailing flush of a stalled root's suppressed
                            # child-successes; the consolidated text was built
                            # and stored on the event at emit time.
                            msg = ""
                            if ev.payload and ev.payload.get("summary"):
                                msg = str(ev.payload["summary"])
                            if not msg:
                                msg = (
                                    f"⚠️ {tag}Kanban-Auftrag {sub['task_id']} steckt fest; "
                                    "bereits erledigte Teilaufgaben siehe Board."
                                )
                        elif kind == "received":
                            msg = _format_received_report(ev, task, board=board_slug)
                        elif kind == "blocked":
                            reason = ""
                            if ev.payload and ev.payload.get("reason"):
                                reason = f": {str(ev.payload['reason'])[:160]}"
                            msg = f"⏸ {tag}Kanban {sub['task_id']} blocked{reason}"
                        elif kind == "gave_up":
                            msg = _gave_up_message(sub["task_id"], tag, ev.payload)
                        elif kind == "crashed":
                            msg = (
                                f"✖ {tag}Kanban {sub['task_id']} worker crashed "
                                f"(pid gone); dispatcher will retry"
                            )
                        elif kind == "timed_out":
                            limit = None
                            if ev.payload and ev.payload.get("limit_seconds"):
                                try:
                                    limit = int(ev.payload["limit_seconds"])
                                except (TypeError, ValueError):
                                    limit = None
                            if limit and limit > 0:
                                msg = (
                                    f"⏱ {tag}Kanban {sub['task_id']} timed out "
                                    f"(max_runtime={limit}s); will retry"
                                )
                            else:
                                msg = f"⏱ {tag}Kanban {sub['task_id']} timed out; will retry"
                        else:
                            continue
                        target = _resolve_report_delivery_target(
                            sub=sub,
                            kind=kind,
                            kanban_cfg=kanban_cfg,
                        )
                        if target is None:
                            logger.warning(
                                "kanban notifier: normal %s report for %s not sent to orchestrator channel %s because kanban.reporting_channel_id is missing",
                                kind,
                                sub["task_id"],
                                sub.get("chat_id"),
                            )
                            continue
                        target_chat_id, target_thread_id = target
                        metadata: dict[str, Any] = {}
                        if target_thread_id:
                            metadata["thread_id"] = target_thread_id
                        event_target_key = (
                            str(board_slug or ""),
                            kind,
                            int(ev.id),
                            platform_str,
                            str(target_chat_id or ""),
                            str(target_thread_id or ""),
                        )
                        if event_target_key in attempted_event_targets:
                            logger.debug(
                                "kanban notifier: skipped duplicate %s event %s for %s to %s/%s on board %s",
                                kind,
                                ev.id,
                                sub["task_id"],
                                target_chat_id,
                                target_thread_id or "",
                                board_slug,
                            )
                            continue
                        attempted_event_targets.add(event_target_key)
                        sub_key = (
                            sub["task_id"], sub["platform"],
                            sub["chat_id"], sub.get("thread_id") or "",
                        )
                        try:
                            await adapter.send(
                                target_chat_id, msg, metadata=metadata,
                            )
                            logger.debug(
                                "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                                kind, sub["task_id"], platform_str, target_chat_id, board_slug,
                            )
                            # After delivering the text notification, surface
                            # any artifact paths the worker referenced in
                            # ``kanban_complete(summary=..., artifacts=[...])``
                            # (or the legacy ``result`` field) as native
                            # uploads. ``extract_local_files`` finds bare
                            # absolute paths in the summary;
                            # ``send_document`` / ``send_image_file`` uploads
                            # them. Only fires on the ``completed`` event so
                            # we never spam attachments on retries.
                            if kind == "completed":
                                try:
                                    await self._deliver_kanban_artifacts(
                                        adapter=adapter,
                                        chat_id=target_chat_id,
                                        metadata=metadata,
                                        event_payload=getattr(ev, "payload", None),
                                        task=task,
                                    )
                                except Exception as art_exc:
                                    logger.debug(
                                        "kanban notifier: artifact delivery for %s failed: %s",
                                        sub["task_id"], art_exc,
                                    )
                            # Reset the failure counter on success.  Advance
                            # delivered_cursor so a subsequent send failure in
                            # the same batch only rewinds to HERE, not to the
                            # start of the batch (FINDING #6 duplicate-report
                            # fix).
                            delivered_cursor = int(ev.id)
                            sub_fail_counts.pop(sub_key, None)
                        except Exception as exc:
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            logger.warning(
                                "kanban notifier: send failed for %s on %s "
                                "(attempt %d/%d): %s",
                                sub["task_id"], platform_str, fails,
                                MAX_SEND_FAILURES, exc,
                            )
                            if fails >= MAX_SEND_FAILURES:
                                logger.warning(
                                    "kanban notifier: dropping subscription "
                                    "%s on %s after %d consecutive send failures",
                                    sub["task_id"], platform_str, fails,
                                )
                                await asyncio.to_thread(self._kanban_unsub, sub, board_slug)
                                sub_fail_counts.pop(sub_key, None)
                            else:
                                # Rewind only to the last successfully delivered
                                # event (delivered_cursor), NOT all the way back
                                # to old_cursor.  This prevents already-sent
                                # events from being re-claimed and re-delivered
                                # on the next tick (FINDING #6).
                                await asyncio.to_thread(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    delivered_cursor,
                                    board_slug,
                                )
                            # Rewind the pre-send claim on transient failure so
                            # a later tick can retry. After too many failures,
                            # dropping the subscription is the terminal action.
                            break
                    else:
                        # Unsubscribe only when the task has reached a truly
                        # final status (done / archived). For blocked /
                        # gave_up / crashed / timed_out the subscription is
                        # kept alive so the user gets notified again if the
                        # dispatcher respawns the task and it cycles into the
                        # same state. See the longer comment near REPORT_KINDS
                        # above for the failure mode this prevents.
                        task_terminal = task and task.status in {"done", "archived"}
                        if task_terminal:
                            # K12: on terminal ``done`` (NOT archived — avoid
                            # receipt-spam when tasks get archived later) write a
                            # fail-soft vault receipt. Own try/except so a write
                            # hiccup can never affect the unsub below; the helper
                            # is itself swallow-all and filesystem-only (never an
                            # adapter send), so the delivery count is unchanged.
                            if task.status == "done":
                                try:
                                    run = runs_by_event_id.get(
                                        int(getattr(ev, "id", 0) or 0)
                                    )
                                    summary = getattr(run, "summary", None) or getattr(
                                        task, "result", None
                                    )
                                    await asyncio.to_thread(
                                        _write_auto_receipt,
                                        task,
                                        board_slug=board_slug,
                                        summary=summary,
                                    )
                                except Exception:
                                    logger.debug(
                                        "kanban notifier: auto-receipt for %s failed",
                                        sub["task_id"], exc_info=True,
                                    )
                            await asyncio.to_thread(
                                self._kanban_unsub, sub, board_slug,
                            )
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            # Sleep with cancellation checks.
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)

    def _kanban_tree_completion(
        self, task: Any, run: Any, event: Any,
        board: Optional[str], kanban_cfg: Optional[dict],
    ) -> Optional[str]:
        """Classify a completed task's role in a decomposed tree (K2).

        Runs in ``to_thread`` (opens its own board-scoped connection).

        Returns:
          ``None``        — standalone task (no ``task_links``); the caller
                            sends the normal single completed report.
          ``"suppress"``  — interior work node (something depends on it); its
                            completion rolls up into the root, send nothing.
          ``str``         — this is the tree's sink/root (waits on everything);
                            the returned text is the ONE consolidated report.

        Fail-soft: any error returns ``None`` so a tree-classification hiccup
        degrades to the existing per-task report rather than dropping it.
        """
        task_id = getattr(task, "id", None)
        if not task_id:
            return None
        from hermes_cli import kanban_db as _kb
        try:
            conn = _kb.connect(board=board)
        except Exception:
            return None
        try:
            parents = _kb.parent_ids(conn, task_id)
            children = _kb.child_ids(conn, task_id)
            if not parents and not children:
                return None  # standalone — normal single report
            if children:
                return "suppress"  # interior work node — rolled into the root
            # Sink/root: aggregate itself + all transitive parents (work tasks).
            details: list[dict[str, Any]] = []
            for mid in _collect_tree_members(conn, task_id, _kb):
                detail = _tree_member_detail(conn, mid, _kb)
                if detail is not None:
                    details.append(detail)
            details.sort(key=lambda m: (m["created_at"], m["id"]))
            return _format_tree_completed_report(
                task, run, event, details, board=board, kanban_cfg=kanban_cfg,
            )
        except Exception:
            logger.debug(
                "kanban notifier: tree-completion classify failed for %s",
                task_id, exc_info=True,
            )
            return None
        finally:
            conn.close()

    def _kanban_flush_stalled_trees(self, kanban_cfg: Optional[dict], _kb: Any) -> None:
        """F1 sweep: emit one-time stall-flush events across every board.

        Runs in ``to_thread`` once per notifier tick (before delivery
        collection) so a freshly-emitted ``tree_stalled_flush`` is delivered
        the same tick. Mirrors the notifier's board enumeration and is fully
        fail-soft per board — never raises into the tick loop.
        """
        cfg = kanban_cfg if isinstance(kanban_cfg, dict) else {}
        try:
            boards = _kb.list_boards(include_archived=False)
        except Exception:
            try:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            except Exception:
                return
        seen_db_paths: set[str] = set()
        for board_meta in boards:
            slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
            db_path = board_meta.get("db_path")
            try:
                resolved = (
                    str(Path(db_path).expanduser().resolve())
                    if db_path else str(_kb.kanban_db_path(slug).resolve())
                )
            except Exception:
                resolved = f"slug:{slug}"
            if resolved in seen_db_paths:
                continue
            seen_db_paths.add(resolved)
            try:
                conn = _kb.connect(board=slug)
            except Exception:
                continue
            try:
                _flush_stalled_trees_for_board(conn, _kb, cfg)
            except Exception:
                logger.debug(
                    "kanban notifier: stall-flush sweep failed on board %s",
                    slug, exc_info=True,
                )
            finally:
                conn.close()

    def _kanban_advance(
        self, sub: dict, cursor: int, board: Optional[str] = None,
    ) -> None:
        """Sync helper: advance a subscription's cursor. Runs in to_thread.

        ``board`` scopes the DB connection to the board that owns this
        subscription. Unsub cursors in one board can't touch another's.
        """
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.advance_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                new_cursor=cursor,
            )
        finally:
            conn.close()

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.remove_notify_sub(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
            )
        finally:
            conn.close()

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper: undo a claimed notification cursor after send failure."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.rewind_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_cursor=claimed_cursor,
                old_cursor=old_cursor,
            )
        finally:
            conn.close()

    async def _deliver_kanban_artifacts(
        self,
        *,
        adapter,
        chat_id: str,
        metadata: dict,
        event_payload: Optional[dict],
        task,
    ) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Workers passing ``kanban_complete(artifacts=[...])`` ship absolute
        file paths through the completion event so downstream humans get
        the deliverable as a native upload instead of a path printed in
        chat.

        Sources scanned, in priority order:
          1. ``event_payload['artifacts']`` (explicit list — preferred)
          2. ``event_payload['summary']`` (truncated first line)
          3. ``task.result`` (legacy fallback)

        Files are deduplicated, missing files are silently skipped (the
        path may have been mentioned for reference only), and delivery
        errors are logged but do not break the notifier loop.
        """
        from pathlib import Path as _Path

        candidates: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if not path:
                return
            expanded = os.path.expanduser(path)
            if expanded in seen:
                return
            if not os.path.isfile(expanded):
                return
            seen.add(expanded)
            candidates.append(expanded)

        # 1. Explicit artifacts list in payload.
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    if isinstance(item, str):
                        _add(item)

            # 2. Paths embedded in the payload summary.
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                paths, _ = adapter.extract_local_files(summary)
                for p in paths:
                    _add(p)

        # 3. Legacy: paths embedded in task.result.
        if task is not None and getattr(task, "result", None):
            result_text = str(task.result)
            paths, _ = adapter.extract_local_files(result_text)
            for p in paths:
                _add(p)

        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

        from urllib.parse import quote as _quote

        # Partition images so they ride a single send_multiple_images call
        # on platforms that support batch image uploads (Signal/Slack RPCs).
        image_paths = [p for p in candidates if _Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if _Path(p).suffix.lower() not in _IMAGE_EXTS]

        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(
                    chat_id=chat_id, images=batch, metadata=metadata,
                )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: image batch upload failed: %s", exc,
                )

        for path in other_paths:
            ext = _Path(path).suffix.lower()
            try:
                if ext in _VIDEO_EXTS:
                    await adapter.send_video(
                        chat_id=chat_id, video_path=path, metadata=metadata,
                    )
                else:
                    await adapter.send_document(
                        chat_id=chat_id, file_path=path, metadata=metadata,
                    )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: artifact upload (%s) failed: %s",
                    path, exc,
                )

    async def _kanban_alerts_watcher(self) -> None:
        """F2 (night-sprint): push-Alerting — one tick every 5 min (config).

        Evaluates the rules in :mod:`gateway.kanban_alerts` (run failed/
        blocked, error rate, daily cost) against the board DB and pushes
        qualifying alerts to a Discord channel. Opt-in: ``kanban.alerts.
        enabled`` defaults to False, so gateways whose config has no alerts
        block (research etc.) stay silent. Discord only — Telegram ist ab.

        Same lifecycle pattern as ``_kanban_dispatcher_watcher``: config read
        once at boot, SQLite work in ``asyncio.to_thread``, per-tick failures
        never kill the loop, sliced sleep for snappy shutdown.
        """
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban alerts: config loader unavailable; disabled")
            return
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban alerts: cannot load config (%s); disabled", exc)
            return
        try:
            from gateway.kanban_alerts import (
                evaluate_alerts as _evaluate_alerts,
                load_alerts_config as _load_alerts_config,
                new_alert_state as _new_alert_state,
            )
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban alerts: engine not importable; disabled")
            return

        acfg = _load_alerts_config(cfg)
        if not acfg["enabled"]:
            logger.info("kanban alerts: disabled (kanban.alerts.enabled not set)")
            return
        if not (acfg["channel_id"] or acfg.get("escalation_channel_id")):
            logger.warning(
                "kanban alerts: enabled but no channel (kanban.alerts.channel_id, "
                "kanban.alerts.escalation_channel_id, or "
                "kanban.reporting_channel_id) — disabled",
            )
            return
        logger.info(
            "kanban alerts: enabled — interval=%ss cooldown=%ss channel=%s",
            acfg["interval_seconds"], acfg["cooldown_seconds"], acfg["channel_id"],
        )

        from gateway.config import Platform as _Platform

        state = _new_alert_state()
        await asyncio.sleep(10)  # let adapters connect before the first tick
        while self._running:
            try:
                def _tick():
                    conn = _kb.connect()
                    try:
                        return _evaluate_alerts(conn, acfg, state)
                    finally:
                        conn.close()

                alerts = await asyncio.to_thread(_tick)
                if alerts:
                    adapter = self.adapters.get(_Platform.DISCORD)
                    if adapter is None:
                        logger.warning(
                            "kanban alerts: discord adapter unavailable; "
                            "%d alert(s) dropped", len(alerts),
                        )
                    else:
                        metadata = (
                            {"thread_id": acfg["thread_id"]}
                            if acfg["thread_id"] else None
                        )
                        for alert in alerts:
                            target_channel_id = (
                                alert.get("channel_id") or acfg["channel_id"]
                            )
                            if not target_channel_id:
                                logger.warning(
                                    "kanban alerts: no channel for rule %s; dropped",
                                    alert["rule"],
                                )
                                continue
                            try:
                                await adapter.send(
                                    target_channel_id, alert["text"],
                                    metadata=metadata,
                                )
                                logger.info(
                                    "kanban alerts: sent %s alert to %s",
                                    alert["rule"], target_channel_id,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "kanban alerts: send failed for rule %s: %s",
                                    alert["rule"], exc,
                                )
            except Exception as exc:
                logger.warning("kanban alerts: tick failed: %s", exc)
            slept = 0.0
            while self._running and slept < acfg["interval_seconds"]:
                await asyncio.sleep(1.0)
                slept += 1.0

    async def _kanban_dispatcher_watcher(self) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` in config.yaml (default True).
        When true, the gateway hosts the single dispatcher for this profile:
        no separate `hermes kanban daemon` process needed. When false, the
        loop exits immediately and an external daemon is expected.

        Each tick calls :func:`kanban_db.dispatch_once` inside
        ``asyncio.to_thread`` so the SQLite WAL lock never blocks the
        event loop. Failures in one tick don't stop subsequent ticks —
        same pattern as `_kanban_notifier_watcher`.

        Shutdown: the loop checks ``self._running`` between ticks; gateway
        stop() flips it to False and cancels pending tasks, and the
        in-flight ``to_thread`` returns on its own after the current
        ``dispatch_once`` call finishes (typically <1ms on an idle board).
        """
        # Read config once at boot. If the user flips the flag later, they
        # restart the gateway; same pattern as every other background
        # watcher here. Honours HERMES_KANBAN_DISPATCH_IN_GATEWAY env var
        # as an escape hatch (false-y value disables without editing YAML).
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return

        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false"
            )
            return

        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return

        try:
            interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
        except (ValueError, TypeError):
            logger.warning(
                "kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                kanban_cfg.get("dispatch_interval_seconds"),
            )
            interval = 60.0
        interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

        # Read max_spawn config to limit concurrent kanban tasks
        max_spawn = kanban_cfg.get("max_spawn", None)
        if max_spawn is not None:
            logger.info(f"kanban dispatcher: max_spawn={max_spawn}")

        # Cap the number of simultaneously running tasks so slow workers
        # (local LLMs, resource-constrained hosts) don't pile up and time
        # out. When set, the dispatcher skips spawning when the board
        # already has this many tasks in 'running' status.
        raw_max_in_progress = kanban_cfg.get("max_in_progress", None)
        max_in_progress = None
        if raw_max_in_progress is not None:
            try:
                max_in_progress = int(raw_max_in_progress)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress=%r; ignoring",
                    raw_max_in_progress,
                )
                max_in_progress = None
            else:
                if max_in_progress < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress=%r is below 1; ignoring",
                        raw_max_in_progress,
                    )
                    max_in_progress = None
                else:
                    logger.info(f"kanban dispatcher: max_in_progress={max_in_progress}")

        raw_failure_limit = kanban_cfg.get("failure_limit", _kb.DEFAULT_FAILURE_LIMIT)
        try:
            failure_limit = int(raw_failure_limit)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT
        if failure_limit < 1:
            logger.warning(
                "kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT

        auto_retry_blocked = bool(kanban_cfg.get("auto_retry_blocked", False))
        raw_auto_retry_backoff = kanban_cfg.get(
            "auto_retry_blocked_backoff_seconds",
            _kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS,
        )
        try:
            auto_retry_blocked_backoff_seconds = int(raw_auto_retry_backoff)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.auto_retry_blocked_backoff_seconds=%r; using default %d",
                raw_auto_retry_backoff,
                _kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS,
            )
            auto_retry_blocked_backoff_seconds = _kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS
        if auto_retry_blocked_backoff_seconds < 0:
            auto_retry_blocked_backoff_seconds = _kb.DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS

        # Read stale_timeout_seconds — 0 disables stale detection.
        raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
        try:
            stale_timeout_seconds = int(raw_stale or 0)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                "disabling stale detection",
                raw_stale,
            )
            stale_timeout_seconds = 0

        # Read kanban.default_assignee — fallback profile for tasks
        # created without an explicit assignee (e.g. via the dashboard).
        # When set, the dispatcher applies it to unassigned ready tasks
        # instead of skipping them indefinitely (#27145). Empty string
        # (the schema default) means "no fallback, keep skipping" —
        # backward-compatible with existing installs.
        default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
        if default_assignee:
            logger.info(
                "kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                "will route to this profile)",
                default_assignee,
            )

        # Read kanban.max_in_progress_per_profile — per-profile concurrency
        # cap (#21582). When set, no single profile gets more than N
        # workers running at once, even if the global max_in_progress
        # would allow it. Prevents one profile's local model / API quota
        # / browser pool from being overwhelmed by a fan-out.
        raw_per_profile = kanban_cfg.get("max_in_progress_per_profile", None)
        max_in_progress_per_profile = None
        if raw_per_profile is not None:
            try:
                max_in_progress_per_profile = int(raw_per_profile)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress_per_profile=%r; ignoring",
                    raw_per_profile,
                )
                max_in_progress_per_profile = None
            else:
                if max_in_progress_per_profile < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress_per_profile=%r is below 1; ignoring",
                        raw_per_profile,
                    )
                    max_in_progress_per_profile = None
                else:
                    logger.info(
                        "kanban dispatcher: max_in_progress_per_profile=%d",
                        max_in_progress_per_profile,
                    )

        serialize_by_repo = bool(kanban_cfg.get("serialize_by_repo", True))
        if not serialize_by_repo:
            logger.info("kanban dispatcher: serialize_by_repo=False (per-repo lock OFF)")

        # Read C1 budget caps (N-C1). Both default OFF (None) — the dispatcher
        # never holds on budget unless the operator sets a positive value, so
        # this is purely opt-in and the no-cap path is byte-identical to before.
        # kanban.daily_token_cap_per_profile: rolling-24h token ceiling per
        # profile (the subscription fleet runs at $0 so tokens are the signal).
        # kanban.daily_cost_cap_usd: rolling-24h board-wide $ ceiling (metered).
        raw_token_cap = kanban_cfg.get("daily_token_cap_per_profile", None)
        daily_token_cap_per_profile = None
        if raw_token_cap is not None:
            try:
                daily_token_cap_per_profile = int(raw_token_cap)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.daily_token_cap_per_profile=%r; ignoring",
                    raw_token_cap,
                )
                daily_token_cap_per_profile = None
            else:
                if daily_token_cap_per_profile < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.daily_token_cap_per_profile=%r is below 1; ignoring",
                        raw_token_cap,
                    )
                    daily_token_cap_per_profile = None
                else:
                    logger.info(
                        "kanban dispatcher: daily_token_cap_per_profile=%d",
                        daily_token_cap_per_profile,
                    )

        raw_cost_cap = kanban_cfg.get("daily_cost_cap_usd", None)
        daily_cost_cap_usd = None
        if raw_cost_cap is not None:
            try:
                daily_cost_cap_usd = float(raw_cost_cap)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.daily_cost_cap_usd=%r; ignoring",
                    raw_cost_cap,
                )
                daily_cost_cap_usd = None
            else:
                if daily_cost_cap_usd <= 0:
                    logger.warning(
                        "kanban dispatcher: kanban.daily_cost_cap_usd=%r is not positive; ignoring",
                        raw_cost_cap,
                    )
                    daily_cost_cap_usd = None
                else:
                    logger.info(
                        "kanban dispatcher: daily_cost_cap_usd=%.2f",
                        daily_cost_cap_usd,
                    )

        # G1 per-task input-token runaway guard. The respawn preflight sums
        # input_tokens across ALL of a task's runs; when the cumulative input
        # exceeds this cap the task is PARKED (blocked) + escalated rather than
        # advisory-held. Default ON at 2_000_000 (config default); None / a
        # non-positive value disables it. Catches a single task burning the
        # subscription quota via a runaway retry / oversized-context loop.
        raw_per_task_input_cap = kanban_cfg.get("per_task_input_token_cap", None)
        per_task_input_token_cap = None
        if raw_per_task_input_cap is not None:
            try:
                per_task_input_token_cap = int(raw_per_task_input_cap)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.per_task_input_token_cap=%r; ignoring",
                    raw_per_task_input_cap,
                )
                per_task_input_token_cap = None
            else:
                if per_task_input_token_cap < 1:
                    # 0 / negative = guard explicitly disabled.
                    per_task_input_token_cap = None
                else:
                    logger.info(
                        "kanban dispatcher: per_task_input_token_cap=%d",
                        per_task_input_token_cap,
                    )

        # Initial delay so the gateway finishes wiring adapters before the
        # dispatcher spawns workers (those workers may hit gateway notify
        # subscriptions etc.). Matches the notifier watcher's delay.
        await asyncio.sleep(5)

        # Health telemetry mirrored from `_cmd_daemon`: warn when ready
        # queue is non-empty but spawns are 0 for N consecutive ticks —
        # usually means broken PATH, missing venv, or credential loss.
        HEALTH_WINDOW = 6
        bad_ticks = 0
        last_warn_at = 0
        # Avoid hot-looping corrupt-looking board DBs, but do not suppress
        # same-fingerprint retries forever: transient WAL/open races can
        # surface as "database disk image is malformed" for one tick.
        CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300
        disabled_corrupt_boards: dict[
            str, tuple[tuple[str, int | None, int | None], float]
        ] = {}

        def _board_db_fingerprint(slug: str) -> tuple[str, int | None, int | None]:
            path = _kb.kanban_db_path(slug)
            try:
                resolved = str(path.expanduser().resolve())
            except Exception:
                resolved = str(path)
            try:
                stat = path.stat()
            except OSError:
                return (resolved, None, None)
            return (resolved, stat.st_mtime_ns, stat.st_size)

        def _is_corrupt_board_db_error(exc: Exception) -> bool:
            corrupt_guard_error = getattr(_kb, "KanbanDbCorruptError", None)
            if corrupt_guard_error is not None and isinstance(exc, corrupt_guard_error):
                return True
            if not isinstance(exc, sqlite3.DatabaseError):
                return False
            msg = str(exc).lower()
            return (
                "file is not a database" in msg
                or "database disk image is malformed" in msg
            )

        def _tick_once_for_board(slug: str) -> "Optional[object]":
            """Run one dispatch_once for a specific board.

            Runs in a worker thread via `asyncio.to_thread`. `board=slug`
            is passed through `dispatch_once` so `resolve_workspace` and
            `_default_spawn` see the right paths. The per-board DB is
            opened explicitly so concurrent boards never share a
            connection handle or accidentally claim across each other.
            """
            conn = None
            fingerprint = _board_db_fingerprint(slug)
            disabled_entry = disabled_corrupt_boards.get(slug)
            if disabled_entry is not None:
                disabled_fingerprint, disabled_at = disabled_entry
                age = time.monotonic() - disabled_at
                if (
                    disabled_fingerprint == fingerprint
                    and age < CORRUPT_BOARD_RETRY_AFTER_SECONDS
                ):
                    return None
                if disabled_fingerprint == fingerprint:
                    logger.info(
                        "kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch",
                        slug,
                        age,
                    )
                else:
                    logger.info(
                        "kanban dispatcher: board %s database changed; retrying dispatch",
                        slug,
                    )
                disabled_corrupt_boards.pop(slug, None)
            try:
                conn = _kb.connect(board=slug)
                # `connect()` runs the schema + idempotent migration on
                # first open per process; the previous explicit
                # `init_db()` call here busted the per-process cache and
                # re-ran the migration on a second connection, racing
                # the first. See the matching comment in
                # `_kanban_notifier_watcher` and issue #21378.
                _dispatch_result = _kb.dispatch_once(
                    conn,
                    board=slug,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    stale_timeout_seconds=stale_timeout_seconds,
                    default_assignee=default_assignee,
                    max_in_progress_per_profile=max_in_progress_per_profile,
                    serialize_by_repo=serialize_by_repo,
                    daily_token_cap_per_profile=daily_token_cap_per_profile,
                    daily_cost_cap_usd=daily_cost_cap_usd,
                    per_task_input_token_cap=per_task_input_token_cap,
                    auto_retry_blocked=auto_retry_blocked,
                    auto_retry_blocked_backoff_seconds=auto_retry_blocked_backoff_seconds,
                )
                # Poll Mission-Control for any in-flight OpenClaw dispatches on
                # this board and close their kanban tasks to done/blocked. Runs
                # on the same connection, inside this try (so the conn-closing
                # finally still fires), before we return the dispatch result.
                # Best-effort: a poll-back failure must never break dispatch, so
                # it is guarded independently.
                try:
                    _kb.poll_openclaw_results(conn, board=slug)
                except Exception:
                    logger.debug(
                        "kanban dispatcher: openclaw poll-back failed on board %s",
                        slug, exc_info=True,
                    )
                try:
                    _kb.no_silent_stall_sweep(conn)
                except Exception:
                    logger.debug(
                        "kanban dispatcher: no-silent-stall sweep failed on board %s",
                        slug, exc_info=True,
                    )
                # Safety net: guarantee every operator_escalation gets a paired
                # heiler_classification within one tick (the Stratege's by_class
                # input). Independently guarded so a sweep failure never breaks
                # dispatch.
                try:
                    _kb.classify_escalations_sweep(conn)
                except Exception:
                    logger.debug(
                        "kanban dispatcher: escalation classification sweep "
                        "failed on board %s", slug, exc_info=True,
                    )
                return _dispatch_result
            except sqlite3.DatabaseError as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            except Exception as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        def _tick_once(boards: "Optional[list[dict]]" = None) -> "list[tuple[str, Optional[object]]]":
            """Run one dispatch_once per board. Returns (slug, result) pairs.

            Enumerating boards on every tick keeps the dispatcher honest
            when users create a new board mid-run: no restart required,
            the next tick picks it up automatically.

            ``boards`` may be passed in from the outer tick to avoid a
            redundant list_boards scan; if None it is fetched here
            (preserves independent callability).
            """
            if boards is None:
                try:
                    boards = _kb.list_boards(include_archived=False)
                except Exception:
                    boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            out: list[tuple[str, "Optional[object]"]] = []
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                out.append((slug, _tick_once_for_board(slug)))
            return out

        def _ready_nonempty(boards: "Optional[list[dict]]" = None) -> bool:
            """Cheap probe: is there at least one ready+assigned+unclaimed
            task on ANY board whose assignee maps to a real Hermes profile
            (i.e. one the dispatcher would actually spawn for)?

            Tasks assigned to control-plane lanes (e.g. ``orion-cc``,
            ``orion-research``) are pulled by terminals via
            ``claim_task`` directly and never spawnable, so a queue full
            of those is "correctly idle", not "stuck". Filtering them out
            here keeps the stuck-warn fire only on real failures (broken
            PATH, missing venv, credential loss for a real Hermes profile).

            ``boards`` may be passed in from the outer tick to avoid a
            redundant list_boards scan; if None it is fetched here
            (preserves independent callability).
            """
            if boards is None:
                try:
                    boards = _kb.list_boards(include_archived=False)
                except Exception:
                    boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                conn = None
                try:
                    conn = _kb.connect(board=slug)
                    if _kb.has_spawnable_ready(conn):
                        return True
                    if _kb.has_spawnable_review(conn):
                        return True
                except Exception:
                    continue
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return False

        # Auto-decompose: turn fresh triage tasks into ready workgraphs
        # before the dispatcher fans out workers. Gated by
        # ``kanban.auto_decompose`` (default True). Capped by
        # ``kanban.auto_decompose_per_tick`` (default 3) so a bulk-load
        # of triage tasks doesn't burst-spend the aux LLM in one tick;
        # remainder defers to subsequent ticks.
        auto_decompose_enabled = bool(kanban_cfg.get("auto_decompose", True))
        try:
            auto_decompose_per_tick = int(
                kanban_cfg.get("auto_decompose_per_tick", 3) or 3
            )
        except (TypeError, ValueError):
            auto_decompose_per_tick = 3
        if auto_decompose_per_tick < 1:
            auto_decompose_per_tick = 1

        def _auto_decompose_tick(boards: "Optional[list[dict]]" = None) -> int:
            """Run the auto-decomposer for up to N triage tasks across all
            boards. Returns the number of triage tasks that were
            successfully decomposed or specified this tick.

            ``boards`` may be passed in from the outer tick to avoid a
            redundant list_boards scan; if None it is fetched here
            (preserves independent callability).
            """
            try:
                from hermes_cli import kanban_decompose as _decomp
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "kanban auto-decompose: import failed (%s); skipping", exc,
                )
                return 0
            if boards is None:
                try:
                    boards = _kb.list_boards(include_archived=False)
                except Exception:
                    boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]

            def _bump_decompose_counter(target_id: str, *, ok: bool) -> None:
                """Fail-soft decompose-failure bookkeeping.

                A counter bump must never break the decomposition tick, so
                any DB error is swallowed (logged at debug). The board is
                already pinned via HERMES_KANBAN_BOARD, so connect() with no
                kwarg targets the right DB — same idiom as the rest of this
                function.
                """
                try:
                    with _kb.connect_closing() as _conn:
                        if ok:
                            _kb.reset_decompose_failed(_conn, target_id)
                        else:
                            _kb.record_decompose_failure(_conn, target_id)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(
                        "kanban auto-decompose: decompose_failed bump failed on %s (%s)",
                        target_id, exc,
                    )

            attempted = 0
            successes = 0
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin this board for the duration of the call — same
                # pattern as the dashboard specify endpoint. The
                # decomposer module connects with no board kwarg and
                # relies on the env var.
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug(
                            "kanban auto-decompose: list_triage_ids failed on board %s (%s)",
                            slug, exc,
                        )
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        try:
                            outcome = _decomp.decompose_task(
                                tid, author="auto-decomposer",
                            )
                        except Exception:
                            logger.exception(
                                "kanban auto-decompose: decompose_task crashed on %s",
                                tid,
                            )
                            _bump_decompose_counter(tid, ok=False)
                            continue
                        if outcome.ok:
                            successes += 1
                            _bump_decompose_counter(outcome.task_id, ok=True)
                            if outcome.fanout and outcome.child_ids:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → %d children",
                                    slug, tid, len(outcome.child_ids),
                                )
                            else:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → single task (no fanout)",
                                    slug, tid,
                                )
                        else:
                            _bump_decompose_counter(outcome.task_id, ok=False)
                            # Common no-op reasons (no aux client configured) shouldn't
                            # spam logs every tick. Log at debug.
                            logger.debug(
                                "kanban auto-decompose [%s]: %s skipped: %s",
                                slug, tid, outcome.reason,
                            )
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
            return successes

        logger.info(
            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval
        )
        while self._running:
            try:
                # Reap zombie children before per-board work so a board DB
                # failure cannot block cleanup of unrelated workers.
                pids = await asyncio.to_thread(_kb.reap_worker_zombies)
                if pids:
                    logger.info(
                        "kanban dispatcher: reaped %d zombie worker(s), pids=%s",
                        len(pids),
                        pids,
                    )
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                # Fetch the board list once per tick and share it across all
                # helpers so they don't each repeat the boards-dir scan.
                # FINDING #1: list_boards was previously called 4× per tick.
                def _fetch_boards() -> "list[dict]":
                    try:
                        return _kb.list_boards(include_archived=False)
                    except Exception:
                        return [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]

                tick_boards = await asyncio.to_thread(_fetch_boards)

                if auto_decompose_enabled:
                    await asyncio.to_thread(_auto_decompose_tick, tick_boards)
                results = await asyncio.to_thread(_tick_once, tick_boards)
                any_spawned = False
                for slug, res in (results or []):
                    if res is not None and getattr(res, "spawned", None):
                        any_spawned = True
                        # Quiet by default — only log when something actually
                        # happened, so an idle gateway stays silent.
                        logger.info(
                            "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                            "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                            slug,
                            len(res.spawned),
                            res.reclaimed,
                            len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                            len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                            res.promoted,
                            len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
                        )
                # Health telemetry (aggregate across boards)
                ready_pending = await asyncio.to_thread(_ready_nonempty, tick_boards)
                if ready_pending and not any_spawned:
                    bad_ticks += 1
                else:
                    bad_ticks = 0
                if bad_ticks >= HEALTH_WINDOW:
                    now = int(time.time())
                    if now - last_warn_at >= 300:
                        logger.warning(
                            "kanban dispatcher stuck: ready queue non-empty for "
                            "%d consecutive ticks but 0 workers spawned. Check "
                            "profile health (venv, PATH, credentials) and "
                            "`hermes kanban list --status ready`.",
                            bad_ticks,
                        )
                        last_warn_at = now

                # K16: bounded, profile-aware cost backfill for runs whose
                # final cost flushed to the worker's per-profile state.db
                # AFTER _end_run ran (so cost_usd was left NULL). Runs once
                # per tick, capped tight, fully fail-soft — must NEVER affect
                # dispatch. Off-loop via to_thread like the rest of this tick.
                try:
                    def _backfill_recent_costs() -> int:
                        with _kb.connect_closing() as _c:
                            n = _kb.backfill_run_costs(
                                _c, limit=50, since_seconds=6 * 3600,
                            )
                            # COST-VISIBILITY-WORKERS-S1: session-correlated
                            # pass for the runs worker_session_id / claude-log
                            # can't link (the bulk of kanban workers). Bounded to
                            # recent runs so old, permanently-unlinkable rows
                            # aren't re-scanned against large state.db every tick.
                            n += _kb.backfill_run_costs_from_sessions(
                                _c, limit=50, since_seconds=6 * 3600,
                            )
                            return n
                    n_cost = await asyncio.to_thread(_backfill_recent_costs)
                    if n_cost:
                        logger.info(
                            "kanban dispatcher: backfilled cost on %d recent run(s)",
                            n_cost,
                        )
                except Exception as exc:
                    logger.debug("kanban dispatcher: cost backfill skipped (%s)", exc)

                try:
                    await asyncio.to_thread(
                        _kb.write_kanban_dispatcher_heartbeat,
                        tick_health="ok",
                        boards=tick_boards,
                    )
                except Exception as exc:
                    logger.debug(
                        "kanban dispatcher: heartbeat write skipped (%s)", exc,
                    )
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            # Sleep in 1s slices so shutdown is snappy — otherwise a stop()
            # waits up to `interval` seconds for the current sleep to finish.
            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0
