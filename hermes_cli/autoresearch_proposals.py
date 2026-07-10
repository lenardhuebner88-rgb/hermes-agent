#!/usr/bin/env python3
"""Sprint A1: persistent Autoresearch proposal store + apply-by-id (One-Click).

This is the *mechanism* half of the "One-Click real improvement" flow
(``vault/.../autoresearch-oneclick-real-improvement-handoff-2026-05-29.md``):
generate concrete, previewable proposals → operator sees a plain-language
before/after → applies exactly that one by id, live, reversibly.

Locked decisions it honours:

* **Skill content is deterministic here** (A1) — the legacy generator reuses
  the same conservative "recommended section missing → scaffold block"
  candidates the Phase-5 runner already detects. Code-weakness proposals are a
  separate auxiliary-model-authored path, constrained to a tight repo allowlist and
  applied only through the code-mode test-suite gate.
* **Reversibility is the safety**, not a token (single operator). Apply does:
  backup → write → eval-gate → keep or auto-revert. The preview itself is the
  human approval; an explicit ``confirm`` is the one "are you sure" step.
* **Mutation is gated.** Skill proposals are eval-gated by the skill checker;
  code proposals are restricted to an explicit allowlist and applied only
  through the detached test-suite gate.

A proposal (``autoresearch-proposal-v1``) is one JSON file under
``<audit>/proposals/<id>.json``::

    {id, schema, mode, target, target_path, section, eval_label,
     title, rationale_plain, before_text, after_text, new_text,
     diff_before_after, status, created_at, applied_at, result}

``status`` ∈ {proposed, applied, skipped}. ``mode`` ∈ {skill, code}.
"""
from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
_RUNNER_SCRIPT = _REPO / "scripts" / "run_autoresearch_request.py"
_GATE_RUNNER = _REPO / "scripts" / "run_proposal_code_gate.py"
_TEST_RUNNER = _REPO / "scripts" / "run_tests.sh"
_DEFAULT_AUDIT = _REPO / ".hermes" / "skill-audit"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.autoresearch_writer import _call_llm as _writer_call_llm, draft_fix, draft_section  # noqa: E402
from hermes_cli import capability_researcher  # noqa: E402

PROPOSAL_SCHEMA = "autoresearch-proposal-v1"
_VALID_MODES = {"skill", "code"}
# "testing" = a code proposal is live-written and the full test-suite gate (A3)
# is running in a detached worker; it resolves to "applied" (green) or back to
# "proposed" (red / crashed, auto-reverted).
_VALID_STATUS = {"proposed", "testing", "applied", "skipped", "routed_to_kanban", "pooled", "escalated"}
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

# Code-weakness finder scope. This is intentionally a small, explicit repo
# allowlist: no free repository traversal, no tests, no generated web bundles,
# no migrations, and no gate/self-editing surfaces.
_CODE_ALLOWLIST = (
    "hermes_cli/capability_researcher.py",
    "hermes_cli/commands.py",
    "hermes_cli/model_normalize.py",
    "hermes_cli/skin_engine.py",
)
_CODE_ALLOWLIST_DIRS = ("hermes_cli",)
_CODE_ALLOWLIST_DENY_PARTS = frozenset({
    ".git",
    "tests",
    "web_dist",
    "migrations",
    "migration",
})
_CODE_WEAKNESS_CATEGORIES = {
    "bug_risk": (4, "Bug-Risiko"),
    "dead_logic": (3, "Tote oder unerreichbare Logik"),
    "error_handling": (2, "Unklare Fehlerbehandlung"),
}
# Severity scale shared by both lanes. Model-assigned (critical|high|medium|low);
# falls back to a per-category default when the model omits/garbles it. Drives the
# severity-dominant rank_score and the frontend grouping/collapse — never a drop.
_SEVERITY_ORDINAL = {"critical": 4, "high": 3, "medium": 2, "low": 1}
_SEVERITY_LABELS = {"critical": "kritisch", "high": "hoch", "medium": "mittel", "low": "niedrig"}
_CODE_CATEGORY_SEVERITY = {
    "bug_risk": "high",
    "dead_logic": "medium",
    "error_handling": "medium",
}
_SKILL_CATEGORY_SEVERITY = dict(getattr(capability_researcher, "_SKILL_CATEGORY_SEVERITY", {
    "contradiction": "high",
    "missing_trigger": "high",
    "unclear_trigger": "medium",
    "incomplete_steps": "medium",
    "missing_section": "low",
}))


def _coerce_severity(value: Any, *, fallback: str) -> str:
    """Normalise a model-supplied severity to the known scale, else fall back."""
    sev = str(value or "").strip().lower()
    return sev if sev in _SEVERITY_ORDINAL else fallback


# Intake-Triage (H3): nur high+ Findings werden als `proposed` in die Review-Queue
# gelegt; medium/low sind detection-only — vom Producer geloggt (run record), aber
# NICHT gequeued. Severity liegt am Geburtspunkt jeder spekulativen Finder-Lane vor
# (modell-zugewiesen bzw. Kategorie-Fallback). So fuellt sich die Queue nicht mit
# Rauschen. Verifikations-gegatete Lanes (test-foundry: green@HEAD/red@mutant schon
# bestanden, bevor das Proposal existiert) sind bewusst NICHT severity-gegated.
_INTAKE_MIN_SEVERITY_ORDINAL = _SEVERITY_ORDINAL["high"]


def meets_intake_threshold(proposal: dict[str, Any]) -> bool:
    """True iff a speculative finding is severe enough (high+) to enter the queue."""
    sev = str(proposal.get("severity") or "").strip().lower()
    if sev in _SEVERITY_ORDINAL:
        return _SEVERITY_ORDINAL[sev] >= _INTAKE_MIN_SEVERITY_ORDINAL
    # Kein Severity-Signal → nicht still droppen (fail-open); andere Gates greifen.
    return True


def _model_label_from_response(resp: Any, *, task: str = "skills_hub") -> str:
    model = str(getattr(resp, "model", "") or "").strip()
    if model:
        return model
    try:
        from scripts.autoresearch_writer import _configured_aux_model

        configured = _configured_aux_model(task)
        if configured:
            return configured
    except Exception:
        pass
    return "aux-model"
