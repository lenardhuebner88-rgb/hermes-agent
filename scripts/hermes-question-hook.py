#!/usr/bin/env python3
"""Claude-Code-Hook → Frage-Assistent-Ingest (I2, Hook-Quelle P1a).

Registriert in ~/.claude/settings.json als PreToolUse+PostToolUse-Hook
(matcher AskUserQuestion). Liest das Hook-JSON von stdin und meldet die
Frage mit EXAKTEN Optionen an das Dashboard (`POST /api/agent-questions/ingest`)
bzw. löst sie nach Terminal-Antwort auf (`POST /api/agent-questions/resolve`).

Design-Regeln (bezahlt in P0/I1 — nicht aufweichen):
- NIE Claude Code blocken: Parent forkt und exitet SOFORT 0; die Netzarbeit
  passiert im detachten Kind (setsid), timeout 2s, jede Exception → stiller Exit.
- Kill-Switch: HERMES_QUESTION_HOOK=0 → no-op.
- Ohne TMUX_PANE → no-op (nicht pane-adressierbar, also nicht beantwortbar).
- stdlib only (läuft mit System-python3, kein venv).
- Auth: Session-Token via loopback GET / (dokumentiertes Smoke-Muster; das
  Dashboard injiziert HERMES_SESSION_TOKEN__ in die Root-Seite).

Bewusst NICHT hier (I2-Scope): Notification-/Permission-Prompts — die parsebaren
Dialoge deckt der Scrape ab; Freitext/Presence kommen in I5.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request

DASHBOARD = os.environ.get("HERMES_QUESTION_HOOK_URL", "http://127.0.0.1:9119")
_TOKEN_RE = re.compile(r'HERMES_SESSION_TOKEN__="([^"]+)"')
_RECOMMENDED_RE = re.compile(r"\s*\((?:Recommended|Empfohlen)\)\s*$", re.IGNORECASE)
_LOG = os.path.expanduser("~/.hermes/logs/question-hook.log")


def _log(line: str) -> None:
    try:
        os.makedirs(os.path.dirname(_LOG), exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except Exception:
        pass


def _tmux_context(pane: str) -> tuple[str, str]:
    out = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{session_name}\t#{window_name}"],
        capture_output=True, text=True, timeout=2,
    )
    session, _, window = out.stdout.strip().partition("\t")
    return session, window


def _fetch_token() -> str:
    with urllib.request.urlopen(DASHBOARD + "/", timeout=2) as resp:
        m = _TOKEN_RE.search(resp.read().decode("utf-8", "replace"))
    return m.group(1) if m else ""


def _post(path: str, payload: dict) -> None:
    token = _fetch_token()
    req = urllib.request.Request(
        DASHBOARD + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Hermes-Session-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        resp.read()


def _first_question(tool_input: dict) -> dict | None:
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    q = questions[0]
    return q if isinstance(q, dict) and q.get("question") else None


def _store_options(q: dict) -> list[dict]:
    # nr = POSITION in der Original-Liste (1-basiert): die send-keys-Ziffer
    # muss der TUI-Nummerierung entsprechen, die alle Slots zählt — bei einem
    # (schema-widrigen) Leer-Label wird der Slot übersprungen, seine Nummer
    # aber NICHT neu vergeben.
    options = []
    for i, opt in enumerate(q.get("options") or [], start=1):
        label = str(opt.get("label") or "").strip()
        if not label:
            continue
        recommended = bool(_RECOMMENDED_RE.search(label))
        options.append({
            "nr": i,
            "label": _RECOMMENDED_RE.sub("", label).strip(),
            "recommended": recommended,
        })
    return options


def _action_context(event: dict, q: dict, tool_input: dict) -> str:
    parts = [f"AskUserQuestion: {q.get('header') or q.get('question', '')[:40]}"]
    n = len(tool_input.get("questions") or [])
    if n > 1:
        parts.append(f"{n - 1} weitere Frage(n)")
    if q.get("multiSelect"):
        parts.append("multiSelect")
    return " · ".join(parts)


def _handle(event: dict, pane: str) -> None:
    hook_event = event.get("hook_event_name")
    tool_input = event.get("tool_input") or {}
    hook_key = str(event.get("tool_use_id") or "")
    if not hook_key:
        return
    if hook_event == "PreToolUse":
        q = _first_question(tool_input)
        if q is None:
            return
        session, window = _tmux_context(pane)
        _post("/api/agent-questions/ingest", {
            "pane_id": pane,
            "session": session,
            "window": window,
            "kind": "claude",
            "cwd": event.get("cwd"),
            "question_text": str(q.get("question")),
            "options": _store_options(q),
            "action_context": _action_context(event, q, tool_input),
            "hook_key": hook_key,
        })
        _log(f"ingest ok pane={pane} key={hook_key}")
    elif hook_event == "PostToolUse":
        answers = (event.get("tool_response") or {}).get("answers") or {}
        answer = next(iter(answers.values()), None)
        _post("/api/agent-questions/resolve", {
            "hook_key": hook_key,
            "answer": str(answer) if answer is not None else None,
        })
        _log(f"resolve ok key={hook_key}")


def main() -> int:
    if os.environ.get("HERMES_QUESTION_HOOK", "1") == "0":
        return 0
    pane = os.environ.get("TMUX_PANE", "")
    if not re.fullmatch(r"%\d+", pane):
        return 0
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    # Fire-and-forget: Claude Code wartet auf den Hook-Prozess — sofort
    # detachen, damit die Frage ohne Verzögerung erscheint.
    try:
        if os.fork() > 0:
            return 0
    except OSError:
        pass  # kein fork möglich → synchron weiter (immer noch <2s)
    try:
        os.setsid()
    except OSError:
        pass
    try:
        _handle(event, pane)
    except Exception as exc:  # fail-silent, aber nachvollziehbar
        _log(f"error {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
