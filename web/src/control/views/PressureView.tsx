import type { ComponentType } from "react";
import { Activity, Cpu, Gauge, MemoryStick, Network, ShieldCheck, Wrench } from "lucide-react";
import { cn } from "@/lib/utils";
import { Led, MeterBar, StaleBadge, StatusPill, ToneCallout } from "../components/atoms";
import { Card, Panel, SkeletonCard, Stat, Text } from "../components/primitives";
import { usePressureStatus } from "../hooks/useControlData";
import type { PressureOverall, PressureSource, PressureStatusResponse, TailnetPressureState, ToneName } from "../lib/types";

const overallTone: Record<PressureOverall, ToneName> = {
  ok: "emerald",
  busy: "amber",
  saturated: "red",
  unknown: "zinc",
};

const overallLabel: Record<PressureOverall, string> = {
  ok: "OK",
  busy: "Busy",
  saturated: "Voll",
  unknown: "Unklar",
};

const tailnetLabel: Record<TailnetPressureState, string> = {
  direct: "Direkt",
  relay: "Relay",
  inactive: "Fehlt",
  unknown: "Unklar",
};

const tailnetTone: Record<TailnetPressureState, ToneName> = {
  direct: "emerald",
  relay: "amber",
  inactive: "red",
  unknown: "zinc",
};

function fmtNumber(value: number | null | undefined, suffix = ""): string {
  if (value == null || Number.isNaN(value)) return "-";
  return `${Math.round(value)}${suffix}`;
}

function fmtDecimal(value: number | null | undefined, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "-";
  return value.toFixed(digits);
}

function fmtMs(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "-";
  return `${Math.round(value)}ms`;
}

function fmtMb(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "-";
  return value >= 1024 ? `${(value / 1024).toFixed(1)}GB` : `${Math.round(value)}MB`;
}

function dotFor(overall: PressureOverall) {
  if (overall === "ok") return "live";
  if (overall === "busy") return "warn";
  if (overall === "saturated") return "error";
  return "idle";
}

function sourceLabel(source: PressureSource): string {
  if (source.kind === "test") return "Tests";
  if (source.kind === "browser_test") return "Browser-Tests";
  if (source.kind === "agent") return "Agents";
  if (source.kind === "hermes_service") return "Hermes-Dienste";
  return "Quelle";
}

function recommendationDot(tone: ToneName) {
  if (tone === "red" || tone === "rose") return "error";
  if (tone === "amber") return "warn";
  if (tone === "emerald") return "live";
  return "idle";
}

function loadTone(data: PressureStatusResponse): "cyan" | "amber" | "red" {
  const load1 = data.host.load_avg[0] ?? 0;
  const cores = Math.max(1, data.host.cpu_count || 1);
  if (load1 > cores) return "red";
  if (load1 >= cores * 0.5) return "amber";
  return "cyan";
}

function apiTone(latency: number | null): ToneName {
  if (latency == null) return "zinc";
  if (latency > 1500) return "red";
  if (latency >= 500) return "amber";
  return "emerald";
}

function Chip({ icon: Icon, label, value, hint, tone = "zinc" }: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: string;
  hint?: string;
  tone?: ToneName;
}) {
  return (
    <div className={cn("min-h-16 min-w-0 rounded-lg border px-2.5 py-2 sm:min-h-20 sm:px-3", tone === "red" ? "border-red-500/25 bg-red-500/10" : tone === "amber" ? "border-amber-500/25 bg-amber-500/10" : tone === "emerald" ? "border-emerald-500/25 bg-emerald-500/10" : "border-white/10 bg-white/[.03]")}>
      <div className="flex min-w-0 items-center gap-2">
        <Icon className="h-3.5 w-3.5 shrink-0 hc-dim" />
        <span className="truncate hc-type-label hc-dim">{label}</span>
      </div>
      <p className="mt-1 truncate hc-mono text-sm font-semibold text-white sm:mt-2">{value}</p>
      {hint ? <p className="mt-0.5 line-clamp-1 hc-type-label hc-soft">{hint}</p> : null}
    </div>
  );
}

