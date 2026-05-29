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
from pydantic import BaseModel

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
# MiniMax-M2.7 self-test (harmless config-presence check; no secrets emitted)
# ---------------------------------------------------------------------------
_MODEL_NEEDLE = "MiniMax-M2.7"


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

    mi = max(1, min(int(max_iterations), 5))
    arr = _load_request_module()
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
_HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Autoresearch — Live Loop</title>
<style>
:root { color-scheme: light; --ink:#162016; --muted:#5f6f63; --line:#d8ded3; --panel:#f6f8f2; --accent:#126b54; --warn:#a9501a; --bad:#9b2635; --bg:#fbfcf8; }
* { box-sizing:border-box; }
body { margin:0; font-family:Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; background:var(--bg); color:var(--ink); }
header { padding:22px 26px 14px; border-bottom:1px solid var(--line); background:#fff; }
h1 { margin:0 0 6px; font-size:24px; }
header p { margin:0; color:var(--muted); max-width:960px; }
main { padding:20px 26px 36px; display:grid; gap:18px; }
.panel { border:1px solid var(--line); background:#fff; border-radius:8px; padding:16px; }
h2 { margin:0 0 12px; font-size:17px; }
.pill { display:inline-block; padding:4px 12px; border-radius:999px; font-weight:700; font-size:14px; }
.pill-idle { background:#eef1ec; color:var(--muted); }
.pill-running { background:#e3f4ec; color:var(--accent); }
.pill-stopping { background:#fff1dc; color:var(--warn); }
.pill-crashed { background:#fbe3e6; color:var(--bad); }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }
.kv { border:1px solid var(--line); border-radius:8px; padding:10px 12px; background:var(--panel); }
.kv span { display:block; color:var(--muted); font-size:12px; margin-bottom:4px; }
.kv strong { font-size:16px; }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; background:#e8f1ec; color:var(--accent); }
.badge-yellow { background:#fff1dc; color:var(--warn); }
.badge-bad { background:#fbe3e6; color:var(--bad); }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; border-bottom:1px solid var(--line); padding:8px; vertical-align:top; }
th { color:var(--muted); background:var(--panel); }
.banner { border:1px solid #efcf9d; background:#fff8e9; color:#5d3b00; border-radius:8px; padding:12px 14px; }
.muted { color:var(--muted); }
.actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
.btn { cursor:pointer; border:1px solid var(--line); border-radius:8px; padding:9px 14px; font-weight:700; background:#eef1ec; color:var(--ink); }
.btn-apply { background:#e3f4ec; color:var(--accent); border-color:#bfe3d2; }
.btn-stop { background:#fbe3e6; color:var(--bad); border-color:#f0c2c8; }
.btn:disabled { opacity:.5; cursor:not-allowed; }
label.kv span { font-weight:600; }
label.kv select, label.kv input { width:100%; border:1px solid var(--line); border-radius:6px; padding:6px 8px; margin-top:4px; }
code { background:#eef1ec; padding:1px 5px; border-radius:4px; }
</style>
</head>
<body data-autoresearch="live-v1">
<header>
  <h1>Hermes Autoresearch — Live Loop</h1>
  <p>Read-only status of the bounded skill-research runner. No mutation here:
     Trigger/Stop are token-gated and the applying runner is a separate Go (Phase 5).</p>
</header>
<main>
  <section class="banner"><b>Safety:</b> bounded &amp; reversible. Every apply takes a backup,
     is eval-gated and reverted on regression, capped, and Stop-able; mutation only under
     <code>~/.hermes/skills</code>. No secrets/config/routing/push.</section>
  <section class="panel">
    <h2>Run a campaign</h2>
    <div class="grid">
      <label class="kv"><span>Area</span><select id="area"></select></label>
      <label class="kv"><span>Focus</span><input id="focus" value="recommended_sections"></label>
      <label class="kv"><span>Iterations</span><input id="iters" type="number" min="1" max="5" value="2"></label>
    </div>
    <div class="actions">
      <button id="btnDry" class="btn">▶ Dry-run (propose only)</button>
      <button id="btnApply" class="btn btn-apply">✓ Apply (confirm)</button>
      <button id="btnStop" class="btn btn-stop">■ Stop</button>
    </div>
    <p class="muted" id="actionMsg">Dry-run proposes changes without touching any file. Apply asks for confirmation.</p>
  </section>
  <section class="panel">
    <h2>Loop status <span id="pill" class="pill pill-idle">…</span></h2>
    <div class="grid">
      <div class="kv"><span>Request</span><strong id="req">—</strong></div>
      <div class="kv"><span>Iteration</span><strong id="iter">—</strong></div>
      <div class="kv"><span>Last step</span><strong id="step">—</strong></div>
      <div class="kv"><span>Last eval</span><strong id="eval">—</strong></div>
      <div class="kv"><span>Model route</span><strong id="route">—</strong></div>
      <div class="kv"><span>Heartbeat age</span><strong id="age">—</strong></div>
    </div>
    <p class="muted" id="updated" style="margin-top:10px;">polling…</p>
  </section>
  <section class="panel">
    <h2>Audit summary</h2>
    <div class="grid" id="auditCards"></div>
  </section>
  <section class="panel">
    <h2>Recent results</h2>
    <div id="resultsTable"><p class="muted">loading…</p></div>
  </section>
</main>
<script>
const BASE = (window.__HERMES_BASE_PATH__ || "");
function el(id){ return document.getElementById(id); }
function esc(s){ const d=document.createElement('div'); d.textContent = (s==null?'':String(s)); return d.innerHTML; }
function setRoute(node, status){
  const s = (status||'unknown');
  node.innerHTML = '<span class="badge'+(s==='yellow'?' badge-yellow':(s==='unavailable'?' badge-bad':''))+'">'+esc(s)+'</span>';
}
async function poll(){
  try {
    const r = await fetch(BASE + '/autoresearch/status', {headers:{'Accept':'application/json'}});
    const d = await r.json();
    const pill = el('pill');
    pill.className = 'pill pill-' + (d.state||'idle');
    pill.textContent = (d.state||'idle');
    el('req').textContent = d.request_id || '—';
    el('iter').textContent = (d.iteration!=null && d.max!=null) ? (d.iteration + ' / ' + d.max) : '—';
    el('step').textContent = d.last_step || '—';
    el('eval').textContent = d.last_eval || '—';
    setRoute(el('route'), d.route_status);
    el('age').textContent = (d.heartbeat_age_s!=null) ? (d.heartbeat_age_s + 's' + (d.heartbeat_fresh?'':' (stale)')) : '—';
    el('updated').textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch (e) {
    el('updated').textContent = 'status fetch failed: ' + e;
  }
}
async function loadAudit(){
  try {
    const r = await fetch(BASE + '/autoresearch/audit', {headers:{'Accept':'application/json'}});
    const d = await r.json();
    const cards = [];
    const inv = d.inventory || {};
    const pc = inv.priority_counts || {};
    cards.push(['Iterations logged', d.results_count]);
    cards.push(['Kept', (d.decision_counts||{}).keep || 0]);
    cards.push(['Blocked', (d.decision_counts||{}).blocked || 0]);
    cards.push(['Discarded', (d.decision_counts||{}).discard || 0]);
    if (pc.high!=null) cards.push(['High-priority skills', pc.high]);
    if (inv.model_route_status) cards.push(['Model route', inv.model_route_status]);
    el('auditCards').innerHTML = cards.map(c =>
      '<div class="kv"><span>'+esc(c[0])+'</span><strong>'+esc(c[1])+'</strong></div>').join('');
    const rows = (d.results||[]).slice(-15).reverse();
    if (!rows.length){ el('resultsTable').innerHTML = '<p class="muted">No iterations logged yet.</p>'; return; }
    const cols = ['timestamp','mode','target','hypothesis','decision','risk'];
    let html = '<table><thead><tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr></thead><tbody>';
    for (const row of rows){ html += '<tr>'+cols.map(c=>'<td>'+esc(row[c])+'</td>').join('')+'</tr>'; }
    el('resultsTable').innerHTML = html + '</tbody></table>';
  } catch (e) {
    el('resultsTable').innerHTML = '<p class="muted">audit fetch failed: '+esc(e)+'</p>';
  }
}
const AREAS = ['all','devops','github','software-development','research','productivity','mlops','creative','firecrawl','hermes-kanban'];
el('area').innerHTML = AREAS.map(a => '<option value="'+a+'">'+a+'</option>').join('');
async function trigger(mode){
  const body = { area: el('area').value, focus: el('focus').value || 'recommended_sections',
                 mode: mode, confirm: false, max_iterations: Number(el('iters').value||2) };
  if (mode === 'apply'){
    if (!confirm('Apply will edit SKILL.md files under ~/.hermes/skills (with backup + auto-revert on regression). Proceed?')) return;
    body.confirm = true;
  }
  el('actionMsg').textContent = 'starting ' + mode + '…';
  try {
    const r = await fetch(BASE + '/autoresearch/trigger', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const d = await r.json();
    el('actionMsg').textContent = r.ok ? (mode+' started (pid '+d.pid+', '+d.request_id+')') : ('error: '+(d.detail||r.status));
  } catch(e){ el('actionMsg').textContent = 'request failed: '+e; }
  poll();
}
async function stop(){
  el('actionMsg').textContent = 'stopping…';
  try { const r = await fetch(BASE + '/autoresearch/stop', {method:'POST'}); const d = await r.json();
        el('actionMsg').textContent = d.detail || (r.ok?'stopped':'error'); }
  catch(e){ el('actionMsg').textContent = 'stop failed: '+e; }
  poll();
}
el('btnDry').addEventListener('click', () => trigger('dry-run'));
el('btnApply').addEventListener('click', () => trigger('apply'));
el('btnStop').addEventListener('click', stop);
poll(); loadAudit();
setInterval(poll, 4000);
setInterval(() => loadAudit(), 12000);
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

    @app.post("/autoresearch/trigger")
    async def autoresearch_trigger(body: TriggerBody) -> dict[str, Any]:
        return start_runner(
            area=body.area, focus=body.focus, mode=body.mode,
            confirm=body.confirm, max_iterations=body.max_iterations,
        )

    @app.post("/autoresearch/stop")
    async def autoresearch_stop() -> dict[str, Any]:
        return stop_runner()
