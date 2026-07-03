// Pure helpers + types for the dedicated Strategist surface (G1).
//
// The surface lists held `freigabe:operator` proposals the strategist-cron
// drafted (each with a target metric, ROI estimate and a paired counter-metric)
// and shows the distilled Vision metric snapshot (H1) as triage context. These
// helpers are pure so they can be unit-tested without the polling/render path.

import type { LeverOutcome } from "./schemas";
import { toneClasses } from "./tones";

/** One held strategist proposal (a `freigabe:operator` root awaiting triage). */
export interface StrategistProposal {
  id: string;
  title: string;
  /** Short title with the `PlanSpec <key>:` prefix stripped (backend-computed). */
  display_title?: string | null;
  created_by: string | null;
  created_at: number;
  /** Unix-Epoch since when this root has been held (backend-computed; only
   *  roots carry `freigabe`, stamped at ingest, so `created_at` IS the hold
   *  start — no separate event needed). */
  held_since: number;
  /** Seconds held so far, computed server-side at response time. */
  age_seconds: number;
  subtask_count: number;
  /** Ziel-Kennzahl — null when the strategist left it unannotated. */
  target_metric: string | null;
  /** ROI-Schätzung — null when unannotated. */
  roi: string | null;
  /** Gepaarte Counter-Metrik (Guardrail) — null when unannotated. */
  counter_metric: string | null;
  /** Grounding-Evidenz (Code-/git-log-Beleg, STRATEGIST-SELF-GROUNDING) — null
   *  when the strategist left it unannotated (operator-authored specs carry none). */
  grounding: string | null;
  /** Provenance category: "receipt" | "gate" | "metric" | "other". */
  source?: string | null;
  /** For receipt-keyed proposals: the title of the origin task. */
  origin?: string | null;
}

export interface StrategistProposalsResponse {
  proposals: StrategistProposal[];
  count: number;
  /** The distilled Vision snapshot (H1); null until a snapshot is written. */
  metrics: Record<string, unknown> | null;
  /** Longest-held proposal's age in seconds; null when the list is empty. */
  oldest_age_seconds: number | null;
  checked_at: number;
}

/** Age (seconds) above which a held proposal's badge is flagged as stale. */
export const STALE_HOLD_SECONDS = 48 * 3600;

/** True once a held proposal has waited longer than STALE_HOLD_SECONDS. */
export function isStaleHold(ageSeconds: number): boolean {
  return ageSeconds > STALE_HOLD_SECONDS;
}

/** Oldest-first (longest held first) ordering for the triage list. */
export function sortProposalsByAge(proposals: StrategistProposal[]): StrategistProposal[] {
  return [...proposals].sort((a, b) => a.held_since - b.held_since);
}

export interface MetricRow {
  key: string;
  label: string;
  value: string;
}

/** snake_case / kebab-case → "Title Case" fallback label. */
export function humanizeMetricKey(key: string): string {
  return key
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
}

// Curated headline metrics pulled from the nested `metrics` object in the
// vision-metrics.json envelope.  Each entry: [nested path parts, label, formatter].
type MetricSpec = {
  path: [string, string];
  label: string;
  fmt: (v: number) => string;
};

const round1 = (v: number) => String(Math.round(v * 10) / 10);

const CURATED_METRICS: MetricSpec[] = [
  { path: ["autonomy", "autonomy_pct"],                              label: "Autonomie",               fmt: (v) => `${round1(v)}%` },
  { path: ["green_gate_streak", "streak"],                           label: "Green-Gate-Streak",       fmt: (v) => String(Math.round(v)) },
  { path: ["escalation_rate", "escalations_per_week"],               label: "Eskalationen/Woche",      fmt: (v) => String(Math.round(v)) },
  { path: ["cost_per_task", "recent_avg_cost_per_task"],             label: "Kosten/Aufgabe",          fmt: (v) => `$${v.toFixed(2)}` },
  { path: ["classification_coverage", "coverage_pct"],               label: "Klassifik.-Abdeckung",    fmt: (v) => `${round1(v)}%` },
];

/** Turn the Vision metrics envelope into ordered curated display rows.
 *
 *  The live `vision-metrics.json` is nested:
 *  `{ schema_version, generated_at, metrics: { autonomy: {…}, … } }`.
 *  We unwrap the inner `metrics` object and pull only the curated headline set.
 *  Falls back to treating the input as flat when no inner `metrics` key exists
 *  (forward-compat safety). Returns [] for null/undefined/non-object/empty. */
export function metricSnapshotRows(input: Record<string, unknown> | null | undefined): MetricRow[] {
  if (!input || typeof input !== "object") return [];
  // Unwrap envelope: if input.metrics is itself an object, use it.
  const inner: Record<string, unknown> =
    input.metrics && typeof input.metrics === "object" && !Array.isArray(input.metrics)
      ? (input.metrics as Record<string, unknown>)
      : input;
  if (Object.keys(inner).length === 0) return [];

  const rows: MetricRow[] = [];
  for (const spec of CURATED_METRICS) {
    const [group, field] = spec.path;
    const groupObj = inner[group];
    if (!groupObj || typeof groupObj !== "object" || Array.isArray(groupObj)) continue;
    const raw = (groupObj as Record<string, unknown>)[field];
    if (typeof raw !== "number" || !Number.isFinite(raw)) continue;
    rows.push({ key: `${group}.${field}`, label: spec.label, value: spec.fmt(raw) });
  }
  return rows;
}