_CODE_FINDER_MAX_FILE_CHARS = 45_000
_CODE_FINDER_MAX_FINDINGS_PER_FILE = 1

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
def _usage_min_use_count() -> float:
    """Minimum use_count for a skill to be an AR3 research candidate. Env-tunable
    (``HERMES_AUTORESEARCH_MIN_USE_COUNT``) so the operator can widen the net to
    lightly-used skills without a code change; defaults to the historical 5."""
    raw = os.environ.get("HERMES_AUTORESEARCH_MIN_USE_COUNT")
    if raw is None or not raw.strip():
        return 5.0
    try:
        val = float(raw)
    except ValueError:
        return 5.0
    # float() also parses "nan"/"inf"; nan makes every `use < min` comparison
    # False (net silently closes), inf excludes everything — both are the
    # opposite of a usable threshold, so fall back to the default.
    if not math.isfinite(val):
        return 5.0
    return val


# Back-compat constant: the historical default. Live filtering reads
# _usage_min_use_count() so the env lever takes effect; tests still import this.
_USAGE_MIN_USE_COUNT = 5


# ---------------------------------------------------------------------------
# Paths (env-overridable so tests point at a tmp dir — mirrors autoresearch_view)
# ---------------------------------------------------------------------------
def _audit_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_AUDIT_DIR")
    return Path(override) if override else _DEFAULT_AUDIT


def _proposals_dir() -> Path:
    return _audit_dir() / "proposals"


# --- P1: incremental code-scan state (content-hash per allowlisted file) -------
def _code_scan_state_path() -> Path:
    return _audit_dir() / "researched-code.json"


def _content_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _read_code_scan_state() -> dict[str, str]:
    """Map repo-relative path → content-sha at last scan. Tolerant: {} on error."""
    path = _code_scan_state_path()
    try:
        if not path.exists() or path.stat().st_size == 0:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else {}


def _write_code_scan_state(state: dict[str, str]) -> None:
    try:
        path = _code_scan_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"files": state}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


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
    status_rank = {"proposed": 0, "testing": 1, "routed_to_kanban": 2, "pooled": 3, "escalated": 4, "applied": 5, "skipped": 6}
    out.sort(key=lambda p: status_rank.get(p.get("status"), 3))
    return out


# ---------------------------------------------------------------------------
# Public list payload (drops the bulky full-text fields)
# ---------------------------------------------------------------------------
_LIST_FIELDS = (
    "id", "schema", "mode", "target", "section", "title",
    "rationale_plain", "diff_before_after", "writer", "writer_rationale", "status", "last_outcome", "result",
    "created_at", "applied_at", "rank_score", "rank_reason", "gate",
    "proposal_type", "category", "severity", "evidence", "fix_hint", "apply_blocked_reason",
)


def _to_card(proposal: dict[str, Any]) -> dict[str, Any]:
    return {k: proposal.get(k) for k in _LIST_FIELDS}


def _is_actionable(p: dict[str, Any]) -> bool:
    return p.get("status") == "proposed" and p.get("last_outcome") != "reverted_no_improvement"


def _is_reverted_no_improvement(p: dict[str, Any]) -> bool:
    return p.get("status") == "proposed" and p.get("last_outcome") == "reverted_no_improvement"


def proposals_payload() -> dict[str, Any]:
    items = list_proposals()
    cards = [_to_card(p) for p in items]
    open_count = sum(1 for p in items if _is_actionable(p))
    return {
        "schema": "autoresearch-proposals-v1",
        "count": len(cards),
        "open_count": open_count,
        "reverted_count": sum(1 for p in items if _is_reverted_no_improvement(p)),
        "testing_count": sum(1 for p in items if p.get("status") == "testing"),
        "applied_count": sum(1 for p in items if p.get("status") == "applied"),
        "skipped_count": sum(1 for p in items if p.get("status") == "skipped"),
        "proposals": cards,
        "proposals_dir": str(_proposals_dir()),
    }


# ---------------------------------------------------------------------------
# Sprint A migration: structured last_outcome backfill
# ---------------------------------------------------------------------------
def _infer_last_outcome(proposal: dict[str, Any]) -> str | None:
    if proposal.get("status") == "applied":
        return "applied"
    result = str(proposal.get("result") or "").lower()
    if proposal.get("status") == "proposed" and "zurückgerollt" in result and "keine verbesserung" in result:
        return "reverted_no_improvement"
    return None


def _copy_proposals_backup() -> Path | None:
    pdir = _proposals_dir()
    if not pdir.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = pdir.with_name(f"{pdir.name}.bak-{stamp}")
    suffix = 1
    while dest.exists():
        dest = pdir.with_name(f"{pdir.name}.bak-{stamp}-{suffix}")
        suffix += 1
    shutil.copytree(pdir, dest)
    return dest


