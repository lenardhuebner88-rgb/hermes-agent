#!/usr/bin/env python3
"""Read-only Autoresearch view for the live Hermes dashboard (9119).

Phase 4 of the autoresearch-dashboard plan: add a ``/autoresearch`` surface to
the existing FastAPI dashboard that answers "läuft ein Loop gerade — ja/nein?"
plus the audit history, with **no mutation**.

Routes registered by :func:`register_autoresearch_routes`:

* ``GET  /autoresearch``         — self-contained HTML view (polls the JSON below)
* ``GET  /autoresearch/status``  — live loop state from lock + heartbeat (OPEN)
* ``GET  /autoresearch/audit``   — inventory counts + results history (OPEN)
* ``POST /autoresearch/trigger`` — TOKEN-GATED; Phase 4 has no runner → 503
* ``POST /autoresearch/stop``    — TOKEN-GATED; Phase 4 has no runner → 503

The GET routes are intentionally open: they are read-only and safe over the
tailnet (the dashboard is fronted by Tailscale Serve). The POST routes require a
local token (``HERMES_AUTORESEARCH_TOKEN``, injected at dashboard start, never
written to vault/git/logs/HTML). Without a valid token they return **403**. In
Phase 4 there is deliberately no runner, so even a valid token yields **503**
(the applying runner + Trigger/Stop wiring is Phase 5).

The runner-state contract (written by the Phase 5 runner, or by the tiny
``--dry-run`` heartbeat stub for UI testing) lives under::

    <audit>/runner-state/
        current.lock        {pid, request_id, started_at}    presence == a loop intends to run
        current.heartbeat   {pid, request_id, iteration, max, last_step, last_eval, ts}
        current.status      {state, route_status, last_receipt, updated_at}
"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Imported at module top level (not inside register_autoresearch_routes) so that
# with ``from __future__ import annotations`` FastAPI's get_type_hints can
# resolve handler annotations like ``request: Request`` against module globals.
# This module is only imported by the dashboard, where fastapi is guaranteed.
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ValidationError

# Sprint A1: persistent proposal store + apply-by-id (the One-Click flow).
from hermes_cli import autoresearch_proposals as _proposals
from scripts import autoresearch_writer as _writer

# hermes-agent repo root (this file lives in hermes_cli/).
_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_AUDIT = _REPO / ".hermes" / "skill-audit"

# Heartbeat older than this (seconds) means the loop is no longer alive →
# a present lock with a stale heartbeat is reported as "crashed".
_DEFAULT_HEARTBEAT_TTL = 30.0

_DATA_SCRIPT_RE = re.compile(
    r'<script type="application/json" id="data-autoresearch">(.+?)</script>',
    re.DOTALL,
)
_SCAFFOLD_MARKER = "autoresearch-scaffold"
_SCAFFOLD_TODO_RE = re.compile(r"document the \*\*(?P<section>[^*]+)\*\* of `(?P<skill>[^`]+)`")


# ---------------------------------------------------------------------------
# Path resolution (env-overridable so tests can point at a tmp dir)
# ---------------------------------------------------------------------------
def _audit_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_AUDIT_DIR")
    return Path(override) if override else _DEFAULT_AUDIT


def _state_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_STATE_DIR")
    return Path(override) if override else (_audit_dir() / "runner-state")


def _heartbeat_ttl() -> float:
    try:
        return float(os.environ.get("HERMES_AUTORESEARCH_HEARTBEAT_TTL", _DEFAULT_HEARTBEAT_TTL))
    except (TypeError, ValueError):
        return _DEFAULT_HEARTBEAT_TTL


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _age_seconds(ts: Any, *, now: float | None = None) -> float | None:
    """Seconds elapsed since heartbeat ``ts`` (epoch float/int or ISO8601)."""
    if ts is None:
        return None
    now = time.time() if now is None else now
    # epoch seconds
    if isinstance(ts, (int, float)):
        return max(0.0, now - float(ts))
    if isinstance(ts, str):
        s = ts.strip()
        try:
            return max(0.0, now - float(s))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, now - parsed.timestamp())
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Status (the "läuft / läuft nicht" bridge)
# ---------------------------------------------------------------------------
def read_runner_status(*, now: float | None = None) -> dict[str, Any]:
    """Derive live loop status from lock + heartbeat + status files.

    State machine:
      no lock file                              → idle
      lock + fresh heartbeat + status=stopping  → stopping
      lock + fresh heartbeat                    → running
      lock + stale / missing heartbeat          → crashed
    """
    state_dir = _state_dir()
    lock = _read_json(state_dir / "current.lock")
    heartbeat = _read_json(state_dir / "current.heartbeat")
    status_file = _read_json(state_dir / "current.status") or {}

    # route_status: from the status file when a run wrote it; otherwise a live
    # self-test (config-presence) so the badge is meaningful even at idle.
    route_status = status_file.get("route_status") or self_test()["route_status"]

    base: dict[str, Any] = {
        "schema": "autoresearch-runner-status-v1",
        "state": "idle",
        "pid": None,
        "request_id": None,
        "iteration": None,
        "max": None,
        "last_step": None,
        "last_eval": None,
        "route_status": route_status,
        "heartbeat_age_s": None,
        "heartbeat_fresh": False,
        "last_receipt": status_file.get("last_receipt"),
        "last_run": status_file.get("last_run"),
        "note": status_file.get("note"),
        "state_dir": str(state_dir),
    }

    if not lock:
        return base

    ttl = _heartbeat_ttl()
    age = _age_seconds((heartbeat or {}).get("ts"), now=now)
    fresh = age is not None and age < ttl

    base.update(
        {
            "pid": lock.get("pid"),
            "request_id": lock.get("request_id"),
            "iteration": (heartbeat or {}).get("iteration"),
            "max": (heartbeat or {}).get("max"),
            "last_step": (heartbeat or {}).get("last_step"),
            "last_eval": (heartbeat or {}).get("last_eval"),
            "heartbeat_age_s": round(age, 1) if age is not None else None,
            "heartbeat_fresh": fresh,
        }
    )

    declared = str(status_file.get("state") or "").lower()
    if not fresh:
        base["state"] = "crashed"
    elif declared == "stopping":
        base["state"] = "stopping"
    else:
        base["state"] = "running"
    return base


# ---------------------------------------------------------------------------
# Audit (inventory counts + results history)
# ---------------------------------------------------------------------------
def _parse_results(audit_dir: Path) -> list[dict[str, str]]:
    path = audit_dir / "autoresearch_results.tsv"
    try:
        if not path.exists() or path.stat().st_size == 0:
            return []
        with path.open("r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh, delimiter="\t"))
    except (OSError, ValueError):
        return []


def _embedded_inventory(audit_dir: Path) -> dict[str, Any] | None:
    """Pull the precomputed inventory/counts JSON from the rendered dashboard.html.

    The standalone renderer (Phase 1-3) embeds an
    ``autoresearch-dashboard-data-v1`` blob with priority/area/weakness counts.
    Reusing it avoids re-parsing the rubric here. Returns None if absent.
    """
    path = audit_dir / "dashboard.html"
    try:
        if not path.exists():
            return None
        match = _DATA_SCRIPT_RE.search(path.read_text(encoding="utf-8", errors="replace"))
        if not match:
            return None
        data = json.loads(match.group(1))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def read_audit() -> dict[str, Any]:
    audit_dir = _audit_dir()
    results = _parse_results(audit_dir)
    decision_counts: dict[str, int] = {}
    for row in results:
        decision = (row.get("decision") or "unknown").strip().lower() or "unknown"
        decision_counts[decision] = decision_counts.get(decision, 0) + 1

    embedded = _embedded_inventory(audit_dir)
    inventory: dict[str, Any] | None = None
    if embedded:
        inventory = {
            "priority_counts": embedded.get("priority_counts"),
            "area_counts": embedded.get("area_counts"),
            "weakness_counts": embedded.get("weakness_counts"),
            "model_preference": embedded.get("model_preference"),
            "model_route_status": embedded.get("model_route_status"),
            "inventory_summary": embedded.get("inventory_summary"),
            "generated_at": embedded.get("generated_at"),
        }

    return {
        "schema": "autoresearch-audit-v1",
        "audit_dir": str(audit_dir),
        "results_count": len(results),
        "decision_counts": decision_counts,
        "results": results,
        "inventory": inventory,
    }


# ---------------------------------------------------------------------------
# Worklist: skills that still carry an autoresearch placeholder ("needs input")
# ---------------------------------------------------------------------------
def scan_open_scaffolds() -> dict[str, Any]:
    """Find live skills that have an autoresearch scaffold section still on TODO.

    These are the concrete "next step" items: apply inserts the section skeleton;
    the operator (or a later model pass) fills in the actual wording.
    """
    root = _skills_root()
    items: list[dict[str, str]] = []
    if root.exists():
        for path in sorted(root.rglob("SKILL.md")):
            try:
                rel_parts = path.relative_to(root).parts
            except ValueError:
                rel_parts = path.parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _SCAFFOLD_MARKER not in text:
                continue
            for m in _SCAFFOLD_TODO_RE.finditer(text):
                items.append({
                    "skill": m.group("skill"),
                    "section": m.group("section"),
                    "path": str(path),
                })
    return {"schema": "autoresearch-worklist-v1", "count": len(items),
            "open_scaffolds": items, "skills_root": str(root)}


# ---------------------------------------------------------------------------
# MiniMax-M2.7 self-test (harmless config-presence check; no secrets emitted)
# ---------------------------------------------------------------------------
_MODEL_NEEDLE = "MiniMax-M2.7"


def _skills_root() -> Path:
    override = os.environ.get("HERMES_SKILLS_ROOT")
    return Path(override) if override else (_hermes_home() / "skills")


def _hermes_home() -> Path:
    override = os.environ.get("HERMES_HOME")
    if override:
        return Path(override)
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception:
        return Path.home() / ".hermes"


def self_test() -> dict[str, str]:
    """Is the MiniMax-M2.7 route configured? config-presence only, no secrets."""
    cfg = _hermes_home() / "config.yaml"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"route_status": "yellow", "detail": "config.yaml unreadable"}
    if _MODEL_NEEDLE in text:
        return {"route_status": "configured", "detail": f"{_MODEL_NEEDLE} present in config.yaml"}
    return {"route_status": "unavailable", "detail": f"{_MODEL_NEEDLE} not found in config.yaml"}


# ---------------------------------------------------------------------------
# Runner spawn / stop (no token — single-operator system; safety is the runner's
# reversibility: backup + eval-revert + cap + SIGTERM, mutation only in skills/).
# Apply still needs an explicit operator confirm (the one "are you sure" step).
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parents[1]
_RUNNER_SCRIPT = _REPO / "scripts" / "run_autoresearch_request.py"
_REQUEST_SCRIPT = _REPO / "scripts" / "autoresearch_request.py"


def _load_request_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("autoresearch_request", _REQUEST_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _spawn_runner(args: list[str]) -> int:
    """Spawn the runner detached; return its PID. Isolated for test injection."""
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, str(_RUNNER_SCRIPT), *args],
        cwd=str(_REPO),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def _signal_pid(pid: int, sig: int) -> None:
    os.kill(pid, sig)


class TriggerBody(BaseModel):
    area: str = "all"
    focus: str = "recommended_sections"
    mode: str = "dry-run"  # "dry-run" | "apply"
    confirm: bool = False
    max_iterations: int = 1


class ApplyProposalBody(BaseModel):
    id: str
    confirm: bool = True


class ConfirmBatchBody(BaseModel):
    ids: list[str]
    confirm: bool = True


class SkipProposalBody(BaseModel):
    id: str


def _finding_from_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": proposal.get("id"),
        "proposal_type": proposal.get("proposal_type"),
        "skill": proposal.get("target"),
        "section": proposal.get("section"),
        "category": proposal.get("category"),
        "title": proposal.get("title"),
        "problem": proposal.get("rationale_plain"),
        "evidence": proposal.get("evidence"),
        "fix_hint": proposal.get("fix_hint"),
        "writer_rationale": proposal.get("writer_rationale"),
    }


def confirm_batch_proposals(ids: list[str], *, confirm: bool = True) -> dict[str, Any]:
    """Judge and apply skill proposals by id through the existing apply path."""
    results: list[dict[str, Any]] = []
    if not confirm:
        for pid in ids:
            results.append({
                "id": pid,
                "status": "skipped",
                "reason": "batch confirm requires confirm=true",
            })
        return {"ok": False, "results": results}

    for pid in ids:
        proposal = _proposals.load_proposal(pid)
        if proposal is None:
            results.append({
                "id": pid,
                "status": "skipped",
                "reason": f"no such proposal: {pid}",
            })
            continue
        if proposal.get("status") != "proposed":
            results.append({
                "id": pid,
                "status": "skipped",
                "reason": f"proposal is '{proposal.get('status')}', not actionable",
            })
            continue
        if proposal.get("mode") != "skill":
            results.append({
                "id": pid,
                "status": "skipped",
                "reason": "batch confirm only supports skill proposals",
            })
            continue
        before_text = proposal.get("before_text")
        after_text = proposal.get("after_text")
        if not isinstance(before_text, str) or not isinstance(after_text, str):
            results.append({
                "id": pid,
                "status": "skipped",
                "reason": "proposal is malformed (missing before_text/after_text)",
            })
            continue

        judge = _writer.judge_fix(before_text, after_text, _finding_from_proposal(proposal))
        if not (judge.get("resolved") is True and judge.get("no_regression") is True):
            results.append({
                "id": pid,
                "status": "skipped",
                "reason": str(judge.get("reason") or "judge rejected proposal"),
                "judge": judge,
            })
            continue

        applied = _proposals.apply_proposal(pid, confirm=True, judged=True)
        if applied.get("ok") is True:
            results.append({
                "id": pid,
                "status": "applied",
                "reason": str(applied.get("result") or "applied"),
                "judge": judge,
                "apply_result": applied,
            })
        else:
            results.append({
                "id": pid,
                "status": "skipped",
                "reason": str(applied.get("detail") or applied.get("result") or "apply failed"),
                "judge": judge,
                "apply_result": applied,
            })

    return {
        "ok": all(item["status"] == "applied" for item in results) if results else True,
        "results": results,
    }


def start_runner(*, area: str, focus: str, mode: str, confirm: bool,
                 max_iterations: int) -> dict[str, Any]:
    """Create a run-request and spawn the bounded runner. No token; apply needs confirm."""
    if mode not in {"dry-run", "apply"}:
        raise HTTPException(status_code=400, detail="mode must be 'dry-run' or 'apply'")
    if mode == "apply" and not confirm:
        raise HTTPException(status_code=400, detail="apply requires confirm=true (the operator 'are you sure' step)")

    state = read_runner_status()["state"]
    if state in {"running", "stopping"}:
        raise HTTPException(status_code=409, detail="a run is already in progress")

    arr = _load_request_module()
    mi = max(1, min(int(max_iterations), arr.MAX_ITERATIONS))
    requests_dir = _audit_dir() / "run-requests"
    try:
        request_path = arr.create_request(
            mode="skills", area=area, focus=focus, max_iterations=mi,
            mutation_policy="requires_operator_go", requests_dir=requests_dir,
            repo_root=_REPO, hermes_home=_hermes_home(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid request: {exc}")

    args = [str(request_path)]
    if mode == "apply":
        args += ["--apply", "--confirm"]
    args += ["--max-iterations", str(mi)]
    pid = _spawn_runner(args)
    return {
        "ok": True, "mode": mode, "pid": pid,
        "request_id": Path(request_path).stem, "request_path": str(request_path),
        "area": area, "focus": focus, "max_iterations": mi,
    }


def stop_runner() -> dict[str, Any]:
    """SIGTERM the running loop (read PID from the lock). No token; stop is safe."""
    lock = _read_json(_state_dir() / "current.lock")
    pid = lock.get("pid") if lock else None
    if not pid:
        return {"ok": True, "state": "idle", "detail": "nothing running"}
    try:
        _signal_pid(int(pid), signal.SIGTERM)
    except (OSError, ValueError) as exc:
        return {"ok": False, "detail": f"could not signal pid {pid}: {exc}"}
    return {"ok": True, "signalled": int(pid), "detail": "SIGTERM sent; runner finishes its step and releases the lock"}


# ---------------------------------------------------------------------------
# Self-contained HTML view (polls the JSON routes; no mutation forms)
# ---------------------------------------------------------------------------
_HTML_PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Autoresearch</title>
<style>
:root{
  color-scheme:light;
  --ink:#15201a; --muted:#647069; --line:#dde3da; --panel:#f5f8f2; --card:#ffffff;
  --accent:#0f6b52; --accent-soft:#e4f3ec; --warn:#a9590f; --warn-soft:#fff1dc;
  --bad:#9b2635; --bad-soft:#fbe3e6; --info:#1f5fa8; --info-soft:#e6effb; --bg:#fafcf7;
  --radius:12px; --shadow:0 1px 2px rgba(20,40,30,.06),0 2px 8px rgba(20,40,30,.05);
}
@media (prefers-color-scheme: dark){
  :root{ color-scheme:dark; --ink:#e7efe9;
    --muted:#9baba2; --line:#26312b; --panel:#161d19; --card:#121815; --accent:#48c79e;
    --accent-soft:#16302a; --warn:#e0a35c; --warn-soft:#2c2417; --bad:#f08a96; --bad-soft:#2c191c;
    --info:#7db4ee; --info-soft:#172430; --bg:#0d1210; }
}
*{box-sizing:border-box;}
body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased;}
a{color:var(--accent);}
.topbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding:14px 20px;background:color-mix(in srgb,var(--card) 92%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);}
.topbar h1{margin:0;font-size:18px;letter-spacing:.2px;flex:0 0 auto;}
.topbar .spacer{flex:1 1 auto;}
.updated{color:var(--muted);font-size:13px;}
.iconbtn{cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:6px 10px;font-size:13px;}
main{max-width:1100px;margin:0 auto;padding:18px 18px 48px;display:grid;gap:16px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:16px 18px;}
.card h2{margin:0 0 14px;font-size:15px;font-weight:650;letter-spacing:.2px;display:flex;align-items:center;gap:10px;}
.grid{display:grid;gap:12px;}
.cols{grid-template-columns:repeat(auto-fit,minmax(150px,1fr));}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;}
.pill{display:inline-flex;align-items:center;gap:7px;padding:5px 13px;border-radius:999px;font-weight:700;font-size:14px;}
.pill::before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor;}
.pill-idle{background:var(--panel);color:var(--muted);}
.pill-running{background:var(--accent-soft);color:var(--accent);}
.pill-stopping{background:var(--warn-soft);color:var(--warn);}
.pill-crashed{background:var(--bad-soft);color:var(--bad);}
.pill-running::before{animation:blink 1s infinite;}
@keyframes blink{50%{opacity:.25;}}
.kv{border:1px solid var(--line);border-radius:10px;padding:11px 12px;background:var(--panel);}
.kv .k{display:block;color:var(--muted);font-size:12px;margin-bottom:5px;}
.kv .v{font-size:17px;font-weight:600;word-break:break-word;}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;background:var(--accent-soft);color:var(--accent);}
.badge-yellow{background:var(--warn-soft);color:var(--warn);}
.badge-bad{background:var(--bad-soft);color:var(--bad);}
.badge-info{background:var(--info-soft);color:var(--info);}
.progress{height:9px;border-radius:999px;background:var(--panel);overflow:hidden;border:1px solid var(--line);}
.progress>span{display:block;height:100%;background:var(--accent);width:0;transition:width .4s ease;}
label.field{display:flex;flex-direction:column;gap:5px;font-size:13px;color:var(--muted);min-width:120px;}
label.field select,label.field input{border:1px solid var(--line);border-radius:8px;padding:9px 10px;background:var(--card);color:var(--ink);font-size:14px;}
.btn{cursor:pointer;border:1px solid var(--line);border-radius:9px;padding:10px 16px;font-weight:650;font-size:14px;background:var(--panel);color:var(--ink);transition:filter .15s;}
.btn:hover:not(:disabled){filter:brightness(.97);}
.btn:disabled{opacity:.45;cursor:not-allowed;}
.btn-primary{background:var(--accent);color:#fff;border-color:transparent;}
.btn-apply{background:var(--accent-soft);color:var(--accent);border-color:transparent;}
.btn-stop{background:var(--bad-soft);color:var(--bad);border-color:transparent;}
.toast{margin-top:12px;font-size:13px;color:var(--muted);min-height:18px;}
.toast.err{color:var(--bad);}
.toast.ok{color:var(--accent);}
table{width:100%;border-collapse:collapse;font-size:13px;}
th,td{text-align:left;border-bottom:1px solid var(--line);padding:9px 8px;vertical-align:top;}
th{color:var(--muted);font-weight:600;background:var(--panel);position:sticky;top:0;}
.tablewrap{max-height:380px;overflow:auto;border:1px solid var(--line);border-radius:10px;}
.dec{font-weight:700;text-transform:uppercase;font-size:11px;letter-spacing:.4px;}
.dec-keep{color:var(--accent);} .dec-discard{color:var(--warn);} .dec-proposed{color:var(--info);} .dec-blocked{color:var(--bad);}
.bar{display:flex;align-items:center;gap:10px;margin:6px 0;}
.bar .lbl{flex:0 0 200px;font-size:13px;color:var(--ink);}
.bar .track{flex:1;height:10px;border-radius:999px;background:var(--panel);overflow:hidden;border:1px solid var(--line);}
.bar .track>span{display:block;height:100%;background:var(--warn);}
.bar .n{flex:0 0 32px;text-align:right;color:var(--muted);font-size:13px;}
.muted{color:var(--muted);}
.empty{color:var(--muted);border:1px dashed var(--line);border-radius:10px;padding:14px;background:var(--panel);font-size:13px;}
.safety{font-size:12.5px;color:var(--muted);border-left:3px solid var(--accent);padding:4px 0 4px 12px;}
.lastrun{margin-top:14px;font-size:13.5px;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--panel);}
.lastrun:empty{display:none;}
.lastrun.lr-ok{background:var(--accent-soft);border-color:transparent;color:var(--accent);}
.lastrun.lr-err{background:var(--bad-soft);border-color:transparent;color:var(--bad);}
.lastrun.lr-warn{background:var(--warn-soft);border-color:transparent;color:var(--warn);}
.lastrun b{color:inherit;}
.nextstep{border-left:4px solid var(--accent);}
.nextstep-body{font-size:15px;line-height:1.55;}
.nextstep-body b{color:var(--ink);}
.nextstep-body .big{font-size:16px;font-weight:700;display:block;margin-bottom:6px;}
.how{margin:0;padding-left:20px;color:var(--muted);font-size:13.5px;line-height:1.7;}
.how b{color:var(--ink);}
.wl-item{display:flex;gap:10px;align-items:baseline;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--panel);margin-bottom:8px;flex-wrap:wrap;}
.wl-item .sec{font-weight:700;color:var(--warn);}
.wl-item .sk{font-weight:600;}
.wl-item .p{color:var(--muted);font-size:12px;word-break:break-all;width:100%;}
code{background:var(--panel);padding:1px 6px;border-radius:5px;font-size:12.5px;}
@media (max-width:560px){ .bar .lbl{flex-basis:120px;} .topbar h1{font-size:16px;} }
/* --- A1 proposal cards (the One-Click centerpiece) --- */
.prop-toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:6px;}
.prop-toolbar .spacer{flex:1 1 auto;}
.prop{border:1px solid var(--line);border-radius:11px;background:var(--panel);padding:14px 15px;margin-bottom:12px;}
.prop.is-applied{opacity:.72;border-style:dashed;}
.prop.is-skipped{opacity:.55;border-style:dashed;}
.prop-head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;}
.prop-title{font-weight:700;font-size:15px;}
.mode-badge{font-size:11px;font-weight:700;padding:2px 9px;border-radius:999px;text-transform:uppercase;letter-spacing:.3px;}
.mode-skill{background:var(--accent-soft);color:var(--accent);}
.mode-code{background:var(--bad-soft);color:var(--bad);}
.prop-why{color:var(--muted);font-size:13.5px;line-height:1.55;margin:8px 0 10px;}
.prop-why b{color:var(--ink);}
.diff{margin:0;border:1px solid var(--line);border-radius:9px;background:var(--card);overflow:hidden;}
.diff>summary{cursor:pointer;list-style:none;padding:8px 12px;font-size:12.5px;color:var(--muted);user-select:none;}
.diff>summary::-webkit-details-marker{display:none;}
.diff>summary::before{content:"▸ ";}
.diff[open]>summary::before{content:"▾ ";}
.diff pre{margin:0;max-height:300px;overflow:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;line-height:1.5;border-top:1px solid var(--line);}
.diff .dl{display:block;padding:0 12px;white-space:pre-wrap;word-break:break-word;}
.diff .add{background:var(--accent-soft);color:var(--accent);}
.diff .del{background:var(--bad-soft);color:var(--bad);}
.diff .hdr{color:var(--info);}
.diff .ctx{color:var(--muted);}
.prop-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:11px;align-items:center;}
.prop-result{font-size:13px;margin-top:9px;padding:7px 10px;border-radius:8px;}
.prop-result.ok{background:var(--accent-soft);color:var(--accent);}
.prop-result.err{background:var(--warn-soft);color:var(--warn);}
.done-head{margin:16px 0 8px;font-size:13px;color:var(--muted);font-weight:600;}
</style>
</head>
<body data-autoresearch="live-v2">
<div class="topbar">
  <h1>Hermes Autoresearch</h1>
  <span id="pill" class="pill pill-idle">loading</span>
  <span class="spacer"></span>
  <span class="updated" id="updated">…</span>
  <button class="iconbtn" id="refresh" title="Refresh now">↻</button>
</div>
<main>
  <section class="card nextstep" id="nextstepCard">
    <h2>👉 Dein nächster Schritt</h2>
    <div id="nextstep" class="nextstep-body">…</div>
  </section>

  <section class="card" id="proposalsCard">
    <h2>💡 Verbesserungs-Vorschläge <span class="badge badge-info" id="propOpenCount"></span></h2>
    <div class="prop-toolbar">
      <button class="btn btn-primary" id="btnGenerate">✨ Verbesserungen holen</button>
      <button class="btn" id="btnGenerateCode" title="Allowlisted Hermes-Code mit MiniMax prüfen">Code-Schwächen suchen</button>
      <span class="spacer"></span>
      <button class="btn btn-apply" id="btnApplyAll" title="Alle offenen Skill-Vorschläge übernehmen">✓ Alle übernehmen</button>
    </div>
    <p class="muted" style="margin:2px 0 12px;">Jeder Vorschlag zeigt im Klartext <b>was</b> und <b>warum</b> — mit echtem Vorher/Nachher-Diff. „Übernehmen“ schreibt genau das live (Backup + Auto-Revert, wenn’s nichts verbessert).</p>
    <div id="proposalsOpen"><p class="muted">loading…</p></div>
    <div id="proposalsDone"></div>
  </section>

  <section class="card">
    <h2>Was Autoresearch macht</h2>
    <ol class="how">
      <li><b>Dry-run</b> — sucht Skills, denen ein empfohlener Abschnitt fehlt (When to Use, Safety, Procedure, Output). Ändert nichts, schlägt nur vor.</li>
      <li><b>Apply</b> — fügt das fehlende Abschnitts-<i>Gerüst</i> ein (Überschrift + <code>TODO</code>-Platzhalter). Mit Backup; wird automatisch zurückgerollt, wenn es nichts verbessert.</li>
      <li><b>Du füllst den Platzhalter</b> mit echtem Inhalt — die Liste „Braucht deine Eingabe" unten zeigt genau wo.</li>
    </ol>
  </section>

  <section class="card">
    <h2>Loop status</h2>
    <div class="grid cols">
      <div class="kv"><span class="k">Request</span><span class="v" id="req">—</span></div>
      <div class="kv"><span class="k">Step</span><span class="v" id="step">—</span></div>
      <div class="kv"><span class="k">Last eval</span><span class="v" id="evalv">—</span></div>
      <div class="kv"><span class="k">Model route</span><span class="v" id="route">—</span></div>
      <div class="kv"><span class="k">Heartbeat</span><span class="v" id="age">—</span></div>
    </div>
    <div style="margin-top:14px;">
      <div class="bar"><span class="lbl">Iteration <b id="iterlbl">—</b></span><div class="track"><span id="iterbar"></span></div></div>
    </div>
    <div id="lastrun" class="lastrun"></div>
  </section>

  <section class="card">
    <h2>Run a campaign</h2>
    <div class="row">
      <label class="field">Area<select id="area"></select></label>
      <label class="field">Focus<input id="focus" value="recommended_sections"></label>
      <label class="field" style="min-width:90px;">Iterations<input id="iters" type="number" min="1" max="5" value="2"></label>
      <button class="btn btn-primary" id="btnDry">▶ Dry-run</button>
      <button class="btn btn-apply" id="btnApply">✓ Apply…</button>
      <button class="btn btn-stop" id="btnStop">■ Stop</button>
    </div>
    <p class="toast" id="toast">Dry-run schlägt nur vor (ändert nichts). Apply editiert nur unter <code>~/.hermes/skills</code>, mit Backup und Auto-Revert.</p>
    <p class="safety">Single-operator — kein Token. Reversibel: Backup + eval-Gate + Cap + Stop. Keine Secrets/Config/Routing/Push.</p>
  </section>

  <section class="card">
    <h2>📝 Braucht deine Eingabe <span class="badge badge-info" id="worklistCount"></span></h2>
    <p class="muted" style="margin-top:-6px;">Abschnitte, die Apply als Gerüst eingefügt hat — hier fehlt noch dein Text.</p>
    <div id="worklist"><p class="muted">loading…</p></div>
  </section>

  <section class="card">
    <h2>Audit summary</h2>
    <div class="grid cols" id="metrics"></div>
  </section>

  <section class="card">
    <h2>Where skills are weak <span class="badge badge-info" id="weakTotal"></span></h2>
    <div id="weakness"><p class="muted">loading…</p></div>
  </section>

  <section class="card">
    <h2>Aktivität (was wurde gemacht)</h2>
    <div class="tablewrap"><div id="results"><p class="muted">loading…</p></div></div>
    <p class="muted" id="receipt" style="margin-top:10px;"></p>
  </section>
</main>
<script>
const BASE=(window.__HERMES_BASE_PATH__||"");
const AREAS=['all','devops','github','software-development','research','productivity','mlops','creative','firecrawl','hermes-kanban'];
const $=id=>document.getElementById(id);
const esc=s=>{const d=document.createElement('div');d.textContent=(s==null?'':String(s));return d.innerHTML;};
$('area').innerHTML=AREAS.map(a=>'<option>'+a+'</option>').join('');
let running=false;
let gStatus={}, gWorklist={count:0,open_scaffolds:[]};

function setPill(state){const p=$('pill');p.className='pill pill-'+(state||'idle');p.textContent=state||'idle';}
function routeBadge(s){s=s||'unknown';const c=s==='yellow'?'badge-yellow':(s==='unavailable'?'badge-bad':'badge');return '<span class="badge '+c+'">'+esc(s)+'</span>';}
function setControls(){$('btnDry').disabled=running;$('btnApply').disabled=running;$('btnStop').disabled=!running;}
function fmtTime(iso){try{return new Date(iso).toLocaleTimeString();}catch(e){return iso;}}
let lastSeenFinish=null, wasRunning=false;
function renderLastRun(lr,note){
  const box=$('lastrun');
  if(!lr){box.innerHTML=note?('<span class="lr-note">'+esc(note)+'</span>'):'';return;}
  let cls='lr-ok',label;
  if(lr.ok===false){cls='lr-err';label='⚠ refused — '+esc(lr.refused||note||'see logs');}
  else if(lr.stopped){cls='lr-warn';label='■ stopped after '+esc(lr.iterations)+' step(s)';}
  else if(lr.mode==='apply'){label='✓ apply done — '+esc(lr.kept)+' kept, '+esc(lr.reverted)+' reverted';}
  else {label='▶ dry-run done — '+esc(lr.proposed)+' proposed';}
  let tgts=(lr.targets||[]).length?(' · '+lr.targets.map(esc).join(', ')):'';
  box.className='lastrun '+cls;
  box.innerHTML='<b>Last run</b> ('+esc(lr.mode)+(lr.finished_at?(' · '+fmtTime(lr.finished_at)):'')+'): '+label+tgts;
}
async function poll(){
  try{
    const d=await(await fetch(BASE+'/autoresearch/status',{headers:{'Accept':'application/json'}})).json();
    setPill(d.state); running=(d.state==='running'||d.state==='stopping'); setControls();
    $('req').textContent=d.request_id||'—';
    $('step').textContent=d.last_step||'—';
    $('evalv').textContent=d.last_eval||'—';
    $('route').innerHTML=routeBadge(d.route_status);
    $('age').textContent=(d.heartbeat_age_s!=null)?(d.heartbeat_age_s+'s'+(d.heartbeat_fresh?'':' · stale')):'—';
    const it=d.iteration,mx=d.max;
    $('iterlbl').textContent=(it!=null&&mx!=null)?(it+' / '+mx):'—';
    $('iterbar').style.width=(it!=null&&mx)?Math.min(100,(it/mx)*100)+'%':'0';
    $('updated').textContent='updated '+new Date().toLocaleTimeString();
    if(d.last_receipt)$('receipt').textContent='Last receipt: '+d.last_receipt;
    renderLastRun(d.last_run,d.note);
    gStatus=d; renderNextStep();
    // surface completion of a run (incl. fast/refused ones) as a toast, once
    const lr=d.last_run;
    if(lr&&lr.finished_at&&lr.finished_at!==lastSeenFinish&&(wasRunning||!lastSeenFinish)){
      lastSeenFinish=lr.finished_at;
      if(lr.ok===false)toast('⚠ run refused: '+(lr.refused||d.note||''),'err');
      else if(lr.mode==='apply')toast('✓ apply finished — '+lr.kept+' kept, '+lr.reverted+' reverted','ok');
      else toast('▶ dry-run finished — '+lr.proposed+' proposal(s)','ok');
      loadAudit(); loadWorklist();
    }
    wasRunning=running;
  }catch(e){$('updated').textContent='status failed';}
}
async function loadAudit(){
  try{
    const d=await(await fetch(BASE+'/autoresearch/audit',{headers:{'Accept':'application/json'}})).json();
    const inv=d.inventory||{},pc=inv.priority_counts||{},dc=d.decision_counts||{};
    const cards=[['Iterations logged',d.results_count],['Kept',dc.keep||0],['Reverted',dc.discard||0],['Blocked',dc.blocked||0]];
    if(pc.high!=null){cards.push(['High priority',pc.high]);cards.push(['Medium',pc.medium||0]);cards.push(['Low',pc.low||0]);}
    if(inv.inventory_summary&&inv.inventory_summary['Total SKILL.md files inventoried'])cards.unshift(['Skills inventoried',inv.inventory_summary['Total SKILL.md files inventoried']]);
    $('metrics').innerHTML=cards.map(c=>'<div class="kv"><span class="k">'+esc(c[0])+'</span><span class="v">'+esc(c[1])+'</span></div>').join('');
    const wc=inv.weakness_counts||{};const ents=Object.entries(wc).sort((a,b)=>b[1]-a[1]);
    const tot=ents.reduce((s,e)=>s+(e[1]||0),0);$('weakTotal').textContent=tot?tot+' gaps':'';
    const mx=Math.max(1,...ents.map(e=>e[1]||0));
    $('weakness').innerHTML=ents.length?ents.map(e=>'<div class="bar"><span class="lbl">'+esc(e[0].replace(/_/g,' '))+'</span><div class="track"><span style="width:'+((e[1]||0)/mx*100)+'%"></span></div><span class="n">'+esc(e[1])+'</span></div>').join(''):'<div class="empty">No weakness data yet — run a dry-run.</div>';
    const rows=(d.results||[]).slice(-25).reverse();
    if(!rows.length){$('results').innerHTML='<div class="empty">Noch nichts gelaufen. Klick Dry-run, um Vorschläge zu erzeugen.</div>';return;}
    let h='<table><thead><tr><th>Wann</th><th>Was passierte</th><th>Skill / Abschnitt</th><th>Ergebnis</th></tr></thead><tbody>';
    for(const r of rows){
      const k=(r.decision||'').toLowerCase();
      let what;
      if(k==='keep'&&r.mode==='apply')what='<span class="dec dec-keep">✓ Abschnitt eingefügt</span>';
      else if(k==='discard')what='<span class="dec dec-discard">↩ verworfen (keine Verbesserung)</span>';
      else if(k==='proposed')what='<span class="dec dec-proposed">💡 vorgeschlagen (nichts geändert)</span>';
      else if(k==='blocked')what='<span class="dec dec-blocked">⚠ blockiert</span>';
      else what='<span class="dec">'+esc(r.decision)+'</span>';
      const m=(r.eval_result||'').match(/warnings\\s+(\\d+)\\s*->\\s*(\\d+)/);
      let res=m?((Number(m[1])-Number(m[2]))+' Lücke(n) geschlossen'):esc(r.eval_result||'');
      h+='<tr><td>'+esc(fmtTime(r.timestamp))+'</td><td>'+what+'</td><td>'+esc(r.target||'')+'</td><td>'+res+'</td></tr>';
    }
    $('results').innerHTML=h+'</tbody></table>';
  }catch(e){$('results').innerHTML='<div class="empty">audit fetch failed</div>';}
}
async function loadWorklist(){
  try{
    gWorklist=await(await fetch(BASE+'/autoresearch/worklist',{headers:{'Accept':'application/json'}})).json();
  }catch(e){gWorklist={count:0,open_scaffolds:[]};}
  const items=(gWorklist.open_scaffolds||[]);
  $('worklistCount').textContent=items.length?(items.length+' offen'):'';
  if(!items.length){$('worklist').innerHTML='<div class="empty">Nichts offen — keine Platzhalter-Abschnitte warten auf Text.</div>';}
  else{
    $('worklist').innerHTML=items.map(it=>'<div class="wl-item"><span class="sk">'+esc(it.skill)+'</span>·<span class="sec">'+esc(it.section)+'</span> braucht Inhalt<span class="p">'+esc(it.path)+'</span></div>').join('');
  }
  renderNextStep();
}
function renderNextStep(){
  const box=$('nextstep');
  const open=(gWorklist.open_scaffolds||[]).length;
  const lr=gStatus.last_run;
  if(running){box.innerHTML='<span class="big">⏳ Ein Lauf läuft gerade…</span>Warte, bis er fertig ist — Status oben aktualisiert sich automatisch.';return;}
  if(lr&&lr.ok===false){box.innerHTML='<span class="big">⚠ Letzter Lauf abgelehnt</span>Grund: '+esc(lr.refused||gStatus.note||'')+'. Wähle eine Skills-Area (z. B. <b>all</b>) und versuch es erneut.';return;}
  if(open>0){
    const names=[...new Set((gWorklist.open_scaffolds||[]).map(i=>i.skill))].slice(0,6).join(', ');
    box.innerHTML='<span class="big">📝 '+open+' Abschnitt(e) brauchen deinen Text</span>'+
      'Apply hat Gerüste eingefügt — jetzt fehlt der Inhalt. Öffne die Dateien in <b>„Braucht deine Eingabe"</b> unten und ersetze die <code>TODO</code>-Zeilen mit echtem Inhalt (betroffen: '+esc(names)+'). '+
      'Wenn du keinen Inhalt willst: sag mir Bescheid, ich rolle die Gerüste zurück.';
    return;
  }
  const dc=(gStatus.last_run&&gStatus.last_run.mode==='dry-run');
  if(dc){box.innerHTML='<span class="big">▶ Vorschläge liegen vor</span>Schau unten in <b>Aktivität</b> die 💡-Zeilen an. Wenn sie passen: gleiche Area wählen und <b>Apply</b> klicken, um die Struktur einzufügen.';return;}
  box.innerHTML='<span class="big">✅ Alles aufgeräumt</span>Keine offenen Platzhalter. Starte einen <b>Dry-run</b> (z. B. Area <b>all</b>), um neue Verbesserungs-Kandidaten zu finden.';
}
function toast(msg,kind){const t=$('toast');t.className='toast'+(kind?' '+kind:'');t.textContent=msg;}
async function trigger(mode){
  const body={area:$('area').value,focus:$('focus').value||'recommended_sections',mode:mode,confirm:false,max_iterations:Number($('iters').value||2)};
  if(mode==='apply'){if(!confirm('Apply will edit SKILL.md files under ~/.hermes/skills (backup + auto-revert on regression). Proceed?'))return;body.confirm=true;}
  toast('starting '+mode+'…');
  try{
    const r=await fetch(BASE+'/autoresearch/trigger',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(r.ok)toast(mode+' started — '+d.request_id+' (pid '+d.pid+')','ok');
    else toast('error '+r.status+': '+(d.detail||''),'err');
  }catch(e){toast('request failed: '+e,'err');}
  poll();setTimeout(()=>{poll();loadAudit();loadWorklist();},1500);
}
async function stop(){
  toast('stopping…');
  try{const d=await(await fetch(BASE+'/autoresearch/stop',{method:'POST'})).json();toast(d.detail||'stopped',d.ok?'ok':'err');}
  catch(e){toast('stop failed: '+e,'err');}
  poll();
}
// ---- A1 proposals (One-Click flow) ----
let gProposals={proposals:[],open_count:0};
function renderDiff(diff){
  if(!diff)return '<p class="muted" style="padding:6px 12px;">Kein Diff.</p>';
  const lines=String(diff).split('\\n').map(l=>{
    let cls='ctx';
    if(l.startsWith('+++')||l.startsWith('---'))cls='hdr';
    else if(l.startsWith('@@'))cls='hdr';
    else if(l.startsWith('+'))cls='add';
    else if(l.startsWith('-'))cls='del';
    return '<span class="dl '+cls+'">'+esc(l||' ')+'</span>';
  }).join('');
  return '<details class="diff"><summary>Vorher / Nachher anzeigen</summary><pre>'+lines+'</pre></details>';
}
function propCard(p){
  const st=p.status||'proposed';
  const modeCls=p.mode==='code'?'mode-code':'mode-skill';
  const modeLbl=p.mode==='code'?'Code · riskanter':'Skill';
  let actions='';
  if(st==='proposed'){
    actions='<button class="btn btn-apply" data-apply="'+esc(p.id)+'">✓ Übernehmen</button>'+
            '<button class="btn" data-skip="'+esc(p.id)+'">Überspringen</button>';
  }
  let result=p.result?('<div class="prop-result '+(st==='applied'?'ok':(st==='proposed'?'err':''))+'">'+esc(p.result)+'</div>'):'';
  return '<div class="prop is-'+esc(st)+'" id="prop-'+esc(p.id)+'">'+
    '<div class="prop-head"><span class="prop-title">'+esc(p.title||p.id)+'</span>'+
      '<span class="mode-badge '+modeCls+'">'+esc(modeLbl)+'</span></div>'+
    '<div class="prop-why"><b>Warum:</b> '+esc(p.rationale_plain||'')+'</div>'+
    renderDiff(p.diff_before_after)+
    '<div class="prop-actions">'+actions+'</div>'+result+'</div>';
}
function renderProposals(){
  const all=gProposals.proposals||[];
  const open=all.filter(p=>p.status==='proposed');
  const done=all.filter(p=>p.status!=='proposed');
  $('propOpenCount').textContent=open.length?(open.length+' offen'):'';
  $('btnApplyAll').disabled=!open.some(p=>p.mode!=='code');
  $('proposalsOpen').innerHTML=open.length?open.map(propCard).join(''):
    '<div class="empty">Keine offenen Vorschläge. Klick „Verbesserungen holen“, um welche zu erzeugen.</div>';
  $('proposalsDone').innerHTML=done.length?('<div class="done-head">Erledigt</div>'+done.map(propCard).join('')):'';
  $('proposalsOpen').querySelectorAll('[data-apply]').forEach(b=>b.addEventListener('click',()=>applyProposal(b.getAttribute('data-apply'))));
  $('proposalsOpen').querySelectorAll('[data-skip]').forEach(b=>b.addEventListener('click',()=>skipProposal(b.getAttribute('data-skip'))));
}
async function loadProposals(){
  try{gProposals=await(await fetch(BASE+'/autoresearch/proposals',{headers:{'Accept':'application/json'}})).json();}
  catch(e){gProposals={proposals:[],open_count:0};}
  renderProposals();
}
async function applyProposal(id){
  toast('übernehme '+id+'…');
  try{
    const r=await fetch(BASE+'/autoresearch/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,confirm:true})});
    const d=await r.json();
    if(d.ok)toast('✓ übernommen: '+id,'ok');
    else toast('nicht übernommen: '+(d.detail||d.result||r.status),'err');
  }catch(e){toast('apply fehlgeschlagen: '+e,'err');}
  loadProposals();loadAudit();loadWorklist();
}
async function skipProposal(id){
  try{await fetch(BASE+'/autoresearch/skip',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});toast('übersprungen: '+id,'ok');}
  catch(e){toast('skip fehlgeschlagen: '+e,'err');}
  loadProposals();
}
async function generateProposals(){
  toast('suche Verbesserungen…');
  try{
    const d=await(await fetch(BASE+'/autoresearch/generate',{method:'POST'})).json();
    toast(d.created_count?('✨ '+d.created_count+' neue(r) Vorschlag/Vorschläge'):'keine neuen Kandidaten gefunden',d.created_count?'ok':null);
  }catch(e){toast('generate fehlgeschlagen: '+e,'err');}
  loadProposals();
}
async function generateCodeWeaknessProposals(){
  toast('suche Code-Schwächen…');
  try{
    const d=await(await fetch(BASE+'/autoresearch/generate-code-weaknesses',{method:'POST'})).json();
    toast(d.created_count?('Code: '+d.created_count+' neue(r) Vorschlag/Vorschläge'):'keine neuen Code-Funde',d.created_count?'ok':null);
  }catch(e){toast('code-finder fehlgeschlagen: '+e,'err');}
  loadProposals();
}
async function applyAll(){
  const open=(gProposals.proposals||[]).filter(p=>p.status==='proposed'&&p.mode!=='code');
  if(!open.length){toast('nichts offen zum Übernehmen');return;}
  if(!confirm('Alle '+open.length+' offenen Skill-Vorschläge übernehmen? (Backup + Auto-Revert pro Stück)'))return;
  toast('prüfe Batch…');
  try{
    const d=await(await fetch(BASE+'/autoresearch/confirm-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:open.map(p=>p.id),confirm:true})})).json();
    const applied=(d.results||[]).filter(r=>r.status==='applied').length;
    const skipped=(d.results||[]).filter(r=>r.status==='skipped').length;
    toast('Batch: '+applied+' übernommen, '+skipped+' übersprungen',applied?'ok':null);
  }catch(e){toast('Batch fehlgeschlagen: '+e,'err');}
  loadProposals();loadAudit();loadWorklist();
}
$('btnGenerate').addEventListener('click',generateProposals);
$('btnGenerateCode').addEventListener('click',generateCodeWeaknessProposals);
$('btnApplyAll').addEventListener('click',applyAll);
$('btnDry').addEventListener('click',()=>trigger('dry-run'));
$('btnApply').addEventListener('click',()=>trigger('apply'));
$('btnStop').addEventListener('click',stop);
$('refresh').addEventListener('click',()=>{poll();loadAudit();loadWorklist();loadProposals();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){poll();loadAudit();loadWorklist();loadProposals();}});
let pollTimer=setInterval(()=>{if(!document.hidden)poll();},4000);
let auditTimer=setInterval(()=>{if(!document.hidden){loadAudit();loadWorklist();loadProposals();}},12000);
poll();loadAudit();loadWorklist();loadProposals();setControls();
</script>
</body>
</html>
"""


