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
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

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

# GREEN-GATE-PERSISTENT-RED-TRIAGE-S1: a distinct author for the N-of-M
# persistent-red triage specs (changing causes — not same-cause). Same
# rationale as GATE_FIX_AUTHOR: kept separate from STRATEGIST_AUTHOR so
# ``reflect`` never reflects on an autoheal hold. The held triage spec still
# surfaces on the G1 operator surface (freigabe:operator + scheduled).
GATE_TRIAGE_AUTHOR = "green-gate-persistent-red-triage"

# Re-export the N-of-M triage defaults so the CLI layer has one import site.
GATE_TRIAGE_MIN_REDS = vision_metrics.GATE_TRIAGE_MIN_REDS
GATE_TRIAGE_WINDOW = vision_metrics.GATE_TRIAGE_WINDOW

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
HARVEST_RECEIPT_MAX_CHARS = 12000
HARVEST_MAX_LEVERS = 3  # Sub-Cap: höchstens so viele Follow-ups pro Lauf
AUTORESEARCH_VETO_PREFIX = "autoresearch:"

# LEVER-OUTCOMES-S1: maturity window before a shipped lever is measured.
# Metrics like green_gate_streak need multiple nightly runs to stabilise.
MATURITY_DAYS: int = 3

# Direction map for the known core metrics: +1 means ↑ is better (improved),
# -1 means ↓ is better (improved). Looked up by :func:`_resolve_verdict_direction`
# (exact match first, then the last dotted segment — flattened metric paths from
# vision-metrics.json are fully qualified, e.g. ``autonomy.autonomy_pct``, but
# most entries here key the short/basename form). Unknown keys (not here and not
# in :data:`_DIRECTIONLESS`) yield verdict="unmeasurable" via
# :func:`_compute_verdict`, and measurability="unmapped_metric" via
# :func:`_lever_measurability` — LEVER-OUTCOMES-VALIDITY-S1.
#
# Direction table (reviewed 2026-07-06, source: real
# ~/.hermes/state/vision-metrics.json schema_version 2):
#   autonomy_pct                    +1  higher autonomy share is better
#   escalations_per_week             -1  fewer operator escalations is better
#   green_gate_streak.streak        +1  a longer green streak is better
#   fail_nights                      -1  legacy short key (pre schema-v2 flatten
#                                        put ``fail_nights`` directly under
#                                        green_gate_streak; kept for that shape)
#   recent_avg_cost_per_task         -1  cheaper average task cost is better
#   unclassified_share                -1  smaller unclassified escalation share
#                                        is better (classification coverage)
#   error_escalations_per_week        -1  fewer error-class escalations/week
#                                        is better
#   touches_per_week                  -1  north star: fewer operator touches
#                                        (operator_load; its counter
#                                        held_over_7d is the antagonist)
#   decision_latency_days_median      -1  faster operator absorption of held
#                                        proposals is better (operator_load)
_VERDICT_DIRECTION: dict[str, int] = {
    "autonomy_pct": 1,
    "escalations_per_week": -1,
    "green_gate_streak.streak": 1,
    "fail_nights": -1,
    "recent_avg_cost_per_task": -1,
    "unclassified_share": -1,
    "error_escalations_per_week": -1,
    "touches_per_week": -1,
    "decision_latency_days_median": -1,
}

# Keys with no defensible ROI direction: raw counts, denominators, coverage/
# window metadata and cumulative ("Bestandszähler") totals. A lever should
# never target one of these as its metric_key (delta on a denominator or a
# monotonically-growing total is not a signal), but they are legitimate,
# expected numeric leaves of vision-metrics.json — explicit here so the
# Vollständigkeits-Test can assert every real metric key is a *deliberate*
# omission from :data:`_VERDICT_DIRECTION`, not an accidental gap. Looked up
# the same way as the direction map (exact match, then last dotted segment).
#
# Directionless table (reviewed 2026-07-06):
#   autonomous_done          raw count; autonomy_pct already carries the ratio
#   total_done               denominator (autonomy + cost_per_task.coverage)
#   cost_usd_total           cumulative Bestandszähler — only ever grows
#   tasks_with_cost          coverage numerator/denominator
#   prior_avg_cost_per_task  rolling reference for pct_change, not itself a
#                            target (recent_avg_cost_per_task is the target)
#   with_metered_cost        coverage bucket count
#   subscription_only        coverage bucket count
#   no_cost_data             coverage bucket count
#   coverage_pct             meta: how much of the metric is measured at all,
#                            not the metric itself
#   window_days              window size, not a signal
#   classified_total         denominator
#   escalations              raw count denominator (classification_coverage)
#   classified_within_24h    raw count, subset of escalations
#   green_nights             raw count; green_gate_streak.streak already
#                            carries the direction
#   total_recorded_nights    denominator
#   *.counter.value          per-metric counter values (autonomy/cost_per_task/
#                            escalation_rate/classification_coverage/
#                            green_gate_streak) — each counter's real meaning
#                            is a runtime string (its sibling "name"/
#                            "description" field), not the static key path, so
#                            a single fixed direction per path is unsound;
#                            listed fully qualified (not the generic basename
#                            "value") to avoid over-broad basename matches
_DIRECTIONLESS: frozenset[str] = frozenset({
    "autonomous_done",
    "total_done",
    "cost_usd_total",
    "tasks_with_cost",
    "prior_avg_cost_per_task",
    "with_metered_cost",
    "subscription_only",
    "no_cost_data",
    "coverage_pct",
    "window_days",
    "classified_total",
    "escalations",
    "classified_within_24h",
    "green_nights",
    "total_recorded_nights",
    "autonomy.counter.value",
    "cost_per_task.counter.value",
    "escalation_rate.counter.value",
    "classification_coverage.counter.value",
    "green_gate_streak.counter.value",
    # operator_load (OPERATOR-LOAD-S1): raw components of touches_per_week and
    # the queue-depth signal — held_open is deliberately NOT a lever target
    # (emptying the queue by auto-archive would game it; the touch/latency
    # metrics carry the direction instead).
    "freigabe_decisions",
    "operator_comments",
    "held_open",
    "operator_load.counter.value",
})


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
    # LEVER-OUTCOMES-S1: optional machine-readable metric key from Opus drafts.
    # When set, reflect() uses it for delta-verdict; otherwise attempts exact
    # match against flat metric keys and falls back to None.
    metric_key: Optional[str] = None
    # STRATEGIST-CALIBRATION-S1: human-readable stamp of the calibration factor
    # applied to gain_weight (e.g. "x1.20 (n=4)"), None when no factor applied.
    calibration: Optional[str] = None

    @property
    def roi_score(self) -> float:
        """Cheap expected-return score: signal*gain minus a fixed lever cost."""
        return round(self.signal_strength * self.gain_weight - self.cost, 4)

    @property
    def rank_score(self) -> float:
        """CD3/WSJF-lite value density: roi_score per unit cost.

        ``cost`` is floored at 0.25 so a near-zero-cost lever cannot produce an
        unbounded rank_score. Used only for ranking (STRATEGIST-RANKING-S1);
        :func:`self_gate` keeps judging on ``roi_score`` unchanged.
        """
        return round(self.roi_score / max(self.cost, 0.25), 4)


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
        lane="premium",
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
        lane="premium",
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
        lane="premium",
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


