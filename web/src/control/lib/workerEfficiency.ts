import { profileLabel } from "./tones";
import { isRosterProfile, workerTokens } from "./statsBroadsheet";
import type { ReliabilityProfile, WindowedRollupRoot } from "./schemas";

export const WORKER_EFFICIENCY_BENCH_PROFILES = ["premium", "coder", "reviewer", "scout"] as const;

/** Mirror of the backend roster-stats min-n gate (runs_reliability default = 5).
 *  A review-return rate is only asserted once at least this many of the worker's
 *  outputs were judged — below it the rate stays null (renders "—"), exactly how
 *  the backend suppresses approve_rate under min_n. Prevents a single judged run
 *  from displaying "100 % zurück" and driving a lever off one sample. */
export const DEFAULT_MIN_REVIEW_SAMPLE = 5;

export type WorkerEfficiencyDataQuality = "complete" | "partial" | "missing_review_link";

export interface WorkerEfficiencyRow {
  profile: string;
  label: string;
  tasks: number;
  runs: number;
  tokens_total: number;
  runtime_seconds: number | null;
  cost_effective_usd: number | null;
  token_per_task: number | null;
  token_per_min: number | null;
  cost_per_task: number | null;
  reviewed_outputs: number | null;
  review_request_changes: number | null;
  review_return_rate: number | null;
  data_quality: WorkerEfficiencyDataQuality;
}

export interface WorkerEfficiencyLever {
  key: "coder_review" | "premium_risk" | "token_drag";
  label: string;
  detail: string;
}

interface WorkerEfficiencyAccumulator {
  profile: string;
  rootIds: Set<string>;
  runs: number;
  tokens: number;
  runtimeSeconds: number;
  runtimeEvidence: number;
  costEffectiveUsd: number;
  costEvidence: number;
}

const finitePositive = (value: number | null | undefined): number | null =>
  typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;

const finiteNonNegative = (value: number | null | undefined): number | null =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;

function workerEffectiveCost(worker: WindowedRollupRoot["workers"][number]): number | null {
  const direct = finitePositive(worker.cost_effective_usd);
  if (direct != null) return direct;
  const split = (finitePositive(worker.cost_usd) ?? 0) + (finitePositive(worker.cost_usd_equivalent) ?? 0);
  return split > 0 ? split : null;
}

function reliabilityByProfile(profiles: ReliabilityProfile[]): Map<string, ReliabilityProfile> {
  return new Map(profiles.filter((row) => isRosterProfile(row.profile)).map((row) => [row.profile, row]));
}

