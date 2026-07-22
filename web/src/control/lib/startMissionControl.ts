import type { AccountUsageProvider } from "./types";
import type {
  RunsDailyPoint,
  RunsIssuesResponse,
  HostUsageResponse,
  ProjectCommitFeedEntry,
  SubscriptionTokenBurnResponse,
} from "./schemas";
import {
  DEFAULT_STATS_CONFIG,
  providerField,
  providerLabel,
  type StatsFieldConfig,
} from "./statsFields";
import {
  classifyWindow,
  formatReset,
  sortUsageProviders,
  windowLabelDe,
} from "./accountUsage";

export type StartMatrixMode = "intensity" | "tokens" | "sessions";

type OutcomeBurnFields = {
  completed_runs?: number;
  failed_runs?: number;
  blocked_runs?: number;
};

type StartBurn = Omit<SubscriptionTokenBurnResponse, "by_lane" | "daily"> & {
  by_lane: Array<SubscriptionTokenBurnResponse["by_lane"][number] & OutcomeBurnFields>;
  daily: Array<SubscriptionTokenBurnResponse["daily"][number] & OutcomeBurnFields>;
};

export interface StartMatrixDay {
  date: string;
  tokens: number;
  sessions: number;
  completedRuns: number | null;
  intensity: number;
}

export interface StartProviderRow {
  key: string;
  providerId: string | null;
  label: string;
  plan: string | null;
  lane: string;
  colorToken: string;
  available: boolean;
  cached: boolean;
  signalAt: string | null;
  tokenTelemetry: boolean;
  totalTokens: number;
  todayTokens: number;
  totalSessions: number;
  completedRuns: number | null;
  failedRuns: number | null;
  blockedRuns: number | null;
  successPerMillion: number | null;
  weeklyPercent: number | null;
  sessionPercent: number | null;
  weeklyReset: string;
  sessionReset: string;
  capacityPercent: number | null;
  capacityLabel: string;
  capacityReset: string;
  days: StartMatrixDay[];
}

export interface StartCapacityWindow {
  label: string;
  percent: number;
}

export interface StartCapacityCard {
  providerId: string;
  label: string;
  plan: string | null;
  colorToken: string;
  percent: number | null;
  windowLabel: string;
  reset: string;
  secondary: StartCapacityWindow[];
  state: "live" | "fallback" | "unavailable";
}

export interface StartFlow {
  ended: number;
  successful: number;
  failed: number;
  friction: number;
  delivered: number;
  deliveredTasks: number;
}

export interface StartIssueCause {
  key: "review" | "dependency" | "needs_input" | "capacity" | "budget" | "runtime" | "integration" | "other";
  label: string;
  count: number;
}

const HOST_PROVIDER_LANES: Record<string, string> = {
  claude: "claude",
  codex: "chatgpt",
  kimi: "kimi",
  grok: "grok",
  qwen: "qwen",
  api: "api",
};

const DATA_TOKENS = [
  "var(--color-data-7)",
  "var(--color-data-2)",
  "var(--color-data-3)",
  "var(--color-data-4)",
  "var(--color-data-5)",
  "var(--color-data-1)",
];

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

