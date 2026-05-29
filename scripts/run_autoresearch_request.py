#!/usr/bin/env python3
"""Phase 5: bounded, reversible, autonomous Autoresearch runner.

Executes ONE validated run-request as a small, audited loop. Safety here is
**reversibility, not access control** (this is a single-operator system — see
the project decision to drop the token gate): every apply is preceded by a
backup, eval-gated, and reverted on any regression; the loop is iteration-capped
and SIGTERM-stoppable, and it only ever touches ``~/.hermes/skills``.

Modes::

    run_autoresearch_request.py <request.json>            # dry-run (default): propose only
    run_autoresearch_request.py <request.json> --apply --confirm   # apply, operator-confirmed

``--apply`` preconditions (refuse / downgrade otherwise):
  * request re-validated (schema, allowed/forbidden paths, cap, mutation_policy);
  * every allowed path resolves under ``~/.hermes/skills`` (family / non-skill
    areas are refused for apply);
  * operator confirmation present (``--confirm`` or request ``approved_by_operator``)
    — the single "are you sure" step that replaces the token;
  * MiniMax-M2.7 self-test == configured, else fall back to dry-run (route Yellow);
  * a timestamped backup dir is created before any edit;
  * no fresh lock already held (no double-run).

The improvement step is intentionally **conservative and deterministic**: it
resolves "recommended section missing" gaps that ``eval_local_skills`` already
detects, by appending a clearly-marked scaffold section. Deeper, model-authored
edits are a deliberate future opt-in, not built here.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
REPO = _SCRIPTS.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import autoresearch_request as arr  # noqa: E402  (sibling script)
import eval_local_skills as evals  # noqa: E402  (sibling script)
from hermes_constants import get_hermes_home  # noqa: E402

_DEFAULT_AUDIT = REPO / ".hermes" / "skill-audit"
RESULTS_COLUMNS = [
    "timestamp", "mode", "target", "hypothesis", "change",
    "eval_command", "eval_result", "decision", "risk", "evidence",
]
MODEL_PREFERENCE = "MiniMax-M2.7-highspeed"
MODEL_NEEDLE = "MiniMax-M2.7"
HEARTBEAT_FRESH_S = 30.0

# Maps an eval "recommended section missing" label to a scaffold header whose
# text contains a needle eval_local_skills.SECTION_GROUPS recognises.
_SCAFFOLD = {
    "When to Use / Wann verwenden": "When to Use",
    "Safety / Sicherheit": "Safety",
    "Procedure / Vorgehen": "Procedure",
    "Output / Ergebnis": "Output",
}

# SIGTERM/SIGINT cooperative stop flag.
_STOP = {"requested": False}


def request_stop(*_args) -> None:
    _STOP["requested"] = True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _hermes_home() -> Path:
    override = os.environ.get("HERMES_HOME")
    return Path(override) if override else get_hermes_home()


def _skills_root() -> Path:
    return Path(os.environ.get("HERMES_SKILLS_ROOT", str(_hermes_home() / "skills"))).expanduser()


def _config_yaml() -> Path:
    return _hermes_home() / "config.yaml"


def _audit_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_AUDIT_DIR")
    return Path(override) if override else _DEFAULT_AUDIT


def _state_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_STATE_DIR")
    return Path(override) if override else (_audit_dir() / "runner-state")


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Self-test (harmless config-presence check; no secrets emitted)
# ---------------------------------------------------------------------------
def _call_auxiliary_llm(**kwargs):
    from agent.auxiliary_client import call_llm
    return call_llm(**kwargs)


def self_test() -> tuple[str, str]:
    cfg = _config_yaml()
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "yellow", "config.yaml unreadable; route unverified"
    if MODEL_NEEDLE not in text:
        return "unavailable", f"{MODEL_NEEDLE} not found in config.yaml"
    try:
        resp = _call_auxiliary_llm(
            task="skills_hub",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=8,
            temperature=0,
            timeout=10,
        )
        _ = (resp.choices[0].message.content or "")
        return "configured", f"{MODEL_NEEDLE} reachable via skills_hub aux"
    except Exception as exc:
        return "yellow", f"model ping failed: {type(exc).__name__}"


# ---------------------------------------------------------------------------
# State files (lock / heartbeat / status)
# ---------------------------------------------------------------------------
def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict | None:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _lock_is_fresh(state_dir: Path) -> bool:
    lock = _read_json(state_dir / "current.lock")
    if not lock:
        return False
    hb = _read_json(state_dir / "current.heartbeat") or {}
    ts = hb.get("ts")
    if not isinstance(ts, (int, float)):
        # Lock without a heartbeat ts: treat as fresh only if the PID is alive.
        return _pid_alive(lock.get("pid"))
    return (time.time() - float(ts)) < HEARTBEAT_FRESH_S


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def acquire_lock(state_dir: Path, request_id: str, mode: str) -> bool:
    if _lock_is_fresh(state_dir):
        return False
    _write_json(state_dir / "current.lock", {
        "pid": os.getpid(), "request_id": request_id, "started_at": _utc_now(), "mode": mode,
    })
    return True


def heartbeat(state_dir: Path, request_id: str, iteration: int, max_iter: int,
              last_step: str, last_eval: str | None) -> None:
    _write_json(state_dir / "current.heartbeat", {
        "pid": os.getpid(), "request_id": request_id, "iteration": iteration,
        "max": max_iter, "last_step": last_step, "last_eval": last_eval, "ts": time.time(),
    })


def set_status(state_dir: Path, state: str, route_status: str,
               last_receipt: str | None = None, note: str | None = None,
               last_run: dict | None = None) -> None:
    _write_json(state_dir / "current.status", {
        "state": state, "route_status": route_status, "last_receipt": last_receipt,
        "note": note, "last_run": last_run, "updated_at": _utc_now(),
    })


def _build_last_run(summary: dict) -> dict:
    """Compact, human-facing summary of the most recent run for the dashboard."""
    return {
        "mode": summary.get("mode"),
        "ok": summary.get("ok"),
        "refused": summary.get("refused"),
        "iterations": summary.get("iterations"),
        "kept": summary.get("kept"),
        "reverted": summary.get("reverted"),
        "proposed": summary.get("proposed"),
        "stopped": summary.get("stopped"),
        "targets": [s["target"] for s in summary.get("steps", [])],
        "request_id": summary.get("request_id"),
        "finished_at": _utc_now(),
    }


def _finish_status(state_dir: Path, route_status: str, summary: dict,
                   last_receipt: str | None = None) -> None:
    """Write an idle status that carries the last-run summary (and any refusal/note)."""
    note = summary.get("refused") or ("stopped by signal" if summary.get("stopped") else summary.get("route_note"))
    set_status(state_dir, "idle", route_status, last_receipt=last_receipt,
               note=note, last_run=_build_last_run(summary))


def release_lock(state_dir: Path) -> None:
    for name in ("current.lock", "current.heartbeat"):
        target = state_dir / name
        if target.exists():
            target.unlink()


# ---------------------------------------------------------------------------
# Candidate discovery (reuses eval_local_skills)
# ---------------------------------------------------------------------------
def _missing_label_to_warning(label: str) -> str:
    return f"recommended section missing: {label}"


def discover_candidates(roots: list[Path], attempted: set[tuple[str, str]]) -> list[dict]:
    """List (skill, missing-section) candidates under the given roots.

    Sorted so nearly-complete skills are fixed first ("smallest high-value").
    """
    seen: set[Path] = set()
    cands: list[dict] = []
    for root in roots:
        for path in evals.find_skills(root):
            if path in seen:
                continue
            seen.add(path)
            # never touch archived / hidden skills (e.g. skills/.archive/...).
            # Check parts RELATIVE to the root — the root itself may live under a
            # dotted dir (~/.hermes) which must not disqualify everything.
            try:
                rel_parts = path.relative_to(root).parts
            except ValueError:
                rel_parts = path.parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            _errors, warnings = evals.check_skill(path)
            missing = [lbl for lbl in _SCAFFOLD if _missing_label_to_warning(lbl) in warnings]
            for label in missing:
                key = (str(path), label)
                if key in attempted:
                    continue
                cands.append({
                    "path": path, "skill": path.parent.name, "label": label,
                    "n_missing": len(missing),
                })
    cands.sort(key=lambda c: (c["n_missing"], str(c["path"]), c["label"]))
    return cands


# ---------------------------------------------------------------------------
# Apply / revert (conservative deterministic scaffold)
# ---------------------------------------------------------------------------
def _backup_file(path: Path, skills_root: Path, backup_dir: Path) -> None:
    rel = path.resolve().relative_to(skills_root.resolve())
    dest = backup_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(path, dest)


def _restore_file(path: Path, skills_root: Path, backup_dir: Path) -> None:
    rel = path.resolve().relative_to(skills_root.resolve())
    src = backup_dir / rel
    if src.exists():
        shutil.copy2(src, path)


def build_scaffold_block(skill: str, header: str) -> str:
    """The exact scaffold block apply_scaffold appends. Pure (no I/O) so the
    proposal generator can preview the same text without mutating the file."""
    return (
        f"\n## {header}\n\n"
        f"<!-- autoresearch-scaffold: replace with concrete guidance for `{skill}` -->\n"
        f"TODO: document the **{header}** of `{skill}`.\n"
    )


def apply_scaffold(path: Path, label: str) -> str:
    header = _SCAFFOLD[label]
    skill = path.parent.name
    block = build_scaffold_block(skill, header)
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + block, encoding="utf-8")
    return block


def eval_gate(path: Path, target_warning: str,
              before_warnings: list[str]) -> tuple[bool, str]:
    errors_after, warnings_after = evals.check_skill(path)
    if errors_after:
        return False, f"regression: {len(errors_after)} structural error(s) introduced"
    if target_warning in warnings_after:
        return False, "no improvement: target section still missing"
    if len(warnings_after) >= len(before_warnings):
        return False, "no net improvement in warnings"
    return True, f"warnings {len(before_warnings)} -> {len(warnings_after)}"


# ---------------------------------------------------------------------------
# Results + receipt
# ---------------------------------------------------------------------------
def append_result(row: dict) -> None:
    results_tsv = _audit_dir() / "autoresearch_results.tsv"
    results_tsv.parent.mkdir(parents=True, exist_ok=True)
    new = not results_tsv.exists() or results_tsv.stat().st_size == 0
    with results_tsv.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=RESULTS_COLUMNS, delimiter="\t",
                                extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)


def write_receipt(summary: dict) -> Path:
    receipts_dir = _audit_dir() / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    rid = summary.get("request_id", "unknown")
    path = receipts_dir / f"autoresearch-run-{rid}.md"
    lines = [
        f"# Autoresearch run receipt — {rid}",
        "",
        f"- mode: {summary.get('mode')}",
        f"- finished: {_utc_now()}",
        f"- route_status: {summary.get('route_status')}",
        f"- iterations: {summary.get('iterations')}",
        f"- kept: {summary.get('kept')} | reverted: {summary.get('reverted')} | proposed: {summary.get('proposed')}",
        f"- backup_dir: {summary.get('backup_dir') or '(none — dry-run)'}",
        f"- stopped_by_signal: {summary.get('stopped')}",
        "",
        "## Non-actions",
        "- No secrets/.env/auth.json/config.yaml/*.db touched.",
        "- No global model routing / gateway / systemd / cron / Tailscale change.",
        "- No push/merge/PR/dispatch. Mutation limited to ~/.hermes/skills, eval-gated, reverted on regression.",
        "",
        "## Steps",
    ]
    for step in summary.get("steps", []):
        lines.append(f"- [{step['decision']}] {step['target']} — {step['hypothesis']} ({step['eval_result']})")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------
def run(request_path: Path, *, apply: bool, confirm: bool,
        state_dir: Path | None = None, max_iterations: int | None = None) -> dict:
    state_dir = state_dir or _state_dir()
    skills_root = _skills_root()
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    # Re-validate against the same rules the generator enforces.
    arr.validate_request(request, hermes_home=_hermes_home(), repo_root=REPO)

    request_id = Path(request_path).stem
    cap = int(max_iterations if max_iterations is not None else request.get("max_iterations", 1))
    cap = max(1, min(cap, arr.MAX_ITERATIONS))
    allowed = [Path(p) for p in request.get("allowed_paths", [])]
    route_status, route_detail = self_test()

    summary: dict = {
        "ok": True, "request_id": request_id, "mode": "dry-run",
        "route_status": route_status, "route_detail": route_detail,
        "iterations": 0, "kept": 0, "reverted": 0, "proposed": 0,
        "backup_dir": None, "stopped": False, "steps": [], "refused": None,
    }

    # Apply may only ever mutate under ~/.hermes/skills. A request's allowed_paths
    # legitimately also carries a sibling repo skills root (~/.hermes/hermes-agent/
    # skills) that is NOT under it — we simply don't edit there rather than refusing
    # the whole run. Refuse only when NOTHING resolves under ~/.hermes/skills
    # (e.g. a family / non-skill area like "dashboard").
    under_skills = [p for p in allowed if _under(p, skills_root)]

    # ---- apply gating (reversibility-first, confirm instead of token) ----
    effective_apply = apply
    if apply:
        if not (confirm or request.get("approved_by_operator") is True):
            summary.update(ok=False, refused="apply requires operator confirm (--confirm or approved_by_operator)")
            _finish_status(state_dir, route_status, summary)
            return summary
        if not under_skills:
            summary.update(ok=False, refused="apply refused: no allowed paths under ~/.hermes/skills (family / non-skill area)")
            _finish_status(state_dir, route_status, summary)
            return summary
        if route_status != "configured":
            effective_apply = False
            summary["route_note"] = f"self-test {route_status}: {route_detail} -> dry-run fallback"

    summary["mode"] = "apply" if effective_apply else "dry-run"

    if not acquire_lock(state_dir, request_id, summary["mode"]):
        summary.update(ok=False, refused="a fresh run is already in progress (lock held)")
        _finish_status(state_dir, route_status, summary)
        return summary

    backup_dir: Path | None = None
    if effective_apply:
        backup_dir = _hermes_home() / "backups" / f"skills-before-autoresearch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{request_id[:8]}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        summary["backup_dir"] = str(backup_dir)

    set_status(state_dir, "running", route_status, note=summary.get("route_note"))
    attempted: set[tuple[str, str]] = set()
    # apply scans only under-skills roots; dry-run may scan all allowed roots (read-only).
    roots = [p for p in (under_skills if effective_apply else allowed) if p.exists()]

    try:
        for i in range(1, cap + 1):
            if _STOP["requested"]:
                summary["stopped"] = True
                break
            heartbeat(state_dir, request_id, i, cap, "discover", None)
            # optional pacing so a dry-run loop is observably stoppable
            sleep_s = float(os.environ.get("HERMES_AUTORESEARCH_STEP_SLEEP", "0") or 0)
            if sleep_s:
                time.sleep(sleep_s)
            if _STOP["requested"]:
                summary["stopped"] = True
                break
            cands = discover_candidates(roots, attempted)
            if not cands:
                break
            cand = cands[0]
            path, label, skill = cand["path"], cand["label"], cand["skill"]
            attempted.add((str(path), label))
            hypothesis = f"Add the '{_SCAFFOLD[label]}' section to {skill} to resolve a missing recommended section."
            target_warning = _missing_label_to_warning(label)
            heartbeat(state_dir, request_id, i, cap, "hypothesis", None)

            if effective_apply:
                before_errs, before_warns = evals.check_skill(path)
                _backup_file(path, skills_root, backup_dir)
                change = apply_scaffold(path, label)
                keep, eval_result = eval_gate(path, target_warning, before_warns)
                if keep:
                    decision = "keep"
                    summary["kept"] += 1
                else:
                    _restore_file(path, skills_root, backup_dir)
                    decision = "discard"
                    eval_result = f"reverted: {eval_result}"
                    summary["reverted"] += 1
                change_desc = f"append '## {_SCAFFOLD[label]}' scaffold"
            else:
                decision = "proposed"
                eval_result = "dry-run: no mutation"
                change_desc = f"would append '## {_SCAFFOLD[label]}' scaffold"
                summary["proposed"] += 1

            append_result({
                "timestamp": _utc_now(), "mode": summary["mode"],
                "target": f"{skill}:{_SCAFFOLD[label]}",
                "hypothesis": hypothesis, "change": change_desc,
                "eval_command": "eval_local_skills.check_skill",
                "eval_result": eval_result, "decision": decision,
                "risk": "low", "evidence": str(path),
            })
            summary["steps"].append({
                "target": f"{skill}:{_SCAFFOLD[label]}", "hypothesis": hypothesis,
                "decision": decision, "eval_result": eval_result,
            })
            summary["iterations"] = i
            heartbeat(state_dir, request_id, i, cap, "eval", decision)
            if _STOP["requested"]:
                summary["stopped"] = True
                break
    finally:
        receipt = write_receipt(summary)
        summary["receipt"] = str(receipt)
        _finish_status(state_dir, route_status, summary, last_receipt=str(receipt))
        release_lock(state_dir)
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("request", type=Path, help="path to a validated run-request JSON")
    p.add_argument("--apply", action="store_true", help="apply edits (default is dry-run)")
    p.add_argument("--confirm", action="store_true", help="operator confirmation required for --apply")
    p.add_argument("--state-dir", type=Path, default=None)
    p.add_argument("--max-iterations", type=int, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    summary = run(
        args.request, apply=args.apply, confirm=args.confirm,
        state_dir=args.state_dir, max_iterations=args.max_iterations,
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