export function buildWorkerEfficiencyRows(
  roots: WindowedRollupRoot[],
  reliabilityProfiles: ReliabilityProfile[],
  minReviewSample: number = DEFAULT_MIN_REVIEW_SAMPLE,
): WorkerEfficiencyRow[] {
  const acc = new Map<string, WorkerEfficiencyAccumulator>();
  const reliability = reliabilityByProfile(reliabilityProfiles);

  const bucket = (profile: string): WorkerEfficiencyAccumulator => {
    let current = acc.get(profile);
    if (!current) {
      current = {
        profile,
        rootIds: new Set<string>(),
        runs: 0,
        tokens: 0,
        runtimeSeconds: 0,
        runtimeEvidence: 0,
        costEffectiveUsd: 0,
        costEvidence: 0,
      };
      acc.set(profile, current);
    }
    return current;
  };

  for (const root of roots) {
    for (const worker of root.workers) {
      if (!isRosterProfile(worker.profile)) continue;
      const current = bucket(worker.profile);
      const tokens = workerTokens(worker);
      current.rootIds.add(root.id);
      current.runs += Math.max(0, worker.run_count ?? 0);
      current.tokens += Math.max(0, tokens);
      const cost = workerEffectiveCost(worker);
      if (cost != null) {
        current.costEffectiveUsd += cost;
        current.costEvidence += 1;
      }
    }
    for (const runner of root.runners) {
      if (!isRosterProfile(runner.profile)) continue;
      const runtime = finiteNonNegative(runner.runtime_seconds);
      if (runtime == null || runtime <= 0) continue;
      const current = bucket(runner.profile);
      current.runtimeSeconds += runtime;
      current.runtimeEvidence += 1;
    }
  }

  for (const profile of WORKER_EFFICIENCY_BENCH_PROFILES) {
    if (reliability.has(profile)) bucket(profile);
  }

  return [...acc.values()]
    .map((row) => {
      const reviewed = reliability.get(row.profile)?.judged ?? null;
      const returned = reliability.get(row.profile)?.rejected ?? null;
      const tasks = row.rootIds.size;
      const runtime = row.runtimeEvidence > 0 ? row.runtimeSeconds : null;
      const cost = row.costEvidence > 0 ? row.costEffectiveUsd : null;
      const reviewRate =
        reviewed != null && reviewed >= minReviewSample && returned != null ? returned / reviewed : null;
      const tokenPerTask = tasks > 0 && row.tokens > 0 ? row.tokens / tasks : null;
      const tokenPerMin = runtime != null && runtime > 0 && row.tokens > 0 ? row.tokens / (runtime / 60) : null;
      const costPerTask = tasks > 0 && cost != null ? cost / tasks : null;
      const complete = tokenPerTask != null && tokenPerMin != null && costPerTask != null && reviewRate != null;
      return {
        profile: row.profile,
        label: profileLabel[row.profile] ?? row.profile,
        tasks,
        runs: row.runs,
        tokens_total: row.tokens,
        runtime_seconds: runtime,
        cost_effective_usd: cost,
        token_per_task: tokenPerTask,
        token_per_min: tokenPerMin,
        cost_per_task: costPerTask,
        reviewed_outputs: reviewed,
        review_request_changes: returned,
        review_return_rate: reviewRate,
        data_quality: complete ? "complete" : "partial",
      } satisfies WorkerEfficiencyRow;
    })
    .filter((row) => row.tasks > 0 || row.runs > 0 || (row.reviewed_outputs ?? 0) > 0)
    .sort((a, b) => {
      const ai = WORKER_EFFICIENCY_BENCH_PROFILES.indexOf(a.profile as (typeof WORKER_EFFICIENCY_BENCH_PROFILES)[number]);
      const bi = WORKER_EFFICIENCY_BENCH_PROFILES.indexOf(b.profile as (typeof WORKER_EFFICIENCY_BENCH_PROFILES)[number]);
      if (ai !== -1 || bi !== -1) return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
      return b.tokens_total - a.tokens_total || a.profile.localeCompare(b.profile);
    });
}

const pctPoints = (value: number) => `${Math.round(value * 100)} pp`;

export function workerEfficiencyLevers(rows: WorkerEfficiencyRow[]): WorkerEfficiencyLever[] {
  const byProfile = new Map(rows.map((row) => [row.profile, row]));
  const coder = byProfile.get("coder");
  const premium = byProfile.get("premium");
  const levers: WorkerEfficiencyLever[] = [];

  if (coder?.review_return_rate != null && coder.review_return_rate > 0) {
    levers.push({
      key: "coder_review",
      label: "Coder-Review-Schleifen senken",
      detail: `${Math.round(coder.review_return_rate * 100)} % zurück`,
    });
  }

  if (
    coder?.review_return_rate != null &&
    premium?.review_return_rate != null &&
    premium.review_return_rate < coder.review_return_rate
  ) {
    levers.push({
      key: "premium_risk",
      label: "Premium für riskante Tasks",
      detail: `${pctPoints(coder.review_return_rate - premium.review_return_rate)} weniger zurück`,
    });
  }

  if (
    coder?.token_per_task != null &&
    premium?.token_per_task != null &&
    coder.token_per_task > premium.token_per_task * 1.25
  ) {
    levers.push({
      key: "token_drag",
      label: "Coder Token/Task senken",
      detail: `${Math.round(coder.token_per_task / premium.token_per_task)}× Premium`,
    });
  }

  return levers.slice(0, 3);
}
