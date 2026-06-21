"""Vision-Flywheel Phase 2 — the **Strategist harness** (I1).

This is the *repo-side* logic the operator's ``strategist-cron`` invokes via
``claude -p --model claude-opus-4-8``. The cron wiring (systemd unit + crontab)
is OPERATOR work and lives outside this repo; this module is the callable core
plus a documented call contract (see ``docs/vision-strategist-harness.md``).

Two modes, both self-contained, deterministic and side-effect-bounded:

``propose`` (``hermes vision strategist --mode propose``)
    1. BUDGET-SKIP — if weekly subscription usage > 80 % (``agent.account_usage``)
       the run ends without proposing. Budget discipline before any work.
    2. Reads the distilled vision metrics (H1, ``~/.hermes/state/vision-metrics.json``)
       and the Heiler escalation ledger (Phase 1, :func:`kanban_db.read_escalation_ledger`).
    3. Derives ROI-positive *levers* across the broad Vision corridor — autonomy
       metrics, Heiler root-causes, gate stability — each a candidate PlanSpec
       carrying a target metric, an ROI estimate and a *paired counter-metric*.
    4. SELF-GATE — a cheap, deterministic ROI- *and* counter-metric self-check
       per draft. Only survivors surface. A lever whose guardrail risk exceeds
       the counter budget is dropped (a blunt lever that can't bound its
       counter-metric is correctly refused).
    5. CAP 3–5 per run; idle is allowed (0 specs when nothing is ROI-positive —
       idle is the correct outcome, not a failure).
    6. Ingests the survivors with ``freigabe: operator`` + ``created_by =
       strategist-cron`` so they pass the Phase-1 hardener (rubric + Sonnet
       judge) and land *held* on the G1 strategist surface for operator triage.

``reflect`` (``hermes vision strategist --mode reflect``)
    Scores the strategist's own proposals approved-vs-vetoed since local
    midnight (plus shipped ROI = approved chains that completed) and updates the
    learning notes. Vetoed levers feed the reflection: their keys are recorded
    and *suppressed* on subsequent propose runs — the operator's veto teaches
    the strategist what not to re-raise. That closed loop is the self-improvement.

Division of labour (encoded in the call contract, enforced by structure here):
the **deterministic baseline** in this module guarantees the harness works
headless even with no LLM judgement. When wrapped by ``claude -p`` the Opus
strategist supplies *richer, judged* lever drafts through ``--drafts-file`` (the
same self-gate + cap + provenance + annotation rails apply uniformly), and
delegates heavy code/receipt reads to Sonnet subagents — Opus only judges.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db, planspecs, strategist_surface
from hermes_cli import vision_metrics

logger = logging.getLogger(__name__)

# Audit author stamped on every drafted root. The G1 surface does not filter on
# created_by (the freigabe:operator + scheduled filter is the root-guard), but
# reflect *does* — it scopes its approved/vetoed tally to this author so it never
# reflects on a human-ingested operator hold. Single source of truth lives in
# kanban_db (the silent-block strategist carve-out reads the same constant) so
# the writer and the carve-out can never drift apart.
STRATEGIST_AUTHOR = kanban_db.STRATEGIST_CREATED_BY

# GREEN-GATE-AUTOHEAL-LOOP-S1: a distinct author for the auto-opened nightly
# gate-fix specs. Kept separate from STRATEGIST_AUTHOR so ``reflect`` (which
# scopes its approved/vetoed tally to STRATEGIST_AUTHOR) never reflects on an
# autoheal hold. Like the strategist, the held spec still surfaces on the G1
# operator surface (which gates on freigabe:operator + scheduled, not author).
GATE_FIX_AUTHOR = "green-gate-autoheal"

# Re-export the default streak threshold so the CLI layer has one import site.
GATE_FIX_MIN_NIGHTS = vision_metrics.GATE_FIX_MIN_NIGHTS

# Self-gate budgets (cheap, deterministic). A lever passes iff it has a paired
# counter-metric, a positive ROI score, and a guardrail risk within budget.
CAP_MAX = 5
COUNTER_BUDGET = 0.5
LEDGER_MIN_COUNT = 1  # a Heiler class needs >= this many escalations to drive a lever

# Vision-metric targets that open a lever when the live metric falls short.
AUTONOMY_TARGET = 90.0  # autonomy_pct
GATE_STREAK_TARGET = 7  # green_gate_streak

# COST-AWARENESS-S1: a lane whose effective burn (real $ + API-equivalent of the
# subscription token burn, ``kanban_db.runs_costs``) over the window exceeds this
# threshold opens a cost-efficiency lever. Effective cost only became a real
# signal once the cost-visibility backfill stamped cost_usd_equivalent for the
# subscription lanes (Codex/claude) — before that every lane read $0 and the
# strategist was cost-blind. The signal is capped so one very hot lane cannot
# crowd every other lever out of the cap.
COST_WINDOW_DAYS = 7
COST_LANE_THRESHOLD_USD = 25.0
COST_SIGNAL_CAP = 3.0

# Weekly-usage skip threshold (percent of the subscription window consumed).
BUDGET_THRESHOLD = 80.0
BUDGET_PROVIDER = "anthropic"  # the strategist runs on the Opus subscription lane

# RECEIPT-HARVEST (separater --mode harvest): Fenster/Caps für die Receipt-Ernte.
HARVEST_WINDOW_FALLBACK_SECONDS = 48 * 3600
HARVEST_MAX_RECEIPTS = 30
HARVEST_MIN_RECEIPT_CHARS = 200
HARVEST_MAX_LEVERS = 3  # Sub-Cap: höchstens so viele Follow-ups pro Lauf


# --------------------------------------------------------------------------- #
# Lever model + deterministic catalogue
# --------------------------------------------------------------------------- #
@dataclass
class Lever:
    """A candidate ROI lever the strategist may propose as a held PlanSpec."""

    key: str
    title: str
    lane: str
    target_metric: str
    roi: str
    counter_metric: str
    rationale: str
    # Self-gate inputs (deterministic):
    gain_weight: float
    cost: float
    counter_risk: float
    signal_strength: float = 1.0
    # Grounding evidence (STRATEGIST-SELF-GROUNDING-S1): the Opus propose-prompt
    # greps code + git log per lever (does the target already exist / was it
    # shipped) and emits this non-empty evidence string. The code does NOT judge
    # it — :func:`grounding_gate` only enforces PRESENCE on the draft path. Empty
    # for the deterministic baseline levers (which never traverse that gate).
    grounding: str = ""
    source: str = "baseline"

    @property
    def roi_score(self) -> float:
        """Cheap expected-return score: signal*gain minus a fixed lever cost."""
        return round(self.signal_strength * self.gain_weight - self.cost, 4)


@dataclass
class GateResult:
    passed: bool
    reason: str


@dataclass
class _LedgerTemplate:
    key: str
    title: str
    target_metric: str
    roi: str
    counter_metric: str
    rationale: str
    gain_weight: float
    cost: float
    counter_risk: float


# Heiler-class → lever template. The escalation ledger's per-class distinct-root
# count (``roots_by_class``) is the live signal; a class only drives a lever when
# at least one distinct root actually escalated into it.
_LEDGER_TEMPLATES: dict[str, _LedgerTemplate] = {
    kanban_db.HEILER_CLASS_TRANSIENT: _LedgerTemplate(
        key="HEILER-TRANSIENT",
        title="Heiler: transiente Fehlklasse schneller und sauberer retrien",
        target_metric="transient-Eskalationen pro Tag (Ledger by_class.transient) um mindestens 30 Prozent senken",
        roi="hoch: transiente Fails blockieren Ketten ohne echten Defekt; ein gezielter Retry-Budget-Tune spart Re-Dispatch-Latenz",
        counter_metric="Duplicate-Build-Rate darf nicht steigen (kein Doppel-Dispatch durch zu aggressiven Retry)",
        rationale="Die Heiler-Ledger zeigt wiederholte transiente Eskalationen. Ein engeres, idempotenz-gesichertes Retry-Budget bringt Ketten ohne menschlichen Eingriff zurueck in den Fluss.",
        gain_weight=1.0,
        cost=0.5,
        counter_risk=0.2,
    ),
    kanban_db.HEILER_CLASS_FLAKY: _LedgerTemplate(
        key="HEILER-FLAKY",
        title="Heiler: flaky Failures quarantaenen statt Kette stallen",
        target_metric="flaky-Eskalationen pro Tag (Ledger by_class.flaky) um mindestens 30 Prozent senken",
        roi="mittel-hoch: flaky Gates kosten Re-Runs und Vertrauen; eine sichtbare Quarantaene haelt den Fluss am Laufen",
        counter_metric="echte Regressionen duerfen nicht maskiert werden (jede Quarantaene bleibt im Dashboard sichtbar und ablaufgebunden)",
        rationale="Wiederkehrende flaky-Eskalationen stallen Ketten. Eine sichtbare, zeitlich begrenzte Quarantaene trennt Flake von echtem Defekt, ohne Signale zu verstecken.",
        gain_weight=1.0,
        cost=0.5,
        counter_risk=0.35,
    ),
    kanban_db.HEILER_CLASS_REAL_BUG: _LedgerTemplate(
        key="HEILER-REALBUG",
        title="Pre-Dispatch-Gate fuer die haeufigsten roten Gates haerten",
        target_metric="real-bug-Eskalationen pro Tag (Ledger by_class.real-bug) um mindestens 25 Prozent senken",
        roi="hoch: echte rote Gates spaet im Lauf sind teuer; ein frueher gezielter Vor-Check faengt sie billiger",
        counter_metric="Dispatch-Latenz und Gate-Laufzeit duerfen das Budget nicht sprengen",
        rationale="Die Ledger weist echte Defekte als haeufigste Eskalationsklasse aus. Ein gezielter Vor-Dispatch-Check auf die Top-Fehlermuster verschiebt das Fangen nach links.",
        gain_weight=1.2,
        cost=0.5,
        counter_risk=0.3,
    ),
    kanban_db.HEILER_CLASS_BAD_SPEC: _LedgerTemplate(
        key="HEILER-BADSPEC",
        title="Spec-Haerter-Rubrik um die haeufigsten bad-spec-Muster erweitern",
        target_metric="bad-spec-Eskalationen pro Tag (Ledger by_class.bad-spec) um mindestens 30 Prozent senken",
        roi="hoch: ein unmoeglicher Spec verbrennt einen ganzen Worker-Lauf; die Rubrik faengt ihn vor dem Dispatch",
        counter_metric="Idle- und Falsch-Block-Rate des Haerters darf nicht steigen (kein Ueber-Blocken valider Specs)",
        rationale="Die Ledger zeigt wiederkehrende bad-spec-Eskalationen. Die deterministische Rubrik um diese konkreten Muster zu erweitern verhindert den teuersten Lauf-Typ.",
        gain_weight=1.2,
        cost=0.5,
        counter_risk=0.3,
    ),
    kanban_db.HEILER_CLASS_CONFLICT: _LedgerTemplate(
        key="HEILER-CONFLICT",
        title="Worktree-Isolation gegen Merge-Konflikt-Parks verstaerken",
        target_metric="conflict-Eskalationen pro Tag (Ledger by_class.conflict) um mindestens 30 Prozent senken",
        roi="mittel-hoch: geparkte Konflikt-Ketten brauchen einen Operator; bessere Isolation haelt sie autonom",
        counter_metric="Worktree-Setup-Kosten (Disk und Zeit) duerfen nicht explodieren",
        rationale="Merge-Konflikt-Parks tauchen wiederholt in der Ledger auf. Strengere Pro-Task-Worktree-Isolation reduziert die Ueberlappung, die die Konflikte erzeugt.",
        gain_weight=1.0,
        cost=0.5,
        counter_risk=0.3,
    ),
}


def _autonomy_lever(gap: float) -> Lever:
    """Blunt 'raise autonomy' lever — high leverage but unbounded guardrail.

    Intentionally carries a counter-risk ABOVE budget: a vague, broad autonomy
    push cannot bound its operator-error guardrail, so the self-gate refuses it.
    This is the deterministic 'counter-metric loser'.
    """
    return Lever(
        key="AUTON-UPLIFT",
        title="Autonomie-Quote breit anheben",
        lane="coder-claude",
        target_metric=f"autonomy_pct Richtung {AUTONOMY_TARGET:.0f} Prozent anheben",
        roi="potenziell hoch, aber diffus: 'mehr Autonomie' ohne konkreten Mechanismus",
        counter_metric="Operator-Eskalations- und Fehlerrate darf nicht steigen",
        rationale="Die Autonomie-Quote liegt unter Ziel. Ein breiter, unspezifischer Hebel kann seine Fehlerraten-Guardrail jedoch nicht beschraenken.",
        gain_weight=0.4,
        cost=0.5,
        counter_risk=0.6,
        signal_strength=gap,
        source="metric",
    )


def _gate_stability_lever(gap: float) -> Lever:
    return Lever(
        key="GATE-STABILITY",
        title="Gruene-Gate-Straehne stabilisieren (Flake-Quellen im Nacht-Gate schliessen)",
        lane="coder-claude",
        target_metric=f"green_gate_streak Richtung {GATE_STREAK_TARGET} aufeinanderfolgende gruene Naechte heben",
        roi="mittel-hoch: eine stabile Gate-Straehne ist die Vertrauensbasis fuer autonomen Push und Deploy",
        counter_metric="Gate-Laufzeit und Kosten duerfen nicht steigen",
        rationale="Die gruene-Gate-Straehne liegt unter Ziel. Die konkreten Flake-Quellen im Nacht-Gate zu schliessen hebt die Straehne ohne neue Kosten.",
        gain_weight=0.4,
        cost=0.5,
        counter_risk=0.35,
        signal_strength=gap,
        source="metric",
    )


def _gate_fix_lever(cause: dict[str, Any]) -> Lever:
    """Build the held fix-PlanSpec lever for a recurring red nightly gate.

    Driven by :func:`vision_metrics.derive_consecutive_red_cause`. The lever's
    identity (``key`` → PlanSpec ``slice`` → spec filename) is a pure function of
    the STABLE cause (gate + first_fail fingerprint), so re-running on a third /
    fourth red night of the same cause renders byte-identical markdown and the
    ingest dedups (``already_ingested``) instead of spamming a second chain
    (AC-2). A *different* cause produces a different key → a fresh chain. The
    volatile night-count / raw detail are deliberately NOT rendered, so the
    content hash that backs idempotency cannot drift while the streak grows.
    """
    gate = str(cause.get("gate") or "unknown").strip().lower() or "unknown"
    fingerprint = str(cause.get("fingerprint") or gate)
    token = _cost_lane_token(gate) or "UNKNOWN"
    # Short, stable digest of the fingerprint so two distinct causes on the SAME
    # gate get distinct keys (no false supersede-conflict) while an identical
    # cause keys identically. Deterministic (sha1) — safe for idempotency.
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:8]
    key = f"GATE-FIX-{token}-{digest}"
    return Lever(
        key=key,
        title=f"Naechtlichen green-gate-Fehler am Gate '{gate}' beheben",
        lane="coder-claude",
        target_metric=(
            f"green_gate_streak wieder aufbauen, indem die seit mindestens "
            f"{GATE_FIX_MIN_NIGHTS} aufeinanderfolgenden Naechten am Gate '{gate}' "
            "wiederkehrende rote Ursache geschlossen wird, sodass der naechtliche "
            "green-gate-Heartbeat wieder gruen laeuft"
        ),
        roi=(
            "hoch: ein seit mehreren Naechten roter green-gate ist die "
            "Vertrauensbasis fuer autonomen Push und Deploy; die wiederkehrende "
            "Ursache gezielt zu fixen stellt die gruene Straehne wieder her"
        ),
        counter_metric=(
            "kein neuer Flake und keine maskierte Regression: die Ursache muss "
            "wirklich geschlossen sein (betroffenes Gate laeuft lokal gruen via "
            "scripts/run-affected.sh), nicht das Symptom unterdrueckt oder ein "
            "Test geskippt werden"
        ),
        rationale=(
            f"Der naechtliche green-gate-Heartbeat ist an mindestens "
            f"{GATE_FIX_MIN_NIGHTS} aufeinanderfolgenden Naechten am Gate '{gate}' "
            f"mit derselben Ursache rot (first_fail-Fingerprint: {fingerprint}). "
            "Diese HELD, operator-gated Fix-PlanSpec wurde automatisch eroeffnet, "
            "damit die Ursache nicht unbemerkt auf green_gate_streak=0 stehen "
            "bleibt, bis der woechentliche Strategen-Lauf sie aufgreift. Vorgehen: "
            "die rote Ursache aus dem green-gate-ledger reproduzieren, den Defekt "
            "beheben, und das betroffene Gate lokal gruen fahren."
        ),
        gain_weight=1.0,
        cost=0.5,
        counter_risk=0.3,
        signal_strength=1.0,
        source="gate-autoheal",
    )


def _cost_lane_token(name: str) -> str:
    """Uppercase, slug-safe token for a lane name (drops spaces/parens so the
    lever key round-trips through ``PlanSpec <KEY>:`` titles and filenames)."""
    out = "".join(c if c.isalnum() else "-" for c in str(name).upper())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def _cost_efficiency_lever(lane: str, burn: float, key: str) -> Lever:
    """Concrete, counter-bounded lever to cut the costliest lane's burn.

    Unlike the blunt autonomy lever this is specific (one named lane, a 20 %
    target, a throughput guardrail) so it bounds its counter-metric and passes
    the self-gate. ``signal_strength`` scales with the burn (capped) so a hotter
    lane ranks higher without swamping the cap.
    """
    signal = min(burn / COST_LANE_THRESHOLD_USD, COST_SIGNAL_CAP)
    return Lever(
        key=key,
        title=f"Kosten-Effizienz: teuerste Lane '{lane}' entlasten",
        lane="coder-claude",
        target_metric=(
            f"cost_effective_usd der Lane '{lane}' (aktuell ~${burn:.0f} pro "
            f"{COST_WINDOW_DAYS}-Tage-Fenster, API-Aequivalent des Abo-Verbrauchs) "
            "um mindestens 20 Prozent senken"
        ),
        roi=(
            f"hoch: '{lane}' ist der groesste Aequivalent-Brenner im "
            f"{COST_WINDOW_DAYS}-Tage-Fenster; eine gezielte Token-/Kontext-/"
            "Routing-Straffung senkt den Abo-Verbrauch mit dem groessten Hebel"
        ),
        counter_metric=(
            "Durchsatz und Erfolgsquote der Lane duerfen nicht fallen "
            "(keine Qualitaet gegen Kosten tauschen)"
        ),
        rationale=(
            f"Die Kosten-Sicht (runs_costs, {COST_WINDOW_DAYS}d) weist '{lane}' als "
            f"teuerste Lane aus (~${burn:.0f} API-Aequivalent). Eine gezielte "
            "Reduktion ihres Token-/Kontext-Footprints spart den groessten "
            "Hebel am Abo-Verbrauch, ohne den Durchsatz zu opfern."
        ),
        gain_weight=1.0,
        cost=0.5,
        counter_risk=0.3,
        signal_strength=signal,
        source="cost",
    )


def _cost_lever(cost: Any, suppressed: set[str]) -> Optional[Lever]:
    """Open ONE cost-efficiency lever for the single costliest lane above the
    threshold (or None when nothing is expensive enough — idle is correct).

    Reads the ``runs_costs`` shape (``{"profiles": [{profile, cost_usd,
    cost_usd_equivalent, ...}]}``). Synthetic buckets ('(ohne profil)') are
    skipped. Suppression-aware via the vetoed-lever set.
    """
    if not isinstance(cost, dict):
        return None
    best_lane: Optional[str] = None
    best_burn = 0.0
    for row in cost.get("profiles") or []:
        if not isinstance(row, dict):
            continue
        name = (row.get("profile") or "").strip()
        # skip synthetic / nameless buckets — not a real lane to optimise
        if not name or "(" in name or " " in name:
            continue
        burn = (row.get("cost_usd") or 0.0) + (row.get("cost_usd_equivalent") or 0.0)
        if burn <= COST_LANE_THRESHOLD_USD:
            continue
        if best_lane is None or burn > best_burn:
            best_lane, best_burn = name, burn
    if best_lane is None:
        return None
    token = _cost_lane_token(best_lane)
    if not token:  # degenerate lane name (all punctuation) → no usable key
        return None
    key = f"COST-EFFICIENCY-{token}"
    if key in suppressed:
        return None
    return _cost_efficiency_lever(best_lane, best_burn, key)


# --------------------------------------------------------------------------- #
# Context gathering
# --------------------------------------------------------------------------- #
def _local_midnight_epoch(now: Optional[float] = None) -> int:
    """Local-calendar midnight (today) as a unix timestamp."""
    ts = time.time() if now is None else now
    local = datetime.fromtimestamp(ts)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _read_suppressed(notes_dir: Optional[Path]) -> set[str]:
    """Lever keys the operator vetoed (reflect-fed). Suppressed on propose."""
    if notes_dir is None:
        return set()
    path = Path(notes_dir) / "vetoed_levers.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    if isinstance(data, list):
        return {str(k) for k in data if k}
    return set()


def gather_context(
    conn,
    *,
    metrics: Optional[dict[str, Any]] = None,
    cost: Optional[dict[str, Any]] = None,
    notes_dir: Optional[Path] = None,
    ledger_since: Optional[int] = None,
) -> dict[str, Any]:
    """Collect the cheap signals the strategist reasons over.

    ``metrics`` may be injected (tests / Opus-supplied snapshot); when ``None``
    it is read from the H1 file via :func:`strategist_surface.read_vision_metrics`
    and degrades to ``None`` if absent. ``cost`` (the per-lane effective-burn
    view, ``kanban_db.runs_costs``) is likewise injectable; when ``None`` it is
    computed over the cost window and degrades to ``None`` on any error.
    """
    if metrics is None:
        metrics = strategist_surface.read_vision_metrics()
    if cost is None:
        try:
            cost = kanban_db.runs_costs(conn, days=COST_WINDOW_DAYS)
        except Exception:  # cost view is best-effort; never break propose
            cost = None
    ledger = kanban_db.read_escalation_ledger(conn, since=ledger_since)
    return {
        "metrics": metrics if isinstance(metrics, dict) else None,
        "cost": cost if isinstance(cost, dict) else None,
        "ledger": ledger,
        "suppressed": _read_suppressed(notes_dir),
    }


def derive_levers(context: dict[str, Any]) -> list[Lever]:
    """Map the gathered context to candidate levers (pre self-gate).

    Deterministic baseline across the broad Vision corridor: Heiler root-causes
    (ledger ``roots_by_class`` — distinct escalating roots, falling back to the
    raw ``by_class`` event count for legacy contexts) + autonomy/gate metric
    gaps. Suppressed (recently vetoed) keys are skipped. Empty signal → empty
    list (idle is correct).
    """
    suppressed = set(context.get("suppressed") or ())
    levers: list[Lever] = []

    ledger = context.get("ledger") or {}
    # LEDGER-BYCLASS-DISTINCT-ROOTS-S1: drive the lever off the count of DISTINCT
    # escalating roots per class, so one root that escalates repeatedly cannot
    # over-state its cluster. The raw event count (``by_class``) stays the
    # recurrence record; we only fall back to it when an injected/legacy ledger
    # predates ``roots_by_class`` (graceful degradation, never a silenced signal).
    roots_by_class = ledger.get("roots_by_class")
    counts = roots_by_class if isinstance(roots_by_class, dict) else (ledger.get("by_class") or {})
    for cls, template in _LEDGER_TEMPLATES.items():
        count = int(counts.get(cls, 0) or 0)
        if count < LEDGER_MIN_COUNT:
            continue
        if template.key in suppressed:
            continue
        levers.append(
            Lever(
                key=template.key,
                title=template.title,
                lane="coder-claude",
                target_metric=template.target_metric,
                roi=template.roi,
                counter_metric=template.counter_metric,
                rationale=template.rationale,
                gain_weight=template.gain_weight,
                cost=template.cost,
                counter_risk=template.counter_risk,
                signal_strength=float(count),
                source="ledger",
            )
        )

    metrics = context.get("metrics")
    if isinstance(metrics, dict):
        autonomy = _coerce_number(metrics.get("autonomy_pct"))
        if autonomy is not None and autonomy < AUTONOMY_TARGET and "AUTON-UPLIFT" not in suppressed:
            levers.append(_autonomy_lever(AUTONOMY_TARGET - autonomy))
        streak = _coerce_number(metrics.get("green_gate_streak"))
        if streak is not None and streak < GATE_STREAK_TARGET and "GATE-STABILITY" not in suppressed:
            levers.append(_gate_stability_lever(float(GATE_STREAK_TARGET) - streak))

    # COST-AWARENESS-S1: one lever for the costliest lane above threshold. With
    # cost_effective_usd now real (subscription equivalents stamped), the
    # strategist can finally prioritise $-burn, not just escalations/autonomy.
    cost_lever = _cost_lever(context.get("cost"), suppressed)
    if cost_lever is not None:
        levers.append(cost_lever)

    return levers


def _coerce_number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def self_gate(lever: Lever, *, counter_budget: float = COUNTER_BUDGET) -> GateResult:
    """Cheap ROI + counter-metric self-check. Only survivors are surfaced."""
    if not (lever.counter_metric or "").strip():
        return GateResult(False, "kein gepaartes Counter-Metrik definiert")
    if lever.roi_score <= 0:
        return GateResult(False, f"ROI nicht positiv (score {lever.roi_score:.2f})")
    if lever.counter_risk > counter_budget:
        return GateResult(
            False,
            f"Counter-Metrik-Risiko {lever.counter_risk:.2f} ueber Budget {counter_budget:.2f} "
            f"(Guardrail '{lever.counter_metric}' nicht beschraenkbar)",
        )
    return GateResult(True, "ROI positiv und Counter-Metrik beschraenkt")


def grounding_gate(lever: Lever) -> GateResult:
    """Deterministic PRESENCE-gate for the strategist-DRAFT path ONLY.

    The Opus propose-prompt is what *judges* grounding — it greps code + git log
    per lever (does the target already exist, was it shipped) and emits a
    non-empty ``grounding`` evidence field. This gate does not re-judge that
    evidence; it only enforces, analogous to the hardener's field-checks, that a
    non-empty grounding field is PRESENT. An ungrounded draft can therefore never
    reach ingest.

    SCOPE-CRITICAL (AC-2): this gate is applied solely on the ``--drafts-file`` /
    :func:`_levers_from_drafts` path in :func:`propose`. It is deliberately NOT
    part of :func:`self_gate` (which both paths share) nor of the general
    ``planspecs.ingest_planspec`` — so the deterministic baseline levers, Vault
    specs and operator specs (which carry no grounding field) are unaffected.
    """
    if not (lever.grounding or "").strip():
        return GateResult(
            False,
            "kein nicht-leeres grounding-Evidenzfeld (Code-/git-log-Beleg fehlt)",
        )
    return GateResult(True, "grounding-Evidenz vorhanden")


# --------------------------------------------------------------------------- #
# PlanSpec rendering + ingest
# --------------------------------------------------------------------------- #
def _slug(key: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in key)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "lever"


def lever_to_markdown(lever: Lever) -> str:
    """Render a rubric-clean binding PlanSpec for *lever*.

    Frontmatter carries ``freigabe: operator`` and a ``strategist_meta`` block
    (target/ROI/counter) that :func:`planspecs.build_root_body` renders into the
    held root body as the machine-readable annotation the G1 surface parses.
    No template residue, valid lanes, every subtask carries >= 1 AC.
    """
    import yaml

    build_id = f"{lever.key}-S1"
    review_id = f"{lever.key}-S2"
    strategist_meta = {
        "target_metric": lever.target_metric,
        "roi": lever.roi,
        "counter_metric": lever.counter_metric,
    }
    # Grounding evidence is carried only when present (draft path). A baseline
    # lever has none, so its rendered spec stays byte-for-byte as before.
    grounding = (lever.grounding or "").strip()
    if grounding:
        strategist_meta["grounding"] = grounding
    frontmatter = {
        "status": "vorgeschlagen",
        "owner": "Strategist",
        "slice": lever.key,
        "topic": lever.title,
        "freigabe": "operator",
        "live_test_depth": "smoke",
        "strategist_meta": strategist_meta,
        "taskgraph_hints": {
            "binding": True,
            "subtasks": [
                {
                    "id": build_id,
                    "title": f"{lever.title} (Build plus Test)",
                    "lane": lever.lane,
                    "deps": [],
                    "acceptance_criteria": [
                        f"Ziel-Kennzahl adressiert: {lever.target_metric}",
                        f"Counter-Metrik als Guardrail geprueft und gehalten: {lever.counter_metric}",
                        "Gates gruen via scripts/run-affected.sh, Beleg mit Kommando und Exit-Code",
                    ],
                    "body": lever.rationale,
                },
                {
                    "id": review_id,
                    "title": "Review-Urteil zum Hebel",
                    "lane": "reviewer",
                    "deps": [build_id],
                    "acceptance_criteria": [
                        "Review-Urteil mit Beleg festgehalten; Ziel-Kennzahl und Counter-Metrik beide adressiert",
                    ],
                },
            ],
        },
    }
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    body_lines = [
        f"# {lever.key}",
        "",
        f"Strategist-Hebel: {lever.title}",
        "",
        f"Ziel-Kennzahl: {lever.target_metric}",
        f"ROI: {lever.roi}",
        f"Counter-Metrik: {lever.counter_metric}",
    ]
    if grounding:
        body_lines.append(f"Grounding-Evidenz: {grounding}")
    body_lines += ["", lever.rationale]
    body = "\n".join(body_lines)
    return f"---\n{fm}\n---\n\n{body}\n"


def _write_spec(out_dir: Path, lever: Lever) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_slug(lever.key)}.md"
    path.write_text(lever_to_markdown(lever), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Budget skip
# --------------------------------------------------------------------------- #
def check_budget(
    *,
    provider: str = BUDGET_PROVIDER,
    threshold: float = BUDGET_THRESHOLD,
) -> dict[str, Any]:
    """Weekly-usage skip guard. Returns ``{skip, used_percent, reason}``.

    Skips ONLY on a confirmed weekly usage above *threshold*. An absent snapshot
    / missing weekly window / unreadable percent does NOT skip (a transient
    usage-API hiccup must not silence the strategist forever) — it proceeds and
    logs why the budget was indeterminate.
    """
    try:
        from agent.account_usage import fetch_account_usage
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("strategist budget check: account_usage import failed: %s", exc)
        return {"skip": False, "used_percent": None, "reason": "usage module unavailable"}

    try:
        snapshot = fetch_account_usage(provider)
    except Exception as exc:
        logger.warning("strategist budget check: fetch failed: %s", exc)
        return {"skip": False, "used_percent": None, "reason": "usage fetch failed"}

    if snapshot is None:
        return {"skip": False, "used_percent": None, "reason": "no usage snapshot"}
    # The strategist runs on the Opus subscription lane, so the BINDING window is
    # whichever of {opus_week, weekly} is closest to its limit — opus_week can hit
    # 100% while the overall weekly is still low (the key-burn failure mode). Skip
    # on the most-consumed of the two.
    candidates = [
        w
        for w in getattr(snapshot, "windows", ())
        if getattr(w, "window_key", None) in ("opus_week", "weekly")
        and getattr(w, "used_percent", None) is not None
    ]
    if not candidates:
        return {"skip": False, "used_percent": None, "reason": "no weekly/opus_week window"}
    binding = max(candidates, key=lambda w: float(w.used_percent))
    used = float(binding.used_percent)
    label = getattr(binding, "window_key", "weekly")
    if used > threshold:
        return {
            "skip": True,
            "used_percent": used,
            "reason": f"{label} usage {used:.1f}% > {threshold:.0f}% threshold",
        }
    return {"skip": False, "used_percent": used, "reason": f"{label} usage {used:.1f}% within budget"}


# --------------------------------------------------------------------------- #
# propose / reflect orchestration
# --------------------------------------------------------------------------- #
def _levers_from_drafts(drafts: Iterable[dict[str, Any]]) -> list[Lever]:
    """Coerce Opus-supplied draft dicts into Levers (the --drafts-file seam)."""
    levers: list[Lever] = []
    for raw in drafts or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not key or not title:
            continue
        levers.append(
            Lever(
                key=key,
                title=title,
                lane=str(raw.get("lane") or "coder-claude").strip() or "coder-claude",
                target_metric=str(raw.get("target_metric") or "").strip(),
                roi=str(raw.get("roi") or "").strip(),
                counter_metric=str(raw.get("counter_metric") or "").strip(),
                rationale=str(raw.get("rationale") or title).strip(),
                gain_weight=float(raw.get("gain_weight", 1.0)),
                cost=float(raw.get("cost", 0.5)),
                counter_risk=float(raw.get("counter_risk", 0.3)),
                signal_strength=float(raw.get("signal_strength", 1.0)),
                grounding=str(raw.get("grounding") or "").strip(),
                source="drafts",
            )
        )
    return levers


def propose(
    *,
    board: Optional[str] = None,
    conn=None,
    out_dir: Path,
    metrics: Optional[dict[str, Any]] = None,
    cost: Optional[dict[str, Any]] = None,
    drafts: Optional[Iterable[dict[str, Any]]] = None,
    notes_dir: Optional[Path] = None,
    provider: str = BUDGET_PROVIDER,
    threshold: float = BUDGET_THRESHOLD,
    cap: int = CAP_MAX,
    counter_budget: float = COUNTER_BUDGET,
    do_ingest: bool = True,
    ledger_since: Optional[int] = None,
) -> dict[str, Any]:
    """Run the full propose pipeline. Returns a structured run summary.

    ``conn`` is used only for the ledger read; ingest opens its own board
    connection (so the same file board is fine). Pass ``do_ingest=False`` for a
    dry derive+gate without DB writes.
    """
    budget = check_budget(provider=provider, threshold=threshold)
    if budget["skip"]:
        return {
            "mode": "propose",
            "skipped": True,
            "reason": budget["reason"],
            "used_percent": budget["used_percent"],
            "idle": False,
            "candidates": 0,
            "gated_out": [],
            "grounding_blocked": [],
            "ingested": [],
        }

    owns_conn = conn is None
    if owns_conn:
        conn = kanban_db.connect(board=board)
    try:
        context = gather_context(
            conn, metrics=metrics, cost=cost, notes_dir=notes_dir,
            ledger_since=ledger_since,
        )
    finally:
        if owns_conn:
            conn.close()

    # Grounding presence-gate lives ONLY on the strategist-draft path (AC-2): an
    # Opus-supplied draft without a non-empty grounding field is hard-blocked
    # from ingest. The derive (baseline) path and the general ingest_planspec are
    # untouched. The existing vetoed_levers.json dedup runs alongside it.
    grounding_blocked: list[dict[str, Any]] = []
    if drafts is not None:
        suppressed = set(context.get("suppressed") or ())
        candidates = []
        for lever in _levers_from_drafts(drafts):
            if lever.key in suppressed:
                continue
            verdict = grounding_gate(lever)
            if not verdict.passed:
                grounding_blocked.append(
                    {"key": lever.key, "title": lever.title, "reason": verdict.reason}
                )
                continue
            candidates.append(lever)
    else:
        candidates = derive_levers(context)

    gated_out: list[dict[str, Any]] = []
    survivors: list[Lever] = []
    for lever in candidates:
        verdict = self_gate(lever, counter_budget=counter_budget)
        if verdict.passed:
            survivors.append(lever)
        else:
            gated_out.append({"key": lever.key, "title": lever.title, "reason": verdict.reason})

    # Rank by ROI score (desc), stable on key, then CAP.
    survivors.sort(key=lambda lv: (-lv.roi_score, lv.key))
    capped = survivors[: max(0, cap)]

    ingested: list[dict[str, Any]] = []
    ingest_errors: list[dict[str, Any]] = []
    if do_ingest:
        for lever in capped:
            spec_path = _write_spec(out_dir, lever)
            try:
                result = planspecs.ingest_planspec(
                    spec_path, board=board, author=STRATEGIST_AUTHOR, plans_root=Path(out_dir)
                )
            except planspecs.PlanSpecBlocked as exc:
                # A generic baseline body can be refused by the Sonnet judge in
                # prod — record it and continue; one bad draft must not kill the
                # run (the Opus --drafts-file path supplies judge-passing bodies).
                ingest_errors.append({"key": lever.key, "findings": exc.findings})
                continue
            ingested.append(
                {
                    "key": lever.key,
                    "title": lever.title,
                    "root_task_id": result.get("root_task_id"),
                    "subtask_count": result.get("subtask_count"),
                    "target_metric": lever.target_metric,
                    "roi": lever.roi,
                    "counter_metric": lever.counter_metric,
                    "already_ingested": result.get("already_ingested", False),
                }
            )
    else:
        ingested = [
            {
                "key": lv.key,
                "title": lv.title,
                "target_metric": lv.target_metric,
                "roi": lv.roi,
                "counter_metric": lv.counter_metric,
                "dry_run": True,
            }
            for lv in capped
        ]

    return {
        "mode": "propose",
        "skipped": False,
        "reason": budget["reason"],
        "used_percent": budget["used_percent"],
        # idle = genuinely nothing to propose. A run whose drafts were all
        # grounding-blocked is NOT idle — it had candidates that failed the gate.
        "idle": len(ingested) == 0 and not ingest_errors and not grounding_blocked,
        "candidates": len(candidates),
        "survivors": len(survivors),
        "capped": len(capped),
        "cap": cap,
        "gated_out": gated_out,
        "grounding_blocked": grounding_blocked,
        "ingest_errors": ingest_errors,
        "ingested": ingested,
    }


def propose_gate_fix(
    *,
    board: Optional[str] = None,
    out_dir: Path,
    gate_records: Optional[list[dict[str, Any]]] = None,
    min_nights: int = GATE_FIX_MIN_NIGHTS,
    do_ingest: bool = True,
) -> dict[str, Any]:
    """GREEN-GATE-AUTOHEAL-LOOP-S1 — open a HELD fix-PlanSpec for a stuck gate.

    Bounded + idempotent: reads the green-gate ledger, and ONLY when the most
    recent recorded night is red AND >= ``min_nights`` consecutive recorded
    nights share the same first_fail cause (:func:`vision_metrics.derive_consecutive_red_cause`)
    does it ingest a single ``freigabe:operator`` (HELD) fix-PlanSpec — never
    auto-deploy, never auto-release (AC-1/AC-2). Re-running while the same cause
    persists hits the ingest idempotency key and reports ``already_ingested``
    instead of minting a second chain (no spam). When nothing recurs it is a
    no-op (``triggered: False``) — idle is correct.

    Mirrors :func:`propose`'s structure (write spec → ``ingest_planspec``) so the
    same rubric + Sonnet judge + held-on-G1-surface rails apply uniformly; a
    judge ``PlanSpecBlocked`` is captured, not raised, so the loop is safe to run
    headless from the nightly heartbeat. Pass ``do_ingest=False`` for detection
    only (no DB write). ``gate_records`` defaults to the on-disk ledger; tests
    inject an explicit list.
    """
    if gate_records is None:
        gate_records = vision_metrics.read_gate_records()
    cause = vision_metrics.derive_consecutive_red_cause(
        gate_records, min_nights=min_nights
    )
    if cause is None:
        return {
            "mode": "gate-fix",
            "triggered": False,
            "reason": (
                f"kein wiederkehrender roter green-gate (Kopf gruen oder < "
                f"{min_nights} aufeinanderfolgende Naechte gleicher Ursache)"
            ),
            "ingested": None,
        }

    lever = _gate_fix_lever(cause)
    summary: dict[str, Any] = {
        "mode": "gate-fix",
        "triggered": True,
        "gate": cause["gate"],
        "fingerprint": cause["fingerprint"],
        "red_nights": cause["red_nights"],
        "dates": cause["dates"],
        "key": lever.key,
    }

    if not do_ingest:
        summary["ingested"] = {"key": lever.key, "title": lever.title, "dry_run": True}
        return summary

    spec_path = _write_spec(out_dir, lever)
    try:
        result = planspecs.ingest_planspec(
            spec_path, board=board, author=GATE_FIX_AUTHOR, plans_root=Path(out_dir)
        )
    except planspecs.PlanSpecBlocked as exc:
        # The deterministic baseline body can be refused by the Sonnet judge in
        # prod; record it and return rather than raise — the nightly heartbeat
        # must never crash on a blocked ingest (it will retry next night).
        summary["ingested"] = None
        summary["ingest_error"] = {"key": lever.key, "findings": exc.findings}
        return summary

    summary["ingested"] = {
        "key": lever.key,
        "title": lever.title,
        "root_task_id": result.get("root_task_id"),
        "subtask_count": result.get("subtask_count"),
        "freigabe": result.get("freigabe"),
        "already_ingested": result.get("already_ingested", False),
    }
    return summary


def _key_from_title(title: Optional[str]) -> Optional[str]:
    """Recover the lever key from a ``PlanSpec <KEY>: ...`` root title."""
    if not title:
        return None
    import re

    match = re.match(r"^PlanSpec\s+(\S+):", title)
    return match.group(1) if match else None


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _update_vetoed_set(path: Path, new_keys: Iterable[str]) -> list[str]:
    path = Path(path)
    existing: set[str] = set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            existing = {str(k) for k in data if k}
    except (OSError, ValueError, TypeError):
        existing = set()
    merged = sorted(existing | {str(k) for k in new_keys if k})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    return merged


def reflect(
    conn,
    *,
    since: Optional[int] = None,
    now: Optional[float] = None,
    notes_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Score the strategist's own proposals approved-vs-vetoed since *since*.

    ``since`` defaults to local midnight today. Approved = a ``freigabe_released``
    event in window; vetoed = a ``freigabe_vetoed`` event in window; shipped =
    approved whose root reached ``done``. Vetoed lever keys are recorded and
    merged into the suppression set so the next propose run does not re-raise
    what the operator rejected.
    """
    if since is None:
        since = _local_midnight_epoch(now)

    rows = conn.execute(
        "SELECT id, title, status FROM tasks WHERE created_by = ?",
        (STRATEGIST_AUTHOR,),
    ).fetchall()

    approved: list[dict[str, Any]] = []
    vetoed: list[dict[str, Any]] = []
    shipped: list[dict[str, Any]] = []
    for row in rows:
        task_id = row["id"]
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND created_at >= ?",
            (task_id, int(since)),
        ).fetchall()
        kinds = {e["kind"] for e in events}
        rec = {
            "id": task_id,
            "key": _key_from_title(row["title"]),
            "title": row["title"],
            "status": row["status"],
        }
        if "freigabe_vetoed" in kinds:
            vetoed.append(rec)
        elif "freigabe_released" in kinds:
            approved.append(rec)
            if row["status"] == "done":
                shipped.append(rec)

    vetoed_keys = sorted({r["key"] for r in vetoed if r["key"]})
    approved_keys = sorted({r["key"] for r in approved if r["key"]})
    note = {
        "ts": int(time.time() if now is None else now),
        "since": int(since),
        "approved": len(approved),
        "vetoed": len(vetoed),
        "shipped": len(shipped),
        "approved_levers": approved_keys,
        "vetoed_levers": vetoed_keys,
    }
    suppressed_now: list[str] = []
    if notes_path is not None:
        notes_path = Path(notes_path)
        _append_jsonl(notes_path, note)
        suppressed_now = _update_vetoed_set(notes_path.parent / "vetoed_levers.json", vetoed_keys)

    return {
        "mode": "reflect",
        "approved": approved,
        "vetoed": vetoed,
        "shipped": shipped,
        "note": note,
        "suppressed_levers": suppressed_now,
        "notes_path": str(notes_path) if notes_path is not None else None,
    }


