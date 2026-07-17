"""Web-Push for new open agent questions (Frage-Assistent I3).

Triggered from both new-open paths (hook ingest + scrape insert). Reuses the
kanban dashboard push sender; does not add VAPID/subscription infra.

Process-local debounce state is intentional: the poller lives long; restart
resets the 1/min window (acceptable — documented in plan). Visibility and
event data live in ``question_events.db`` so the web process (heartbeat) and
poller process (sender) share state without a second DB file.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import agent_questions as aq

logger = logging.getLogger(__name__)

# Max one outbound push per minute (bundled). Process-local; lost on restart.
DEBOUNCE_S = 60.0
# Never push for events older than this (wall clock vs event.ts).
MAX_EVENT_AGE_S = 2 * 3600.0
# Skip push when a control tab reported visible within this window.
VISIBILITY_FRESH_S = 30.0

_META_LAST_VISIBLE = "last_visible_ts"
_META_PENDING_IDS = "push_pending_ids"
_META_LAST_PUSH = "push_last_ts"

_lock = threading.Lock()
_flush_timer: threading.Timer | None = None

# Injectable for tests (sender + time).
_send_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _iso_to_epoch(ts: str) -> float | None:
    try:
        return aq._parse_iso_ts(str(ts or ""))
    except (TypeError, ValueError):
        return None


def set_last_visible_ts(
    *,
    now: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Record that a /control tab is visible (frontend heartbeat)."""
    now_ts = time.time() if now is None else float(now)
    aq.set_meta(_META_LAST_VISIBLE, f"{now_ts:.6f}", db_path=db_path, now=now_ts)


def get_last_visible_ts(*, db_path: Optional[Path] = None) -> float | None:
    raw = aq.get_meta(_META_LAST_VISIBLE, db_path=db_path)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _is_tab_visible(*, now: float, db_path: Optional[Path]) -> bool:
    last = get_last_visible_ts(db_path=db_path)
    if last is None:
        return False
    return (now - last) <= VISIBILITY_FRESH_S


def _load_pending(*, db_path: Optional[Path]) -> list[int]:
    raw = aq.get_meta(_META_PENDING_IDS, db_path=db_path)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[int] = []
    for item in data:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _save_pending(
    ids: list[int],
    *,
    db_path: Optional[Path],
    now: float,
) -> None:
    # Dedup preserve order
    seen: set[int] = set()
    ordered: list[int] = []
    for i in ids:
        if i in seen:
            continue
        seen.add(i)
        ordered.append(i)
    aq.set_meta(
        _META_PENDING_IDS,
        json.dumps(ordered, separators=(",", ":")),
        db_path=db_path,
        now=now,
    )


def _get_last_push_ts(*, db_path: Optional[Path]) -> float:
    raw = aq.get_meta(_META_LAST_PUSH, db_path=db_path)
    if not raw:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _set_last_push_ts(ts: float, *, db_path: Optional[Path]) -> None:
    aq.set_meta(_META_LAST_PUSH, f"{ts:.6f}", db_path=db_path, now=ts)


