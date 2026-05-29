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
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
_RUNNER_SCRIPT = _REPO / "scripts" / "run_autoresearch_request.py"
_GATE_RUNNER = _REPO / "scripts" / "run_proposal_code_gate.py"
_TEST_RUNNER = _REPO / "scripts" / "run_tests.sh"
_DEFAULT_AUDIT = _REPO / ".hermes" / "skill-audit"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.autoresearch_writer import draft_section  # noqa: E402

PROPOSAL_SCHEMA = "autoresearch-proposal-v1"
_VALID_MODES = {"skill", "code"}
# "testing" = a code proposal is live-written and the full test-suite gate (A3)
# is running in a detached worker; it resolves to "applied" (green) or back to
# "proposed" (red / crashed, auto-reverted).
_VALID_STATUS = {"proposed", "testing", "applied", "skipped"}
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# A code proposal may never edit the gate's own harness — otherwise a proposal
# could neuter the very test-suite that is supposed to vet it. Repo-relative.
_GATE_SELF_PROTECT = frozenset({
    "scripts/run_tests.sh",
    "scripts/run_tests_parallel.py",
    "scripts/run_proposal_code_gate.py",
    "hermes_cli/autoresearch_proposals.py",
    "conftest.py",
    "tests/conftest.py",
})

# AR2 relevance ranking. Section criticality is a fixed, easily-tuned weight:
# a missing Safety or trigger (When-to-Use) section costs an agent system far
# more than a missing Output contract, so those get drafted first.
_SECTION_CRITICALITY = {
    "Safety / Sicherheit": 4,
    "When to Use / Wann verwenden": 3,
    "Procedure / Vorgehen": 2,
    "Output / Ergebnis": 1,
}
_RANK_W_CRIT = 2.0
_RANK_W_ROI = 1.0
_RANK_W_SUBSTANCE = 0.5
_RANK_W_USAGE = 1.0
# use_count at/above which a skill reads as "frequently used" in the reason text
_RANK_USAGE_FREQUENT = 50.0


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
            # Kill-switch safety: a "testing" proposal whose gate worker died
            # without finalising is auto-reverted here, on the read path, so a
            # crashed gate can never leave a half-applied code edit live.
            out.append(_reconcile_testing(data))
    # Stable multi-pass sort (least-significant first): newest-first, then by
    # AR2 rank_score (highest impact first), then grouped open → applied →
    # skipped. Pre-AR2 proposals lack rank_score → treated as 0, order unchanged.
    out.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
    out.sort(key=lambda p: float(p.get("rank_score") or 0.0), reverse=True)
    status_rank = {"proposed": 0, "applied": 1, "skipped": 2}
    out.sort(key=lambda p: status_rank.get(p.get("status"), 3))
    return out