export function PressureContent({ data, lastUpdated, isStale, error }: {
  data: PressureStatusResponse | null;
  lastUpdated: number | null;
  isStale?: boolean;
  error?: string | null;
}) {
  if (!data) {
    return (
      <div className="space-y-4">
        {error ? <ToneCallout tone="amber">Pressure konnte nicht geladen werden: {error}</ToneCallout> : null}
        <SkeletonCard rows={6} />
      </div>
    );
  }

  const tone = overallTone[data.overall] ?? "zinc";
  const load1 = data.host.load_avg[0] ?? null;
  const load5 = data.host.load_avg[1] ?? null;
  const cores = Math.max(1, data.host.cpu_count || 1);
  const testSources = data.pressure_sources.filter((source) => source.kind === "test" || source.kind === "browser_test");
  const testCount = testSources.reduce((sum, source) => sum + source.count, 0);
  const testCpu = testSources.reduce((sum, source) => sum + source.cpu_percent, 0);
  const testLabel = testCount === 1 ? "1 Test" : `${testCount} Tests`;
  const activeCount = data.pressure_sources.reduce((sum, source) => sum + source.count, 0);
  const activeCpu = data.pressure_sources.reduce((sum, source) => sum + source.cpu_percent, 0);
  const apiLatency = data.access.api_latency_ms;
  const recommendation = data.recommendation;
  const cpuValue = testCount > 0 ? testLabel : activeCount > 0 ? `${activeCount} aktiv` : fmtNumber(data.host.cpu_percent, "%");
  const cpuHint = testCount > 0 ? `${fmtNumber(testCpu, "%")} Test-CPU` : activeCount > 0 ? `${fmtNumber(activeCpu, "%")} Quellen-CPU` : "Host";
  const cpuTone = data.overall === "saturated" ? "red" : testCount > 0 || activeCount > 0 ? "amber" : "zinc";

  return (
    <div className="space-y-4">
      <Card surface="raised" tone={tone} className="overflow-hidden p-0" ariaLabel="Pressure Status">
        <div className="flex flex-col gap-3 border-b border-[var(--hc-border)] px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <p className="hc-eyebrow">Pressure</p>
            <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
              <StatusPill tone={tone} label={overallLabel[data.overall]} dot={dotFor(data.overall)} size="md" />
              <Text as="h2" variant="subtitle" className="truncate text-white">Homeserver-Druck</Text>
            </div>
          </div>
          <StaleBadge isStale={isStale} lastUpdated={lastUpdated} />
        </div>

        <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-5">
          <Chip icon={Gauge} label="Last" value={`${fmtDecimal(load1)} / ${cores}`} hint={load5 != null ? `5m ${fmtDecimal(load5)}` : "Load"} tone={loadTone(data) === "red" ? "red" : loadTone(data) === "amber" ? "amber" : "zinc"} />
          <Chip icon={Cpu} label="CPU" value={cpuValue} hint={cpuHint} tone={cpuTone} />
          <Chip icon={MemoryStick} label="RAM" value={fmtMb(data.dashboard.rss_mb)} hint="Dashboard" />
          <Chip icon={Activity} label="API" value={fmtMs(apiLatency)} hint="p95" tone={apiTone(apiLatency)} />
          <Chip icon={Network} label="Tailnet" value={tailnetLabel[data.access.tailnet]} hint={data.access.detail ?? undefined} tone={tailnetTone[data.access.tailnet]} />
        </div>

        <div className="border-t border-[var(--hc-border)] px-4 py-3">
          <div className="flex min-w-0 items-start gap-2">
            <Wrench className="mt-0.5 h-4 w-4 shrink-0 hc-dim" aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="hc-eyebrow">Nächster Hebel</p>
              <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
                <StatusPill tone={recommendation.tone} label={recommendation.label} dot={recommendationDot(recommendation.tone)} />
                <p className="min-w-0 flex-1 line-clamp-2 text-sm text-white">{recommendation.detail}</p>
              </div>
            </div>
          </div>
        </div>

        <div className="border-t border-[var(--hc-border)] px-4 py-3">
          <p className="line-clamp-2 text-sm text-white"><span className="hc-soft">Grund: </span>{data.cause}</p>
          {data.errors.length > 0 ? <p className="mt-1 truncate hc-type-label text-amber-200">{data.errors.length} Teilwert unklar</p> : null}
        </div>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[1fr_1fr]">
        <Panel title="System" eyebrow="Host">
          <div className="grid gap-2 sm:grid-cols-2">
            <Stat label="Load 1m" value={`${fmtDecimal(load1)} / ${cores}`} hint={`${cores}-Core-Schwelle`} />
            <Stat label="Host CPU" value={fmtNumber(data.host.cpu_percent, "%")} hint="Momentaufnahme" />
            <Stat label="Host RAM" value={fmtNumber(data.host.memory_percent, "%")} hint="System" />
            <Stat label="Dashboard" value={fmtMb(data.dashboard.rss_mb)} hint={`CPU ${fmtNumber(data.dashboard.cpu_percent, "%")}`} />
          </div>
          <div className="mt-3">
            <MeterBar label="Load-Auslastung" value={load1 ?? 0} max={cores} tone={loadTone(data)} />
          </div>
        </Panel>

        <Panel title="Zugang" eyebrow="Tailscale + API">
          <div className="grid gap-2 sm:grid-cols-2">
            <Stat label="Tailnet" value={tailnetLabel[data.access.tailnet]} hint={data.access.detail ?? "Status"} tone={tailnetTone[data.access.tailnet]} />
            <Stat label="API" value={fmtMs(apiLatency)} hint="p95 aus Selbstmetriken" tone={apiTone(apiLatency)} />
            <Stat label="CPUWeight" value={data.dashboard.cpu_weight ?? "-"} hint="Dashboard" />
            <Stat label="CPUQuota" value={data.dashboard.cpu_quota || "-"} hint="Dashboard" />
          </div>
        </Panel>
      </div>

      <Panel title="Aktive Druckquellen" eyebrow="Rollen statt Cmdlines">
        {data.pressure_sources.length === 0 ? (
          <div className="flex min-h-20 items-center gap-3 rounded-lg border border-white/10 bg-white/[.03] px-3 py-3">
            <ShieldCheck className="h-5 w-5 text-emerald-300" />
            <div>
              <p className="text-sm font-medium text-white">Keine auffaellige Quelle</p>
              <p className="text-xs hc-soft">Keine Tests, Browser oder Agenten als Druckquelle erkannt.</p>
            </div>
          </div>
        ) : (
          <div className="grid gap-2">
            {data.pressure_sources.map((source) => (
              <div key={`${source.kind}-${source.label}-${source.scope}`} className="grid min-w-0 gap-2 rounded-lg border border-white/10 bg-white/[.03] px-3 py-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                <div className="min-w-0">
                  <div className="flex min-w-0 items-center gap-2">
                    <Led kind={source.throttled ? "live" : source.kind === "test" || source.kind === "browser_test" ? "warn" : "idle"} />
                    <p className="truncate text-sm font-medium text-white">{sourceLabel(source)}: {source.label}</p>
                  </div>
                  <p className="mt-0.5 truncate text-xs hc-soft">{source.scope} - {source.throttled ? "gedrosselt" : "ohne harte Kappe"}</p>
                </div>
                <div className="grid grid-cols-3 gap-2 sm:w-64">
                  <Stat label="Anzahl" value={source.count} />
                  <Stat label="CPU" value={fmtNumber(source.cpu_percent, "%")} />
                  <Stat label="RAM" value={fmtMb(source.rss_mb)} />
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      {data.errors.length > 0 ? (
        <ToneCallout tone="amber">
          Einige Pressure-Werte konnten nicht gelesen werden. Die Anzeige bleibt absichtlich vorsichtig und zeigt keine Rohpfade oder Cmdlines.
        </ToneCallout>
      ) : null}
    </div>
  );
}

export function PressureView() {
  const pressure = usePressureStatus();
  return (
    <PressureContent
      data={pressure.data}
      lastUpdated={pressure.lastUpdated}
      isStale={pressure.isStale}
      error={pressure.error}
    />
  );
}
