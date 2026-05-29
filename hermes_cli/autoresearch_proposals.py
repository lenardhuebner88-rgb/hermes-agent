#!/usr/bin/env python3
"""Sprint A1: persistent Autoresearch proposal store + apply-by-id (One-Click).

This is the *mechanism* half of the "One-Click real improvement" flow
(``vault/.../autoresearch-oneclick-real-improvement-handoff-2026-05-29.md``):
generate concrete, previewable proposals → operator sees a plain-language
before/after → applies exactly that one by id, live, reversibly.

Locked decisions it honours:

* **Content is deterministic here** (A1) — the generator reuses the same
  conservative "recommended section missing → scaffold block" candidates the
  Phase-5 runner already detects. Zero model risk. The MiniMax writer (A2) and
  the ``mode='code'`` test-suite gate (A3) slot into this same store later.
* **Reversibility is the safety**, not a token (single operator). Apply does:
  backup → write → eval-gate → keep or auto-revert. The preview itself is the
  human approval; an explicit ``confirm`` is the one "are you sure" step.
* **Mutation only under ~/.hermes/skills.** ``mode='code'`` apply is refused
  until A3 wires the hard test-suite gate.

A proposal (``autoresearch-proposal-v1``) is one JSON file under
``<audit>/proposals/<id>.json``::

    {id, schema, mode, target, target_path, section, eval_label,
     title, rationale_plain, before_text, after_text, new_text,
     diff_before_after, status, created_at, applied_at, result}

``status`` ∈ {proposed, applied, skipped}. ``mode`` ∈ {skill, code}.
"""
from __future__ import annotations

import difflib
import importlib.util
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
_RUNNER_SCRIPT = _REPO / "scripts" / "run_autoresearch_request.py"
_DEFAULT_AUDIT = _REPO / ".hermes" / "skill-audit"

PROPOSAL_SCHEMA = "autoresearch-proposal-v1"
_VALID_MODES = {"skill", "code"}
_VALID_STATUS = {"proposed", "applied", "skipped"}
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Paths (env-overridable so tests point at a tmp dir — mirrors autoresearch_view)
# ---------------------------------------------------------------------------
def _audit_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_AUDIT_DIR")
    return Path(override) if override else _DEFAULT_AUDIT


def _proposals_dir() -> Path:
    return _audit_dir() / "proposals"


_RUNNER_CACHE: dict[str, Any] = {}