# --------------------------------------------------------------------------- #
# Receipt-Harvest: deterministic gatherer + filter
# --------------------------------------------------------------------------- #
def gather_recent_receipts(
    conn,
    *,
    since_ts: int,
    max_tasks: int = HARVEST_MAX_RECEIPTS,
    min_chars: int = HARVEST_MIN_RECEIPT_CHARS,
) -> list[dict[str, Any]]:
    """Kürzlich abgeschlossene Tasks mit substanziellem Receipt (≥ ``min_chars``).

    Schließt den eigenen ``strategist-cron``-Autor aus (Anti-Rekursion) und liest
    pro Task den kanonischen Receipt-Kommentar via :func:`funnel.draft_text`.
    """
    from hermes_cli import funnel

    rows = conn.execute(
        "SELECT id, title, assignee, completed_at FROM tasks "
        "WHERE status = 'done' AND completed_at IS NOT NULL AND completed_at >= ? "
        "AND (created_by IS NULL OR created_by <> ?) "
        "ORDER BY completed_at DESC LIMIT ?",
        (int(since_ts), STRATEGIST_AUTHOR, int(max_tasks)),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        excerpt = funnel.draft_text(conn, r["id"], max_chars=2000)
        if not excerpt or len(excerpt) < min_chars:
            continue
        out.append(
            {
                "task_id": r["id"],
                "title": r["title"],
                "assignee": r["assignee"],
                "completed_at": r["completed_at"],
                "excerpt": excerpt,
            }
        )
    return out


def filter_followup_candidates(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nur Receipts mit Follow-up-Marker; ergänzt einen stabilen ``suggested_key``."""
    kept: list[dict[str, Any]] = []
    for rc in receipts:
        low = (rc.get("excerpt") or "").lower()
        if any(marker in low for marker in FOLLOWUP_MARKERS):
            kept.append({**rc, "suggested_key": f"receipt-{rc['task_id']}"})
    return kept


# --------------------------------------------------------------------------- #
# Default runtime paths (CLI fills these; tests inject explicit paths)
# --------------------------------------------------------------------------- #
def default_state_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state" / "strategist"


def append_run_history(state_dir: Path, entry: dict[str, Any]) -> None:
    """Append one run-summary line to ``<state_dir>/run-history.jsonl`` (best-effort)."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        with (Path(state_dir) / "run-history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("append_run_history failed", exc_info=True)


def read_last_runs(state_dir: Path) -> dict[str, Any]:
    """Most-recent run-history entry per mode (harvest/propose), or None each."""
    out: dict[str, Any] = {"harvest": None, "propose": None}
    path = Path(state_dir) / "run-history.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        mode = entry.get("mode")
        if mode in out:
            out[mode] = entry  # later lines win → most recent
    return out


def run_propose(args) -> dict[str, Any]:
    """CLI adapter: resolve defaults from the runtime layout, then propose."""
    state_dir = default_state_dir()
    out_dir = Path(args.out_dir) if getattr(args, "out_dir", None) else state_dir / "specs"
    drafts = None
    drafts_file = getattr(args, "drafts_file", None)
    if drafts_file:
        loaded = json.loads(Path(drafts_file).read_text(encoding="utf-8"))
        drafts = loaded.get("levers", loaded) if isinstance(loaded, dict) else loaded
    result = propose(
        board=getattr(args, "board", None),
        out_dir=out_dir,
        notes_dir=state_dir,
        drafts=drafts,
        provider=getattr(args, "budget_provider", BUDGET_PROVIDER),
        threshold=getattr(args, "budget_threshold", BUDGET_THRESHOLD),
        cap=getattr(args, "cap", CAP_MAX),
        do_ingest=not getattr(args, "dry_run", False),
    )
    append_run_history(
        default_state_dir(),
        {
            "ts": int(time.time()),
            "mode": "propose",
            "candidates": int(result.get("candidates", 0) or 0),
            "ingested": len(result.get("ingested", []) or []),
        },
    )
    return result


def run_gate_fix(args) -> dict[str, Any]:
    """CLI adapter: resolve defaults from the runtime layout, then gate-fix-check."""
    state_dir = default_state_dir()
    out_dir = Path(args.out_dir) if getattr(args, "out_dir", None) else state_dir / "specs"
    return propose_gate_fix(
        board=getattr(args, "board", None),
        out_dir=out_dir,
        min_nights=getattr(args, "min_nights", GATE_FIX_MIN_NIGHTS),
        do_ingest=not getattr(args, "dry_run", False),
    )


def run_reflect(args) -> dict[str, Any]:
    """CLI adapter: resolve defaults from the runtime layout, then reflect."""
    state_dir = default_state_dir()
    notes_path = state_dir / "reflections.jsonl"
    conn = kanban_db.connect(board=getattr(args, "board", None))
    try:
        return reflect(conn, notes_path=notes_path)
    finally:
        conn.close()


# Marker, die im Receipt-Text auf unerledigte Out-of-Scope-Arbeit hindeuten.
FOLLOWUP_MARKERS = (
    "outside scope",
    "out of scope",
    "nicht im scope",
    "ausser scope",
    "remaining",
    "verbleib",
    "separat",
    "follow-up",
    "followup",
    "folge-task",
    "anschließend",
    "nächster schritt",
    "next step",
    "todo",
    "sollte noch",
    "should be",
)


def _read_harvest_since(marker_path: Path, *, now: int) -> int:
    """Letzter Harvest-Lauf aus dem Marker, sonst Fenster-Fallback (now-48h)."""
    try:
        ts = int(json.loads(Path(marker_path).read_text(encoding="utf-8"))["ts"])
        return ts
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return now - HARVEST_WINDOW_FALLBACK_SECONDS


def run_harvest(args) -> dict[str, Any]:
    """CLI-Adapter: Receipts sammeln + vorfiltern → Kandidaten-Datei + Marker.

    KEIN LLM-Call und KEIN Ingest hier — rein deterministisch. Die Destillation
    der Kandidaten zu Lever-Drafts und der Ingest passieren im billigen
    ``claude -p``-Wrapper über den bestehenden ``--mode propose``-Pfad.
    """
    state_dir = default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "harvest_last_run.json"
    now = int(time.time())
    since_ts = _read_harvest_since(marker, now=now)

    conn = kanban_db.connect(board=getattr(args, "board", None))
    try:
        receipts = gather_recent_receipts(conn, since_ts=since_ts)
    finally:
        conn.close()
    candidates = filter_followup_candidates(receipts)

    cand_path = state_dir / "harvest_candidates.json"
    cand_path.write_text(
        json.dumps(
            {"generated_ts": now, "since_ts": since_ts, "candidates": candidates},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    marker.write_text(json.dumps({"ts": now}), encoding="utf-8")
    append_run_history(state_dir, {"ts": now, "mode": "harvest", "receipts": len(receipts), "candidates": len(candidates)})
    return {
        "mode": "harvest",
        "since_ts": since_ts,
        "receipts": len(receipts),
        "candidates": len(candidates),
        "candidates_path": str(cand_path),
    }
