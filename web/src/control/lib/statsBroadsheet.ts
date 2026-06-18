/**
 * ST4 — pure data logic for the /control/statistik broadsheet (PlanSpec
 * 2026-06-17, Richtung B · Broadsheet). Every function is side-effect-free and
 * unit-tested (statsBroadsheet.test.ts); the view (StatistikView) only formats
 * and composes the Broadsheet primitives around these numbers.
 *
 * The masthead headline is the fleet Akzeptanzrate (verifier verdicts), with
 * three supporting KPIs (Autonomie, Kosten je Lieferung, Nutzerwert). The
 * reliability leaderboard is phantom-filtered against the known worker roster,
 * and the error taxonomy buckets the recurring failures — all of which are
 * harness-lifecycle endstates, never product-logic bugs.
 */
import { broadsheet, type BroadsheetStatus } from "./broadsheetTokens";
import { profileLabel } from "./tones";
import type { IssueGroup, ReliabilityProfile, RunsDailyPoint } from "./schemas";

// ── Phantom filter ──────────────────────────────────────────────────────────
// The known worker roster — mirrors ~/.hermes/profiles/ and the control-wide
// profileLabel map. A reliability/cost row whose profile isn't in the roster is
// a phantom: NULL/legacy sentinels the backend coerces to names like "w",
// "unbekannt" or "(ohne profil)". The masthead/leaderboard count only real
// configured workers, so those phantoms are dropped.
export const ROSTER_PROFILES: ReadonlySet<string> = new Set<string>([
  ...Object.keys(profileLabel),
  // FO-build lanes present on disk but not in the orchestrator label map.
  "family-ui",
  "fo-brain",
]);

export function isRosterProfile(profile: string): boolean {
  return ROSTER_PROFILES.has(profile);
}

export function rosterProfiles<T extends { profile: string }>(rows: T[]): T[] {
  return rows.filter((r) => isRosterProfile(r.profile));
}

// ── Masthead: Akzeptanzrate (verifier verdicts) ─────────────────────────────
export interface AcceptanceStat {
  approved: number;
  rejected: number;
  /** approved / (approved + rejected); null when there are no verdicts. */
  rate: number | null;
}

export function acceptance(profiles: ReliabilityProfile[]): AcceptanceStat {
  let approved = 0;
  let rejected = 0;
  for (const p of profiles) {
    approved += p.approved;
    rejected += p.rejected;
  }
  const judged = approved + rejected;
  return { approved, rejected, rate: judged > 0 ? approved / judged : null };
}

/** Δ of the acceptance rate against the 30-day baseline, in percentage points. */
export function acceptanceDelta(
  profiles: ReliabilityProfile[],
  baseline: ReliabilityProfile[],
): number | null {
  const cur = acceptance(profiles).rate;
  const base = acceptance(baseline).rate;
  if (cur == null || base == null) return null;
  return Math.round((cur - base) * 100);
}

// ── Supporting KPI: Autonomie (completed runs ÷ runs) ───────────────────────
export function profileCompletedRuns(p: ReliabilityProfile): number {
  const explicit = p.outcomes?.completed;
  if (typeof explicit === "number") return explicit;
  return p.completed_rate == null ? 0 : Math.round(p.completed_rate * p.runs);
}

/** Share of runs that completed without giving up/crashing; null if no runs. */
export function autonomy(profiles: ReliabilityProfile[]): number | null {
  const runs = profiles.reduce((acc, p) => acc + p.runs, 0);
  if (runs <= 0) return null;
  const completed = profiles.reduce((acc, p) => acc + profileCompletedRuns(p), 0);
  return completed / runs;
}

// ── Supporting KPI: Kosten je Lieferung (measured $ ÷ delivered roots) ───────
// Uses real task_runs.cost_usd only; subscription roots ($0) drag the average
// toward the honest marginal price. null when nothing measured or no roots.
export function costPerDelivery(series: RunsDailyPoint[]): number | null {
  let cost = 0;
  let roots = 0;
  let measured = false;
  for (const p of series) {
    if (p.cost_usd != null) {
      cost += p.cost_usd;
      measured = true;
    }
    roots += p.done_roots;
  }
  if (!measured || roots <= 0) return null;
  return cost / roots;
}

// ── Supporting KPI: Nutzerwert (delivered user-feature roots) ────────────────
export function nutzerwert(series: RunsDailyPoint[]): number {
  return series.reduce((acc, p) => acc + (p.done_roots_by_class?.nutzer ?? 0), 0);
}