export function localDateKey(date: Date): string {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

export function startDateAxis(days: number, nowSeconds: number): string[] {
  const safeDays = Math.max(1, Math.min(14, Math.round(days || 7)));
  const now = new Date(nowSeconds * 1000);
  now.setHours(12, 0, 0, 0);
  return Array.from({ length: safeDays }, (_, index) => {
    const day = new Date(now);
    day.setDate(now.getDate() - (safeDays - 1 - index));
    return localDateKey(day);
  });
}

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function clampPercent(value: number | null): number | null {
  return value == null ? null : Math.max(0, Math.min(100, value));
}

function accountProviderForLane(
  providers: AccountUsageProvider[],
  lane: string,
  config: StatsFieldConfig,
): AccountUsageProvider | undefined {
  return providers.find((provider) => providerField(config, provider.provider)?.lane === lane);
}

function sumOutcome(rows: Array<OutcomeBurnFields>, key: keyof OutcomeBurnFields): number | null {
  let seen = false;
  let total = 0;
  for (const row of rows) {
    const value = finite(row[key]);
    if (value == null) continue;
    seen = true;
    total += value;
  }
  return seen ? total : null;
}

function capacityWindow(
  provider: AccountUsageProvider | undefined,
  config: StatsFieldConfig,
  nowMs: number,
): { percent: number | null; label: string; reset: string } {
  const candidates = (provider?.windows ?? [])
    .map((window, index) => ({
      window,
      index,
      kind: classifyWindow(window, config),
      percent: clampPercent(finite(window.used_percent)),
    }))
    .filter((entry) => (entry.kind === "session" || entry.kind === "weekly") && entry.percent != null)
    .sort((a, b) => (b.percent ?? 0) - (a.percent ?? 0) || a.index - b.index);
  const tightest = candidates[0];
  if (!tightest) return { percent: null, label: "", reset: "" };
  const baseLabel = windowLabelDe(tightest.window, config);
  const detail = tightest.window.detail?.trim();
  return {
    percent: tightest.percent,
    label: detail && !baseLabel.toLowerCase().includes(detail.toLowerCase())
      ? `${baseLabel} · ${detail}`
      : baseLabel,
    reset: formatReset(tightest.window.reset_at ?? null, nowMs),
  };
}

export function buildStartCapacityCards(
  providers: AccountUsageProvider[],
  config: StatsFieldConfig = DEFAULT_STATS_CONFIG,
  nowMs: number,
): StartCapacityCard[] {
  return sortUsageProviders(providers, config, "subscription").map((provider, index) => {
    const candidates = provider.windows
      .map((window, windowIndex) => ({
        window,
        windowIndex,
        kind: classifyWindow(window, config),
        percent: clampPercent(finite(window.used_percent)),
      }))
      .filter((entry) => (entry.kind === "session" || entry.kind === "weekly") && entry.percent != null)
      .sort((a, b) => (b.percent ?? 0) - (a.percent ?? 0) || a.windowIndex - b.windowIndex);
    const tightest = candidates[0];
    const detail = tightest?.window.detail?.trim() ?? "";
    const usefulDetail = detail && !/verbleibend|remaining|\d+\s*\/\s*\d+/i.test(detail) ? detail : "";
    const percent = tightest?.percent ?? null;
    return {
      providerId: provider.provider,
      label: providerLabel(config, provider.provider),
      plan: provider.plan,
      colorToken: DATA_TOKENS[index % DATA_TOKENS.length],
      percent,
      windowLabel: tightest
        ? [windowLabelDe(tightest.window, config), usefulDetail].filter(Boolean).join(" · ")
        : "",
      reset: formatReset(tightest?.window.reset_at ?? null, nowMs),
      secondary: candidates.slice(1, 3).map(({ window, percent }) => ({
        label: windowLabelDe(window, config),
        percent: percent ?? 0,
      })),
      state: percent == null
        ? "unavailable"
        : provider.fallback
          ? "fallback"
          : "live",
    } satisfies StartCapacityCard;
  });
}

export function buildStartProviderRows({
  providers,
  burn,
  hostUsage,
  config = DEFAULT_STATS_CONFIG,
  nowMs,
}: {
  providers: AccountUsageProvider[];
  burn: StartBurn | null | undefined;
  hostUsage?: HostUsageResponse | null;
  config?: StatsFieldConfig;
  nowMs: number;
}): StartProviderRow[] {
  const dates = hostUsage?.dates?.length
    ? hostUsage.dates
    : startDateAxis(burn?.days ?? 7, burn?.now ?? Math.floor(nowMs / 1000));
  const burnLanes = new Set([
    ...(burn?.by_lane ?? []).map((row) => row.subscription),
    ...(burn?.daily ?? []).map((row) => row.subscription),
  ]);
  const configuredLanes = config.providers
    .filter((field) => field.visible && field.lane)
    .map((field) => field.lane as string);
  const hostByLane = new Map(
    (hostUsage?.providers ?? []).map((row) => [HOST_PROVIDER_LANES[row.provider] ?? row.provider, row]),
  );
  const lanes = [...new Set([...configuredLanes, ...burnLanes, ...hostByLane.keys()])];

  const rawRows = lanes.map((lane, index) => {
    const account = accountProviderForLane(providers, lane, config);
    const field = account ? providerField(config, account.provider) : config.providers.find((item) => item.lane === lane);
    const providerId = account?.provider ?? field?.id ?? null;
    const laneRows = (burn?.by_lane ?? []).filter((row) => row.subscription === lane);
    const host = hostByLane.get(lane);
    const hostDailyByDate = new Map((host?.daily ?? []).map((row) => [row.date, row]));
    const weekly = account?.windows.find((window) => classifyWindow(window, config) === "weekly");
    const session = account?.windows.find((window) => classifyWindow(window, config) === "session");
    const capacity = capacityWindow(account, config, nowMs);
    const outcomeTokens = laneRows.reduce((sum, row) => sum + row.total_tokens, 0);
    const totalTokens = host?.total_tokens ?? 0;
    const totalSessions = host?.sessions ?? 0;
    const completedRuns = sumOutcome(laneRows, "completed_runs");
    const failedRuns = sumOutcome(laneRows, "failed_runs");
    const blockedRuns = sumOutcome(laneRows, "blocked_runs");
    const days = dates.map((date) => {
      const row = hostDailyByDate.get(date);
      return {
        date,
        tokens: row?.tokens ?? 0,
        sessions: row?.sessions ?? 0,
        completedRuns: completedRuns == null ? null : 0,
        intensity: 0,
      };
    });
    return {
      key: lane,
      providerId,
      label: providerId ? providerLabel(config, providerId) : host?.label ?? lane,
      plan: account?.plan ?? null,
      lane,
      colorToken: DATA_TOKENS[index % DATA_TOKENS.length],
      available: account?.available ?? false,
      cached: account?.cached ?? false,
      signalAt: account?.signal_at ?? account?.fetched_at ?? null,
      tokenTelemetry: Boolean(host),
      totalTokens,
      todayTokens: days.at(-1)?.tokens ?? 0,
      totalSessions,
      completedRuns,
      failedRuns,
      blockedRuns,
      successPerMillion:
        completedRuns != null && outcomeTokens > 0 ? completedRuns / (outcomeTokens / 1_000_000) : null,
      weeklyPercent: clampPercent(finite(weekly?.used_percent)),
      sessionPercent: clampPercent(finite(session?.used_percent)),
      weeklyReset: formatReset(weekly?.reset_at ?? null, nowMs),
      sessionReset: formatReset(session?.reset_at ?? null, nowMs),
      capacityPercent: capacity.percent,
      capacityLabel: capacity.label,
      capacityReset: capacity.reset,
      days,
    } satisfies StartProviderRow;
  });

  const globalMax = Math.max(0, ...rawRows.flatMap((row) => row.days.map((day) => day.tokens)));
  return rawRows.map((row) => ({
    ...row,
    days: row.days.map((day) => ({
      ...day,
      intensity: globalMax > 0 ? day.tokens / globalMax : 0,
    })),
  }));
}

export function startFlowFromToday(today: RunsDailyPoint | null | undefined): StartFlow {
  const successful = today?.runs_completed ?? 0;
  const failed = today?.runs_failed ?? 0;
  const friction = failed;
  return {
    ended: successful + friction,
    successful,
    failed,
    friction,
    delivered: today?.done_roots ?? 0,
    deliveredTasks: today?.done_tasks ?? 0,
  };
}

const OUTCOME_CAUSES: Record<string, StartIssueCause["key"]> = {
  blocked: "other",
  timed_out: "budget",
  iteration_budget_exhausted: "budget",
  gave_up: "budget",
  spawn_failed: "runtime",
  crashed: "runtime",
  integration_parked: "integration",
};

const CAUSE_LABELS: Record<StartIssueCause["key"], string> = {
  review: "Review-Korrektur",
  dependency: "Abhängigkeit",
  needs_input: "Entscheidung / Input",
  capacity: "Kapazität / Limit",
  budget: "Zeit / Iterationen",
  runtime: "Start / Laufzeit",
  integration: "Integration",
  other: "Sonstige",
};

export function aggregateStartIssueCauses(issues: RunsIssuesResponse | null | undefined): StartIssueCause[] {
  const counts = new Map<StartIssueCause["key"], number>();
  for (const issue of issues?.issues ?? []) {
    const classified = issue.cause_key as StartIssueCause["key"];
    if (classified in CAUSE_LABELS && classified !== "other") {
      counts.set(classified, (counts.get(classified) ?? 0) + issue.count);
      continue;
    }
    for (const [outcome, count] of Object.entries(issue.outcomes)) {
      const key = OUTCOME_CAUSES[outcome] ?? "other";
      counts.set(key, (counts.get(key) ?? 0) + count);
    }
  }
  return [...counts.entries()]
    .map(([key, count]) => ({ key, label: CAUSE_LABELS[key], count }))
    .filter((row) => row.count > 0)
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

export function classifyCommitTheme(commit: ProjectCommitFeedEntry): string {
  const evidence = `${commit.message} ${commit.attribution?.label ?? ""}`.toLowerCase();
  if (/provider|usage|token|quota|limit/.test(evidence)) return "Provider-Nutzung";
  if (/review|verifier|snapshot|evidence|gate/.test(evidence)) return "Prüfsicherheit";
  if (/worker|dispatch|worktree|session|isolation/.test(evidence)) return "Ausführung";
  if (/dashboard|start|mobile|desktop|ui\b|design/.test(evidence)) return "Dashboard";
  if (/fix|bug|hardening|stabili|retry|timeout/.test(evidence)) return "Stabilität";
  return commit.project_name || commit.project || "Änderung";
}

export function readableCommitTopic(commit: ProjectCommitFeedEntry): string {
  if (commit.attribution?.task_id && commit.attribution.label?.trim()) {
    return commit.attribution.label.trim();
  }
  let message = commit.message.trim() || "Änderung ohne Beschreibung";
  const reapply = message.match(/^Reapply\s+"(.+)"$/i);
  const revert = message.match(/^Revert\s+"(.+)"$/i);
  if (reapply) message = `Erneut eingespielt: ${reapply[1]}`;
  else if (revert) message = `Zurückgenommen: ${revert[1]}`;
  message = message
    .replace(/^(?:codex|claude|hermes|auto-sync):\s*/i, "")
    .replace(/^kanban:\s+merge\s+kanban\/t_[0-9a-f]+\s*(?:\([^)]*\))?\s*/i, "Zusammengeführt: ")
    .replace(/^(?:kanban|wip)\(t_[0-9a-f]+\):\s*/i, "")
    .replace(/^loop\([^)]+\):\s*/i, "");
  return message || "Änderung eingespielt";
}

export function visibleAccountProviders(
  providers: AccountUsageProvider[],
  config: StatsFieldConfig = DEFAULT_STATS_CONFIG,
): AccountUsageProvider[] {
  return sortUsageProviders(providers, config, "subscription");
}
