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
import { profileLabel } from "./tones";
import type {
  AccountUsageProvider,
  AccountUsageWindow,
  CostProfileRow,
  IssueGroup,
  ReliabilityProfile,
  RunsDailyPoint,
  SubscriptionBurnClass,
  SubscriptionBurnLane,
  SubscriptionTokenBurnResponse,
  WindowedRollupRoot,
  WindowedRollupWorker,
} from "./schemas";

/** Semantic status of a figure/meter. */
export type FigureStatus = "ok" | "warn" | "crit" | "neutral";
/** Error-taxonomy segment fills, severity order — Leitstand status tokens (no raw hex). */
export const ERROR_SERIES = [
  "var(--color-status-alert)",
  "var(--color-status-warn)",
  "var(--color-brand)",
  "var(--color-ink-3)",
] as const;

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

// ── Kettenkosten: window/root/worker money split ─────────────────────────────
// Real spend and subscription-equivalent value are intentionally separate: the
// view may highlight both, but must not collapse them back into one effective
// number. null on roots remains null so unknown stamps are not rendered as $0.
export interface LedgerMoneySplit {
  echt: number | null;
  abo: number | null;
}

export interface ChainMoneySplit extends LedgerMoneySplit {
  effective: number | null;
}

export interface WindowCostSummary {
  echtUsd: number;
  aboUsd: number;
}

export type MotherLedgerSortKey = "usd" | "tokens" | "runs";

export function workerTokens(worker: WindowedRollupWorker): number {
  return worker.input_tokens + worker.output_tokens;
}

export function rootRuns(root: WindowedRollupRoot): number {
  return root.workers.reduce((sum, worker) => sum + worker.run_count, 0);
}

export function rootTokens(root: WindowedRollupRoot): number {
  return root.workers.reduce((sum, worker) => sum + workerTokens(worker), 0);
}

export function windowCostSummary(roots: WindowedRollupRoot[]): WindowCostSummary {
  return roots.reduce<WindowCostSummary>(
    (sum, root) => ({
      echtUsd: sum.echtUsd + (root.cost_usd ?? 0),
      aboUsd: sum.aboUsd + (root.cost_usd_equivalent ?? 0),
    }),
    { echtUsd: 0, aboUsd: 0 },
  );
}

export function chainCost(root: WindowedRollupRoot): ChainMoneySplit {
  return {
    echt: root.cost_usd,
    abo: root.cost_usd_equivalent,
    effective: root.cost_effective_usd,
  };
}

export function workerCost(worker: WindowedRollupWorker): LedgerMoneySplit {
  return {
    echt: worker.cost_usd,
    abo: worker.cost_usd_equivalent,
  };
}

export function chainShare(root: WindowedRollupRoot, topAbo: number): number {
  if (topAbo <= 0) return 0;
  return Math.max(0, Math.min(1, (root.cost_usd_equivalent ?? 0) / topAbo));
}

export function rootUsd(root: WindowedRollupRoot): number | null {
  return chainCost(root).effective ?? root.cost_usd ?? null;
}

export function sortedLedgerRoots(roots: WindowedRollupRoot[], sortKey: MotherLedgerSortKey): WindowedRollupRoot[] {
  const value = (root: WindowedRollupRoot): number => {
    if (sortKey === "tokens") return rootTokens(root);
    if (sortKey === "runs") return rootRuns(root);
    return root.cost_usd_equivalent ?? 0;
  };
  return [...roots].sort((a, b) => {
    const primary = value(b) - value(a);
    if (primary !== 0) return primary;
    const meteredTie = (b.cost_usd ?? 0) - (a.cost_usd ?? 0);
    if (meteredTie !== 0) return meteredTie;
    return rootTokens(b) - rootTokens(a);
  });
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
  status: FigureStatus;
  lowSample: boolean;
}