def backfill_last_outcome(*, dry_run: bool = True) -> dict[str, Any]:
    pdir = _proposals_dir()
    changes: list[dict[str, Any]] = []
    if not pdir.exists():
        return {"ok": True, "dry_run": dry_run, "would_update": 0, "updated": 0, "backup_dir": None, "changes": []}
    for path in sorted(pdir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        inferred = _infer_last_outcome(data)
        if inferred is None or data.get("last_outcome") == inferred:
            continue
        changes.append({"path": str(path), "id": data.get("id"), "from": data.get("last_outcome"), "to": inferred})

    if dry_run or not changes:
        return {"ok": True, "dry_run": dry_run, "would_update": len(changes), "updated": 0, "backup_dir": None, "changes": changes}

    backup_dir = _copy_proposals_backup()
    updated = 0
    for change in changes:
        path = Path(change["path"])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        data["last_outcome"] = change["to"]
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        updated += 1
    return {"ok": True, "dry_run": False, "would_update": len(changes), "updated": updated, "backup_dir": str(backup_dir) if backup_dir else None, "changes": changes}


def _parse_created_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _older_than_days(value: Any, days: int, *, now: datetime | None = None) -> bool:
    created = _parse_created_at(value)
    if created is None:
        return False
    now = now or datetime.now(timezone.utc)
    return created < now - timedelta(days=max(0, int(days)))


def _archive_destination(path: Path, archive_dir: Path) -> Path:
    dest = archive_dir / path.name
    if not dest.exists():
        return dest
    stem = path.stem
    suffix = path.suffix
    idx = 1
    while True:
        candidate = archive_dir / f"{stem}-{idx}{suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


def prune_proposals(archive_done_older_than_days: int = 7, proposed_ttl_days: int = 30) -> dict[str, int]:
    pdir = _proposals_dir()
    if not pdir.exists():
        return {"archived": 0, "auto_skipped": 0}
    archive_dir = pdir / "_archive"
    auto_skipped_paths: set[Path] = set()
    auto_skipped = 0
    for path in sorted(pdir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        is_proposed = data.get("status") == "proposed"
        reverted_stale = (
            is_proposed
            and data.get("last_outcome") == "reverted_no_improvement"
            and _older_than_days(data.get("created_at"), 14)
        )
        # TTL-Auto-Reject (H3): ein `proposed`, das nie gegated/reviewed wurde, raeumt
        # sich nach proposed_ttl_days selbst (created_at-basiert). Sonst bleiben frisch
        # geborene proposed (last_outcome=None) ewig in der Queue — der reverted-Filter
        # oben faengt sie nicht.
        ttl_expired = is_proposed and _older_than_days(data.get("created_at"), proposed_ttl_days)
        if reverted_stale or ttl_expired:
            data["status"] = "skipped"
            data["result"] = data.get("result") or (
                "auto-skipped by autoresearch prune: reverted without improvement"
                if reverted_stale
                else f"auto-skipped by autoresearch prune: proposed >{proposed_ttl_days}d ohne Review (TTL)"
            )
            save_proposal(data)
            auto_skipped += 1
            auto_skipped_paths.add(path)

    archived = 0
    archive_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(pdir.glob("*.json")):
        if path in auto_skipped_paths:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") not in {"applied", "skipped"}:
            continue
        if not _older_than_days(data.get("created_at"), archive_done_older_than_days):
            continue
        try:
            shutil.move(str(path), str(_archive_destination(path, archive_dir)))
            archived += 1
        except OSError:
            continue
    return {"archived": archived, "auto_skipped": auto_skipped}


def _infer_severity_from_category(proposal: dict[str, Any]) -> str | None:
    category = str(proposal.get("category") or "").strip()
    if not category:
        return None
    if proposal.get("mode") == "code":
        return _CODE_CATEGORY_SEVERITY.get(category)
    return _SKILL_CATEGORY_SEVERITY.get(category) or _CODE_CATEGORY_SEVERITY.get(category)


def backfill_missing_severity(*, dry_run: bool = True) -> dict[str, Any]:
    pdir = _proposals_dir()
    changes: list[dict[str, Any]] = []
    if not pdir.exists():
        return {"ok": True, "dry_run": dry_run, "would_update": 0, "updated": 0, "backup_dir": None, "changes": []}
    for path in sorted(pdir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("severity"):
            continue
        severity = _infer_severity_from_category(data)
        if not severity:
            continue
        changes.append({"path": str(path), "id": data.get("id"), "category": data.get("category"), "to": severity})

    if dry_run or not changes:
        return {"ok": True, "dry_run": dry_run, "would_update": len(changes), "updated": 0, "backup_dir": None, "changes": changes}

    backup_dir = _copy_proposals_backup()
    updated = 0
    for change in changes:
        path = Path(change["path"])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("severity"):
            continue
        data["severity"] = change["to"]
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        updated += 1
    return {"ok": True, "dry_run": False, "would_update": len(changes), "updated": updated, "backup_dir": str(backup_dir) if backup_dir else None, "changes": changes}


# ---------------------------------------------------------------------------
# Generate. Reuses the runner's candidate discovery.
# ---------------------------------------------------------------------------
def _build_proposal_for_candidate(cand: dict[str, Any], runner) -> dict[str, Any] | None:
    path: Path = cand["path"]
    if "category" in cand and "evidence" in cand:
        return _build_proposal_for_finding(cand, path)

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
        writer = "aux-section-writer"
        rationale = writer_res.get("rationale") or "Aux-Modell hat einen fertigen Abschnitt vorgeschlagen."
    else:
        block = runner.build_scaffold_block(skill, header)
        reason = writer_res.get("reason") or "writer unavailable"
        rationale = (
            f"Dem Skill `{skill}` fehlt der empfohlene Abschnitt „{header}“. "
            f"Der Aux-Schreiber lieferte keinen validen Abschnitt ({reason}); "
            f"Autoresearch fällt deshalb auf das reversible Gerüst zurück."
        )
    after = before if before.endswith("\n") else before + "\n"
    after = after + block
    pid = f"{_slug(skill)}-{_slug(header)}"
    base_rationale = (
        rationale if writer == "scaffold" else
        f"Dem Skill `{skill}` fehlt der empfohlene Abschnitt „{header}“. "
        f"Autoresearch hat dafür einen fertigen Aux-Modell-Abschnitt erzeugt. "
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
        "last_outcome": None,
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
        return _load_skill_usage_from_root(_runner()._skills_root())
    except AttributeError:
        return {}


def _load_skill_usage_from_root(skills_root: Path) -> dict[str, float]:
    try:
        usage_file = skills_root / ".usage.json"
        data = json.loads(usage_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
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


def _visible_skill_paths(roots: list[Path], runner) -> list[Path]:
    seen: set[Path] = set()
    paths: list[Path] = []
    for root in roots:
        for path in runner.evals.find_skills(root):
            if path in seen:
                continue
            seen.add(path)
            try:
                rel_parts = path.relative_to(root).parts
            except ValueError:
                rel_parts = path.parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            paths.append(path)
    return paths


def _skills_for_capability_research(
    roots: list[Path],
    runner,
    usage: dict[str, float],
) -> tuple[list[tuple[str, str]], dict[str, Path], int]:
    skills: list[tuple[str, str]] = []
    path_by_skill: dict[str, Path] = {}
    skipped_low_usage = 0
    for path in _visible_skill_paths(roots, runner):
        skill = path.parent.name
        if float(usage.get(skill, 0.0)) < _usage_min_use_count():
            skipped_low_usage += 1
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        skills.append((skill, text))
        path_by_skill.setdefault(skill, path)
    return skills, path_by_skill, skipped_low_usage


def _proposal_id_for_finding(finding: dict[str, Any]) -> str:
    skill = str(finding.get("skill") or "skill")
    category = str(finding.get("category") or "weakness")
    # Evidence-bearing findings dedup on their verbatim quote. Absence findings
    # (missing_trigger/missing_section) carry no evidence, so they would all
    # collapse to one id per (skill, category) and clobber each other; fall back
    # to the problem text so distinct absence findings stay distinct + stable.
    discriminator = str(finding.get("evidence") or "") or str(finding.get("problem") or "")
    digest = hashlib.sha1(
        f"{skill}\0{category}\0{discriminator}".encode("utf-8")
    ).hexdigest()[:10]
    return f"{_slug(skill)}-{_slug(category)}-{digest}"


def _build_proposal_for_finding(finding: dict[str, Any], path: Path) -> dict[str, Any] | None:
    skill = str(finding.get("skill") or path.parent.name)
    category = str(finding.get("category") or "")
    category_label = capability_researcher.WEAKNESS_CATEGORIES.get(category, (0, category))[1]
    evidence = str(finding.get("evidence") or "")
    problem = str(finding.get("problem") or "Konkrete Skill-Schwäche gefunden.")
    fix_hint = str(finding.get("fix_hint") or "Gezielt präzisieren, ohne neue generische Abschnitte zu scaffolden.")
    before = path.read_text(encoding="utf-8")
    try:
        writer_res = draft_fix(skill, finding, before)
    except Exception as exc:
        writer_res = {"ok": False, "reason": f"writer failed: {type(exc).__name__}"}
    if not writer_res.get("ok") or not isinstance(writer_res.get("text"), str):
        return None
    after = str(writer_res["text"])
    if after == before:
        return None
    rank_reason = finding.get("rank_reason")
    rationale = (
        f"Priorität: {rank_reason}. {problem}" if rank_reason else problem
    )
    writer_rationale = writer_res.get("rationale") or "Aux-Modell hat einen grounded AR3-Fix vorgeschlagen."
    return {
        "id": _proposal_id_for_finding(finding),
        "schema": PROPOSAL_SCHEMA,
        "mode": "skill",
        "proposal_type": "capability_research",
        "target": skill,
        "target_path": str(path),
        "section": None,
        "eval_label": None,
        "category": category,
        "severity": finding.get("severity"),
        "evidence": evidence,
        "fix_hint": fix_hint,
        "title": f"Skill-Schwäche in {skill}: {category_label}",
        "rationale_plain": rationale,
        "before_text": before,
        "after_text": after,
        "new_text": after,
        "writer": "aux-ar3-fix-writer",
        "writer_rationale": writer_rationale,
        "diff_before_after": _make_diff(before, after, f"{skill}/SKILL.md"),
        "status": "proposed",
        "last_outcome": None,
        "created_at": _utc_now(),
        "applied_at": None,
        "result": None,
        "rank_score": finding.get("rank_score"),
        "rank_reason": rank_reason,
    }


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
    # Scaffolder off by default ("kein Schein"): the instant deterministic button
    # must not mint flat "add a missing section" proposals. Real AR3 weakness
    # research is slow (a model pass per used skill) and runs detached via the
    # research loop (/autoresearch/trigger), not synchronously here.
    if not getattr(runner, "_ENABLE_SECTION_SCAFFOLD_DISCOVERY", False):
        return {
            "ok": True, "created": [], "created_count": 0,
            "skipped_existing": 0, "candidates_seen": 0, "ranked_drafted": 0,
            "note": "Section-Scaffolder ist aus. AR3-Substanzfunde liefert der "
                    "Research-Loop (Button „Research-Loop starten“).",
        }
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
# Code weakness finder (aux model, allowlisted, proposal-only)
# ---------------------------------------------------------------------------
def _repo_relative_name(path: Path) -> str:
    try:
        return path.resolve().relative_to(_REPO.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _code_allowlisted_repo_relative(rel: str) -> tuple[bool, str]:
    parts = Path(rel).parts
    if rel in _GATE_SELF_PROTECT:
        return False, "refused: code finder may not target the gate's own harness"
    if set(parts) & _CODE_ALLOWLIST_DENY_PARTS:
        return False, "refused: code finder target is in a denied path family"
    if not rel.endswith(".py"):
        return False, "refused: code finder target must be a Python file"
    if not parts or parts[0] not in _CODE_ALLOWLIST_DIRS:
        return False, "refused: code finder target is outside _CODE_ALLOWLIST_DIRS"
    return True, ""


def _code_allowlisted(path: Path) -> tuple[bool, str]:
    try:
        rp = path.resolve()
        rel = rp.relative_to(_REPO.resolve()).as_posix()
    except (ValueError, OSError):
        return False, f"refused: code finder target must live inside the repo ({_REPO})"
    return _code_allowlisted_repo_relative(rel)


def _iter_code_allowlist_paths() -> list[Path]:
    seen: set[Path] = set()
    paths: list[Path] = []
    for dirname in _CODE_ALLOWLIST_DIRS:
        base = _REPO / dirname
        for path in sorted(base.rglob("*.py")):
            try:
                rp = path.resolve()
            except OSError:
                continue
            if rp in seen or not rp.exists() or not rp.is_file():
                continue
            ok, _why = _code_allowlisted(rp)
            if not ok:
                continue
            seen.add(rp)
            paths.append(rp)
    paths.sort(key=_repo_relative_name)
    return paths


def _strip_llm_json_fence(text: str) -> str:
    body = re.sub(r"\A\s*<think>.*?</think>\s*", "", text or "", flags=re.I | re.S).strip()
    if body.startswith("```"):
        body = re.sub(r"\A```(?:json)?\s*", "", body, flags=re.I).strip()
        body = re.sub(r"\s*```\s*\Z", "", body).strip()
    return body


def _loads_llm_json(text: str) -> Any:
    body = _strip_llm_json_fence(text)
    try:
        return json.loads(body)
    except ValueError:
        start = body.find("{")
        end = body.rfind("}")
        if start >= 0 and end > start:
            return json.loads(body[start:end + 1])
        raise


def _extract_llm_content(resp) -> str:
    return (resp.choices[0].message.content or "").strip()


def _llm_total_tokens(resp: Any) -> int:
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, dict):
        usage = resp.get("usage")
    total_tokens = getattr(usage, "total_tokens", None)
    if total_tokens is None and isinstance(usage, dict):
        total_tokens = usage.get("total_tokens")
    try:
        return int(total_tokens) if total_tokens is not None else 0
    except (TypeError, ValueError):
        return 0


def _normalise_code_finding(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("finding") is None and not raw.get("findings"):
        return None
    if isinstance(raw.get("finding"), dict):
        return raw["finding"]
    findings = raw.get("findings")
    if isinstance(findings, list):
        for item in findings:
            if isinstance(item, dict):
                return item
    return None


def _validate_code_weakness_finding(
    finding: dict[str, Any],
    *,
    path: Path,
    before: str,
) -> tuple[dict[str, Any] | None, str | None]:
    category = str(finding.get("category") or "").strip()
    if category not in _CODE_WEAKNESS_CATEGORIES:
        return None, "unknown category"
    title = str(finding.get("title") or _CODE_WEAKNESS_CATEGORIES[category][1]).strip()
    problem = str(finding.get("problem") or "").strip()
    evidence = str(finding.get("evidence_quote") or finding.get("evidence") or "").strip()
    old_snippet = str(finding.get("old_snippet") or "").strip("\n")
    new_snippet = str(finding.get("new_snippet") or "").strip("\n")
    fix_hint = str(finding.get("fix_hint") or finding.get("fix_summary") or "").strip()
    if not problem or not evidence or not old_snippet or not new_snippet:
        return None, "missing required grounded fields"
    if evidence not in before:
        return None, "evidence quote is not verbatim in target"
    if old_snippet not in before:
        return None, "old snippet is not verbatim in target"
    if before.count(old_snippet) != 1:
        return None, "old snippet must match exactly once"
    if evidence not in old_snippet:
        return None, "evidence quote must be inside old snippet"
    if old_snippet == new_snippet:
        return None, "replacement is identical"
    after = before.replace(old_snippet, new_snippet, 1)
    if after == before:
        return None, "replacement produced no change"
    if "\0" in after:
        return None, "replacement contains NUL byte"
    ok, why = _code_allowlisted(path)
    if not ok:
        return None, why
    severity = _coerce_severity(finding.get("severity"), fallback=_CODE_CATEGORY_SEVERITY[category])
    return {
        "category": category,
        "severity": severity,
        "title": title[:120],
        "problem": problem[:800],
        "evidence": evidence[:800],
        "old_snippet": old_snippet,
        "new_snippet": new_snippet,
        "fix_hint": fix_hint[:800] or "Gezielten Code-Patch über den test-gated Proposal-Pfad anwenden.",
        "after_text": after,
    }, None


def _call_code_weakness_finder(path: Path, text: str, *, timeout: float) -> dict[str, Any]:
    rel = _repo_relative_name(path)
    system = (
        "Du bist ein vorsichtiger Code-Reviewer fuer Hermes. Finde hoechstens "
        "eine reale, kleine Code-Schwaeche in der gegebenen Python-Datei: "
        "Bug-Risiko, tote/unerreichbare Logik oder unklare Fehlerbehandlung. "
        "Antworte nur mit JSON. Erfinde nichts: evidence_quote und old_snippet "
        "muessen wortwoertlich im Code stehen. Wenn kein sicherer Fund vorliegt, "
        "antworte {\"finding\": null}. Keine Tests, keine Migrationen, keine "
        "grossen Refactors. Bewerte den Schweregrad ehrlich (severity): critical = "
        "Datenverlust/Absturz im Normalbetrieb, high = realer Bug-Pfad, medium = "
        "riskante Unklarheit, low = Nitpick."
    )
    user = (
        "JSON-Schema:\n"
        "{\n"
        "  \"finding\": {\n"
        "    \"category\": \"bug_risk|dead_logic|error_handling\",\n"
        "    \"severity\": \"critical|high|medium|low\",\n"
        "    \"severity_reason\": \"ein Satz: warum dieser Schweregrad\",\n"
        "    \"title\": \"kurzer Titel\",\n"
        "    \"problem\": \"warum das real riskant ist\",\n"
        "    \"evidence_quote\": \"kurzes wortwoertliches Zitat aus old_snippet\",\n"
        "    \"old_snippet\": \"exakter zu ersetzender Codeausschnitt, eindeutig\",\n"
        "    \"new_snippet\": \"vollstaendiger Ersatz fuer old_snippet\",\n"
        "    \"fix_hint\": \"knappe Reparaturabsicht\"\n"
        "  }\n"
        "}\n\n"
        f"Datei: {rel}\n\n"
        "Code:\n"
        "```python\n"
        f"{text}\n"
        "```"
    )
    from hermes_cli.autoresearch_budget import BudgetExhausted, guarded_llm_call

    try:
        resp, _entry = guarded_llm_call(
            lane="code",
            call=_writer_call_llm,
            task="skills_hub",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=2800,
            temperature=0.2,
            timeout=timeout,
        )
        return {
            "ok": True, "raw": _loads_llm_json(_extract_llm_content(resp)),
            "reason": None, "resp": resp, "ledger_entry": _entry,
        }
    except BudgetExhausted as exc:
        return {"ok": False, "raw": None, "reason": f"budget exhausted: {exc}", "resp": None}
    except Exception as exc:
        return {"ok": False, "raw": None, "reason": f"model call failed: {type(exc).__name__}", "resp": None}


def _verify_code_finding_importance(
    path: Path, finding: dict[str, Any], before: str, *, timeout: float
) -> dict[str, Any]:
    """Second, precision-focused lens (single LLM call) that asks whether a
    validated finding is a *real, important* defect — not a nitpick or
    false-positive. Returns ``{"real": bool, "reason": str, "tokens": int}``.

    Fail-open: any call/parse error returns ``real=True`` with a ``verify_error``
    reason so a flaky verifier never silently drops grounded findings. The
    Env-Schalter ``HERMES_AUTORESEARCH_VERIFY=0`` skips this pass entirely (the
    caller checks it; this function always runs when invoked)."""
    rel = _repo_relative_name(path)
    system = (
        "Du bist ein strenger Zweit-Reviewer. Ein Erst-Reviewer hat eine "
        "Code-Schwaeche gemeldet. Entscheide nur: ist das ein ECHTER, WICHTIGER "
        "Defekt — oder ein Nitpick / False-Positive? Sei streng: Stil, Kosmetik, "
        "hypothetische Randfaelle ohne realen Pfad = nicht wichtig. Antworte nur "
        "mit JSON {\"real\": true|false, \"reason\": \"<ein knapper Satz>\"}."
    )
    user = (
        f"Datei: {rel}\n"
        f"Kategorie: {finding.get('category')}\n"
        f"Gemeldeter Schweregrad: {finding.get('severity')}\n"
        f"Problem: {finding.get('problem')}\n"
        f"Beleg (woertlich aus dem Code): {finding.get('evidence')}\n\n"
        "Betroffener Codeausschnitt:\n"
        "```python\n"
        f"{finding.get('old_snippet')}\n"
        "```"
    )
    from hermes_cli.autoresearch_budget import BudgetExhausted, guarded_llm_call

    try:
        resp, ledger_entry = guarded_llm_call(
            lane="code",
            call=_writer_call_llm,
            task="skills_hub",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=300,
            temperature=0.1,
            timeout=timeout,
        )
        tokens = int(ledger_entry.get("total_tokens") or 0) or _llm_total_tokens(resp)
        usage_source = str(ledger_entry.get("usage_source") or "unknown")
        raw = _loads_llm_json(_extract_llm_content(resp))
        if not isinstance(raw, dict) or "real" not in raw:
            return {"real": True, "reason": "verify_error: unparseable verdict",
                    "tokens": tokens, "usage_source": usage_source}
        return {
            "real": bool(raw.get("real")),
            "reason": str(raw.get("reason") or "")[:300],
            "tokens": tokens,
            "usage_source": usage_source,
        }
    except BudgetExhausted as exc:
        # Fail CLOSED: without the mandatory importance gate no proposal may
        # be saved — the caller stops the scan and re-scans the file later.
        return {
            "real": False,
            "reason": f"budget exhausted: {exc}",
            "tokens": 0,
            "usage_source": "measured",
            "budget_exhausted": True,
        }
    except Exception as exc:
        return {"real": True, "reason": f"verify_error: {type(exc).__name__}", "tokens": 0,
                "usage_source": "measured"}


def _proposal_id_for_code_finding(path: Path, finding: dict[str, Any]) -> str:
    rel = _repo_relative_name(path)
    digest = hashlib.sha1(
        "\0".join([
            rel,
            str(finding.get("category") or ""),
            str(finding.get("evidence") or ""),
            str(finding.get("old_snippet") or ""),
        ]).encode("utf-8")
    ).hexdigest()[:10]
    return f"code-weakness-{_slug(rel)}-{digest}"


def _deep_audit_target_from_fileline(fileline: str) -> tuple[str, Path]:
    match = re.match(r"^(.+?):\d+(?::\d+)?$", str(fileline or "").strip())
    rel = match.group(1) if match else "unknown"
    return rel, (_REPO / rel).resolve()


def _proposal_id_for_deep_audit_finding(finding: dict[str, Any]) -> str:
    fileline = str(finding.get("fileline") or "")
    digest = hashlib.sha1(
        "\0".join([
            fileline,
            str(finding.get("category") or ""),
            str(finding.get("evidence") or ""),
        ]).encode("utf-8")
    ).hexdigest()[:10]
    return f"deep-audit-{_slug(fileline)}-{digest}"


def _build_deep_audit_proposal(finding: dict[str, Any]) -> dict[str, Any]:
    """Build a detection-only code proposal from a read-only Deep-Audit finding."""
    fileline = str(finding.get("fileline") or "").strip()
    rel, target_path = _deep_audit_target_from_fileline(fileline)
    severity = _coerce_severity(finding.get("severity"), fallback="medium")
    category = str(finding.get("category") or "bug_risk").strip() or "bug_risk"
    evidence = str(finding.get("evidence") or "").strip()
    title = str(finding.get("title") or "Deep-Audit-Befund").strip()
    problem = str(finding.get("problem") or "Deep-Audit hat einen manuellen Prüffund gemeldet.").strip()
    fix_hint = str(finding.get("fix_hint") or "Manuell prüfen und gezielt beheben.").strip()
    model_label = str(finding.get("_model_label") or "").strip() or _model_label_from_response(None)
    rank_score = float(_SEVERITY_ORDINAL[severity] * 10)
    return {
        "id": _proposal_id_for_deep_audit_finding(finding),
        "schema": PROPOSAL_SCHEMA,
        "mode": "code",
        "proposal_type": "deep_audit",
        "target": rel,
        "target_path": str(target_path),
        "section": None,
        "eval_label": None,
        "category": category,
        "severity": severity,
        "evidence": evidence,
        "fix_hint": fix_hint,
        "title": f"Deep-Audit in {fileline}: {title}",
        "rationale_plain": f"{problem} Grounding: „{evidence}“",
        "before_text": None,
        "after_text": None,
        "new_text": None,
        "writer": "aux-deep-audit",
        "writer_rationale": f"{model_label} via code_audit; read-only subsystem audit with verbatim evidence validation.",
        "diff_before_after": "",
        "status": "proposed",
        "last_outcome": None,
        "created_at": _utc_now(),
        "applied_at": None,
        "result": None,
        "apply_blocked_reason": "Deep-Audit-Befund — Fix manuell",
        "rank_score": rank_score,
        "rank_reason": f"{_SEVERITY_LABELS[severity]}; read-only Deep-Audit finding; manual fix required",
    }


def _build_code_weakness_proposal(path: Path, before: str, finding: dict[str, Any]) -> dict[str, Any]:
    rel = _repo_relative_name(path)
    category = str(finding["category"])
    weight, category_label = _CODE_WEAKNESS_CATEGORIES[category]
    severity = _coerce_severity(finding.get("severity"), fallback=_CODE_CATEGORY_SEVERITY[category])
    # Severity dominates ordering; the category weight is only a within-tier tiebreaker.
    rank_score = float(_SEVERITY_ORDINAL[severity] * 10 + weight)
    evidence = str(finding["evidence"])
    after = str(finding["after_text"])
    title = str(finding.get("title") or category_label)
    problem = str(finding.get("problem") or "Das Aux-Modell hat eine konkrete Code-Schwäche gefunden.")
    fix_hint = str(finding.get("fix_hint") or "Gezielt beheben.")
    model_label = str(finding.get("_model_label") or "").strip() or _model_label_from_response(None)
    return {
        "id": _proposal_id_for_code_finding(path, finding),
        "schema": PROPOSAL_SCHEMA,
        "mode": "code",
        "proposal_type": "code_weakness",
        "target": rel,
        "target_path": str(path.resolve()),
        "section": None,
        "eval_label": None,
        "category": category,
        "severity": severity,
        "evidence": evidence,
        "fix_hint": fix_hint,
        "title": f"Code-Schwäche in {rel}: {title}",
        "rationale_plain": f"{problem} Grounding: „{evidence}“",
        "before_text": before,
        "after_text": after,
        "new_text": None,
        "writer": "aux-code-weakness-finder",
        "writer_rationale": f"{model_label} via skills_hub; verbatim old_snippet/evidence validated before proposal save.",
        "diff_before_after": _make_diff(before, after, rel),
        "status": "proposed",
        "last_outcome": None,
        "created_at": _utc_now(),
        "applied_at": None,
        "result": None,
        "rank_score": rank_score,
        "rank_reason": f"{_SEVERITY_LABELS[severity]}; {category_label}; allowlisted code target; full test-suite gate on apply",
    }


def generate_code_weakness_proposals(*, limit: int = 3, timeout: float = 120.0,
                                     scope: str = "incremental", max_files: int = 12) -> dict[str, Any]:
    """Find real code weaknesses in the repo allowlist and persist them as
    ``mode='code'`` proposals. The finder never writes target files; applying a
    saved proposal uses the existing backup → write → test-suite gate path.

    ``scope='incremental'`` (default) scans only files whose content changed since
    the last scan (or were never scanned), up to ``max_files`` — so a click is fast
    and repeated clicks walk the allowlist. ``scope='full'`` scans everything (the
    historical behaviour). Both modes refresh the content-hash state."""
    incremental = scope != "full"
    verify_enabled = os.environ.get("HERMES_AUTORESEARCH_VERIFY", "1") != "0"
    state = _read_code_scan_state()
    created: list[str] = []
    errors: list[dict[str, str]] = []
    vetoes: list[dict[str, str]] = []
    skipped_existing = 0
    skipped_unchanged = 0
    vetoed = 0
    detection_only = 0  # high+-Intake-Gate: medium/low geloggt, nicht gequeued (H3)
    files_seen = 0
    findings_seen = 0
    tokens = 0
    model_label = ""
    budget_stop: str | None = None
    estimated_any = False
    cap = max(1, int(max_files))
    allowlist_paths = _iter_code_allowlist_paths()
    for path in allowlist_paths:
        if len(created) >= max(1, int(limit)):
            break
        if incremental and files_seen >= cap:
            break
        rel = _repo_relative_name(path)
        try:
            before = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append({"target": rel, "reason": f"read failed: {type(exc).__name__}"})
            continue
        sha = _content_sha(before)
        if incremental and state.get(rel) == sha:
            skipped_unchanged += 1
            continue
        files_seen += 1
        state[rel] = sha  # mark scanned — won't re-scan until the file content changes
        if not before.strip() or len(before) > _CODE_FINDER_MAX_FILE_CHARS:
            errors.append({"target": rel, "reason": "file empty or too large for code finder"})
            continue
        model_res = _call_code_weakness_finder(path, before, timeout=timeout)
        model_label = _model_label_from_response(model_res.get("resp"))
        ledger_entry = model_res.get("ledger_entry") or {}
        tokens += int(ledger_entry.get("total_tokens") or 0) or _llm_total_tokens(model_res.get("resp"))
        if str(ledger_entry.get("usage_source") or "") == "estimated":
            estimated_any = True
        if not model_res.get("ok"):
            reason = str(model_res.get("reason") or "model failed")
            if reason.startswith("budget exhausted"):
                # Shared daily ledger spent: stop scanning; the file was not
                # researched, so let the next run pick it up again.
                state.pop(rel, None)
                files_seen -= 1
                budget_stop = reason
                break
            errors.append({"target": rel, "reason": reason})
            continue
        raw_finding = _normalise_code_finding(model_res.get("raw"))
        if raw_finding is None:
            continue
        for _idx in range(_CODE_FINDER_MAX_FINDINGS_PER_FILE):
            valid, reason = _validate_code_weakness_finding(raw_finding, path=path, before=before)
            if valid is None:
                errors.append({"target": rel, "reason": reason or "invalid finding"})
                break
            valid["_model_label"] = model_label
            findings_seen += 1
            if verify_enabled:
                verdict = _verify_code_finding_importance(path, valid, before, timeout=timeout)
                tokens += int(verdict.get("tokens") or 0)
                if str(verdict.get("usage_source") or "") == "estimated":
                    estimated_any = True
                if verdict.get("budget_exhausted"):
                    # Importance gate could not run: stop the scan, drop the
                    # file from the scanned state so the next run redoes it,
                    # and never save an ungated proposal.
                    state.pop(rel, None)
                    budget_stop = str(verdict.get("reason") or "budget exhausted")
                    break
                if not verdict.get("real"):
                    vetoed += 1
                    vetoes.append({"target": rel, "reason": str(verdict.get("reason") or "not important")})
                    break
            proposal = _build_code_weakness_proposal(path, before, valid)
            existing = load_proposal(proposal["id"])
            if existing and existing.get("status") in _VALID_STATUS:
                skipped_existing += 1
                break
            if not meets_intake_threshold(proposal):
                # medium/low → detection-only: geloggt, aber nicht in die Queue.
                detection_only += 1
                break
            save_proposal(proposal)
            created.append(proposal["id"])
            break
        if budget_stop:
            break
    _write_code_scan_state(state)
    result = {
        "ok": True,
        "created": created,
        "created_count": len(created),
        "skipped_existing": skipped_existing,
        "skipped_unchanged": skipped_unchanged,
        "vetoed": vetoed,
        "detection_only": detection_only,
        "files_seen": files_seen,
        "findings_seen": findings_seen,
        "tokens": tokens,
        "usage_source": "estimated" if estimated_any else "measured",
        "scope": "incremental" if incremental else "full",
        "allowlist": sorted(_repo_relative_name(p) for p in allowlist_paths),
        "errors": errors,
        "vetoes": vetoes,
        "budget_stop": budget_stop,
    }
    from hermes_cli.autoresearch_lane_contracts import classify_lane_outcome

    reason_text = "; ".join(str(item.get("reason") or "") for item in errors[:3])
    if budget_stop:
        reason_text = budget_stop if not reason_text else f"{budget_stop}; {reason_text}"
    try:
        lane_outcome = classify_lane_outcome(
            "code",
            scanned=files_seen,
            errors=len(errors),
            yielded=findings_seen,
            ok=True,
            reason=reason_text,
        )
        result["outcome"] = lane_outcome.outcome
        result["ok"] = not lane_outcome.fatal
    except Exception as exc:
        result["outcome"] = "invalid_output"
        result["outcome_reason"] = f"lane contract invalid: {type(exc).__name__}"
        result["ok"] = False
    try:  # P2: best-effort ROI log for the code lane (never sink the scan)
        from hermes_cli import autoresearch_runs
        autoresearch_runs.append_run(lane="code", tokens=tokens, proposed=len(created),
                                     errors=len(errors), vetoed=vetoed, scanned=files_seen,
                                     model=model_label or None,
                                     usage_source=result["usage_source"])
    except Exception:
        pass
    try:  # zero-yield cooldown bookkeeping (best-effort, never sinks the scan)
        from hermes_cli.autoresearch_budget import record_lane_run_for_cooldown
        record_lane_run_for_cooldown(
            "code",
            outcome=str(result.get("outcome") or ""),
            yielded=findings_seen,
            healthy_calls=max(0, files_seen - len(errors)),
        )
    except Exception:
        pass
    return result


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
    # Ein altes failed/crashed gate.phase aus einem frueheren Lauf neutral stempeln,
    # damit das uebersprungene Proposal nicht als Gate-Zombie "rot" weiterzaehlt.
    gate = proposal.get("gate")
    if isinstance(gate, dict) and gate.get("phase") in {"failed", "crashed", "running"}:
        gate = dict(gate)
        gate["phase"] = "skipped"
        proposal["gate"] = gate
    save_proposal(proposal)
    return {"ok": True, "status": "skipped", "id": pid}


def apply_proposal(pid: str, *, confirm: bool = True, judged: bool = False) -> dict[str, Any]:
    """Apply exactly this proposal: backup → write after_text → eval-gate →
    keep (status=applied) or auto-revert (status stays proposed).

    ``judged`` is set only by the batch-confirm caller, which has already run
    ``judge_fix`` on a capability_research fix. An AR3 grounded fix is therefore
    written ONLY through the judged batch path — a direct single-apply call
    (``judged=False``) refuses it, honouring the signed decision "Skills =
    Judge + Batch-Confirm". Scaffold/code proposals are unaffected by ``judged``.
    """
    proposal = load_proposal(pid)
    if proposal is None:
        return {"ok": False, "detail": f"no such proposal: {pid}", "status": None}
    if proposal.get("status") != "proposed":
        return {"ok": False, "detail": f"proposal is '{proposal.get('status')}', not actionable",
                "status": proposal.get("status")}
    if not confirm:
        return {"ok": False, "detail": "apply requires confirm=true (the operator 'are you sure' step)",
                "status": "proposed"}
    if proposal.get("apply_blocked_reason"):
        return {"ok": False,
                "detail": proposal["apply_blocked_reason"],
                "status": proposal.get("status", "proposed")}
    # AR3 capability_research proposals: a *detection-only* finding carries an
    # explicit apply_blocked_reason and stays read-only. A *grounded fix* finding
    # carries a full replacement after_text (drafted by draft_fix) and IS written
    # here — behind a backup — via the replace-file path, NOT the section-append/
    # scaffold-eval path below. It must be judge-gated: only the batch-confirm
    # caller (judged=True) may write it; a direct single-apply is refused.
    if proposal.get("proposal_type") == "capability_research":
        if not judged:
            return {"ok": False,
                    "detail": "AR3-Fix nur über Batch-Confirm (judge-gated) anwendbar",
                    "status": proposal.get("status", "proposed")}
        return _apply_capability_fix(proposal, pid)

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
        proposal["last_outcome"] = "applied"
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
    proposal["last_outcome"] = "reverted_no_improvement"
    save_proposal(proposal)
    return {"ok": False, "status": "proposed", "id": pid,
            "detail": proposal["result"], "reverted": True, "eval_result": eval_result}


def _apply_capability_fix(proposal: dict[str, Any], pid: str) -> dict[str, Any]:
    """Write an AR3 grounded skill fix: backup → replace file with after_text → keep.

    Unlike the scaffold path this REPLACES the whole skill (the fix is a full
    rewrite, not an appendable section) and carries no section eval-gate — the
    substantive gate is ``judge_fix``, already passed in ``confirm_batch_proposals``
    before we get here. Reversible by construction via the backup dir.
    """
    runner = _runner()
    skills_root = runner._skills_root()
    target_path = Path(proposal.get("target_path", ""))
    before_text = proposal.get("before_text")
    after_text = proposal.get("after_text")
    if not isinstance(before_text, str) or not isinstance(after_text, str):
        return {"ok": False, "detail": "proposal malformed (missing before_text/after_text)",
                "status": "proposed"}
    if after_text == before_text:
        return {"ok": False, "detail": "no-op fix (after == before)", "status": "proposed"}
    if not target_path.exists():
        return {"ok": False, "detail": f"target no longer exists: {target_path}", "status": "proposed"}
    if not runner._under(target_path, skills_root):
        return {"ok": False,
                "detail": f"refused: target not under skills root ({skills_root})",
                "status": "proposed"}
    # Stale-guard: the fix was drafted+judged against before_text. If the skill
    # changed under us, don't clobber it — kick back to the operator to regenerate.
    current = target_path.read_text(encoding="utf-8")
    if current != before_text:
        return {"ok": False,
                "detail": "Skill seit dem Entwurf geändert — Fix neu erzeugen",
                "status": "proposed"}

    backup_dir = (runner._hermes_home() / "backups"
                  / f"skills-before-proposal-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{_slug(pid)[:16]}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    runner._backup_file(target_path, skills_root, backup_dir)

    target_path.write_text(after_text, encoding="utf-8")
    proposal["status"] = "applied"
    proposal["last_outcome"] = "applied"
    proposal["result"] = "✓ übernommen — AR3-Fix geschrieben (judge-bestätigt)"
    proposal["applied_at"] = _utc_now()
    proposal["backup_dir"] = str(backup_dir)
    save_proposal(proposal)
    return {"ok": True, "status": "applied", "id": pid, "result": proposal["result"]}


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
    if proposal.get("proposal_type") == "code_weakness":
        ok, why = _code_allowlisted(target_path)
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
        proposal["last_outcome"] = "applied"
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
    proposal["last_outcome"] = "reverted_no_improvement"
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
    # last_outcome stempeln, damit ein wiederholt abgebrochenes Gate in den
    # prune-Auto-Skip-Filter faellt (sonst akkumulieren crashed-Proposals unbegrenzt).
    proposal["last_outcome"] = "reverted_no_improvement"
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
        "last_outcome": None,
        "created_at": _utc_now(),
        "applied_at": None,
        "result": None,
        "rank_score": None,
        "rank_reason": None,
    }
    save_proposal(proposal)
    return proposal
