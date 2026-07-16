import { Activity, Cpu, Gauge, MemoryStick, Network, ShieldCheck, TriangleAlert, Wrench } from "lucide-react";
import { MeterBar, StaleBadge } from "../../components/atoms";
import { Card, Eyebrow, Panel, SkeletonCard } from "../../components/primitives";
import { KpiTile, SignalChip, SignalLabel, signalToneFromLegacy } from "../../components/leitstand";
import type { PressureOverall, PressureSource, PressureStatusResponse, TailnetPressureState, ToneName } from "../../lib/types";

// PressureContent lebt seit dem Abriss (S5) hier unter views/system/, weil die
// eigenständige PressureView-Route zum System-Redirect wurde. Die System-View
// bettet diesen Inhalt als "Druck · Zugang · Druckquellen"-Sektion ein.

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

function sourceLabel(source: PressureSource): string {
  if (source.kind === "test") return "Tests";
  if (source.kind === "browser_test") return "Browser-Tests";
  if (source.kind === "agent") return "Agents";
  if (source.kind === "hermes_service") return "Hermes-Dienste";
  return "Quelle";
}

function NextLeverDetail({ data }: { data: PressureStatusResponse }) {
  const recommendation = data.recommendation;
  return (
    <div className="flex min-w-0 items-start gap-2">
      <Wrench className="mt-0.5 h-4 w-4 shrink-0 text-ink-3" aria-hidden />
      <div className="min-w-0 flex-1">
        <Eyebrow>Nächster Hebel</Eyebrow>
        <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
          <SignalChip tone={signalToneFromLegacy(recommendation.tone)} label={recommendation.label} />
          <p className="min-w-0 flex-1 line-clamp-2 text-sec text-ink">{recommendation.detail}</p>
        </div>
      </div>
    </div>
  );
}