const SOURCE_LABELS: Record<string, string> = {
  "strategist-cron": "Stratege",
  strategist: "Stratege",
};

/** Human label for the proposal's origin (created_by). @deprecated Use sourceLabel instead. */
export function proposalSource(createdBy: string | null): string {
  if (!createdBy) return "Stratege";
  return SOURCE_LABELS[createdBy] ?? createdBy;
}

/** Human-readable one-liner for a harvest or propose run. */
export function runSummaryText(
  kind: "harvest" | "propose",
  run: { ts?: number; receipts?: number; candidates?: number; ingested?: number } | null,
): string {
  if (!run) return "noch nicht gelaufen";
  if (kind === "harvest") {
    const r = run.receipts ?? 0, c = run.candidates ?? 0;
    return `${r} Receipt${r === 1 ? "" : "s"} → ${c} ${c === 1 ? "Vorschlag" : "Vorschläge"}`;
  }
  const i = run.ingested ?? 0;
  return `${i} ${i === 1 ? "Vorschlag" : "Vorschläge"}`;
}

/** Human label for the provenance `source` field. */
export function sourceLabel(source: string | null | undefined): string {
  switch (source) {
    case "receipt": return "Aus eurer Arbeit";
    case "gate":    return "Gate-Heilung";
    case "metric":  return "Aus Kennzahl";
    default:        return "Stratege";
  }
}

// `created_by` values authored by the strategist control-plane: the propose-cron
// (`strategist-cron`, incl. ledger-harvested ROI levers) and the gate self-heal
// (`green-gate-autoheal`). The held `freigabe:operator` surface is shared by ANY
// hand-ingested PlanSpec too (freigabe:operator is the generic operator gate, not
// a strategist marker) — those carry a different author and are NOT strategist
// proposals, so we split them out instead of letting them masquerade as one.
export const STRATEGIST_AUTHORS = new Set(["strategist-cron", "green-gate-autoheal"]);

/** True when a proposal's `created_by` is one of the strategist control-plane authors. */
export function isStrategistAuthored(createdBy: string | null | undefined): boolean {
  return !!createdBy && STRATEGIST_AUTHORS.has(createdBy.trim());
}

export interface PartitionedProposals {
  /** The strategist's own self-gated drafts (the real "Stratege-Ergebnis"). */
  strategist: StrategistProposal[];
  /** Hand-ingested operator-held PlanSpecs that merely share this surface. */
  manual: StrategistProposal[];
}

/** Split held `freigabe:operator` proposals by true provenance (`created_by`),
 *  preserving input order within each group. */
export function partitionProposals(proposals: StrategistProposal[]): PartitionedProposals {
  const strategist: StrategistProposal[] = [];
  const manual: StrategistProposal[] = [];
  for (const p of proposals) {
    (isStrategistAuthored(p.created_by) ? strategist : manual).push(p);
  }
  return { strategist, manual };
}

// ── Wirkungs-Historie (Ziel-4) ──────────────────────────────────────────────
// Shipped levers get measured MATURITY_DAYS after release (reflect step); the
// verdict compares the metric_key's delta against its direction. `verdict` is
// `null` on records not measured yet (status "proposed"/"shipped") — the
// surface treats that the same as the writer's own "unknown" string, since
// both mean "no evidence yet" to the operator.

const OUTCOME_STATUS_LABELS: Record<string, string> = {
  proposed: "Vorgeschlagen",
  shipped: "Geshippt",
  measured: "Gemessen",
};

/** Human label for a lever-outcome's lifecycle `status`. */
export function outcomeStatusLabel(status: string | null | undefined): string {
  if (!status) return "Unbekannt";
  return OUTCOME_STATUS_LABELS[status] ?? status;
}

const OUTCOME_VERDICT_LABELS: Record<string, string> = {
  improved: "verbessert",
  worsened: "verschlechtert",
  neutral: "neutral",
  unmeasurable: "nicht messbar",
  unchanged: "unverändert",
  unknown: "unbekannt",
};

/** Human label for a lever-outcome's `verdict` — `null`/unrecognised → "unbekannt". */
export function outcomeVerdictLabel(verdict: string | null | undefined): string {
  return OUTCOME_VERDICT_LABELS[verdict ?? ""] ?? OUTCOME_VERDICT_LABELS.unknown;
}

/** Tone classes for the verdict chip: improved emerald / worsened red /
 *  neutral/unchanged zinc / unmeasurable amber / unknown (incl. `null`, not
 *  measured yet) dim — no tint. */
export function outcomeVerdictToneClass(verdict: string | null | undefined): string {
  switch (verdict) {
    case "improved": return toneClasses("emerald");
    case "worsened": return toneClasses("red");
    case "unmeasurable": return toneClasses("amber");
    case "neutral":
    case "unchanged": return toneClasses("zinc");
    default: return "border-white/10 bg-white/[.03] hc-dim";
  }
}

/** Return the scalar signed `delta` written by the backend — `null` when
 *  unmeasured or the value isn't finite. */
export function outcomeDeltaValue(outcome: LeverOutcome): number | null {
  const delta = outcome.delta;
  return typeof delta === "number" && Number.isFinite(delta) ? delta : null;
}

/** Format a delta value with an explicit sign: "+6", "-3.2", "0". */
export function formatSignedDelta(value: number): string {
  const rounded = Math.round(value * 100) / 100;
  return rounded > 0 ? `+${rounded}` : String(rounded);
}
