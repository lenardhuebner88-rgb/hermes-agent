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
  /** Short title with the `PlanSpec <key>:` prefix stripped (backend-computed). */
  display_title?: string | null;
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
  checked_at: number;
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

/** Human label for the provenance `source` field. */
export function sourceLabel(source: string | null | undefined): string {
  switch (source) {
    case "receipt": return "Aus eurer Arbeit";
    case "gate":    return "Gate-Heilung";
    case "metric":  return "Aus Kennzahl";
    default:        return "Stratege";
  }
}