// ── Reliability leaderboard ─────────────────────────────────────────────────
export interface LeaderEntry {
  profile: string;
  label: string;
  /** completed_rate (0..1) or null when undamped/unknown. */
  rate: number | null;
  runs: number;
  status: BroadsheetStatus;
  lowSample: boolean;
}

export function reliabilityStatus(rate: number | null, lowSample: boolean): BroadsheetStatus {
  if (lowSample || rate == null) return "neutral";
  if (rate >= 0.85) return "ok";
  if (rate >= 0.6) return "warn";
  return "crit";
}

/** Phantom-filtered, sorted: well-sampled first, then completion-rate desc. */
export function leaderboard(profiles: ReliabilityProfile[]): LeaderEntry[] {
  return rosterProfiles(profiles)
    .map((p) => ({
      profile: p.profile,
      label: profileLabel[p.profile] ?? p.profile,
      rate: p.completed_rate,
      runs: p.runs,
      status: reliabilityStatus(p.completed_rate, p.low_sample),
      lowSample: p.low_sample,
    }))
    .sort((a, b) => {
      if (a.lowSample !== b.lowSample) return a.lowSample ? 1 : -1;
      const ar = a.rate ?? -1;
      const br = b.rate ?? -1;
      if (br !== ar) return br - ar;
      return b.runs - a.runs;
    });
}

// ── Error taxonomy ──────────────────────────────────────────────────────────
// Every failure surfaced by /runs/issues is a harness-lifecycle endstate (the
// backend only groups these outcomes). We fold them into four severity buckets,
// aligned to broadsheet.errorSeries [red, amber, navy, neutral].
export const LIFECYCLE_OUTCOMES: ReadonlySet<string> = new Set([
  "crashed",
  "spawn_failed",
  "timed_out",
  "gave_up",
  "iteration_budget_exhausted",
  "blocked",
]);

interface BucketDef {
  key: "dead" | "timeout" | "budget" | "other";
  outcomes: string[];
}
const ERROR_BUCKETS: BucketDef[] = [
  { key: "dead", outcomes: ["crashed", "spawn_failed"] },
  { key: "timeout", outcomes: ["timed_out"] },
  { key: "budget", outcomes: ["gave_up", "iteration_budget_exhausted"] },
  { key: "other", outcomes: ["blocked"] }, // + any residual outcome
];

export interface ErrorBucket {
  key: BucketDef["key"];
  count: number;
  color: string;
  /** count / total as a 0..100 width. */
  pct: number;
}

export interface ErrorTaxonomy {
  buckets: ErrorBucket[];
  total: number;
  /** true when every counted outcome is a known harness-lifecycle endstate. */
  allLifecycle: boolean;
}

export function errorTaxonomy(issues: IssueGroup[]): ErrorTaxonomy {
  const counts: Record<string, number> = {};
  let total = 0;
  for (const group of issues) {
    for (const [outcome, n] of Object.entries(group.outcomes ?? {})) {
      counts[outcome] = (counts[outcome] ?? 0) + n;
      total += n;
    }
  }
  const known = new Set(ERROR_BUCKETS.flatMap((b) => b.outcomes));
  const buckets = ERROR_BUCKETS.map((b, i) => {
    let count = b.outcomes.reduce((acc, o) => acc + (counts[o] ?? 0), 0);
    if (b.key === "other") {
      for (const [o, n] of Object.entries(counts)) {
        if (!known.has(o)) count += n;
      }
    }
    return {
      key: b.key,
      count,
      color: broadsheet.errorSeries[i],
      pct: total > 0 ? (count / total) * 100 : 0,
    };
  }).filter((b) => b.count > 0);
  const allLifecycle = Object.keys(counts).every((o) => LIFECYCLE_OUTCOMES.has(o));
  return { buckets, total, allLifecycle };
}

// ── German date for the masthead meta (UTC for deterministic tests) ─────────
const MONTHS_DE = [
  "Januar", "Februar", "März", "April", "Mai", "Juni",
  "Juli", "August", "September", "Oktober", "November", "Dezember",
];
export function germanDate(epochSec: number): string {
  const d = new Date(epochSec * 1000);
  return `${d.getUTCDate()}. ${MONTHS_DE[d.getUTCMonth()]}`;
}
