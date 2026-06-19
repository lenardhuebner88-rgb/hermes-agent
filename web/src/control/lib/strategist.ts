// Pure helpers + types for the dedicated Strategist surface (G1).
//
// The surface lists held `freigabe:operator` proposals the strategist-cron
// drafted (each with a target metric, ROI estimate and a paired counter-metric)
// and shows the distilled Vision metric snapshot (H1) as triage context. These
// helpers are pure so they can be unit-tested without the polling/render path.

/** One held strategist proposal (a `freigabe:operator` root awaiting triage). */
export interface StrategistProposal {
  id: string;
  title: string;
  created_by: string | null;
  created_at: number;
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
}

export interface StrategistProposalsResponse {
  proposals: StrategistProposal[];
  count: number;
  /** The distilled Vision snapshot (H1); null until a snapshot is written. */
  metrics: Record<string, unknown> | null;
  checked_at: number;
}

export interface MetricRow {
  key: string;
  label: string;
  value: string;
}

// Friendly German labels for the metric keys H1 is expected to write. Unknown
// keys still render — humanised from snake_case — so the surface never hides a
// metric just because this map lags the writer.
const METRIC_LABELS: Record<string, string> = {
  autonomy_pct: "Autonomie-%",
  autonomy: "Autonomie-%",
  cost_per_task: "Kosten/Task",
  cost_per_task_usd: "Kosten/Task",
  cost_trend: "Kosten-Trend",
  escalation_rate: "Eskalations-Rate",
  escalation_rate_per_week: "Eskalations-Rate/Woche",
  green_gate_streak: "Green-Gate-Streak",
  // Paired counter-metrics (guardrails).
  missed_escalation_rate: "Fehl-Eskalations-Rate",
  should_have_escalated_rate: "Hätte-eskalieren-sollen-Rate",
  generated_at: "Stand",
  window: "Fenster",
};

/** snake_case / kebab-case → "Title Case" fallback label. */
export function humanizeMetricKey(key: string): string {
  return key
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
}

function formatMetricValue(key: string, value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "boolean") return value ? "ja" : "nein";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return "—";
    // Percent-ish keys get a % suffix; otherwise trim to 2 decimals max.
    const rounded = Math.round(value * 100) / 100;
    if (/pct|percent|rate/.test(key)) return `${rounded}%`;
    return String(rounded);
  }
  if (typeof value === "string") return value;
  // Objects/arrays: compact JSON so a nested counter-metric still shows.
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/** Turn the (free-shaped) metric snapshot into ordered display rows. Returns []
 *  for a null/empty/invalid snapshot so the caller can show "no snapshot yet". */
export function metricSnapshotRows(metrics: Record<string, unknown> | null | undefined): MetricRow[] {
  if (!metrics || typeof metrics !== "object") return [];
  return Object.entries(metrics).map(([key, value]) => ({
    key,
    label: METRIC_LABELS[key] ?? humanizeMetricKey(key),
    value: formatMetricValue(key, value),
  }));
}

const SOURCE_LABELS: Record<string, string> = {
  "strategist-cron": "Stratege",
  strategist: "Stratege",
};

/** Human label for the proposal's origin (created_by). */
export function proposalSource(createdBy: string | null): string {
  if (!createdBy) return "Stratege";
  return SOURCE_LABELS[createdBy] ?? createdBy;
}