def _runner():
    """Load the Phase-5 runner once and reuse its backup/eval/discover helpers
    so the proposal apply-gate is byte-for-byte the same as the autonomous loop."""
    mod = _RUNNER_CACHE.get("mod")
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location("run_autoresearch_request", _RUNNER_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _RUNNER_CACHE["mod"] = mod
    return mod


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-") or "item"


# ---------------------------------------------------------------------------
# Store CRUD
# ---------------------------------------------------------------------------
def _proposal_path(pid: str) -> Path:
    # pid is already slugged on creation; guard against traversal regardless.
    safe = _slug(pid)
    return _proposals_dir() / f"{safe}.json"


def save_proposal(proposal: dict[str, Any]) -> Path:
    path = _proposal_path(proposal["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_proposal(pid: str) -> dict[str, Any] | None:
    path = _proposal_path(pid)
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def list_proposals() -> list[dict[str, Any]]:
    """All proposals, newest first (proposed before applied/skipped)."""
    out: list[dict[str, Any]] = []
    pdir = _proposals_dir()
    if not pdir.exists():
        return out
    for path in pdir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    # Stable two-pass sort: newest-first, then grouped open → applied → skipped.
    out.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
    status_rank = {"proposed": 0, "applied": 1, "skipped": 2}
    out.sort(key=lambda p: status_rank.get(p.get("status"), 3))
    return out


# ---------------------------------------------------------------------------
# Public list payload (drops the bulky full-text fields)
# ---------------------------------------------------------------------------
_LIST_FIELDS = (
    "id", "schema", "mode", "target", "section", "title",
    "rationale_plain", "diff_before_after", "status", "result",
    "created_at", "applied_at",
)


def _to_card(proposal: dict[str, Any]) -> dict[str, Any]:
    return {k: proposal.get(k) for k in _LIST_FIELDS}


def proposals_payload() -> dict[str, Any]:
    items = list_proposals()
    cards = [_to_card(p) for p in items]
    open_count = sum(1 for p in items if p.get("status") == "proposed")
    return {
        "schema": "autoresearch-proposals-v1",
        "count": len(cards),
        "open_count": open_count,
        "proposals": cards,
        "proposals_dir": str(_proposals_dir()),
    }


# ---------------------------------------------------------------------------
# Generate (deterministic — A1). Reuses the runner's candidate discovery.
# ---------------------------------------------------------------------------
def _build_proposal_for_candidate(cand: dict[str, Any], runner) -> dict[str, Any]:
    path: Path = cand["path"]
    label: str = cand["label"]
    skill: str = cand["skill"]
    header = runner._SCAFFOLD[label]
    before = path.read_text(encoding="utf-8")
    block = runner.build_scaffold_block(skill, header)
    after = before if before.endswith("\n") else before + "\n"
    after = after + block
    pid = f"{_slug(skill)}-{_slug(header)}"
    return {
        "id": pid,
        "schema": PROPOSAL_SCHEMA,
        "mode": "skill",
        "target": skill,
        "target_path": str(path),
        "section": header,
        "eval_label": label,
        "title": f"Abschnitt „{header}“ zu {skill} hinzufügen",
        "rationale_plain": (
            f"Dem Skill `{skill}` fehlt der empfohlene Abschnitt „{header}“. "
            f"Autoresearch fügt ein klar markiertes Gerüst ein, das du danach mit "
            f"echtem Inhalt füllst. Wird automatisch zurückgerollt, wenn die "
            f"Skill-Prüfung dadurch nicht besser wird."
        ),
        "before_text": before,
        "after_text": after,
        "new_text": block,
        "diff_before_after": _make_diff(before, after, f"{skill}/SKILL.md"),
        "status": "proposed",
        "created_at": _utc_now(),
        "applied_at": None,
        "result": None,
    }


def _make_diff(before: str, after: str, name: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="",
    )
    return "\n".join(diff)


def generate_proposals(*, limit: int = 20) -> dict[str, Any]:
    """Discover deterministic skill-improvement candidates and persist any that
    don't already have an open proposal. Idempotent per (skill, section)."""
    runner = _runner()
    skills_root = runner._skills_root()
    roots = [skills_root] if skills_root.exists() else []
    cands = runner.discover_candidates(roots, attempted=set()) if roots else []

    created: list[str] = []
    skipped_existing = 0
    for cand in cands:
        if len(created) >= max(1, int(limit)):
            break
        skill = cand["skill"]
        header = runner._SCAFFOLD[cand["label"]]
        pid = f"{_slug(skill)}-{_slug(header)}"
        existing = load_proposal(pid)
        # Don't churn an already-decided or already-open proposal.
        if existing and existing.get("status") in {"proposed", "applied", "skipped"}:
            skipped_existing += 1
            continue
        proposal = _build_proposal_for_candidate(cand, runner)
        save_proposal(proposal)
        created.append(pid)

    return {
        "ok": True,
        "created": created,
        "created_count": len(created),
        "skipped_existing": skipped_existing,
        "candidates_seen": len(cands),
    }


# ---------------------------------------------------------------------------
# Apply / skip (reversible, eval-gated — reuses runner backup/gate)
# ---------------------------------------------------------------------------
def skip_proposal(pid: str) -> dict[str, Any]:
    proposal = load_proposal(pid)
    if proposal is None:
        return {"ok": False, "detail": f"no such proposal: {pid}", "status": None}
    if proposal.get("status") != "proposed":
        return {"ok": False, "detail": f"proposal is '{proposal.get('status')}', not actionable",
                "status": proposal.get("status")}
    proposal["status"] = "skipped"
    proposal["result"] = "übersprungen"
    proposal["applied_at"] = _utc_now()
    save_proposal(proposal)
    return {"ok": True, "status": "skipped", "id": pid}


def apply_proposal(pid: str, *, confirm: bool = True) -> dict[str, Any]:
    """Apply exactly this proposal: backup → write after_text → eval-gate →
    keep (status=applied) or auto-revert (status stays proposed)."""
    proposal = load_proposal(pid)
    if proposal is None:
        return {"ok": False, "detail": f"no such proposal: {pid}", "status": None}
    if proposal.get("status") != "proposed":
        return {"ok": False, "detail": f"proposal is '{proposal.get('status')}', not actionable",
                "status": proposal.get("status")}
    if not confirm:
        return {"ok": False, "detail": "apply requires confirm=true (the operator 'are you sure' step)",
                "status": "proposed"}

    mode = proposal.get("mode")
    if mode not in _VALID_MODES:
        return {"ok": False, "detail": f"unknown mode '{mode}'", "status": "proposed"}
    if mode == "code":
        # Code edits go live only behind the hard test-suite gate (Sprint A3).
        return {
            "ok": False,
            "detail": "code-mode apply ist noch nicht freigeschaltet — kommt mit dem "
                      "Test-Suite-Gate in Sprint A3. Skill-Vorschläge sind übernehmbar.",
            "status": "proposed",
            "gated": "test-suite (A3)",
        }

    runner = _runner()
    skills_root = runner._skills_root()
    target_path = Path(proposal.get("target_path", ""))

    if not target_path.exists():
        return {"ok": False, "detail": f"target no longer exists: {target_path}", "status": "proposed"}
    if not runner._under(target_path, skills_root):
        return {"ok": False,
                "detail": f"refused: target not under skills root ({skills_root})",
                "status": "proposed"}

    new_text = proposal.get("new_text")
    eval_label = proposal.get("eval_label")
    if not isinstance(new_text, str) or eval_label not in runner._SCAFFOLD:
        return {"ok": False, "detail": "proposal is malformed (missing new_text/eval_label)",
                "status": "proposed"}

    target_warning = runner._missing_label_to_warning(eval_label)
    _before_errs, before_warns = runner.evals.check_skill(target_path)

    backup_dir = (runner._hermes_home() / "backups"
                  / f"skills-before-proposal-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{_slug(pid)[:16]}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    runner._backup_file(target_path, skills_root, backup_dir)

    # Append the proposed block to the CURRENT file content (not a stale snapshot)
    # so multiple section-proposals for the same file compose instead of clobber.
    current = target_path.read_text(encoding="utf-8")
    if not current.endswith("\n"):
        current += "\n"
    target_path.write_text(current + new_text, encoding="utf-8")
    keep, eval_result = runner.eval_gate(target_path, target_warning, before_warns)

    if keep:
        proposal["status"] = "applied"
        proposal["result"] = f"✓ übernommen — Skill: eval grün ({eval_result})"
        proposal["applied_at"] = _utc_now()
        proposal["backup_dir"] = str(backup_dir)
        save_proposal(proposal)
        return {"ok": True, "status": "applied", "id": pid,
                "result": proposal["result"], "eval_result": eval_result}

    # revert — proposal stays open so the operator can retry/skip
    runner._restore_file(target_path, skills_root, backup_dir)
    proposal["result"] = f"↩ zurückgerollt — keine Verbesserung: {eval_result}"
    proposal["applied_at"] = None
    proposal["status"] = "proposed"
    save_proposal(proposal)
    return {"ok": False, "status": "proposed", "id": pid,
            "detail": proposal["result"], "reverted": True, "eval_result": eval_result}