def render_autoresearch_html() -> str:
    return _HTML_PAGE


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------
def register_autoresearch_routes(app: Any) -> None:
    """Register the read-only /autoresearch view + token-gated POST stubs.

    Must be called before the SPA catch-all (``/{full_path:path}``) is mounted
    so these explicit paths take precedence.
    """

    @app.get("/autoresearch", include_in_schema=False)
    @app.get("/autoresearch/", include_in_schema=False)
    async def autoresearch_view() -> HTMLResponse:
        return HTMLResponse(
            render_autoresearch_html(),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/autoresearch/status")
    async def autoresearch_status() -> dict[str, Any]:
        return read_runner_status()

    @app.get("/autoresearch/audit")
    async def autoresearch_audit() -> dict[str, Any]:
        return read_audit()

    @app.get("/autoresearch/selftest")
    async def autoresearch_selftest() -> dict[str, Any]:
        return self_test()

    @app.get("/autoresearch/worklist")
    async def autoresearch_worklist() -> dict[str, Any]:
        return scan_open_scaffolds()

    @app.post("/autoresearch/trigger")
    async def autoresearch_trigger(body: TriggerBody) -> dict[str, Any]:
        return start_runner(
            area=body.area, focus=body.focus, mode=body.mode,
            confirm=body.confirm, max_iterations=body.max_iterations,
        )

    @app.post("/autoresearch/stop")
    async def autoresearch_stop() -> dict[str, Any]:
        return stop_runner()

    # --- Sprint A1: One-Click proposals (persistent store + apply-by-id) ---
    @app.get("/autoresearch/proposals")
    async def autoresearch_proposals() -> dict[str, Any]:
        return _proposals.proposals_payload()

    @app.post("/autoresearch/generate")
    async def autoresearch_generate() -> dict[str, Any]:
        """Deterministic (A1): discover skill-improvement candidates and persist
        them as previewable proposals. No mutation."""
        return _proposals.generate_proposals()

    @app.post("/autoresearch/generate-code-weaknesses")
    async def autoresearch_generate_code_weaknesses() -> dict[str, Any]:
        """MiniMax-backed, allowlisted code weakness finder. It only persists
        mode=code proposals; applying them uses the existing code gate."""
        return _proposals.generate_code_weakness_proposals()

    @app.post("/autoresearch/apply")
    async def autoresearch_apply_proposal(body: ApplyProposalBody) -> dict[str, Any]:
        """Apply exactly one stored proposal: backup → write → eval-gate →
        keep/auto-revert. Code-mode goes through the detached test-suite gate."""
        return _proposals.apply_proposal(body.id, confirm=body.confirm)

    @app.post("/autoresearch/confirm-batch")
    async def autoresearch_confirm_batch(request: Request) -> dict[str, Any]:
        """Judge each skill proposal, then delegate accepted ids to apply_proposal."""
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        if isinstance(payload, list):
            try:
                body = ConfirmBatchBody(ids=payload)
            except ValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        elif isinstance(payload, dict):
            try:
                body = ConfirmBatchBody(**payload)
            except ValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        else:
            raise HTTPException(status_code=400, detail="body must be an id list or {ids:[...]}")
        return confirm_batch_proposals(body.ids, confirm=body.confirm)

    @app.post("/autoresearch/skip")
    async def autoresearch_skip_proposal(body: SkipProposalBody) -> dict[str, Any]:
        return _proposals.skip_proposal(body.id)
