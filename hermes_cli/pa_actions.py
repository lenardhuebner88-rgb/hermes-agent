"""Operator-confirmed action proposals for the Projekte PA.

The PA may enqueue typed proposals, but this module is the only execution path.
It claims the corresponding ``question_events`` row exactly once before any
handler runs, then persists local evidence in both stores.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hermes_cli import agent_questions

_log = logging.getLogger(__name__)

PA_ACTION_OPTIONS = [
    {"nr": 1, "label": "Ausführen", "recommended": False},
    {"nr": 2, "label": "Ablehnen", "recommended": False},
]
_PANE_TAIL_MAX_CHARS = 4_000

ActionHandler = Callable[[dict[str, str]], dict[str, Any]]


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _new_tmux_service() -> Any:
    from hermes_cli.agent_terminals import TmuxAgentSessionService

    return TmuxAgentSessionService()


def _capture_evidence(service: Any, session: str, window: str) -> dict[str, Any]:
    try:
        raw = service.capture(session, window, start=-50, log=False)
        try:
            from hermes_cli.agent_terminals import strip_ansi

            tail = strip_ansi(raw or "")
        except Exception:
            tail = raw or ""
        return {"pane_tail": tail[-_PANE_TAIL_MAX_CHARS:]}
    except Exception as exc:
        # The mutation already landed.  A missing post-action capture is evidence
        # degradation, not permission to retry the action.
        return {"pane_tail": None, "pane_tail_error": str(exc)[:800]}


def _tmux_target(service: Any, payload: dict[str, str]) -> tuple[str, str]:
    session = service.validate_name(payload["session"], field="session")
    window = service.validate_name(payload["window"], field="window")
    return session, window


def _handle_tmux_send_keys(payload: dict[str, str]) -> dict[str, Any]:
    service = _new_tmux_service()
    session, window = _tmux_target(service, payload)
    keys = payload["keys"]
    service.send_keys(session, window, keys)
    return {
        "ok": True,
        "exit": 0,
        "payload": {
            "session": session,
            "window": window,
            "bytes": len(keys.encode("utf-8")),
        },
        **_capture_evidence(service, session, window),
    }


def _handle_tmux_interrupt(payload: dict[str, str]) -> dict[str, Any]:
    service = _new_tmux_service()
    session, window = _tmux_target(service, payload)
    service.interrupt(session, window)
    return {
        "ok": True,
        "exit": 0,
        "payload": {"session": session, "window": window, "signal": "C-c"},
        **_capture_evidence(service, session, window),
    }


def _kanban_result_payload(kb: Any, conn: Any, card_id: str) -> dict[str, Any]:
    task = kb.get_task(conn, card_id)
    return {
        "card_id": card_id,
        "status": getattr(task, "status", None) if task is not None else None,
    }


def _handle_kanban(category: str, payload: dict[str, str]) -> dict[str, Any]:
    # These are the same server-side primitives used by kanban tools/dashboard
    # routes.  This is already the audited, operator-confirmed write, so it must
    # not opt into HERMES_SANDBOX_MODE.
    from hermes_cli import kanban_db as kb

    card_id = payload["card_id"]
    reason = payload.get("reason")
    ok = False
    detail: dict[str, Any] = {}
    with kb.connect_closing() as conn:
        if category == "kanban.unblock":
            if reason:
                kb.add_comment(
                    conn,
                    card_id,
                    author="pa-executor",
                    body=f"UNBLOCK: {reason}",
                )
            ok = bool(kb.unblock_task(conn, card_id))
        elif category == "kanban.nudge":
            comment_id = kb.add_comment(
                conn,
                card_id,
                author="pa-executor",
                body=reason or "Operator-Nudge via PA: bitte Status prüfen / weitermachen.",
            )
            ok = True
            detail["comment_id"] = comment_id
        elif category == "kanban.hold":
            hold_reason = "operator hold via pa-executor"
            if reason:
                hold_reason += f": {reason}"
            ok = bool(kb.hold_task(conn, card_id, reason=hold_reason))
        elif category == "kanban.resume":
            if reason:
                kb.add_comment(
                    conn,
                    card_id,
                    author="pa-executor",
                    body=f"RESUME: {reason}",
                )
            ok = bool(kb.unblock_task(conn, card_id))
        elif category == "kanban.kill":
            ok = bool(
                kb.reclaim_task(
                    conn,
                    card_id,
                    reason=reason or "operator kill via pa-executor",
                )
            )
        elif category == "kanban.release":
            if kb.release_freigabe_hold(conn, card_id, author="pa-executor"):
                ok = True
                detail["release_kind"] = "freigabe"
            elif kb.release_uireal_root(conn, card_id, author="pa-executor"):
                ok = True
                detail["release_kind"] = "ui-real"
            if ok and reason:
                kb.add_comment(
                    conn,
                    card_id,
                    author="pa-executor",
                    body=f"RELEASE: {reason}",
                )
        result_payload = _kanban_result_payload(kb, conn, card_id)
    return {
        "ok": ok,
        "exit": 0 if ok else 1,
        "payload": {**result_payload, **detail},
        **({} if ok else {"error": f"{category} wurde vom Kanban-Store abgelehnt"}),
    }


def _handle_kanban_unblock(payload: dict[str, str]) -> dict[str, Any]:
    return _handle_kanban("kanban.unblock", payload)


def _handle_kanban_nudge(payload: dict[str, str]) -> dict[str, Any]:
    return _handle_kanban("kanban.nudge", payload)


def _handle_kanban_hold(payload: dict[str, str]) -> dict[str, Any]:
    return _handle_kanban("kanban.hold", payload)


def _handle_kanban_resume(payload: dict[str, str]) -> dict[str, Any]:
    return _handle_kanban("kanban.resume", payload)


def _handle_kanban_kill(payload: dict[str, str]) -> dict[str, Any]:
    return _handle_kanban("kanban.kill", payload)


def _handle_kanban_release(payload: dict[str, str]) -> dict[str, Any]:
    return _handle_kanban("kanban.release", payload)


def _handle_planspec_ingest(payload: dict[str, str]) -> dict[str, Any]:
    from hermes_cli.pa_planspec import ingest_draft

    return ingest_draft(payload)


ACTION_HANDLERS: dict[str, ActionHandler] = {
    "tmux.send_keys": _handle_tmux_send_keys,
    "tmux.interrupt": _handle_tmux_interrupt,
    "kanban.unblock": _handle_kanban_unblock,
    "kanban.nudge": _handle_kanban_nudge,
    "kanban.hold": _handle_kanban_hold,
    "kanban.resume": _handle_kanban_resume,
    "kanban.kill": _handle_kanban_kill,
    "kanban.release": _handle_kanban_release,
    "planspec.ingest": _handle_planspec_ingest,
}


def execute_action(category: str, payload: Any) -> dict[str, Any]:
    """Validate again at the trust boundary and dispatch through the registry."""
    normalized = agent_questions.normalize_pa_action_payload(category, payload)
    handler = ACTION_HANDLERS.get(category)
    if handler is None:  # Defensive: schemas and registry must remain in lockstep.
        raise ValueError(f"Kein Handler für pa_action-Kategorie: {category}")
    result = handler(normalized)
    if not isinstance(result, dict):
        raise TypeError(f"Handler für {category} lieferte kein Ergebnisobjekt")
    return result


def enqueue_pa_action(
    category: str,
    payload: Any,
    *,
    reason: str | None,
    db_path: Path | None = None,
) -> int:
    """Validate and enqueue one gated action, deduplicating open equivalents."""
    envelope = agent_questions.build_pa_action_envelope(
        category,
        payload,
        reason=reason,
    )
    normalized = envelope["payload"]
    if category == "planspec.ingest":
        from hermes_cli.pa_planspec import build_ingest_question

        question_text = build_ingest_question(envelope)
    else:
        question_text = (
            f"PA-Aktion ausführen: {category}?"
            + (f" — {envelope['reason']}" if envelope.get("reason") else "")
        )
    fingerprint = agent_questions.pa_action_fingerprint(category, normalized)
    event_id = agent_questions.insert_question_event(
        session=agent_questions.PA_ACTION_SENTINEL,
        window=agent_questions.PA_ACTION_SENTINEL,
        pane_id=agent_questions.PA_ACTION_SENTINEL,
        fingerprint=fingerprint,
        question_text=question_text,
        options=PA_ACTION_OPTIONS,
        kind="pa_action",
        source="pa",
        class_="action",
        action_payload=envelope,
        db_path=db_path,
    )
    if event_id is not None:
        return event_id
    existing = agent_questions.find_open_event(
        agent_questions.PA_ACTION_SENTINEL,
        fingerprint,
        db_path=db_path,
    )
    if existing is None:  # A concurrent close won the race; a retry may enqueue.
        retry_id = agent_questions.insert_question_event(
            session=agent_questions.PA_ACTION_SENTINEL,
            window=agent_questions.PA_ACTION_SENTINEL,
            pane_id=agent_questions.PA_ACTION_SENTINEL,
            fingerprint=fingerprint,
            question_text=question_text,
            options=PA_ACTION_OPTIONS,
            kind="pa_action",
            source="pa",
            class_="action",
            action_payload=envelope,
            db_path=db_path,
        )
        if retry_id is not None:
            return retry_id
        existing = agent_questions.find_open_event(
            agent_questions.PA_ACTION_SENTINEL,
            fingerprint,
            db_path=db_path,
        )
    if existing is None:
        raise RuntimeError("pa_action konnte nicht atomar eingereiht werden")
    return int(existing["id"])


def _thread_message(evidence: dict[str, Any]) -> str:
    category = str(evidence.get("category") or "unbekannt")
    status = str(evidence.get("status") or "unbekannt")
    request = evidence.get("request") if isinstance(evidence.get("request"), dict) else {}
    result = evidence.get("result") if isinstance(evidence.get("result"), dict) else {}
    lines = [
        f"PA-Aktion `{category}`: {status}.",
        "Anfrage: " + json.dumps(request, ensure_ascii=False, sort_keys=True),
        "Ergebnis: " + json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
    ]
    pane_tail = result.get("pane_tail")
    if pane_tail:
        lines.extend(["Pane-Evidenz:", str(pane_tail)])
    chain_id = result.get("chain_id")
    if chain_id:
        lines.append(f"Ketten-ID: {chain_id}")
    task_ids = result.get("task_ids")
    if isinstance(task_ids, list) and task_ids:
        lines.append("Task-IDs: " + ", ".join(str(item) for item in task_ids))
    stdout_tail = result.get("stdout_tail")
    if stdout_tail:
        lines.extend(["CLI-Evidenz:", str(stdout_tail)])
    return "\n".join(lines)


def _append_thread_evidence(event_id: int, evidence: dict[str, Any]) -> None:
    from hermes_cli.pa_chat import PAStore

    PAStore().append_executor_message(event_id, _thread_message(evidence))


def answer_pa_action(
    event_id: int,
    answer: str,
    *,
    event: dict[str, Any],
    answered_by: str,
    db_path: Path | None,
) -> dict[str, Any]:
    """Claim once, then reject or execute without touching the tmux answer path."""
    envelope = agent_questions.normalize_pa_action_envelope(event.get("action_payload"))
    category = envelope["category"]
    payload = envelope["payload"]
    if not agent_questions._claim_event(
        event_id,
        answer=answer,
        answered_by=answered_by,
        answer_source="operator_free",
        db_path=db_path,
    ):
        return {"ok": False, "reason": "not-open"}

    executed = answer == "1"
    if not executed:
        result: dict[str, Any] = {"ok": True, "exit": 0, "executed": False}
        status = "rejected"
    else:
        try:
            result = execute_action(category, payload)
        except Exception as exc:
            _log.warning("pa_action execution failed event_id=%s: %s", event_id, exc)
            result = {
                "ok": False,
                "exit": 1,
                "error": str(exc).strip()[:1000] or exc.__class__.__name__,
            }
        status = "succeeded" if result.get("ok") else "failed"

    evidence: dict[str, Any] = {
        "version": agent_questions.PA_ACTION_VERSION,
        "event_id": int(event_id),
        "category": category,
        "status": status,
        "executed": executed,
        "request": envelope,
        "result": result,
        "ts": _utc_now(),
    }
    agent_questions.set_pa_action_result(event_id, evidence, db_path=db_path)
    try:
        _append_thread_evidence(event_id, evidence)
    except Exception as exc:
        # The confirmed action must never be retried because a secondary evidence
        # sink was unavailable.  Record that degradation on the primary event.
        _log.warning("pa_action thread evidence failed event_id=%s: %s", event_id, exc)
        evidence["thread_error"] = str(exc).strip()[:1000] or exc.__class__.__name__
        agent_questions.set_pa_action_result(event_id, evidence, db_path=db_path)

    return {
        "ok": True,
        "verified": bool(result.get("ok")),
        "executed": executed,
        "action_result": evidence,
    }