function CauseDetail({ data }: { data: PressureStatusResponse }) {
  return (
    <>
      <p className="line-clamp-2 text-sec text-ink"><span className="text-ink-2">Grund: </span>{data.cause}</p>
      {data.errors.length > 0 ? <SignalLabel tone="warn" label={`${data.errors.length} Teilwert unklar`} className="mt-1" /> : null}
    </>
  );
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

export function PressureContent({ data, lastUpdated, isStale, error, embedded }: {
  data: PressureStatusResponse | null;
  lastUpdated: number | null;
  isStale?: boolean;
  error?: string | null;
  /** In der System-Fusion (S1) trägt der geteilte Kopf die Status-Zeile — dann
   *  entfällt hier die eigene Pressure-Status-Karte, die Sektionen bleiben. */
  embedded?: boolean;
}) {
  if (!data) {
    return (
      <div className="space-y-4">
        {error ? <div className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />Pressure konnte nicht geladen werden: {error}</div> : null}
        {!error ? <SkeletonCard rows={6} /> : null}
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
  const cpuValue = testCount > 0 ? testLabel : activeCount > 0 ? `${activeCount} aktiv` : fmtNumber(data.host.cpu_percent, "%");
  const cpuHint = testCount > 0 ? `${fmtNumber(testCpu, "%")} Test-CPU` : activeCount > 0 ? `${fmtNumber(activeCpu, "%")} Quellen-CPU` : "Host";
  const cpuTone = data.overall === "saturated" ? "red" : testCount > 0 || activeCount > 0 ? "amber" : "zinc";

  return (
    <div className="space-y-4">
      {embedded ? (
        <Panel title="Nächster Hebel" eyebrow="Empfohlene Aktion">
          <NextLeverDetail data={data} />
          <div className="mt-3">
            <CauseDetail data={data} />
          </div>
        </Panel>
      ) : (
      <Card surface="raised" className="overflow-hidden border border-line p-0" ariaLabel="Pressure Status">
        <div className="flex flex-col gap-3 border-b border-line px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <Eyebrow>Pressure</Eyebrow>
            <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
              <SignalChip tone={signalToneFromLegacy(tone)} label={overallLabel[data.overall]} />
              <h2 className="truncate font-display text-emph font-semibold text-ink">Homeserver-Druck</h2>
            </div>
          </div>
          <StaleBadge isStale={isStale} lastUpdated={lastUpdated} />
        </div>

        <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-5">
          <KpiTile icon={Gauge} label="Last" value={`${fmtDecimal(load1)} / ${cores}`} delta={load5 != null ? `5m ${fmtDecimal(load5)}` : "Load"} dot={loadTone(data) === "red" ? "error" : loadTone(data) === "amber" ? "warn" : "idle"} />
          <KpiTile icon={Cpu} label="CPU" value={cpuValue} delta={cpuHint} dot={cpuTone === "red" ? "error" : cpuTone === "amber" ? "warn" : "idle"} />
          <KpiTile icon={MemoryStick} label="RAM" value={fmtMb(data.dashboard.rss_mb)} delta="Dashboard" />
          <KpiTile icon={Activity} label="API" value={fmtMs(apiLatency)} delta="p95" dot={apiTone(apiLatency) === "red" ? "error" : apiTone(apiLatency) === "amber" ? "warn" : apiTone(apiLatency) === "emerald" ? "ready" : "idle"} />
          <KpiTile icon={Network} label="Tailnet" value={tailnetLabel[data.access.tailnet]} delta={data.access.detail ?? undefined} dot={data.access.tailnet === "direct" ? "ready" : data.access.tailnet === "relay" ? "warn" : data.access.tailnet === "inactive" ? "error" : "idle"} />
        </div>

        <div className="border-t border-line px-4 py-3">
          <NextLeverDetail data={data} />
        </div>

        <div className="border-t border-line px-4 py-3">
          <CauseDetail data={data} />
        </div>
      </Card>
      )}

      <div className="grid gap-4 xl:grid-cols-[1fr_1fr]">
        <Panel title="System" eyebrow="Host">
          <div className="grid gap-2 sm:grid-cols-2">
            <KpiTile label="Load 1m" value={`${fmtDecimal(load1)} / ${cores}`} delta={`${cores}-Core-Schwelle`} />
            <KpiTile label="Host CPU" value={fmtNumber(data.host.cpu_percent, "%")} delta="Momentaufnahme" />
            <KpiTile label="Host RAM" value={fmtNumber(data.host.memory_percent, "%")} delta="System" />
            <KpiTile label="Dashboard" value={fmtMb(data.dashboard.rss_mb)} delta={`CPU ${fmtNumber(data.dashboard.cpu_percent, "%")}`} />
          </div>
          <div className="mt-3">
            <MeterBar label="Load-Auslastung" value={load1 ?? 0} max={cores} tone={loadTone(data)} />
          </div>
        </Panel>

        <Panel title="Zugang" eyebrow="Tailscale + API">
          <div className="grid gap-2 sm:grid-cols-2">
            <KpiTile label="Tailnet" value={tailnetLabel[data.access.tailnet]} delta={data.access.detail ?? "Status"} dot={data.access.tailnet === "direct" ? "ready" : data.access.tailnet === "relay" ? "warn" : data.access.tailnet === "inactive" ? "error" : "idle"} />
            <KpiTile label="API" value={fmtMs(apiLatency)} delta="p95 aus Selbstmetriken" dot={apiTone(apiLatency) === "red" ? "error" : apiTone(apiLatency) === "amber" ? "warn" : apiTone(apiLatency) === "emerald" ? "ready" : "idle"} />
            <KpiTile label="CPUWeight" value={data.dashboard.cpu_weight ?? "-"} delta="Dashboard" />
            <KpiTile label="CPUQuota" value={data.dashboard.cpu_quota || "-"} delta="Dashboard" />
          </div>
        </Panel>
      </div>

      <Panel title="Aktive Druckquellen" eyebrow="Rollen statt Cmdlines">
        {data.pressure_sources.length === 0 ? (
          <div className="flex min-h-20 items-center gap-3 rounded-card border border-line bg-surface-2 px-3 py-3">
            <ShieldCheck className="h-5 w-5 text-status-ok" />
            <div>
              <p className="text-sec font-medium text-ink">Keine auffaellige Quelle</p>
              <p className="text-sec text-ink-2">Keine Tests, Browser oder Agenten als Druckquelle erkannt.</p>
            </div>
          </div>
        ) : (
          <div className="grid gap-2">
            {data.pressure_sources.map((source) => (
              <div key={`${source.kind}-${source.label}-${source.scope}`} className="grid min-w-0 gap-2 rounded-card border border-line bg-surface-2 px-3 py-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                <div className="min-w-0">
                  <div className="flex min-w-0 items-center gap-2">
                    <SignalLabel tone={source.throttled ? "ok" : source.kind === "test" || source.kind === "browser_test" ? "warn" : "neutral"} label={source.throttled ? "gedrosselt" : "aktiv"} />
                    <p className="truncate text-sec font-medium text-ink">{sourceLabel(source)}: {source.label}</p>
                  </div>
                  <p className="mt-0.5 truncate text-sec text-ink-2">{source.scope} - {source.throttled ? "gedrosselt" : "ohne harte Kappe"}</p>
                </div>
                <div className="grid grid-cols-3 gap-2 sm:w-64">
                  <KpiTile label="Anzahl" value={source.count} />
                  <KpiTile label="CPU" value={fmtNumber(source.cpu_percent, "%")} />
                  <KpiTile label="RAM" value={fmtMb(source.rss_mb)} />
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      {data.errors.length > 0 ? (
        <div className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />Einige Pressure-Werte konnten nicht gelesen werden. Die Anzeige bleibt absichtlich vorsichtig und zeigt keine Rohpfade oder Cmdlines.</div>
      ) : null}
    </div>
  );
}