export function reliabilityStatus(rate: number | null, lowSample: boolean): FigureStatus {
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
// aligned to ERROR_SERIES [red, amber, navy, neutral].
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
      color: ERROR_SERIES[i],
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

// ── ST5 · Budget-Ledger (Provider-Limits, Engpass zuerst) ───────────────────
// Aus GET /api/account-usage: pro Provider die knappste (höchstes used_percent)
// Fenster-Zeile. Claude (anthropic) / ChatGPT (openai-codex) liefern echte
// OAuth-Fenster (session/weekly); Kimi ist geschätzt (source=
// kanban_subscription_tokens, kein Provider-Limit) und wird so markiert.
const PROVIDER_LABEL: Record<string, string> = {
  anthropic: "Claude",
  "openai-codex": "ChatGPT",
  kimi: "Kimi",
};
const WINDOW_LABEL: Record<string, string> = {
  session: "Session",
  weekly: "Woche",
  opus_week: "Opus · Woche",
  sonnet_week: "Sonnet · Woche",
};
/** source-Wert, der eine geschätzte (kein-Provider-Limit) Bilanz markiert. */
export const ESTIMATED_SOURCE = "kanban_subscription_tokens";

export interface LedgerEntry {
  provider: string;
  label: string;
  /** menschliches Fenster-Label der knappsten Zeile; "" wenn keine Zeile. */
  window: string;
  /** 0..100 used_percent der knappsten Zeile; null wenn unbekannt. */
  usedPercent: number | null;
  status: FigureStatus;
  /** Kimi-Schätzung — kein echtes Provider-Kontingent. */
  estimated: boolean;
  resetAt: string | null;
  available: boolean;
  unavailableReason: string | null;
}

/** Auslastungs-Status: je näher am Limit, desto kritischer. */
export function budgetStatus(usedPercent: number | null): FigureStatus {
  if (usedPercent == null) return "neutral";
  if (usedPercent >= 90) return "crit";
  if (usedPercent >= 75) return "warn";
  return "ok";
}

/** Knappstes Fenster (höchstes used_percent); null, wenn kein Fenster ein
 *  bekanntes used_percent hat (z. B. Kimi ohne konfiguriertes Cap). */
function tightestWindow(windows: AccountUsageWindow[]): AccountUsageWindow | null {
  let top: AccountUsageWindow | null = null;
  for (const w of windows) {
    if (w.used_percent == null) continue;
    if (top == null || w.used_percent > (top.used_percent ?? -1)) top = w;
  }
  return top;
}

/** Eine Ledger-Zeile je Provider (knappste Auslastung), Engpass zuerst. */
export function budgetLedger(providers: AccountUsageProvider[]): LedgerEntry[] {
  const entries: LedgerEntry[] = providers.map((p) => {
    const top = tightestWindow(p.windows);
    const usedPercent = top?.used_percent ?? null;
    return {
      provider: p.provider,
      label: PROVIDER_LABEL[p.provider] ?? p.provider,
      window: top ? WINDOW_LABEL[top.window_key ?? ""] ?? top.label : "",
      usedPercent,
      status: budgetStatus(usedPercent),
      estimated: p.source === ESTIMATED_SOURCE,
      resetAt: top?.reset_at ?? null,
      available: p.available,
      unavailableReason: p.unavailable_reason,
    };
  });
  // Engpass zuerst: höchstes used_percent oben, unbekannte (null) ans Ende.
  return entries.sort((a, b) => (b.usedPercent ?? -1) - (a.usedPercent ?? -1));
}

// ── ST5 · Flotten-Effizienz ─────────────────────────────────────────────────
// Token-Burn je Lane aus runs_costs().profiles[] (In+Out-Tokens, phantom-
// gefiltert wie das Leaderboard), absteigend nach Burn. Das ist Lane-
// Attribution (welche Lane verbrennt das Budget), keine Vanity-Tagesmetrik.
export interface LaneBurn {
  profile: string;
  label: string;
  /** input + output Tokens im Fenster. */
  tokens: number;
  /** ≈ API-Wert (echte $ + Subscription-Äquivalent); null wenn ungestampt. */
  costEquivalent: number | null;
  /** Tatsächliche Kosten inkl. kWh-basierter Neuralwatt-Abrechnung. */
  actualCostUsd: number | null;
  neuralwattKwh: number | null;
  neuralwattCostUsd: number | null;
  /** metered $-Rohkomponente (p.cost_usd); null wenn ungestampt. Abo-Lanes
   *  stempeln hier ehrliche 0 (K17) → der Renderer leitet daraus den
   *  "gesch."-Schätzwert-Marker via formatEffectiveCost ab. */
  costUsd: number | null;
  runs: number;
}

export interface SubscriptionBurnFlag {
  kind: "top" | "anti";
  title: string;
  detail: string;
  tokens: number;
}

export interface SubscriptionBurnTrendPoint {
  date: string;
  total_tokens: number;
  runs: number;
  /** Share of the window total (0..1). */
  share: number;
}

export interface SubscriptionBurnBreakdown {
  totals: SubscriptionTokenBurnResponse["totals"];
  topLanes: Array<SubscriptionBurnLane & { share: number }>;
  classes: Array<SubscriptionBurnClass & { share: number }>;
  flags: SubscriptionBurnFlag[];
  subscriptionCount: number;
  /** Daily aggregated burn (all subscriptions summed), ascending by date. */
  trend: SubscriptionBurnTrendPoint[];
}

export function laneBurn(profiles: CostProfileRow[], limit = 5): LaneBurn[] {
  return rosterProfiles(profiles)
    .map((p) => {
      const tokens = (p.input_tokens ?? 0) + (p.output_tokens ?? 0);
      const cost = (p.cost_usd ?? 0) + (p.cost_usd_equivalent ?? 0);
      const actualCost = p.actual_cost_usd ?? p.cost_usd ?? null;
      const neuralwattKwh = p.billing_neuralwatt_kwh ?? null;
      const neuralwattCostUsd = p.billing_neuralwatt_cost_usd ?? null;
      return {
        profile: p.profile,
        label: profileLabel[p.profile] ?? p.profile,
        tokens,
        costEquivalent: cost > 0 ? cost : null,
        actualCostUsd: actualCost,
        neuralwattKwh,
        neuralwattCostUsd,
        costUsd: p.cost_usd,
        runs: p.runs,
      };
    })
    .filter((l) => l.tokens > 0)
    .sort((a, b) => b.tokens - a.tokens)
    .slice(0, limit);
}

export function subscriptionBurnBreakdown(
  burn: SubscriptionTokenBurnResponse | null | undefined,
  limit = 5,
): SubscriptionBurnBreakdown {
  const totals = burn?.totals ?? { runs: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0 };
  const totalTokens = Math.max(0, totals.total_tokens || 0);
  const pct = (share: number) => `${Math.round(share * 100)} %`;
  const withShare = <T extends { total_tokens: number }>(row: T): T & { share: number } => ({
    ...row,
    share: totalTokens > 0 ? row.total_tokens / totalTokens : 0,
  });
  const topLanes = [...(burn?.by_lane ?? [])]
    .filter((row) => row.total_tokens > 0)
    .sort((a, b) => b.total_tokens - a.total_tokens)
    .slice(0, limit)
    .map(withShare);
  const classes = [...(burn?.by_class ?? [])]
    .filter((row) => row.total_tokens > 0)
    .sort((a, b) => b.total_tokens - a.total_tokens)
    .slice(0, limit)
    .map(withShare);
  const subscriptionCount = new Set(
    (burn?.by_lane ?? []).filter((row) => row.total_tokens > 0).map((row) => row.subscription),
  ).size;
  const flags = [
    ...topLanes.slice(0, 3).map((row): SubscriptionBurnFlag => ({
      kind: "top",
      title: `${profileLabel[row.profile] ?? row.profile} · ${row.subscription}`,
      detail: `${pct(row.share)} des Fenster-Burns`,
      tokens: row.total_tokens,
    })),
    ...classes
      .filter((row) => row.value_class !== "nutzer" && row.share >= 0.2)
      .slice(0, 3)
      .map((row): SubscriptionBurnFlag => ({
        kind: "anti",
        title: `${row.value_class} · ${row.subscription}`,
        detail: `${pct(row.share)} nicht-nutzernaher Burn`,
        tokens: row.total_tokens,
      })),
  ]
    .sort((a, b) => b.tokens - a.tokens)
    .slice(0, limit);

  // Aggregate daily rows by date (sum across all subscriptions), then sort asc.
  const dailyByDate = new Map<string, { total_tokens: number; runs: number }>();
  for (const row of burn?.daily ?? []) {
    if (!row.date) continue;
    const existing = dailyByDate.get(row.date);
    if (existing) {
      existing.total_tokens += row.total_tokens;
      existing.runs += row.runs;
    } else {
      dailyByDate.set(row.date, { total_tokens: row.total_tokens, runs: row.runs });
    }
  }
  const trend: SubscriptionBurnTrendPoint[] = [...dailyByDate.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, { total_tokens, runs }]) => ({
      date,
      total_tokens,
      runs,
      share: totalTokens > 0 ? total_tokens / totalTokens : 0,
    }));

  return { totals, topLanes, classes, flags, subscriptionCount, trend };
}

/** Gate-Effektivität = Σ rejected / Σ runs über die Roster-Profile (Reliability-Rows):
 *  Anteil der Läufe, die das Verifier-Gate abgelehnt hat. Roster-gefiltert wie die
 *  Akzeptanz-Headline und `laneBurn` — Phantome ("w"/"unbekannt") fließen NICHT in
 *  den Nenner, sonst verdünnt ein 3025-Läufe-Phantom 12/396 auf 12/3438 → "0 %".
 *  null, wenn keine Läufe im Fenster. */
export function gateEffectiveness(profiles: ReliabilityProfile[]): number | null {
  const roster = rosterProfiles(profiles);
  const runs = roster.reduce((acc, p) => acc + p.runs, 0);
  if (runs <= 0) return null;
  const rejected = roster.reduce((acc, p) => acc + p.rejected, 0);
  return rejected / runs;
}