def _truncate(value: str, limit: int = 220) -> str:
    clean = " ".join((value or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def build_question_push_payload(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build hermes-control-push-v1 payload for one or more open questions.

    ``url`` deep-links to AnswerSheet: ``/control/agent-terminals?question=<id>``.
    Bundled: title "N offene Fragen"; single: "Frage von <agent>".
    """
    if not events:
        return None
    # Newest-first preference: highest id first when ties.
    ordered = sorted(events, key=lambda e: int(e.get("id") or 0), reverse=True)
    primary = ordered[0]
    primary_id = int(primary.get("id") or 0)
    if primary_id <= 0:
        return None
    n = len(ordered)
    if n == 1:
        kind = str(primary.get("kind") or "").strip() or "agent"
        title = f"Frage von {kind}"
        body = _truncate(str(primary.get("question_text") or ""))
        tag = f"agent-question-{primary_id}"
    else:
        title = f"{n} offene Fragen"
        body = _truncate(str(primary.get("question_text") or f"{n} neue Fragen"))
        tag = "agent-question-bundle"
    return {
        "schema": "hermes-control-push-v1",
        "title": title,
        "body": body,
        "tag": tag,
        "task_id": f"question-{primary_id}",
        "url": f"/control/agent-terminals?question={primary_id}",
    }


def _default_send(payload: dict[str, Any]) -> dict[str, Any]:
    """Reuse kanban dashboard web-push sender (default board subscriptions)."""
    try:
        from plugins.kanban.dashboard.plugin_api import _send_web_push_payload
    except Exception as exc:
        logger.info("agent_question_push: sender import failed: %s", exc)
        return {"enabled": False, "sent": 0, "removed": 0, "failed": 0}
    # board=None → default board (same as existing kanban pushes).
    return _send_web_push_payload(board=None, payload=payload)


def _send_payload(payload: dict[str, Any]) -> dict[str, Any]:
    fn = _send_fn if _send_fn is not None else _default_send
    return fn(payload)


def flush_pending_pushes(
    *,
    now: Optional[float] = None,
    db_path: Optional[Path] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Send one bundled push for pending event ids if debounce allows.

    Returns a small status dict for tests/logging.
    """
    now_ts = time.time() if now is None else float(now)
    with _lock:
        pending = _load_pending(db_path=db_path)
        if not pending:
            return {"sent": False, "reason": "empty"}
        if _is_tab_visible(now=now_ts, db_path=db_path):
            # Operator is looking — drop pending (no delayed surprise).
            _save_pending([], db_path=db_path, now=now_ts)
            return {"sent": False, "reason": "visible"}
        last = _get_last_push_ts(db_path=db_path)
        if not force and last > 0 and (now_ts - last) < DEBOUNCE_S:
            return {
                "sent": False,
                "reason": "debounce",
                "retry_in_s": DEBOUNCE_S - (now_ts - last),
            }

        events: list[dict[str, Any]] = []
        for eid in pending:
            ev = aq._load_event(int(eid), db_path=db_path)
            if ev is None:
                continue
            if str(ev.get("status") or "") != "open":
                continue
            ts_epoch = _iso_to_epoch(str(ev.get("ts") or ""))
            if ts_epoch is not None and (now_ts - ts_epoch) > MAX_EVENT_AGE_S:
                continue
            events.append(ev)

        _save_pending([], db_path=db_path, now=now_ts)
        if not events:
            return {"sent": False, "reason": "no-open"}

        payload = build_question_push_payload(events)
        if payload is None:
            return {"sent": False, "reason": "no-payload"}

        result = _send_payload(payload)
        _set_last_push_ts(now_ts, db_path=db_path)
        return {
            "sent": True,
            "n": len(events),
            "payload": payload,
            "result": result,
        }


def _schedule_flush(delay_s: float, *, db_path: Optional[Path]) -> None:
    global _flush_timer
    with _lock:
        if _flush_timer is not None:
            try:
                _flush_timer.cancel()
            except Exception:
                pass
            _flush_timer = None

        path = db_path

        def _fire() -> None:
            global _flush_timer
            try:
                flush_pending_pushes(db_path=path)
            except Exception:
                logger.warning("agent_question_push delayed flush failed", exc_info=True)
            finally:
                with _lock:
                    _flush_timer = None

        timer = threading.Timer(max(0.05, float(delay_s)), _fire)
        timer.daemon = True
        _flush_timer = timer
        timer.start()


def maybe_push_question(
    event_id: int,
    *,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
    event: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Queue a new open question for web-push (debounce + visibility + age).

    Safe to call from hook ingest and scrape insert. Never raises to caller.
    """
    try:
        return _maybe_push_question_impl(
            event_id, db_path=db_path, now=now, event=event
        )
    except Exception:
        logger.warning(
            "maybe_push_question failed event_id=%s", event_id, exc_info=True
        )
        return {"queued": False, "reason": "error"}


def _maybe_push_question_impl(
    event_id: int,
    *,
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
    event: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    now_ts = time.time() if now is None else float(now)
    eid = int(event_id)
    if eid <= 0:
        return {"queued": False, "reason": "bad-id"}

    ev = event if event is not None else aq._load_event(eid, db_path=db_path)
    if ev is None:
        return {"queued": False, "reason": "not-found"}
    if str(ev.get("status") or "") != "open":
        return {"queued": False, "reason": "not-open"}

    ts_epoch = _iso_to_epoch(str(ev.get("ts") or ""))
    if ts_epoch is not None and (now_ts - ts_epoch) > MAX_EVENT_AGE_S:
        return {"queued": False, "reason": "too-old"}

    if _is_tab_visible(now=now_ts, db_path=db_path):
        return {"queued": False, "reason": "visible"}

    with _lock:
        pending = _load_pending(db_path=db_path)
        if eid not in pending:
            pending.append(eid)
        _save_pending(pending, db_path=db_path, now=now_ts)
        last = _get_last_push_ts(db_path=db_path)
        elapsed = now_ts - last if last > 0 else DEBOUNCE_S
        can_send = last <= 0 or elapsed >= DEBOUNCE_S

    if can_send:
        return {
            "queued": True,
            "flush": flush_pending_pushes(now=now_ts, db_path=db_path),
        }

    remaining = max(0.05, DEBOUNCE_S - elapsed)
    _schedule_flush(remaining, db_path=db_path)
    return {"queued": True, "reason": "debounced", "retry_in_s": remaining}


def reset_push_state_for_tests(*, db_path: Optional[Path] = None) -> None:
    """Clear process-local timer + meta keys (tests only)."""
    global _flush_timer, _send_fn
    with _lock:
        if _flush_timer is not None:
            try:
                _flush_timer.cancel()
            except Exception:
                pass
            _flush_timer = None
        _send_fn = None
    if db_path is not None or True:
        now_ts = time.time()
        try:
            aq.set_meta(_META_PENDING_IDS, "[]", db_path=db_path, now=now_ts)
            aq.set_meta(_META_LAST_PUSH, "0", db_path=db_path, now=now_ts)
            # leave last_visible for explicit tests
        except Exception:
            pass