# ---------------------------------------------------------------------------
# Public list payload (drops the bulky full-text fields)
# ---------------------------------------------------------------------------
_LIST_FIELDS = (
    "id", "schema", "mode", "target", "section", "title",
    "rationale_plain", "diff_before_after", "writer", "writer_rationale", "status", "result",
    "created_at", "applied_at", "rank_score", "rank_reason", "gate",
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
# Generate. Reuses the runner's candidate discovery.
# ---------------------------------------------------------------------------
def _build_proposal_for_candidate(cand: dict[str, Any], runner) -> dict[str, Any]:
    path: Path = cand["path"]
    label: str = cand["label"]
    skill: str = cand["skill"]
    header = runner._SCAFFOLD[label]
    before = path.read_text(encoding="utf-8")
    writer = "scaffold"
    writer_res: dict[str, Any]
    try:
        writer_res = draft_section(skill, header, before)
    except Exception as exc:
        writer_res = {"ok": False, "reason": f"writer failed: {type(exc).__name__}"}
    if writer_res.get("ok") and isinstance(writer_res.get("text"), str):
        block = writer_res["text"]
        writer = "minimax"
        rationale = writer_res.get("rationale") or "MiniMax hat einen fertigen Abschnitt vorgeschlagen."
    else:
        block = runner.build_scaffold_block(skill, header)
        reason = writer_res.get("reason") or "writer unavailable"
        rationale = (
            f"Dem Skill `{skill}` fehlt der empfohlene Abschnitt „{header}“. "
            f"Der MiniMax-Schreiber lieferte keinen validen Abschnitt ({reason}); "
            f"Autoresearch fällt deshalb auf das reversible Gerüst zurück."
        )
    after = before if before.endswith("\n") else before + "\n"
    after = after + block
    pid = f"{_slug(skill)}-{_slug(header)}"
    base_rationale = (
        rationale if writer == "scaffold" else
        f"Dem Skill `{skill}` fehlt der empfohlene Abschnitt „{header}“. "
        f"Autoresearch hat dafür einen fertigen MiniMax-Abschnitt erzeugt. "
        f"Wird automatisch zurückgerollt, wenn die Skill-Prüfung dadurch nicht besser wird."
    )
    # AR2: lead with the "why this one first" so the card explains its own
    # priority. (cand carries rank_reason when it came through rank_candidates.)
    rank_reason = cand.get("rank_reason")
    rationale_plain = (
        f"Priorität: {rank_reason}. {base_rationale}" if rank_reason else base_rationale
    )
    return {
        "id": pid,
        "schema": PROPOSAL_SCHEMA,
        "mode": "skill",
        "target": skill,
        "target_path": str(path),
        "section": header,
        "eval_label": label,
        "title": f"Abschnitt „{header}“ zu {skill} hinzufügen",
        "rationale_plain": rationale_plain,
        "before_text": before,
        "after_text": after,
        "new_text": block,
        "writer": writer,
        "writer_rationale": rationale,
        "diff_before_after": _make_diff(before, after, f"{skill}/SKILL.md"),
        "status": "proposed",
        "created_at": _utc_now(),
        "applied_at": None,
        "result": None,
        "rank_score": cand.get("rank_score"),
        "rank_reason": rank_reason,
    }


def _make_diff(before: str, after: str, name: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="",
    )
    return "\n".join(diff)


# ---------------------------------------------------------------------------
# AR2: relevance ranking — draft the highest-impact gaps first, capped, each
# with a plain "why first". Deterministic. No telemetry system is built
# (single operator): the only usage signal is the cheap, already-maintained
# ``~/.hermes/skills/.usage.json`` sidecar, and ranking degrades gracefully to
# "no usage signal" when it is absent.
# ---------------------------------------------------------------------------
def _load_skill_usage() -> dict[str, float]:
    """Best-effort map of *skill leaf name* → ``use_count`` from the curator
    usage sidecar. Returns ``{}`` on any problem — a generate run never fails
    over a missing/broken sidecar, it just loses the usage factor. Read-only;
    we never write telemetry here."""
    try:
        usage_file = _runner()._skills_root() / ".usage.json"
        data = json.loads(usage_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        try:
            count = float(val.get("use_count") or 0)
        except (TypeError, ValueError):
            count = 0.0
        leaf = str(key).rstrip("/").split("/")[-1]
        if count > out.get(leaf, 0.0):
            out[leaf] = count
    return out


def _candidate_content_len(cand: dict[str, Any]) -> int:
    path = cand.get("path")
    if not isinstance(path, Path):
        return 0
    try:
        return len(path.read_text(encoding="utf-8"))
    except OSError:
        return 0


def _rank_reason(label: str, n_missing: int, use_count: float) -> str:
    """Short, plain-language 'why this one first' for the proposal card."""
    clauses: list[str] = []
    if label == "Safety / Sicherheit":
        clauses.append("Safety-Lücke (für ein Agentensystem am kostspieligsten)")
    elif label == "When to Use / Wann verwenden":
        clauses.append("fehlender Aktivierungs-Trigger")
    if int(n_missing or 1) <= 1:
        clauses.append("sonst vollständig — nur dieser Abschnitt fehlt")
    if use_count >= _RANK_USAGE_FREQUENT:
        clauses.append(f"häufig genutzt ({int(use_count)}×)")
    elif use_count > 0 and not clauses:
        clauses.append(f"genutzt ({int(use_count)}×)")
    if not clauses:
        clauses.append("empfohlener Abschnitt fehlt")
    return "; ".join(clauses[:2])


def rank_candidates(
    cands: list[dict[str, Any]],
    *,
    limit: int | None = None,
    usage: dict[str, float] | None = None,
    exclude_ids: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Rank gap-candidates by likely impact (criticality + completeness-ROI +
    substance + optional usage), highest first, deterministically. Candidates
    whose ``id`` is in ``exclude_ids`` (already proposed/applied/skipped) are
    dropped; ``limit`` caps the result to the AR2 Top-N. Each returned dict is
    a shallow copy annotated with ``rank_score`` and ``rank_reason``."""
    usage = usage or {}
    ranked: list[dict[str, Any]] = []
    for cand in cands:
        if cand.get("id") in exclude_ids:
            continue
        skill = cand.get("skill", "")
        label = cand.get("label", "")
        n_missing = int(cand.get("n_missing") or 1)
        crit = _SECTION_CRITICALITY.get(label, 1)
        roi = max(0, 5 - n_missing)
        substance = min(_candidate_content_len(cand) / 1500.0, 3.0)
        use_count = float(usage.get(skill, 0.0))
        usage_w = min(use_count / 50.0, 3.0)
        score = (
            crit * _RANK_W_CRIT
            + roi * _RANK_W_ROI
            + substance * _RANK_W_SUBSTANCE
            + usage_w * _RANK_W_USAGE
        )
        annotated = dict(cand)
        annotated["rank_score"] = round(score, 4)
        annotated["rank_reason"] = _rank_reason(label, n_missing, use_count)
        ranked.append(annotated)
    # Deterministic: descending score, then stable by path/skill/label.
    ranked.sort(key=lambda c: (
        -c["rank_score"], str(c.get("path", "")), c.get("skill", ""), c.get("label", ""),
    ))
    if limit is not None:
        ranked = ranked[: max(1, int(limit))]
    return ranked


def generate_proposals(*, limit: int = 10) -> dict[str, Any]:
    """Discover deterministic skill-improvement candidates, rank them by impact
    (AR2), and draft only the capped Top-N that don't already have a decided
    proposal. Idempotent per (skill, section); ranking the cap means the model
    writer runs on the highest-value gaps, not the alphabetically-first ones."""
    runner = _runner()
    skills_root = runner._skills_root()
    roots = [skills_root] if skills_root.exists() else []
    cands = runner.discover_candidates(roots, attempted=set()) if roots else []

    # Annotate each candidate with its proposal id and collect the ones already
    # decided so they're neither re-ranked nor re-drafted.
    exclude_ids: set[str] = set()
    for cand in cands:
        header = runner._SCAFFOLD[cand["label"]]
        cand["id"] = f"{_slug(cand['skill'])}-{_slug(header)}"
        existing = load_proposal(cand["id"])
        if existing and existing.get("status") in _VALID_STATUS:
            exclude_ids.add(cand["id"])

    usage = _load_skill_usage()
    ranked = rank_candidates(
        cands, limit=max(1, int(limit)), usage=usage, exclude_ids=exclude_ids
    )

    created: list[str] = []
    for cand in ranked:
        proposal = _build_proposal_for_candidate(cand, runner)
        save_proposal(proposal)
        created.append(cand["id"])

    return {
        "ok": True,
        "created": created,
        "created_count": len(created),
        "skipped_existing": len(exclude_ids),
        "candidates_seen": len(cands),
        "ranked_drafted": len(ranked),
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
        # A3: code edits go live behind the full test-suite gate. Validate →
        # backup → write → mark "testing" → spawn detached gate worker. The
        # worker runs the whole suite and keeps (green) or auto-reverts (red).
        return _apply_code_proposal(proposal, pid)

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


# ---------------------------------------------------------------------------
# A3: code-mode test-suite gate
#
# A code proposal carries a full ``after_text`` for one repo file. Applying it
# is reversible by construction: backup → write → run the *whole* test suite in
# a detached worker → keep on green, auto-revert on red (or on a crashed gate,
# reconciled on the next read). The full suite is the honest gate (a code edit
# can break tests far from the file it touches); it runs out-of-band so the
# HTTP apply returns immediately with status "testing".
# ---------------------------------------------------------------------------
def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _backup_code_file(path: Path, backup_dir: Path) -> None:
    rel = path.resolve().relative_to(_REPO.resolve())
    dest = backup_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(path, dest)


def _restore_code_file(path: Path, backup_dir: Path) -> None:
    rel = path.resolve().relative_to(_REPO.resolve())
    src = backup_dir / rel
    if src.exists():
        shutil.copy2(src, path)


def _code_target_ok(path: Path) -> tuple[bool, str]:
    """A code proposal may only touch a real file inside the repo, never a
    secret/auth/config/db surface, never ``.git``, never the gate's own harness."""
    try:
        rp = path.resolve()
    except OSError:
        return False, f"target path unreadable: {path}"
    if not rp.exists() or not rp.is_file():
        return False, f"target no longer exists: {path}"
    if not _under(rp, _REPO):
        return False, f"refused: code target must live inside the repo ({_REPO})"
    try:
        from scripts.autoresearch_request import forbidden_paths  # lazy: avoid import cost on skill path
        home = _runner()._hermes_home()
        for f in forbidden_paths(home):
            fp = Path(f).resolve()
            if rp == fp or _under(rp, fp):
                return False, "refused: secrets/auth/config/db surfaces are off-limits"
    except Exception:
        # Validator unavailable → fail closed on the obviously-sensitive names.
        if rp.name in {".env", "auth.json", "config.yaml"} or rp.suffix == ".db":
            return False, "refused: secrets/auth/config/db surfaces are off-limits"
    rel = rp.relative_to(_REPO.resolve())
    if rel.parts and rel.parts[0] == ".git":
        return False, "refused: .git is off-limits"
    if rel.as_posix() in _GATE_SELF_PROTECT:
        return False, "refused: a proposal may not modify the test-suite gate's own harness"
    return True, ""


def _code_backup_root() -> Path:
    return _runner()._hermes_home() / "backups"


def _apply_code_proposal(proposal: dict[str, Any], pid: str) -> dict[str, Any]:
    after_text = proposal.get("after_text")
    if not isinstance(after_text, str):
        return {"ok": False, "detail": "code proposal is malformed (missing after_text)",
                "status": "proposed"}
    target_path = Path(proposal.get("target_path", ""))
    ok, why = _code_target_ok(target_path)
    if not ok:
        return {"ok": False, "detail": why, "status": "proposed"}

    backup_dir = (_code_backup_root()
                  / f"code-before-proposal-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{_slug(pid)[:16]}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    _backup_code_file(target_path, backup_dir)

    log_path = _proposals_dir() / f"{_slug(pid)}.gate.log"
    # Write the candidate edit live, then hand off to the detached gate worker.
    target_path.write_text(after_text, encoding="utf-8")
    proposal["status"] = "testing"
    proposal["applied_at"] = None
    proposal["result"] = "Test-Suite läuft … (volle Suite, dauert ein paar Minuten)"
    proposal["gate"] = {
        "phase": "running",
        "started_at": _utc_now(),
        "finished_at": None,
        "returncode": None,
        "summary": None,
        "backup_dir": str(backup_dir),
        "log_path": str(log_path),
        "pid": None,
    }
    save_proposal(proposal)

    gate_pid = _spawn_code_gate(pid)
    # Re-load before stamping the pid so we never clobber a same-tick edit; the
    # full suite takes minutes, so the worker cannot have finalised this fast.
    latest = load_proposal(pid) or proposal
    gate = latest.get("gate") or proposal["gate"]
    gate["pid"] = gate_pid
    latest["gate"] = gate
    save_proposal(latest)
    return {"ok": True, "status": "testing", "id": pid,
            "result": latest.get("result"), "gate": gate}


def _spawn_code_gate(pid: str) -> int:
    """Spawn the detached code-gate worker; return its PID. Isolated so tests
    can stub it (mirrors autoresearch_view._spawn_runner)."""
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, str(_GATE_RUNNER), pid],
        cwd=str(_REPO),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def _summarize_test_log(tail: str, returncode: int) -> str:
    """A short, human line for the proposal card — the pytest summary line if we
    can find it, else a generic pass/fail."""
    for line in reversed(tail.splitlines()):
        stripped = line.strip().strip("= ")
        low = stripped.lower()
        if any(tok in low for tok in ("passed", "failed", "error", "no tests")):
            return stripped[:200]
    return "Tests grün" if returncode == 0 else f"Tests rot (exit {returncode})"


def _run_test_suite(log_path: Path) -> tuple[int, str]:
    """Run the canonical full suite, streaming output to ``log_path``; return
    (returncode, log_tail). Isolated so finalize_code_gate is testable without
    actually spawning ~26k tests."""
    import subprocess

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(
            ["bash", str(_TEST_RUNNER)],
            cwd=str(_REPO),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    return proc.returncode, _tail(log_path)


def finalize_code_gate(pid: str, *, run_suite=None) -> dict[str, Any]:
    """Run the test-suite gate for one "testing" code proposal and resolve it:
    keep on green (status=applied), auto-revert on red (status back to
    proposed). Called by the detached gate worker; ``run_suite`` is injectable
    for tests."""
    run_suite = run_suite or _run_test_suite
    proposal = load_proposal(pid)
    if proposal is None:
        return {"ok": False, "detail": f"no such proposal: {pid}", "status": None}
    if proposal.get("status") != "testing":
        return {"ok": False, "detail": f"proposal is '{proposal.get('status')}', not under test",
                "status": proposal.get("status")}

    gate = dict(proposal.get("gate") or {})
    target_path = Path(proposal.get("target_path", ""))
    backup_dir = Path(gate.get("backup_dir", ""))
    log_path = Path(gate.get("log_path") or (_proposals_dir() / f"{_slug(pid)}.gate.log"))

    returncode, tail = run_suite(log_path)
    summary = _summarize_test_log(tail, returncode)
    gate["returncode"] = returncode
    gate["finished_at"] = _utc_now()
    gate["summary"] = summary

    if returncode == 0:
        gate["phase"] = "passed"
        proposal["status"] = "applied"
        proposal["applied_at"] = _utc_now()
        proposal["result"] = f"✓ übernommen — Test-Suite grün ({summary})"
        proposal["gate"] = gate
        save_proposal(proposal)
        return {"ok": True, "status": "applied", "id": pid,
                "result": proposal["result"], "returncode": returncode}

    # Tests red → roll the edit back; the proposal reopens for retry/skip.
    if backup_dir and backup_dir.exists():
        _restore_code_file(target_path, backup_dir)
    gate["phase"] = "failed"
    proposal["status"] = "proposed"
    proposal["applied_at"] = None
    proposal["result"] = f"↩ zurückgerollt — Test-Suite rot ({summary})"
    proposal["gate"] = gate
    save_proposal(proposal)
    return {"ok": False, "status": "proposed", "id": pid,
            "detail": proposal["result"], "reverted": True, "returncode": returncode}


def _reconcile_testing(proposal: dict[str, Any]) -> dict[str, Any]:
    """If a code proposal is stuck in "testing" but its gate worker is gone
    without a verdict, auto-revert it. Idempotent; safe to call on every read."""
    if proposal.get("status") != "testing":
        return proposal
    gate = proposal.get("gate") or {}
    pid = gate.get("pid")
    if not isinstance(pid, int):
        return proposal  # not spawned/stamped yet — leave the grace window
    if _pid_alive(pid) or gate.get("phase") != "running":
        return proposal

    gate = dict(gate)
    target_path = Path(proposal.get("target_path", ""))
    backup_dir = Path(gate.get("backup_dir", ""))
    try:
        if backup_dir and backup_dir.exists():
            _restore_code_file(target_path, backup_dir)
    except OSError:
        pass
    gate["phase"] = "crashed"
    gate["finished_at"] = _utc_now()
    proposal["status"] = "proposed"
    proposal["applied_at"] = None
    proposal["result"] = "↩ zurückgerollt — Test-Gate abgebrochen (Prozess beendet ohne Ergebnis)"
    proposal["gate"] = gate
    save_proposal(proposal)
    return proposal


# ---------------------------------------------------------------------------
# Minimal code-proposal generator
#
# Code proposals don't come from the deterministic skill-gap discovery; they
# come from an author (an agent like Codex/Claude, or the operator) handing a
# concrete file rewrite into the same preview → gate → apply flow. This keeps
# the authoring surface tiny and model-free: supply the full new text for one
# file, get back a previewable, test-gated proposal. (CLI: make_code_proposal.py)
# ---------------------------------------------------------------------------
def build_code_proposal(
    target_path: str | Path,
    after_text: str,
    *,
    title: str,
    rationale: str,
    pid: str | None = None,
    section: str | None = None,
) -> dict[str, Any]:
    path = Path(target_path)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    rel_name = path.name
    try:
        rel_name = str(path.resolve().relative_to(_REPO.resolve()))
    except (ValueError, OSError):
        pass
    pid = pid or _slug(f"code-{rel_name}-{title}")
    proposal = {
        "id": pid,
        "schema": PROPOSAL_SCHEMA,
        "mode": "code",
        "target": rel_name,
        "target_path": str(path.resolve() if path.exists() else path),
        "section": section,
        "eval_label": None,
        "title": title,
        "rationale_plain": rationale,
        "before_text": before,
        "after_text": after_text,
        "new_text": None,
        "writer": "operator",
        "writer_rationale": None,
        "diff_before_after": _make_diff(before, after_text, rel_name),
        "status": "proposed",
        "created_at": _utc_now(),
        "applied_at": None,
        "result": None,
        "rank_score": None,
        "rank_reason": None,
    }
    save_proposal(proposal)
    return proposal
