import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import type { HealthStatus, MetricsLiteResponse, SubsystemHealth, SystemHealthResponse } from "../lib/types";
import type { DotKind } from "../lib/tones";
import { Led } from "./atoms";
import { elapsedSeconds } from "../lib/derive";

type SubsystemKey = keyof SystemHealthResponse["subsystems"];
type HealthTone = "emerald" | "amber" | "red" | "zinc";

// error_rate above this fraction turns the metrics tile red + shows a badge.
const ERROR_RATE_THRESHOLD = 0.05;

interface Props {
  data: SystemHealthResponse | null;
  error?: string | null;
  now?: number;
  metrics?: MetricsLiteResponse | null;
}

const toneClass: Record<HealthTone, string> = {
  emerald: "border-status-ok/25 bg-status-ok/10 text-emerald-200",
  amber: "border-status-warn/25 bg-status-warn/10 text-amber-200",
  red: "border-status-alert/25 bg-status-alert/10 text-red-200",
  zinc: "border-zinc-600/25 bg-zinc-600/10 text-zinc-200",
};

const statusTone: Record<HealthStatus, HealthTone> = {
  healthy: "emerald",
  degraded: "amber",
  offline: "red",
};

const statusDot: Record<HealthStatus, DotKind> = {
  healthy: "live",
  degraded: "warn",
  offline: "error",
};

const subsystems: Array<{ key: SubsystemKey; label: string }> = [
  { key: "gateway", label: de.systemHealth.gateway },
  { key: "autoresearch", label: de.systemHealth.autoresearch },
  { key: "kanban_db", label: de.systemHealth.kanban },
];

const unknownHealth: SubsystemHealth = { status: "offline", detail: de.systemHealth.unknown, error: null };

export function SystemHealthStrip({ data, error, now, metrics }: Props) {
  const isUnknown = !data || Boolean(error);
  const overallTone = isUnknown ? "zinc" : statusTone[data.overall];
  const checkedAge = data && now ? elapsedSeconds(data.checked_at, now) : null;
  const title = error ?? (data && now && checkedAge == null
    ? `${de.systemHealth.title}: Zeit ungültig`
    : checkedAge !== null ? `${de.systemHealth.title}: ${checkedAge}s` : de.systemHealth.title);

  return (
    <section className={cn("hc-card border px-3 py-2", toneClass[overallTone])} title={title}>
      <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
        <div className="flex shrink-0 items-center gap-2 text-xs font-semibold uppercase tracking-normal">
          <Led kind={isUnknown ? "idle" : statusDot[data.overall]} size={9} />
          <span>{de.systemHealth.title}</span>
        </div>
        <div className="grid min-w-0 flex-1 gap-2 sm:grid-cols-3">
          {subsystems.map((item) => (
            <SubsystemLight
              key={item.key}
              label={item.label}
              health={isUnknown ? unknownHealth : data.subsystems[item.key]}
              unknown={isUnknown}
              error={isUnknown ? error : null}
            />
          ))}
        </div>
      </div>
      {metrics !== undefined ? <MetricsTile metrics={metrics} /> : null}
    </section>
  );
}

function MetricsTile({ metrics }: { metrics: MetricsLiteResponse | null }) {
  if (!metrics || metrics.error) {
    return (
      <div className="mt-2 border-t border-white/10 pt-2 text-[11px] hc-dim">
        {de.systemHealth.metricsTitle}: {de.systemHealth.metricsError}
      </div>
    );
  }
  const groups = Object.values(metrics.groups);
  const totalRequests = groups.reduce((sum, g) => sum + g.count, 0);
  const totalErrors = groups.reduce((sum, g) => sum + g.error_count, 0);
  const errorRate = totalRequests > 0 ? totalErrors / totalRequests : 0;
  const worstP95 = groups.reduce((max, g) => Math.max(max, g.p95_ms), 0);
  const hot = errorRate > ERROR_RATE_THRESHOLD;

  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-white/10 pt-2 text-[11px] hc-soft">
      <span className="font-semibold uppercase tracking-normal hc-dim">{de.systemHealth.metricsTitle}</span>
      <span>{de.systemHealth.requests}: <span className="hc-mono text-white">{totalRequests}</span></span>
      <span className={cn(hot && "text-red-300")}>
        {de.systemHealth.errorRate}: <span className="hc-mono">{(errorRate * 100).toFixed(1)}%</span>
      </span>
      <span>{de.systemHealth.p95}: <span className="hc-mono text-white">{Math.round(worstP95)}ms</span></span>
      {hot ? <span className="rounded-full border border-status-alert/40 bg-status-alert/10 px-2 py-0.5 text-red-200">{de.systemHealth.metricsErrorBadge}</span> : null}
    </div>
  );
}

function SubsystemLight({ label, health, unknown, error }: { label: string; health: SubsystemHealth; unknown: boolean; error?: string | null }) {
  const tone = unknown ? "zinc" : statusTone[health.status];
  const dot = unknown ? "idle" : statusDot[health.status];
  const statusLabel = unknown ? de.systemHealth.unknown : de.systemHealth[health.status];
  const detail = error ?? health.error ?? health.detail;
  const showDetail = Boolean(detail) && (unknown || health.status !== "healthy");

  return (
    <div className={cn("min-w-0 rounded-lg border px-3 py-2 text-xs", toneClass[tone])} title={detail || statusLabel}>
      <div className="flex min-w-0 items-center gap-2">
        <Led kind={dot} />
        <span className="min-w-0 flex-1 truncate font-medium text-white">{label}</span>
        <span className="shrink-0 hc-mono">{statusLabel}</span>
      </div>
      {showDetail ? <p className="mt-1 truncate text-[11px] opacity-90">{detail}</p> : null}
    </div>
  );
}