def _persistent_red_triage_lever(cause: dict[str, Any]) -> Lever:
    """Build the held triage-PlanSpec lever for N-of-M persistent red nights.

    Driven by :func:`vision_metrics.derive_persistent_red_triage`. Orthogonal to
    :func:`_gate_fix_lever`: that one fires on consecutive SAME-cause nights;
    this one fires when the head is red AND >=N reds in the last M nights —
    REGARDLESS of whether the first_fail cause changed between nights. The
    changing-cause case is exactly what the same-cause path skips, leaving the
    operator with a persistent red head and no triage item (AC-1).

    The lever's identity (``key`` → spec filename) is a pure function of the
    CURRENT red file set (the head night's failing files), so re-running on a
    follow-up night with the same red files renders byte-identical markdown and
    the ingest dedups (``already_ingested``) — no spam (AC-2). When the red file
    set changes (a new test broke), the fingerprint changes and a fresh triage
    chain opens — correct: the operator SHOULD see a new item for a new failure
    pattern. The volatile red-count / dates are deliberately NOT rendered, so
    the content hash that backs idempotency cannot drift while the window grows.
    """
    gate = str(cause.get("gate") or "unknown").strip().lower() or "unknown"
    fingerprint = str(cause.get("fingerprint") or gate)
    red_files = cause.get("red_files") or set()
    token = _cost_lane_token(gate) or "UNKNOWN"
    # Short, stable digest of the file-set fingerprint so two distinct red-file
    # sets on the same gate get distinct keys while an identical set keys
    # identically (idempotent). Deterministic (sha1) — safe for idempotency.
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:8]
    key = f"GATE-TRIAGE-{token}-{digest}"
    file_list = ", ".join(sorted(red_files)) if red_files else "(unbekannt)"
    return Lever(
        key=key,
        title=(
            f"Persistente rote Naechte am Gate '{gate}' triagieren "
            f"(wechselnde Ursachen)"
        ),
        lane="premium",
        target_metric=(
            f"green_gate_streak entroeten, indem persistente Roete IMMER autonom "
            f"in handelbare Arbeit muendet: bei rotem Kopf UND >=N roten Naechten "
            f"im letzten-M-Fenster (egal ob gleiche oder wechselnde first_fail-"
            f"Ursache) wird eine konsolidierte HELD Triage-PlanSpec eroeffnet, "
            f"die die aktuell roten Test-Dateien auflistet"
        ),
        roi=(
            "hoch: ein seit mehreren Naechten roter green-gate mit "
            "wechselnden Ursachen ist ein chronisches Problem, das der "
            "same-cause-Pfad gezielt ueberspringt; konsolidierte Triage stellt "
            "sicher, dass 0 Naechte mit streak=0 ohne offenes Triage-Item "
            "dastehen"
        ),
        counter_metric=(
            "darf NICHT bei einer einzelnen isolierten Flake-Nacht feuern und "
            "darf nicht spammen: Trigger nur wenn Kopf rot UND >=N reds im "
            "M-Fenster; Dedup ueber den Fingerprint des aktuell roten Datei-"
            "Sets, sodass Re-Runs mit gleichem Set keine zweite PlanSpec "
            "oeffnen (Duplicate-PlanSpec-Rate muss 0 bleiben). Der bestehende "
            "same-cause-Pfad bleibt unveraendert (keine Doppel-Ingest)"
        ),
        rationale=(
            # Render only STABLE fields (gate + the sorted red-file set, which is
            # the fingerprint axis). The volatile red-count / window are kept OUT
            # of the spec body on purpose: PlanSpec ingest dedups on the content
            # hash, so interpolating a count that climbs while the window ramps
            # (2-of-3 → 3-of-3) would drift the hash and turn a same-file-set
            # re-run into a conflict instead of an idempotent already_ingested.
            # Mirrors _gate_fix_lever, which renders only stable fields too.
            f"Der naechtliche green-gate-Heartbeat ist am Gate '{gate}' an "
            f"mehreren der letzten Naechte rot (wechselnde first_fail-Ursachen "
            f"— der same-cause-Pfad greift nicht). aktuell rote Test-Dateien: "
            f"{file_list}. Diese HELD, operator-gated Triage-PlanSpec wurde "
            f"automatisch eroeffnet, damit die chronische Roete nicht "
            f"unbemerkt auf green_gate_streak=0 stehen bleibt. Vorgehen: die "
            f"aufgezaehlten roten Test-Dateien reproduzieren, die jeweiligen "
            f"Defekte beheben, und das betroffene Gate lokal gruen fahren."
        ),
        gain_weight=1.0,
        cost=0.5,
        counter_risk=0.3,
        signal_strength=1.0,
        source="gate-persistent-red-triage",
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
        lane="premium",
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


# LOOP-HEALTH-S1: an unhealthy loop pack (repeated non-transient fails
# outnumbering verified rounds) opens a deterministic lever. Kept small on
# purpose: gain_weight/cost/counter_risk chosen so a pack with exactly the
# threshold (3 fails) still passes self_gate (roi_score = 3*0.3 - 0.5 = 0.4 > 0,
# counter_risk 0.3 <= COUNTER_BUDGET 0.5) — mirrors the other cheap templates.
LOOP_HEALTH_MIN_FAILS = 3


def _loop_health_pack_token(pack: str) -> str:
    """Stable key token for a pack name; punctuation-only names must not
    collide on a shared fallback, so they get a per-name digest suffix."""
    token = _cost_lane_token(pack)
    if token:
        return token
    return f"UNKNOWN-{hashlib.sha1(pack.encode('utf-8')).hexdigest()[:8].upper()}"


def _loop_health_lever(pack: str, fail_kind: str, fails: int, verified: int) -> Lever:
    return Lever(
        key=f"LOOP-HEALTH-{_loop_health_pack_token(pack)}",
        title=f"Loop-Pack '{pack}' Fehlerquote senken (dominant: {fail_kind})",
        lane="premium",
        target_metric=(
            f"Fails im Loop-Pack '{pack}' (ledger.jsonl, dominant fail_kind "
            f"'{fail_kind}') von aktuell {fails} auf 0 senken und wieder "
            "verifizierte Runden erreichen"
            if verified == 0
            else (
                f"Fails im Loop-Pack '{pack}' (ledger.jsonl, dominant fail_kind "
                f"'{fail_kind}') von aktuell {fails} auf unter {verified} "
                "verifizierte Runden senken"
            )
        ),
        roi=(
            f"mittel-hoch: das Pack '{pack}' hat mehr Fails ({fails}) als "
            f"verifizierte Runden ({verified}) — ein gezielter Fix am "
            f"dominanten Fehlermuster ('{fail_kind}') bringt den Loop zurueck "
            "in einen produktiven Zustand"
        ),
        counter_metric="Verify-Laufzeit und Bounce-Rate des Packs duerfen nicht steigen",
        rationale=(
            f"Die strukturierte Ledger des Loop-Packs '{pack}' zeigt {fails} "
            f"Fails gegenueber {verified} verifizierten Runden, dominant "
            f"'{fail_kind}'. Den dominanten Fehlermuster gezielt zu beheben "
            "bringt das Pack wieder in einen gesunden Zustand, ohne den "
            "Loop-Stop-Mechanismus anzufassen."
        ),
        gain_weight=0.3,
        cost=0.5,
        counter_risk=0.3,
        signal_strength=float(fails),
        source="loop-health",
    )


def _loop_health_levers(loop_stats: Any, suppressed: set[str]) -> list[Lever]:
    """LOOP-HEALTH-S1: one lever per unhealthy pack (fails+blocked >= threshold
    and fails+blocked > verified, excluding usage_limit from both counts —
    usage_limit is an external throttle, not a pack health signal)."""
    if not isinstance(loop_stats, dict):
        return []
    levers: list[Lever] = []
    for pack, stats in sorted(loop_stats.items()):
        if not isinstance(stats, dict):
            continue
        fails_by_kind = stats.get("fails_by_kind") or {}
        if not isinstance(fails_by_kind, dict):
            continue
        blocked_by_kind = stats.get("blocked_by_kind") or {}
        if not isinstance(blocked_by_kind, dict):
            blocked_by_kind = {}
        counted = {k: v for k, v in fails_by_kind.items() if k != "usage_limit"}
        counted_blocked = {k: v for k, v in blocked_by_kind.items() if k != "usage_limit"}
        total_fails = sum(int(v or 0) for v in counted.values())
        total_blocked = sum(int(v or 0) for v in counted_blocked.values())
        total = total_fails + total_blocked
        if total < LOOP_HEALTH_MIN_FAILS:
            continue
        verified = int(stats.get("verified") or 0)
        if total <= verified:
            continue
        combined = dict(counted)
        for k, v in counted_blocked.items():
            combined[k] = combined.get(k, 0) + v
        dominant_kind = max(combined.items(), key=lambda kv: kv[1])[0] if combined else "unknown"
        key = f"LOOP-HEALTH-{_loop_health_pack_token(pack)}"
        if key in suppressed:
            continue
        levers.append(_loop_health_lever(pack, dominant_kind, total, verified))
    return levers


def _cost_coverage_pct(metrics: Any) -> Optional[float]:
    if not isinstance(metrics, dict):
        return None
    cost_metric = metrics.get("cost_per_task")
    if not isinstance(cost_metric, dict):
        return None
    coverage = cost_metric.get("coverage")
    if not isinstance(coverage, dict):
        return None
    raw = coverage.get("coverage_pct")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _cost_lever(
    cost: Any, suppressed: set[str], *, metrics: Any = None
) -> Optional[Lever]:
    """Open ONE cost-efficiency lever for the single costliest lane above the
    threshold (or None when nothing is expensive enough — idle is correct).

    Reads the ``runs_costs`` shape (``{"profiles": [{profile, cost_usd,
    cost_usd_equivalent, ...}]}``). Synthetic buckets ('(ohne profil)') are
    skipped. Suppression-aware via the vetoed-lever set.
    """
    if not isinstance(cost, dict):
        return None
    coverage_pct = _cost_coverage_pct(metrics)
    if coverage_pct is None or coverage_pct < 25.0:
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
    outcomes_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Collect the cheap signals the strategist reasons over.

    ``metrics`` may be injected (tests / Opus-supplied snapshot); when ``None``
    it is read from the H1 file via :func:`strategist_surface.read_vision_metrics`
    and degrades to ``None`` if absent. ``cost`` (the per-lane effective-burn
    view, ``kanban_db.runs_costs``) is likewise injectable; when ``None`` it is
    computed over the cost window and degrades to ``None`` on any error.

    LEVER-OUTCOMES-S1: when ``outcomes_path`` is given, the last 10 outcome
    records are added as ``lever_outcomes`` (compact view) so they flow into
    the Opus propose-prompt context without further changes to the callers.
    """
    if metrics is None:
        metrics = strategist_surface.read_vision_metrics()
    if cost is None:
        try:
            cost = kanban_db.runs_costs(conn, days=COST_WINDOW_DAYS)
        except Exception:  # cost view is best-effort; never break propose
            cost = None
    ledger = kanban_db.read_escalation_ledger(conn, since=ledger_since)
    ctx: dict[str, Any] = {
        "metrics": metrics if isinstance(metrics, dict) else None,
        "cost": cost if isinstance(cost, dict) else None,
        "ledger": ledger,
        "suppressed": _read_suppressed(notes_dir),
    }
    if outcomes_path is not None:
        ctx["lever_outcomes"] = _outcomes_compact(_read_lever_outcomes(outcomes_path))
        ctx["lever_calibration"] = _read_lever_calibration(
            default_lever_calibration_path(Path(outcomes_path))
        )
    # LOOP-STRATEGIST-COUPLING-S1: best-effort loop-pack health signal. Never
    # breaks gather_context — an import/read failure just leaves loop_stats
    # absent, same degradation contract as ``cost``/``metrics`` above.
    try:
        from loops.runner import read_all_ledger_stats, DEFAULT_STATE_ROOT

        ctx["loop_stats"] = read_all_ledger_stats(DEFAULT_STATE_ROOT)
    except Exception:
        pass
    return ctx


def _lever_class_of_key(lever_key: str) -> str:
    """Map a lever ``key`` to a stable calibration CLASS.

    Static keys (``HEILER-TRANSIENT``, ``AUTON-UPLIFT``, ``GATE-STABILITY``,
    ``COST-EFFICIENCY-<token>``) are their own class. Dynamic per-cause keys
    (``GATE-FIX-<token>-<digest>``, ``GATE-TRIAGE-<token>-<digest>``) carry a
    trailing 8-hex-char sha1 digest that makes every instance unique — stripped
    here so repeated fix/triage levers of the same gate accumulate outcomes
    under one class instead of each starting at n=0 forever.
    """
    if not (lever_key.startswith("GATE-FIX-") or lever_key.startswith("GATE-TRIAGE-")):
        return lever_key
    prefix, _, suffix = lever_key.rpartition("-")
    if prefix and len(suffix) == 8 and all(c in "0123456789abcdef" for c in suffix.lower()):
        return prefix
    return lever_key


def _apply_calibration(lever: Lever, calibration: dict[str, Any]) -> Lever:
    """Multiply *lever*'s ``gain_weight`` by its class's calibration factor.

    No-op (lever returned unchanged) when *calibration* has no entry for the
    lever's class — today's behaviour is preserved exactly in that case.
    """
    if not calibration:
        return lever
    entry = calibration.get(_lever_class_of_key(lever.key))
    if not isinstance(entry, dict):
        return lever
    factor = entry.get("factor")
    n = entry.get("n")
    if isinstance(factor, bool) or not isinstance(factor, (int, float)):
        return lever
    try:
        if not math.isfinite(factor) or not (
            _CALIBRATION_CLAMP[0] <= factor <= _CALIBRATION_CLAMP[1]
        ):
            return lever
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n):
            return lever
        if int(n) < _CALIBRATION_MIN_N:
            return lever
    except OverflowError:
        # math.isfinite(huge int) overflows on the int->float conversion —
        # a poisoned calibration entry must degrade to a no-op, never raise.
        return lever
    stamp = f"x{factor:.2f} (n={int(n)})"
    return Lever(
        key=lever.key,
        title=lever.title,
        lane=lever.lane,
        target_metric=lever.target_metric,
        roi=lever.roi,
        counter_metric=lever.counter_metric,
        rationale=f"{lever.rationale} [kalibriert {stamp}]",
        gain_weight=round(lever.gain_weight * float(factor), 4),
        cost=lever.cost,
        counter_risk=lever.counter_risk,
        signal_strength=lever.signal_strength,
        grounding=lever.grounding,
        source=lever.source,
        metric_key=lever.metric_key,
        calibration=stamp,
    )


def derive_levers(context: dict[str, Any]) -> list[Lever]:
    """Map the gathered context to candidate levers (pre self-gate).

    Deterministic baseline across the broad Vision corridor: Heiler root-causes
    (ledger ``roots_by_class`` — distinct escalating roots, falling back to the
    raw ``by_class`` event count for legacy contexts) + autonomy/gate metric
    gaps. Suppressed (recently vetoed) keys are skipped. Empty signal → empty
    list (idle is correct).

    STRATEGIST-CALIBRATION-S1: when the context carries a ``lever_calibration``
    map (written by :func:`reflect`), each lever's ``gain_weight`` is scaled by
    its class's factor before ranking.

    STRATEGIST-RANKING-S1: the returned list is sorted by ``rank_score``
    descending (deterministic tiebreak: lever ``key``), so callers that cap the
    list (propose's CAP_MAX) keep the highest value-density levers first.
    """
    suppressed: set[str] = {str(item) for item in (context.get("suppressed") or ())}
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
                lane="premium",
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
    cost_lever = _cost_lever(
        context.get("cost"), suppressed, metrics=context.get("metrics")
    )
    if cost_lever is not None:
        levers.append(cost_lever)

    # LOOP-HEALTH-S1: one lever per unhealthy loop pack (best-effort ctx key).
    levers.extend(_loop_health_levers(context.get("loop_stats"), suppressed))

    calibration = context.get("lever_calibration") or {}
    levers = [_apply_calibration(lv, calibration) for lv in levers]
    levers.sort(key=lambda lv: (-lv.rank_score, lv.key))
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


_REPO_ROOT = Path(__file__).resolve().parent.parent

# Token split for grounding-gate verification: whitespace and common quoting/
# punctuation delimiters, so a token like "hermes_cli/strategist.py:265" or
# "`green_gate_streak`" still yields a matchable candidate.
_GROUNDING_TOKEN_SPLIT = re.compile(r"[\s,;:()\[\]{}\"'`]+")


def _grounding_known_tokens() -> set[str]:
    """Known metric/counter names the strategist context actually uses.

    Derived from the deterministic templates (Heiler classes driving
    :data:`_LEDGER_TEMPLATES`) and the verdict-direction map (the metric keys
    :func:`reflect` can measure) — never a hand-authored parallel list.
    """
    return set(_LEDGER_TEMPLATES.keys()) | set(_VERDICT_DIRECTION.keys())


def _grounding_path_exists(token: str) -> bool:
    """True iff *token* resolves to an existing FILE, absolute or repo-relative.

    ``os.path.exists`` alone also matches directories, so bare prose tokens
    like "tests" or "docs" (which happen to be top-level repo directories)
    would otherwise pass as a "verifiable path" — ``os.path.isfile`` already
    excludes directories, so relying on it alone rejects that false positive
    without also rejecting real extensionless files (``Dockerfile``,
    ``LICENSE``) that a "/ or ." shape precheck would wrongly reject.
    """
    try:
        if os.path.isfile(token):
            return True
        if not os.path.isabs(token):
            return os.path.isfile(_REPO_ROOT / token)
    except (OSError, ValueError):
        return False
    return False


def grounding_gate(lever: Lever) -> GateResult:
    """Deterministic gate for the strategist-DRAFT path ONLY.

    The Opus propose-prompt is what *judges* grounding — it greps code + git log
    per lever (does the target already exist, was it shipped) and emits a
    non-empty ``grounding`` evidence field. This gate does not re-judge the
    JUDGEMENT, but (STRATEGIST-GROUNDING-HARDEN-S1) it does require the
    evidence to contain at least one VERIFIABLE token: an existing file path
    (repo-relative or absolute) or a known metric/counter name (the Heiler
    classes / verdict-mapped metric keys the strategist context actually uses).
    A non-empty but unverifiable string (e.g. free-form prose with no path or
    known metric) is rejected with an explicit reason.

    SCOPE-CRITICAL (AC-2): this gate is applied solely on the ``--drafts-file`` /
    :func:`_levers_from_drafts` path in :func:`propose`. It is deliberately NOT
    part of :func:`self_gate` (which both paths share) nor of the general
    ``planspecs.ingest_planspec`` — so the deterministic baseline levers, Vault
    specs and operator specs (which carry no grounding field) are unaffected.
    """
    evidence = (lever.grounding or "").strip()
    if not evidence:
        return GateResult(
            False,
            "kein nicht-leeres grounding-Evidenzfeld (Code-/git-log-Beleg fehlt)",
        )
    known = _grounding_known_tokens()
    for raw_tok in _GROUNDING_TOKEN_SPLIT.split(evidence):
        tok = raw_tok.strip()
        if not tok:
            continue
        if tok in known:
            return GateResult(True, f"grounding-Evidenz verifizierbar (Metrik/Counter '{tok}')")
        if _grounding_path_exists(tok):
            return GateResult(True, f"grounding-Evidenz verifizierbar (Pfad '{tok}')")
    return GateResult(
        False,
        "grounding-Evidenz enthaelt keinen verifizierbaren Pfad und keine bekannte "
        "Metrik/Counter (nur unverifizierbare Prosa)",
    )


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
                lane=str(raw.get("lane") or "premium").strip() or "premium",
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
                metric_key=str(raw.get("metric_key") or "").strip() or None,
            )
        )
    return levers


BACKPRESSURE_MAX_HELD_DEFAULT: int = 6


def _held_backpressure(conn, max_held: Optional[int] = None) -> dict[str, Any]:
    """STRATEGIST-BACKPRESSURE-S1 — deterministic ingest pre-gate on the held queue.

    The operator is the pipeline's bottleneck (funnel evidence 2026-07-05:
    187 proposals lifetime, 36 released, 178 archived) — generating into a
    backed-up queue only grows the archive. When >= ``max_held`` undecided
    held roots wait (:func:`vision_metrics.held_strategist_roots`), propose/
    harvest ingest self-skips with ``skipped: true`` — same contract as the
    budget self-skip, so the Opus wrapper treats it as a valid no-op. Dry-run
    (``do_ingest=False``) and reflect are unaffected. Config:
    ``strategist.propose_max_held`` (0 disables the gate).
    """
    if max_held is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config()
            strat_cfg = cfg.get("strategist", {}) if isinstance(cfg, dict) else {}
            max_held = int(
                strat_cfg.get("propose_max_held", BACKPRESSURE_MAX_HELD_DEFAULT)
            )
        except Exception:
            max_held = BACKPRESSURE_MAX_HELD_DEFAULT
    if max_held <= 0:
        return {"skip": False, "held_open": None, "max_held": max_held, "reason": None}
    held_open = len(vision_metrics.held_strategist_roots(conn))
    if held_open >= max_held:
        return {
            "skip": True,
            "held_open": held_open,
            "max_held": max_held,
            "reason": (
                f"backpressure: {held_open} undecided held proposals "
                f">= max_held {max_held}"
            ),
        }
    return {"skip": False, "held_open": held_open, "max_held": max_held, "reason": None}


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
    outcomes_path: Optional[Path] = None,
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
        # STRATEGIST-BACKPRESSURE-S1: only the ingest path is gated — a
        # dry-run stays available so the Opus wrapper keeps its context step.
        if do_ingest:
            backpressure = _held_backpressure(conn)
            if backpressure["skip"]:
                return {
                    "mode": "propose",
                    "skipped": True,
                    "reason": backpressure["reason"],
                    "held_open": backpressure["held_open"],
                    "max_held": backpressure["max_held"],
                    "idle": False,
                    "candidates": 0,
                    "gated_out": [],
                    "grounding_blocked": [],
                    "ingested": [],
                }
        context = gather_context(
            conn, metrics=metrics, cost=cost, notes_dir=notes_dir,
            ledger_since=ledger_since, outcomes_path=outcomes_path,
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

    # Rank by rank_score (value density, desc) to keep derive_levers' ordering
    # intact after self_gate filtering, stable on key, then CAP.
    survivors.sort(key=lambda lv: (-lv.rank_score, lv.key))
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

    # LEVER-OUTCOMES-S1: write a baseline record for each newly ingested lever.
    # Only runs when outcomes_path is provided and do_ingest=True (dry runs do
    # not produce real root_task_ids and must never write baselines).
    if outcomes_path is not None and do_ingest and ingested:
        _outcomes_write_baselines(
            outcomes_path=Path(outcomes_path),
            ingested=ingested,
            capped=capped,
            flat_metrics=_flatten_numeric(_metrics_payload(context.get("metrics"))),
        )

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
        # LEVER-OUTCOMES-S1: pre-existing outcomes from context (written by prior
        # runs); present in --dry-run JSON so Opus can read the wirkungs-history.
        "lever_outcomes": context.get("lever_outcomes") or [],
    }


def propose_gate_fix(
    *,
    board: Optional[str] = None,
    out_dir: Path,
    gate_records: Optional[list[dict[str, Any]]] = None,
    min_nights: int = GATE_FIX_MIN_NIGHTS,
    do_ingest: bool = True,
    night_log_reader: Optional[Callable[[str, list], Optional[str]]] = None,
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

    ``night_log_reader`` (GREEN-GATE-AUTOHEAL-LEGACY-NIGHT-S1) enables the
    legacy-night log backfill in
    :func:`vision_metrics.derive_consecutive_red_cause`: a red but un-attributed
    night directly preceding an attributed head is adopted into the streak only
    when its on-disk gate log proves the same failing-test signature. Defaults to
    ``None`` (pure ledger); the CLI adapter :func:`run_gate_fix` wires the real
    filesystem reader so the live 06-20/06-21 case heals.
    """
    if gate_records is None:
        gate_records = vision_metrics.read_gate_records()
    cause = vision_metrics.derive_consecutive_red_cause(
        gate_records, min_nights=min_nights, night_log_reader=night_log_reader
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


def propose_persistent_red_triage(
    *,
    board: Optional[str] = None,
    out_dir: Path,
    gate_records: Optional[list[dict[str, Any]]] = None,
    min_reds: int = GATE_TRIAGE_MIN_REDS,
    window: int = GATE_TRIAGE_WINDOW,
    do_ingest: bool = True,
) -> dict[str, Any]:
    """GREEN-GATE-PERSISTENT-RED-TRIAGE-S1 — open a HELD Triage-PlanSpec for
    persistent red nights with CHANGING causes.

    Bounded + idempotent: reads the green-gate ledger, and ONLY when the most
    recent recorded night is red AND >= ``min_reds`` of the last ``window``
    recorded nights are red — regardless of whether the first_fail cause
    changed between nights — does it ingest a single ``freigabe:operator``
    (HELD) Triage-PlanSpec listing the CURRENTLY red test files (AC-1).
    Never auto-deploy, never auto-release. Re-running while the same red file
    set persists hits the ingest idempotency key and reports
    ``already_ingested`` instead of minting a second chain (AC-2). When the head
    is green or fewer than ``min_reds`` reds are in the window it is a no-op
    (``triggered: False``) — idle is correct, and a single isolated flake-night
    is deliberately NOT triage-opening (AC-2 guard).

    Orthogonal to :func:`propose_gate_fix` (same-cause path): that one fires on
    consecutive SAME-cause nights; this one fires on N-of-M red nights with
    any-cause. The two paths produce distinct keys (``GATE-FIX-*`` vs
    ``GATE-TRIAGE-*``) so a night that matches both opens both — a same-cause
    fix-PlanSpec AND a persistent-red triage-PlanSpec, each HELD for the
    operator. They are different lenses (one names a recurring cause to fix, the
    other flags a persistently-red head), not duplicates; within each path the
    fingerprint/key dedup prevents re-opening the SAME spec on re-runs. The
    existing same-cause path remains UNCHANGED (AC-2:
    no Doppel-Ingest).
    """
    if gate_records is None:
        gate_records = vision_metrics.read_gate_records()
    cause = vision_metrics.derive_persistent_red_triage(
        gate_records, min_reds=min_reds, window=window
    )
    if cause is None:
        return {
            "mode": "gate-triage",
            "triggered": False,
            "reason": (
                f"kein persistenter roter Kopf (Kopf gruen oder < "
                f"{min_reds} rote von {window} Naechten)"
            ),
            "ingested": None,
        }

    lever = _persistent_red_triage_lever(cause)
    summary: dict[str, Any] = {
        "mode": "gate-triage",
        "triggered": True,
        "gate": cause["gate"],
        "fingerprint": cause["fingerprint"],
        "red_count": cause["red_count"],
        "window": cause["window"],
        "red_files": sorted(cause["red_files"]),
        "dates": cause["dates"],
        "key": lever.key,
    }

    if not do_ingest:
        summary["ingested"] = {"key": lever.key, "title": lever.title, "dry_run": True}
        return summary

    spec_path = _write_spec(out_dir, lever)
    try:
        result = planspecs.ingest_planspec(
            spec_path, board=board, author=GATE_TRIAGE_AUTHOR, plans_root=Path(out_dir)
        )
    except planspecs.PlanSpecBlocked as exc:
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


# --------------------------------------------------------------------------- #
# LEVER-OUTCOMES-S1 — anchor-file helpers
# --------------------------------------------------------------------------- #
def _flatten_numeric(d: Any, prefix: str = "") -> dict[str, float]:
    """Recursively flatten *d* and keep only numeric (int/float) leaves.

    Returns a dict with dotted-path keys, e.g. ``{"green_gate_streak.streak": 3.0}``.
    Boolean values are excluded even though ``isinstance(True, int)`` is True in
    Python — they are not meaningful metrics.
    """
    out: dict[str, float] = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[key] = float(v)
        elif isinstance(v, dict):
            out.update(_flatten_numeric(v, key))
    return out


def _metrics_payload(metrics: Any) -> dict[str, Any]:
    """Unwrap the H1 snapshot wrapper before flattening.

    ``vision-metrics.json`` is ``{schema_version, generated_at, generated_epoch,
    window_days, metrics: {...}}`` — flattening the wrapper would prefix every
    key with ``metrics.`` and drag meta fields (schema_version, window_days) in
    as fake metrics. Injected test dicts without a ``metrics`` child pass
    through unchanged.
    """
    if isinstance(metrics, dict):
        inner = metrics.get("metrics")
        if isinstance(inner, dict):
            return inner
        return metrics
    return {}


def _lever_metric_key(lever: "Lever", flat: dict[str, float]) -> Optional[str]:
    """Resolve the metric_key for a lever given the flattened metrics snapshot.

    Priority:
    1. Explicit ``lever.metric_key`` (set from Opus draft ``metric_key`` field).
    2. Exact match of ``lever.target_metric`` text against a flat key.
    3. ``None`` (no machine-readable key — verdict stays None on this record).
    """
    if lever.metric_key:
        return lever.metric_key
    if lever.target_metric in flat:
        return lever.target_metric
    return None


def _read_lever_outcomes(path: Any) -> list[dict[str, Any]]:
    """Read the lever-outcomes list from *path*; return [] on missing/bad JSON."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _write_lever_outcomes_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    """Persist *records* to *path* atomically via tmp+rename (os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def default_lever_outcomes_path() -> Path:
    """Return the canonical strategist lever-outcomes path under HERMES_HOME."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "state" / "strategist" / "lever-outcomes.json"


# STRATEGIST-CALIBRATION-S1: the per-class calibration factors live in a
# sibling file next to lever-outcomes.json rather than a new top-level key
# inside it — the outcomes file's on-disk shape is a plain JSON *list*
# (:func:`_read_lever_outcomes` returns ``[]`` for anything else) and is read
# by several independent writers (:func:`stamp_lever_outcome_shipped`,
# :func:`_outcomes_write_baselines`, :func:`reflect`); migrating it to a keyed
# object would touch all of them. A dedicated file in the same "ledger"
# directory keeps the two concerns (raw outcomes vs. derived calibration)
# independently readable/writable while living next to each other.
def default_lever_calibration_path(outcomes_path: Optional[Path] = None) -> Path:
    """Return the calibration-ledger path sibling to *outcomes_path*."""
    base = Path(outcomes_path) if outcomes_path is not None else default_lever_outcomes_path()
    return base.parent / "lever-calibration.json"


def _read_lever_calibration(path: Any) -> dict[str, Any]:
    """Read the per-class calibration map from *path*; ``{}`` on missing/bad JSON."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_lever_calibration_atomic(path: Path, data: dict[str, Any]) -> None:
    """Persist the calibration map to *path* atomically via tmp+rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# STRATEGIST-CALIBRATION-S1: bounded mean-based mapping from measured verdicts
# to a factor. improved=+1, worsened=-1, neutral=0; "unmeasurable"/"confounded"/
# None carry no directional signal and are excluded from both the mean and n.
_CALIBRATION_MIN_N = 3
_CALIBRATION_CLAMP = (0.5, 1.5)
_VERDICT_SCORE: dict[str, float] = {"improved": 1.0, "neutral": 0.0, "worsened": -1.0}


def compute_lever_calibration(
    records: list[dict[str, Any]], *, now: Optional[float] = None
) -> dict[str, dict[str, Any]]:
    """Aggregate measured outcome verdicts into a per-class calibration factor.

    HONESTY GATE: a class only gets a factor once it has >= :data:`_CALIBRATION_MIN_N`
    measured outcomes with a directional verdict (unmeasurable/confounded
    excluded). ``factor`` is the mean verdict score mapped linearly onto
    ``[0.5, 1.5]`` (mean +1 -> 1.5, mean -1 -> 0.5, mean 0 -> 1.0), then clamped.
    """
    by_class: dict[str, list[float]] = {}
    for rec in records:
        if rec.get("status") != "measured":
            continue
        score = _VERDICT_SCORE.get(rec.get("verdict"))
        if score is None:
            continue
        lever_key = rec.get("lever_key")
        if not lever_key:
            continue
        cls = _lever_class_of_key(str(lever_key))
        by_class.setdefault(cls, []).append(score)

    ts = datetime.fromtimestamp(time.time() if now is None else now).isoformat()
    out: dict[str, dict[str, Any]] = {}
    for cls, scores in by_class.items():
        n = len(scores)
        if n < _CALIBRATION_MIN_N:
            continue
        mean = sum(scores) / n
        factor = 1.0 + mean * 0.5
        factor = max(_CALIBRATION_CLAMP[0], min(_CALIBRATION_CLAMP[1], factor))
        out[cls] = {"factor": round(factor, 4), "n": n, "updated_at": ts}
    return out


def stamp_lever_outcome_shipped(
    root_task_id: str,
    *,
    shipped_at: int | None = None,
    outcomes_path: Path | None = None,
) -> bool:
    """Stamp a lever-outcomes record as shipped for a completed PlanSpec root.

    Fail-open for completion callers: missing file, missing record, malformed JSON,
    or write errors are logged and reported as ``False`` rather than raised.
    Idempotent: an already stamped record is left unchanged.
    """
    path = Path(outcomes_path) if outcomes_path is not None else default_lever_outcomes_path()
    try:
        if not path.exists():
            logger.info("lever-outcomes ship stamp skipped; file missing: %s", path)
            return False
        records = _read_lever_outcomes(path)
        if not records:
            return False
        now_ts = int(time.time() if shipped_at is None else shipped_at)
        changed = False
        matched = False
        for rec in records:
            if rec.get("root_task_id") != root_task_id:
                continue
            matched = True
            if rec.get("shipped_at") is None:
                rec["shipped_at"] = now_ts
                changed = True
            if rec.get("status") == "proposed":
                rec["status"] = "shipped"
                changed = True
                # LEVER-OUTCOMES-VALIDITY-S1: ship-time validity check — warn
                # now, ~MATURITY_DAYS before the operator would otherwise learn
                # the metric_key can never yield a real verdict.
                mk = rec.get("metric_key")
                measurability = _lever_measurability(mk)
                rec["measurability"] = measurability
                _warn_if_unmeasurable(
                    stage="ship-stamp", lever_key=rec.get("lever_key"),
                    root_task_id=root_task_id, metric_key=mk, measurability=measurability,
                )
        if not matched or not changed:
            return False
        _write_lever_outcomes_atomic(path, records)
        return True
    except Exception:
        logger.warning(
            "lever-outcomes ship stamp failed for root %s at %s",
            root_task_id,
            path,
            exc_info=True,
        )
        return False


def _resolve_verdict_direction(metric_key: str) -> Optional[int]:
    """Resolve a +1/-1 direction for *metric_key* via :data:`_VERDICT_DIRECTION`.

    Tries an exact match first (some entries are stored fully qualified, e.g.
    ``green_gate_streak.streak``), then falls back to the last dotted segment
    — flattened metric paths are fully qualified (e.g. ``autonomy.autonomy_pct``)
    but most map entries key the short/basename form. Shared by
    :func:`_compute_verdict` and :func:`_lever_measurability` so both use
    identical lookup logic (LEVER-OUTCOMES-VALIDITY-S1).
    """
    direction = _VERDICT_DIRECTION.get(metric_key)
    if direction is None and "." in metric_key:
        direction = _VERDICT_DIRECTION.get(metric_key.rsplit(".", 1)[-1])
    return direction


def _lever_measurability(metric_key: Optional[str]) -> str:
    """Classify whether *metric_key* can ever produce a directional verdict.

    ``ok``              — resolves to a known direction (same lookup as
                           :func:`_compute_verdict`); reflect() will be able to
                           measure it once mature.
    ``no_metric``       — no metric_key at all (an Opus draft omitted it, or
                           a deterministic baseline lever never set one).
    ``unmapped_metric`` — metric_key is set but resolves to no direction —
                           either genuinely unknown or one of the
                           deliberately-directionless keys in
                           :data:`_DIRECTIONLESS`. Either way the lever will
                           only ever be stamped verdict="unmeasurable".
    """
    if not metric_key:
        return "no_metric"
    if _resolve_verdict_direction(metric_key) is None:
        return "unmapped_metric"
    return "ok"


def _warn_if_unmeasurable(*, stage: str, lever_key: Any, root_task_id: Any, metric_key: Optional[str], measurability: str) -> None:
    """Log a WARN when a lever's metric_key cannot ever yield a real verdict.

    Visible in the run log at propose/ship time, before the operator releases
    or the maturity window elapses — LEVER-OUTCOMES-VALIDITY-S1.
    """
    if measurability == "ok":
        return
    logger.warning(
        "lever-outcomes %s: measurability=%s for lever_key=%s root_task_id=%s "
        "metric_key=%r — this lever can only ever be stamped verdict=unmeasurable",
        stage, measurability, lever_key, root_task_id, metric_key,
    )


def _confound_window(rec: dict[str, Any]) -> Optional[tuple[int, int]]:
    """Return the (start, end) epoch bounds of *rec*'s maturity window.

    Start = shipped_at. End = measured_at if the record has already been
    measured (its influence on the metric is considered settled once
    measured), else the full theoretical shipped_at + MATURITY_DAYS*86400
    maturity window — LEVER-OUTCOMES-VALIDITY-S1 Confound-Guard.
    """
    shipped_at = rec.get("shipped_at")
    if shipped_at is None:
        return None
    start = int(shipped_at)
    measured_at = rec.get("measured_at")
    end = int(measured_at) if measured_at is not None else start + MATURITY_DAYS * 86400
    return start, end


def _confounding_levers(
    rec: dict[str, Any], others: Iterable[dict[str, Any]], metric_key: Optional[str]
) -> list[str]:
    """Return lever_keys of *others* sharing *metric_key* with an overlapping
    maturity window against *rec* — two levers moving the same metric at the
    same time make either verdict unattributable, regardless of what the
    direction map says (Confound-Guard, LEVER-OUTCOMES-VALIDITY-S1).
    """
    if not metric_key:
        return []
    window = _confound_window(rec)
    if window is None:
        return []
    start, end = window
    root_id = rec.get("root_task_id")
    confounded: list[str] = []
    for other in others:
        if other.get("root_task_id") == root_id:
            continue
        if other.get("metric_key") != metric_key:
            continue
        other_window = _confound_window(other)
        if other_window is None:
            continue
        o_start, o_end = other_window
        if start <= o_end and o_start <= end:
            confounded.append(other.get("lever_key"))
    return confounded


def _compute_verdict(delta_val: float, metric_key: str, baseline_val: float | None = None) -> str:
    """Return improved|neutral|worsened|unmeasurable for a metric delta.

    Uses :func:`_resolve_verdict_direction`; ``+1`` means ↑ is better, ``-1``
    means ↓ is better. Unknown keys yield ``"unmeasurable"`` regardless of
    direction. Deltas smaller than 5% relative to the baseline are neutral.
    """
    direction = _resolve_verdict_direction(metric_key)
    if direction is None:
        return "unmeasurable"
    if baseline_val is not None:
        if abs(float(baseline_val)) > 1e-9:
            if abs(float(delta_val)) / abs(float(baseline_val)) < 0.05:
                return "neutral"
        elif abs(float(delta_val)) < 1e-9:
            return "neutral"
    elif abs(delta_val) < 1e-9:
        return "neutral"
    return "improved" if direction * delta_val > 0 else "worsened"


def _epoch_from_generated_at(value: Any) -> Optional[int]:
    """Coerce a metrics ``generated_at`` (epoch number or ISO-8601 string) to epoch.

    The H1 snapshot writes ISO strings like ``2026-07-02T04:00:50+00:00``; older
    or injected snapshots may carry numeric epochs. Anything unparseable yields
    ``None`` — the stale-metrics flag is then simply skipped instead of crashing
    the measurement pass.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp())
        except ValueError:
            return None
    return None


def _outcomes_compact(records: list[dict[str, Any]], n: int = 10) -> list[dict[str, Any]]:
    """Return the last *n* records in compact form for the propose context."""
    sorted_recs = sorted(records, key=lambda r: r.get("proposed_at") or 0, reverse=True)
    out = []
    for r in sorted_recs[:n]:
        mk = r.get("metric_key")
        delta = r.get("delta")
        delta_key = delta.get(mk) if mk and isinstance(delta, dict) else delta
        out.append({
            "lever_key": r.get("lever_key"),
            "status": r.get("status"),
            "verdict": r.get("verdict"),
            "metric_key": mk,
            "delta_key": delta_key,
            "proposed_at": r.get("proposed_at"),
            "shipped_at": r.get("shipped_at"),
            "measured_at": r.get("measured_at"),
        })
    return out


def _outcomes_write_baselines(
    *,
    outcomes_path: Path,
    ingested: list[dict[str, Any]],
    capped: list[Any],
    flat_metrics: dict[str, float],
) -> None:
    """Append baseline records for newly ingested levers (read-modify-write).

    Skips levers whose ``root_task_id`` already has a record (idempotent on
    re-ingest / ``already_ingested=True``).  Writes atomically.
    """
    records = _read_lever_outcomes(outcomes_path)
    existing_ids = {r.get("root_task_id") for r in records if r.get("root_task_id") is not None}
    now_ts = int(time.time())
    changed = False
    for item in ingested:
        root_id = item.get("root_task_id")
        if root_id is None or root_id in existing_ids:
            continue
        lever = next((lv for lv in capped if lv.key == item["key"]), None)
        mk = _lever_metric_key(lever, flat_metrics) if lever is not None else None
        # LEVER-OUTCOMES-VALIDITY-S1: ship-time validity check — computed at
        # baseline creation so an unmeasurable metric_key surfaces in the run
        # log before the operator ever releases the drafted PlanSpec.
        measurability = _lever_measurability(mk)
        _warn_if_unmeasurable(
            stage="ingest", lever_key=item["key"], root_task_id=root_id,
            metric_key=mk, measurability=measurability,
        )
        records.append({
            "schema_version": 1,
            "lever_key": item["key"],
            "root_task_id": root_id,
            "proposed_at": now_ts,
            "baseline": flat_metrics,
            "metric_key": mk,
            "measurability": measurability,
            "shipped_at": None,
            "measured_at": None,
            "current": None,
            "delta": None,
            "verdict": None,
            "status": "proposed",
        })
        existing_ids.add(root_id)
        changed = True
    if changed:
        _write_lever_outcomes_atomic(outcomes_path, records)


def _event_payload(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _autoresearch_vetoes(conn, since: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT t.id, t.title, t.status, esc.payload "
        "FROM tasks t "
        "JOIN task_events veto ON veto.task_id = t.id AND veto.kind = 'freigabe_vetoed' "
        "JOIN task_events esc ON esc.task_id = t.id AND esc.kind = ? "
        "WHERE veto.created_at >= ? "
        "ORDER BY esc.id DESC",
        (kanban_db.OPERATOR_ESCALATION_EVENT, int(since)),
    ).fetchall()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        task_id = row["id"]
        if task_id in seen:
            continue
        payload = _event_payload(row["payload"])
        if payload.get("source") != "autoresearch":
            continue
        signal = str(
            payload.get("signal_key")
            or ((payload.get("evidence") or {}).get("context") or {}).get("theme")
            or ""
        ).strip()
        if not signal:
            continue
        seen.add(task_id)
        out.append({
            "id": task_id,
            "key": f"{AUTORESEARCH_VETO_PREFIX}{signal}",
            "signal_key": signal,
            "source": "autoresearch",
            "title": row["title"],
            "status": row["status"],
        })
    return out


def _recalculate_outcome_measurement(
    rec: dict[str, Any],
    *,
    metrics: dict[str, Any],
    now_ts: int,
) -> tuple[dict[str, Any], bool]:
    """Return an updated measured outcome record and whether relevant fields changed."""
    flat_current = _flatten_numeric(_metrics_payload(metrics))
    flat_baseline = rec.get("baseline") or {}
    mk = rec.get("metric_key")
    current_val: float | None = None
    baseline_val: float | None = None
    delta_val: float | None = None
    verdict = "unmeasurable"
    if mk and mk in flat_current and mk in flat_baseline:
        current_val = float(flat_current[mk])
        baseline_val = float(flat_baseline[mk])
        delta_val = round(current_val - baseline_val, 9)
        verdict = _compute_verdict(delta_val, mk, baseline_val)

    desired = dict(rec)
    desired["current"] = current_val
    desired["delta"] = delta_val
    desired["status"] = "measured"
    desired["verdict"] = verdict
    gen_epoch = _epoch_from_generated_at(metrics.get("generated_at"))
    if gen_epoch is not None and now_ts - gen_epoch > 86400:
        desired["stale_metrics"] = True
    else:
        desired.pop("stale_metrics", None)

    relevant_fields = ("current", "delta", "status", "verdict", "stale_metrics")
    changed = any(rec.get(field) != desired.get(field) for field in relevant_fields)
    changed = changed or ("stale_metrics" in rec) != ("stale_metrics" in desired)
    if changed or rec.get("measured_at") is None:
        desired["measured_at"] = now_ts
        changed = changed or rec.get("measured_at") != now_ts
    else:
        desired["measured_at"] = rec.get("measured_at")
    return desired, changed


def _outcome_matches_filter(
    rec: dict[str, Any],
    *,
    lever_keys: Optional[set[str]] = None,
    root_task_ids: Optional[set[str]] = None,
    statuses: Optional[set[str]] = None,
) -> bool:
    if lever_keys is not None and str(rec.get("lever_key")) not in lever_keys:
        return False
    if root_task_ids is not None and str(rec.get("root_task_id")) not in root_task_ids:
        return False
    if statuses is not None and str(rec.get("status")) not in statuses:
        return False
    return True


def backfill_lever_outcomes(
    *,
    outcomes_path: Path,
    metrics: Optional[dict[str, Any]] = None,
    now: Optional[float] = None,
    apply: bool = False,
    limit: Optional[int] = None,
    lever_keys: Optional[list[str]] = None,
    root_task_ids: Optional[list[str]] = None,
    statuses: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Dry-run/apply recalculation for existing strategist outcome rows.

    The path is intentionally explicit and bounded: it never appends rows, only
    recalculates matching existing rows in place when ``apply=True``.
    """
    outcomes_path = Path(outcomes_path)
    records = _read_lever_outcomes(outcomes_path)
    if limit is not None and limit < 0:
        raise ValueError("outcome backfill --limit must be non-negative")
    current_metrics = metrics if metrics is not None else strategist_surface.read_vision_metrics()
    if current_metrics is None:
        raise ValueError("No vision metrics available for strategist outcome backfill")

    now_ts = int(time.time() if now is None else now)
    lever_filter = {str(v) for v in lever_keys} if lever_keys else None
    root_filter = {str(v) for v in root_task_ids} if root_task_ids else None
    status_filter = {str(v) for v in statuses} if statuses else {"measured", "shipped"}
    remaining = limit if limit is not None else None
    matched = 0
    changed_count = 0
    changed_examples: list[dict[str, Any]] = []
    new_records: list[dict[str, Any]] = []

    for rec in records:
        if remaining == 0 or not _outcome_matches_filter(
            rec,
            lever_keys=lever_filter,
            root_task_ids=root_filter,
            statuses=status_filter,
        ):
            new_records.append(rec)
            continue
        if rec.get("status") == "shipped":
            shipped_at = rec.get("shipped_at")
            if shipped_at is None or now_ts < int(shipped_at) + MATURITY_DAYS * 86400:
                new_records.append(rec)
                continue
        matched += 1
        if remaining is not None:
            remaining -= 1
        updated, changed = _recalculate_outcome_measurement(rec, metrics=current_metrics, now_ts=now_ts)
        if changed:
            changed_count += 1
            if len(changed_examples) < 10:
                changed_examples.append({
                    "lever_key": rec.get("lever_key"),
                    "root_task_id": rec.get("root_task_id"),
                    "old_verdict": rec.get("verdict"),
                    "new_verdict": updated.get("verdict"),
                    "old_delta": rec.get("delta"),
                    "new_delta": updated.get("delta"),
                })
        new_records.append(updated if apply else rec)

    if apply and changed_count:
        _write_lever_outcomes_atomic(outcomes_path, new_records)

    return {
        "mode": "outcomes-backfill",
        "outcomes_path": str(outcomes_path),
        "apply": apply,
        "matched": matched,
        "updated": changed_count if apply else 0,
        "would_update": 0 if apply else changed_count,
        "limit": limit,
        "statuses": sorted(status_filter),
        "lever_keys": sorted(lever_filter) if lever_filter else [],
        "root_task_ids": sorted(root_filter) if root_filter else [],
        "changed_examples": changed_examples,
        "note": "dry-run only; rerun with --apply to rewrite matching rows" if not apply else "applied in-place; no rows appended",
    }


def reflect(
    conn,
    *,
    since: Optional[int] = None,
    now: Optional[float] = None,
    notes_path: Optional[Path] = None,
    outcomes_path: Optional[Path] = None,
    metrics: Optional[dict[str, Any]] = None,
    calibration_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Score the strategist's own proposals approved-vs-vetoed since *since*.

    ``since`` defaults to local midnight today. Approved = a ``freigabe_released``
    event in window; vetoed = a ``freigabe_vetoed`` event in window; shipped =
    approved whose root reached ``done``. Vetoed lever keys are recorded and
    merged into the suppression set so the next propose run does not re-raise
    what the operator rejected.

    LEVER-OUTCOMES-S1: when ``outcomes_path`` is given, also:
    (a) stamps ``shipped_at`` on proposed records whose root is now done +
        freigabe_released (status→shipped);
    (b) measures records that have been shipped for >= MATURITY_DAYS and writes
        ``current``, ``delta``, ``verdict``, status→measured;
    (c) adds ``outcomes: {shipped_stamped, measured}`` to the note record.
    ``metrics`` may be injected (tests); otherwise read from the H1 file.
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

    autoresearch_vetoed = _autoresearch_vetoes(conn, int(since))
    vetoed.extend(autoresearch_vetoed)

    vetoed_keys = sorted({r["key"] for r in vetoed if r["key"]})
    approved_keys = sorted({r["key"] for r in approved if r["key"]})
    autoresearch_signals = sorted({r["signal_key"] for r in autoresearch_vetoed if r.get("signal_key")})

    # LEVER-OUTCOMES-S1: update anchor file before building the note so that
    # outcomes counts can be included in the written note record.
    outcomes_shipped_stamped = 0
    outcomes_measured = 0
    outcomes_verdicts: list[dict[str, Any]] = []
    if outcomes_path is not None:
        now_ts = int(time.time() if now is None else now)
        outcome_records = _read_lever_outcomes(outcomes_path)
        # Confound-Guard reference: a snapshot of the on-disk state taken
        # before this pass mutates anything, so overlap detection is
        # deterministic regardless of list order / which record this loop
        # happens to touch first (LEVER-OUTCOMES-VALIDITY-S1).
        _records_snapshot = [dict(r) for r in outcome_records]
        # Lazy-read current metrics only when needed for measuring.
        _current_metrics: Optional[dict[str, Any]] = metrics
        changed = False

        for rec in outcome_records:
            status = rec.get("status")

            # (a) Stamp shipped_at: proposed records whose task is now done+released.
            if status == "proposed" and rec.get("shipped_at") is None:
                root_id = rec.get("root_task_id")
                if root_id is not None:
                    task_row = conn.execute(
                        "SELECT status, completed_at FROM tasks WHERE id = ?",
                        (root_id,),
                    ).fetchone()
                    if task_row and task_row["status"] == "done":
                        has_release = conn.execute(
                            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'freigabe_released'",
                            (root_id,),
                        ).fetchone()
                        if has_release:
                            shipped_ts = task_row["completed_at"] or now_ts
                            rec["shipped_at"] = int(shipped_ts)
                            rec["status"] = "shipped"
                            outcomes_shipped_stamped += 1
                            changed = True
                            # LEVER-OUTCOMES-VALIDITY-S1: ship-time validity
                            # check, warn now rather than after the maturity
                            # window silently expires into "unmeasurable".
                            ship_mk = rec.get("metric_key")
                            ship_measurability = _lever_measurability(ship_mk)
                            rec["measurability"] = ship_measurability
                            _warn_if_unmeasurable(
                                stage="reflect-ship-stamp", lever_key=rec.get("lever_key"),
                                root_task_id=root_id, metric_key=ship_mk,
                                measurability=ship_measurability,
                            )

            # (b) Measure: shipped records past the maturity window.
            if rec.get("status") == "shipped" and rec.get("measured_at") is None:
                shipped_at = rec.get("shipped_at")
                if shipped_at is not None and now_ts >= int(shipped_at) + MATURITY_DAYS * 86400:
                    if _current_metrics is None:
                        _current_metrics = strategist_surface.read_vision_metrics()
                    if _current_metrics is not None:
                        flat_current = _flatten_numeric(_metrics_payload(_current_metrics))
                        flat_baseline = rec.get("baseline") or {}
                        mk = rec.get("metric_key")
                        current_val: float | None = None
                        baseline_val: float | None = None
                        delta_val: float | None = None
                        if mk and mk in flat_current and mk in flat_baseline:
                            current_val = float(flat_current[mk])
                            baseline_val = float(flat_baseline[mk])
                            delta_val = round(current_val - baseline_val, 9)

                        # Confound-Guard: runs BEFORE the directional verdict —
                        # two levers sharing metric_key with an overlapping
                        # maturity window make the delta unattributable, even
                        # when the direction map would otherwise resolve it.
                        confounded_with = _confounding_levers(rec, _records_snapshot, mk)
                        if confounded_with:
                            verdict = "confounded"
                        elif delta_val is not None:
                            verdict = _compute_verdict(delta_val, mk, baseline_val)
                        else:
                            verdict = "unmeasurable"

                        rec["current"] = current_val
                        rec["delta"] = delta_val
                        rec["measured_at"] = now_ts
                        rec["status"] = "measured"
                        # Stale-metrics flag: generated_at older than 24 h.
                        # The H1 file writes ISO-8601 strings; epochs stay accepted.
                        gen_epoch = _epoch_from_generated_at(_current_metrics.get("generated_at"))
                        if gen_epoch is not None and now_ts - gen_epoch > 86400:
                            rec["stale_metrics"] = True
                        rec["verdict"] = verdict
                        rec["measurability"] = _lever_measurability(mk)
                        if confounded_with:
                            rec["confounded_with"] = confounded_with
                        else:
                            rec.pop("confounded_with", None)
                        outcomes_measured += 1
                        outcomes_verdicts.append(
                            {"lever_key": rec.get("lever_key"), "verdict": verdict}
                        )
                        changed = True

        if changed:
            _write_lever_outcomes_atomic(Path(outcomes_path), outcome_records)

        # STRATEGIST-CALIBRATION-S1: recompute the per-class calibration map
        # from the full (possibly just-mutated) outcome history and persist it
        # whenever it differs from what's on disk, independent of `changed` —
        # a class can cross the min-n threshold on a run that measured a
        # DIFFERENT lever's record.
        calib_path = (
            Path(calibration_path)
            if calibration_path is not None
            else default_lever_calibration_path(Path(outcomes_path))
        )
        new_calibration = compute_lever_calibration(outcome_records, now=now)
        if new_calibration != _read_lever_calibration(calib_path):
            _write_lever_calibration_atomic(calib_path, new_calibration)

    note = {
        "ts": int(time.time() if now is None else now),
        "since": int(since),
        "approved": len(approved),
        "vetoed": len(vetoed),
        "shipped": len(shipped),
        "approved_levers": approved_keys,
        "vetoed_levers": vetoed_keys,
        "vetoed_autoresearch_signals": autoresearch_signals,
        # LEVER-OUTCOMES-S1: outcome counts in the note so they appear in the
        # reflections.jsonl record read by the Opus propose-prompt.
        # LEVER-OUTCOMES-VALIDITY-S1: "verdicts" lists only the records
        # measured *in this run* (not the whole outcomes file) so reflections
        # document actual lever wirkung as it happens.
        "outcomes": {
            "shipped_stamped": outcomes_shipped_stamped,
            "measured": outcomes_measured,
            "verdicts": outcomes_verdicts,
        },
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
        excerpt = funnel.draft_text(conn, r["id"], max_chars=HARVEST_RECEIPT_MAX_CHARS)
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
    """Nur Receipts mit Follow-up-Marker; ergänzt einen stabilen ``suggested_key``.

    Markiert jeden Kandidaten mit ``source="keyword-fallback"`` — Übergangs-Fallback
    für Alt-Receipts, die noch kein Ledger-Item besitzen (vor FRD Phase 1a).
    Phase 4 kann diesen Pfad entfernen, sobald genug Ledger-Daten vorliegen.
    """
    kept: list[dict[str, Any]] = []
    for rc in receipts:
        low = (rc.get("excerpt") or "").lower()
        if any(marker in low for marker in FOLLOWUP_MARKERS):
            age = _age_days(rc.get("completed_at"), now=int(time.time()))
            cand = {
                **rc,
                "suggested_key": f"receipt-{rc['task_id']}",
                "source": "keyword-fallback",
                "kind": "follow_up",
                "source_severity": "scope-note",
                "triage_severity": "scope-note",
                "severity": "scope-note",
                "overdue": False,
            }
            if age is not None:
                cand["age_days"] = age
            kept.append(cand)
    return kept


def load_followup_candidates_from_ledger(
    conn, *, since_ts: int, realrisk_escalate_days: int = 2, now: int | None = None
) -> list[dict[str, Any]]:
    """Primäre Quelle: lädt offene Disposition-Ledger-Items als Harvest-Kandidaten.

    Reguläre Kandidaten sind ``follow_up``, ``risk`` und ``still_open`` mit
    ``created_at >= since_ts``. Ältere ``real-risk``-Items bleiben zusätzlich im
    Kandidatensatz, wenn sie den Eskalations-Cutoff überschreiten.

    Kandidaten-Format ist kompatibel mit dem keyword-Pfad (``filter_followup_candidates``),
    damit beide Quellen im Merge in ``run_harvest`` identisch behandelt werden.
    ``source_severity`` bleibt das Ledger-Signal; ``triage_severity`` ist die
    deterministische Harvest-Einordnung (real-risk|overdue|scope-note|none).
    """
    candidate_types = {"follow_up", "risk", "still_open"}
    now_ts = int(time.time()) if now is None else int(now)
    escalate_days = max(0, int(realrisk_escalate_days or 0))
    overdue_cutoff_ts = now_ts - (escalate_days * 86400)
    items = kanban_db.list_disposition_items(conn, status="open")
    out: list[dict[str, Any]] = []
    for item in items:
        if item.get("typ") not in candidate_types:
            continue
        item_created_at = item.get("created_at") or 0
        disposition = item.get("disposition") or ""
        source_severity = item.get("severity") or "none"
        age_days = _age_days(item_created_at, now=now_ts)
        is_real_risk = source_severity == "real-risk"
        is_overdue = is_real_risk and item_created_at < overdue_cutoff_ts
        triage_severity = _triage_severity(
            source_severity,
            age_days=age_days,
            realrisk_escalate_days=escalate_days,
        )
        if item_created_at < since_ts and not is_overdue:
            continue
        source_task_id = item["source_task_id"]
        # Titel-Fallback: kein tasks-Eintrag → source_task_id als Titel
        row = conn.execute(
            "SELECT title FROM tasks WHERE id = ?", (source_task_id,)
        ).fetchone()
        title = row["title"] if row is not None else source_task_id
        # excerpt: next_action + evidence; Fallback auf disposition
        next_action = item.get("next_action") or ""
        evidence = item.get("evidence") or ""
        if next_action or evidence:
            excerpt = next_action + (" — " + evidence if evidence else "")
        else:
            excerpt = disposition
        out.append(
            {
                "task_id": source_task_id,
                "title": title,
                "assignee": None,
                "completed_at": item_created_at,
                "excerpt": excerpt,
                "disposition_item_id": item["id"],
                "suggested_key": f"disposition-{item['id']}",
                "source": "ledger",
                "kind": item.get("typ"),
                "typ": item.get("typ"),
                "disposition": disposition,
                "source_severity": source_severity,
                "triage_severity": triage_severity,
                "severity": triage_severity,
                "age_days": age_days,
                "overdue": triage_severity == "overdue",
            }
        )
    return out


def reap_worker_drop_disposition_items(conn) -> int:
    """Status-reap open disposition-ledger items explicitly marked ``drop``.

    This is intentionally non-destructive: rows stay in ``disposition_items``
    and receive the normal terminal ``dismissed`` audit stamp.  "Reaped" in
    harvest/digest artifacts therefore means "filtered from future candidate
    sets", not "deleted from the ledger".
    """
    reaped = 0
    for item in kanban_db.list_disposition_items(conn, status="open", disposition="drop"):
        updated = kanban_db.set_disposition_status(
            conn,
            item["id"],
            status="dismissed",
            decided_by="harvest-reaper",
        )
        if updated is not None:
            reaped += 1
    if reaped:
        logger.info("harvest reaper dismissed %d worker-drop disposition items", reaped)
    return reaped


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
    """Most-recent run-history entry per mode, or None."""
    out: dict[str, Any] = {
        "harvest": None,
        "harvest-watch": None,
        "propose": None,
        "digest": None,
    }
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


# --------------------------------------------------------------------------- #
# Disposition digest (A3): the Sonnet harvest step persists its clustering
# decision here so the dashboard can show the full triage — not just the levers
# that survived the propose gate.
# --------------------------------------------------------------------------- #
_DIGEST_RECOMMENDATIONS = ("drop", "collect", "planspec")


def _normalize_digest_triage_severity(value: Any) -> str:
    severity = str(value or "none").strip().lower() or "none"
    if severity not in TRIAGE_SEVERITIES:
        raise ValueError(
            f"digest triage_severity {severity!r} not in {sorted(TRIAGE_SEVERITIES)}"
        )
    return severity


def disposition_digest_path(state_dir: Optional[Path] = None) -> Path:
    """Resolve the disposition-digest artifact path.

    ``HERMES_STRATEGIST_DIGEST_PATH`` is the explicit override (test isolation);
    otherwise ``<state_dir>/disposition_digest.json`` where ``state_dir``
    defaults to the runtime strategist state dir. The dashboard reader
    (:func:`strategist_surface.disposition_digest_path`) delegates here so both
    sides resolve the SAME path."""
    override = os.environ.get("HERMES_STRATEGIST_DIGEST_PATH", "").strip()
    if override:
        return Path(override)
    base = Path(state_dir) if state_dir is not None else default_state_dir()
    return base / "disposition_digest.json"


def _normalize_digest(payload: dict[str, Any], *, now: int) -> dict[str, Any]:
    """Validate + normalize the Sonnet-supplied clustering decision into the
    persisted digest schema. Raises ``ValueError`` on a malformed contract so a
    bad LLM payload fails loud (CLI exits 2) instead of writing garbage.

    ``generated_at`` is ALWAYS stamped from ``now`` (Python wall-clock), never
    trusted from the payload — an LLM cannot backdate the digest. ``total_open``
    and ``reaped`` are honoured if the step supplied valid ints, else derived:
    ``total_open`` = distinct items across clusters + left, ``reaped`` = number
    of items in clusters recommended for ``drop``.  ``reaped`` is an audit
    counter for filtered/obsolete digest entries, not evidence of DB deletion.
    """
    if not isinstance(payload, dict):
        raise ValueError("digest payload must be a JSON object")

    raw_clusters = payload.get("clusters", [])
    if not isinstance(raw_clusters, list):
        raise ValueError("digest 'clusters' must be a list")
    clusters: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    reaped_derived = 0
    for idx, cluster in enumerate(raw_clusters):
        if not isinstance(cluster, dict):
            raise ValueError(f"cluster[{idx}] must be an object")
        theme = str(cluster.get("theme") or "").strip()
        if not theme:
            raise ValueError(f"cluster[{idx}] has an empty 'theme'")
        recommendation = str(cluster.get("recommendation") or "").strip().lower()
        if recommendation not in _DIGEST_RECOMMENDATIONS:
            raise ValueError(
                f"cluster[{idx}] recommendation {recommendation!r} not in "
                f"{_DIGEST_RECOMMENDATIONS}"
            )
        raw_ids = cluster.get("item_ids", [])
        if not isinstance(raw_ids, list):
            raise ValueError(f"cluster[{idx}] 'item_ids' must be a list")
        item_ids = [str(i) for i in raw_ids]
        seen_items.update(item_ids)
        if recommendation == "drop":
            reaped_derived += len(item_ids)
        triage_severity = _normalize_digest_triage_severity(
            cluster.get("triage_severity", cluster.get("severity"))
        )
        norm = {
            "theme": theme,
            "item_ids": item_ids,
            "kind": str(cluster.get("kind") or "cluster").strip() or "cluster",
            "source_severity": str(cluster.get("source_severity") or "none").strip() or "none",
            "triage_severity": triage_severity,
            "severity": triage_severity,
            "recommendation": recommendation,
        }
        age_days = cluster.get("age_days")
        if isinstance(age_days, int) and not isinstance(age_days, bool) and age_days >= 0:
            norm["age_days"] = age_days
        planspec_key = cluster.get("planspec_key")
        if planspec_key:
            norm["planspec_key"] = str(planspec_key).strip()
        clusters.append(norm)

    raw_left = payload.get("left", [])
    if not isinstance(raw_left, list):
        raise ValueError("digest 'left' must be a list")
    left: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw_left):
        if not isinstance(entry, dict):
            raise ValueError(f"left[{idx}] must be an object")
        item_id = str(entry.get("item_id") or "").strip()
        if not item_id:
            raise ValueError(f"left[{idx}] has an empty 'item_id'")
        seen_items.add(item_id)
        triage_severity = _normalize_digest_triage_severity(
            entry.get("triage_severity", entry.get("severity"))
        )
        norm_left: dict[str, Any] = {
            "item_id": item_id,
            "reason": str(entry.get("reason") or "").strip(),
            "kind": str(entry.get("kind") or "item").strip() or "item",
            "source_severity": str(entry.get("source_severity") or "none").strip() or "none",
            "triage_severity": triage_severity,
            "severity": triage_severity,
        }
        age_days = entry.get("age_days")
        if isinstance(age_days, int) and not isinstance(age_days, bool) and age_days >= 0:
            norm_left["age_days"] = age_days
        disposition = entry.get("disposition")
        if disposition:
            norm_left["disposition"] = str(disposition).strip()
        left.append(norm_left)

    total_open = payload.get("total_open")
    if not isinstance(total_open, int) or isinstance(total_open, bool) or total_open < 0:
        total_open = len(seen_items)
    reaped = payload.get("reaped")
    if not isinstance(reaped, int) or isinstance(reaped, bool) or reaped < 0:
        reaped = reaped_derived

    return {
        "generated_at": int(now),
        "total_open": total_open,
        "reaped": reaped,
        "clusters": clusters,
        "left": left,
    }


def write_disposition_digest(
    state_dir: Path, payload: dict[str, Any], *, now: int
) -> Path:
    """Validate + persist the harvest clustering decision atomically. Returns
    the digest path. See :func:`_normalize_digest` for the contract."""
    digest = _normalize_digest(payload, now=now)
    path = disposition_digest_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def run_digest(args) -> dict[str, Any]:
    """CLI adapter: read the harvest step's ``--digest-file`` clustering JSON,
    validate + persist it as ``disposition_digest.json``, append a run-history
    line. Deterministic — no LLM call, no ingest (the propose path handles the
    levers; this only records the transparent triage)."""
    state_dir = default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    digest_file = getattr(args, "digest_file", None)
    if not digest_file:
        raise FileNotFoundError("--mode digest requires --digest-file <path>")
    payload = json.loads(Path(digest_file).read_text(encoding="utf-8"))
    now = int(time.time())
    path = write_disposition_digest(state_dir, payload, now=now)
    digest = json.loads(path.read_text(encoding="utf-8"))
    append_run_history(
        state_dir,
        {
            "ts": now,
            "mode": "digest",
            "clusters": len(digest["clusters"]),
            "total_open": digest["total_open"],
            "reaped": digest["reaped"],
            "left": len(digest["left"]),
        },
    )
    return {
        "mode": "digest",
        "digest_path": str(path),
        "clusters": len(digest["clusters"]),
        "total_open": digest["total_open"],
        "reaped": digest["reaped"],
        "left": len(digest["left"]),
    }


SPECIAL_HARVEST_COOLDOWN_SECONDS = 6 * 60 * 60


def _read_special_harvest_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
        return {"armed": True, "last_special_run_ts": None}
    return {
        "armed": bool(raw.get("armed", True)),
        "last_special_run_ts": raw.get("last_special_run_ts"),
    }


def _write_special_harvest_state(path: Path, state: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _open_disposition_item_count(conn) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM disposition_items WHERE status = 'open'").fetchone()
    return int(row["n"] if row is not None else 0)


def run_harvest_watch(args) -> dict[str, Any]:
    """Cheap count-watchdog that triggers an extra harvest run when backlog spikes.

    The watcher does no LLM work and uses the normal :func:`run_harvest` path for
    the actual special run, so ``harvest_last_run.json`` remains the single
    source of truth for harvest windows. A separate watchdog state only guards
    cooldown + hysteresis to avoid firing repeatedly while the backlog stays high.
    """
    state_dir = default_state_dir()
    state_path = state_dir / "harvest_special_run.json"
    now = int(time.time())

    from hermes_cli.config import load_config

    cfg = load_config()
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    threshold = int(kanban_cfg.get("disposition_special_run_threshold", 25))
    rearm = int(kanban_cfg.get("disposition_special_run_rearm", 20))

    conn = kanban_db.connect(board=getattr(args, "board", None))
    try:
        open_count = _open_disposition_item_count(conn)
    finally:
        conn.close()

    state = _read_special_harvest_state(state_path)
    last_special_run_ts = state.get("last_special_run_ts")
    if open_count < rearm and not state.get("armed", True):
        state["armed"] = True
        _write_special_harvest_state(state_path, state)

    if open_count <= threshold:
        return {
            "mode": "harvest-watch",
            "triggered": False,
            "reason": "below-threshold",
            "open_disposition_items": open_count,
            "threshold": threshold,
            "rearm": rearm,
            "state_path": str(state_path),
        }
    if not state.get("armed", True):
        return {
            "mode": "harvest-watch",
            "triggered": False,
            "reason": "not-rearmed",
            "open_disposition_items": open_count,
            "threshold": threshold,
            "rearm": rearm,
            "state_path": str(state_path),
        }
    if (
        isinstance(last_special_run_ts, int)
        and now - last_special_run_ts < SPECIAL_HARVEST_COOLDOWN_SECONDS
    ):
        return {
            "mode": "harvest-watch",
            "triggered": False,
            "reason": "cooldown",
            "open_disposition_items": open_count,
            "threshold": threshold,
            "rearm": rearm,
            "cooldown_remaining_seconds": SPECIAL_HARVEST_COOLDOWN_SECONDS
            - (now - last_special_run_ts),
            "state_path": str(state_path),
        }

    harvest = run_harvest(args)
    state.update({"armed": False, "last_special_run_ts": now})
    _write_special_harvest_state(state_path, state)
    return {
        "mode": "harvest-watch",
        "triggered": True,
        "reason": "threshold-exceeded",
        "open_disposition_items": open_count,
        "threshold": threshold,
        "rearm": rearm,
        "harvest": harvest,
        "state_path": str(state_path),
    }


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
        outcomes_path=state_dir / "lever-outcomes.json",
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
    """CLI adapter: resolve defaults from the runtime layout, then gate-fix-check.

    Wires the real filesystem ``night_log_reader`` so a legacy un-attributed red
    night directly preceding an attributed head can be log-backfilled into the
    streak (GREEN-GATE-AUTOHEAL-LEGACY-NIGHT-S1 — the live 06-20/06-21 case)."""
    state_dir = default_state_dir()
    out_dir = Path(args.out_dir) if getattr(args, "out_dir", None) else state_dir / "specs"
    return propose_gate_fix(
        board=getattr(args, "board", None),
        out_dir=out_dir,
        min_nights=getattr(args, "min_nights", GATE_FIX_MIN_NIGHTS),
        do_ingest=not getattr(args, "dry_run", False),
        night_log_reader=vision_metrics.default_night_log_reader(),
    )


def run_persistent_red_triage(args) -> dict[str, Any]:
    """CLI adapter: resolve defaults from the runtime layout, then triage-check.

    GREEN-GATE-PERSISTENT-RED-TRIAGE-S1 — the N-of-M changing-cause trigger,
    orthogonal to :func:`run_gate_fix` (same-cause). When the head is red AND
    >=N reds in the last M nights, ingests a single HELD Triage-PlanSpec
    listing the currently-red test files; idempotent on the file-set fingerprint.
    """
    state_dir = default_state_dir()
    out_dir = Path(args.out_dir) if getattr(args, "out_dir", None) else state_dir / "specs"
    return propose_persistent_red_triage(
        board=getattr(args, "board", None),
        out_dir=out_dir,
        min_reds=getattr(args, "min_reds", GATE_TRIAGE_MIN_REDS),
        window=getattr(args, "window", GATE_TRIAGE_WINDOW),
        do_ingest=not getattr(args, "dry_run", False),
    )


def run_reflect(args) -> dict[str, Any]:
    """CLI adapter: resolve defaults from the runtime layout, then reflect."""
    state_dir = default_state_dir()
    notes_path = state_dir / "reflections.jsonl"
    conn = kanban_db.connect(board=getattr(args, "board", None))
    try:
        return reflect(
            conn,
            notes_path=notes_path,
            outcomes_path=state_dir / "lever-outcomes.json",
        )
    finally:
        conn.close()


def run_outcomes_backfill(args) -> dict[str, Any]:
    """CLI adapter for bounded, explicit strategist outcome recalculation."""
    state_dir = default_state_dir()
    result = backfill_lever_outcomes(
        outcomes_path=state_dir / "lever-outcomes.json",
        apply=bool(getattr(args, "apply", False)),
        limit=getattr(args, "limit", None),
        lever_keys=getattr(args, "lever_key", None),
        root_task_ids=getattr(args, "root_task_id", None),
        statuses=getattr(args, "status", None),
    )
    if bool(getattr(args, "apply", False)):
        append_run_history(
            state_dir,
            {
                "ts": int(time.time()),
                "mode": "outcomes-backfill",
                "apply": True,
                "matched": int(result.get("matched", 0) or 0),
                "updated": int(result.get("updated", 0) or 0),
                "would_update": int(result.get("would_update", 0) or 0),
            },
        )
    return result


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
    "sollte noch",
)

TRIAGE_SEVERITIES = {"real-risk", "overdue", "scope-note", "none"}


def _age_days(created_at: Any, *, now: int) -> int | None:
    try:
        created = int(created_at)
    except (TypeError, ValueError):
        return None
    return max(0, (now - created) // 86400)


def _triage_severity(
    source_severity: Any, *, age_days: int | None, realrisk_escalate_days: int
) -> str:
    source = str(source_severity or "none")
    if source == "scope-note":
        return "scope-note"
    if source == "real-risk":
        if age_days is not None and age_days >= int(realrisk_escalate_days):
            return "overdue"
        return "real-risk"
    return "none"


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

    Merge-Strategie (zwei Quellen):
      1. Primär: ``load_followup_candidates_from_ledger`` — getypte follow_up-Items
         aus dem Disposition-Ledger (FRD Phase 1a+).
      2. Übergangs-Fallback: ``filter_followup_candidates`` auf Keyword-Match im
         Receipt-Text — für Alt-Receipts ohne Ledger-Item (vor FRD Phase 1a).
         Phase 4 kann diesen Pfad entfernen, sobald genug Ledger-Daten vorliegen.
    Dedup nach task_id: Ledger gewinnt — strukturierte Quelle schlägt Keyword.
    """
    state_dir = default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "harvest_last_run.json"
    now = int(time.time())
    since_ts = _read_harvest_since(marker, now=now)
    from hermes_cli.config import load_config

    cfg = load_config()
    realrisk_escalate_days = (
        cfg.get("kanban", {}).get("disposition_realrisk_escalate_days", 2)
        if isinstance(cfg, dict)
        else 2
    )

    conn = kanban_db.connect(board=getattr(args, "board", None))
    try:
        # Beide Quellen laden, bevor conn geschlossen wird.  Drop-Reaping ist
        # ein expliziter Status-Übergang (dismissed + decided_by), kein DELETE.
        reaped_dispositions = reap_worker_drop_disposition_items(conn)
        ledger_cands = load_followup_candidates_from_ledger(
            conn,
            since_ts=since_ts,
            realrisk_escalate_days=realrisk_escalate_days,
            now=now,
        )
        receipts = gather_recent_receipts(conn, since_ts=since_ts)
    finally:
        conn.close()

    keyword_cands = filter_followup_candidates(receipts)

    # Merge: Ledger-Kandidaten zuerst; Keyword nur für task_ids, die das Ledger noch
    # nicht kennt (Übergangs-Fallback für Alt-Receipts ohne Ledger-Item).
    ledger_task_ids: set[str] = {c["task_id"] for c in ledger_cands}
    keyword_only = [c for c in keyword_cands if c["task_id"] not in ledger_task_ids]
    candidates = ledger_cands + keyword_only

    cand_path = state_dir / "harvest_candidates.json"
    cand_path.write_text(
        json.dumps(
            {
                "generated_ts": now,
                "since_ts": since_ts,
                "ledger_candidates": len(ledger_cands),
                "keyword_candidates": len(keyword_cands),
                "reaped_dispositions": reaped_dispositions,
                "candidates": candidates,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    marker.write_text(json.dumps({"ts": now}), encoding="utf-8")
    append_run_history(
        state_dir,
        {
            "ts": now,
            "mode": "harvest",
            "receipts": len(receipts),
            "ledger_candidates": len(ledger_cands),
            "keyword_candidates": len(keyword_cands),
            "reaped_dispositions": reaped_dispositions,
            "candidates": len(candidates),
        },
    )
    return {
        "mode": "harvest",
        "since_ts": since_ts,
        "receipts": len(receipts),
        "ledger_candidates": len(ledger_cands),
        "keyword_candidates": len(keyword_cands),
        "reaped_dispositions": reaped_dispositions,
        "candidates": len(candidates),
        "candidates_path": str(cand_path),
    }
