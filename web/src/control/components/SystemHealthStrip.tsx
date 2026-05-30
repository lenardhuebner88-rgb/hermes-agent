import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import type { HealthStatus, SubsystemHealth, SystemHealthResponse } from "../lib/types";
import type { DotKind } from "../lib/tones";
import { Led } from "./atoms";

type SubsystemKey = keyof SystemHealthResponse["subsystems"];
type HealthTone = "emerald" | "amber" | "red" | "zinc";

interface Props {
  data: SystemHealthResponse | null;
  error?: string | null;
  now?: number;
}

const toneClass: Record<HealthTone, string> = {
  emerald: "border-emerald-500/25 bg-emerald-500/10 text-emerald-200",
  amber: "border-amber-500/25 bg-amber-500/10 text-amber-200",
  red: "border-red-500/25 bg-red-500/10 text-red-200",
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
  { key: "openclaw", label: de.systemHealth.openclaw },
  { key: "autoresearch", label: de.systemHealth.autoresearch },
  { key: "kanban_db", label: de.systemHealth.kanban },
];

const unknownHealth: SubsystemHealth = { status: "offline", detail: de.systemHealth.unknown, error: null };

export function SystemHealthStrip({ data, error, now }: Props) {
  const isUnknown = !data || Boolean(error);
  const overallTone = isUnknown ? "zinc" : statusTone[data.overall];
  const checkedAge = data && now ? Math.max(0, Math.floor(now - data.checked_at)) : null;
  const title = error ?? (checkedAge !== null ? `${de.systemHealth.title}: ${checkedAge}s` : de.systemHealth.title);

  return (
    <section className={cn("hc-card border px-3 py-2", toneClass[overallTone])} title={title}>
      <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
        <div className="flex shrink-0 items-center gap-2 text-xs font-semibold uppercase tracking-normal">
          <Led kind={isUnknown ? "idle" : statusDot[data.overall]} size={9} />
          <span>{de.systemHealth.title}</span>
        </div>
        <div className="grid min-w-0 flex-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
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
    </section>
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
