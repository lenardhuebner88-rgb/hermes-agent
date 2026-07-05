import { Activity, Bot, Cpu, Gauge, GitBranch, Network } from "lucide-react";
import { StaleBadge, StatusPill } from "../../components/atoms";
import { Card, Text } from "../../components/primitives";
import { SectionHeader, StatusChip } from "../../components/leitstand";
import { useOperatorInventory, usePressureStatus } from "../../hooks/useControlData";
import { usePulseData } from "../../hooks/usePulseData";
import type { DotKind } from "../../lib/tones";
import type { PressureOverall, Proposal, TailnetPressureState, ToneName } from "../../lib/types";
import { PressureContent } from "./PressureContent";
import { OpsRadarContent } from "./OpsRadarContent";
import { PulseTally, PulseTimeline } from "./PulsePanels";

// ── Fusions-Vokabular ───────────────────────────────────────────────────────
// Der geteilte Kopf leitet die Druck-Ampel (Status-Trio) und den Mismatch-
// Indikator aus denselben Feldern ab wie die alten Einzel-Views — hier nur die
// Teile, die die kombinierte Status-Zeile braucht.

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

const overallDot: Record<PressureOverall, DotKind> = {
  ok: "live",
  busy: "warn",
  saturated: "error",
  unknown: "idle",
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

function fmtDecimal(value: number | null | undefined, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "-";
  return value.toFixed(digits);
}

function fmtNumber(value: number | null | undefined, suffix = ""): string {
  if (value == null || Number.isNaN(value)) return "-";
  return `${Math.round(value)}${suffix}`;
}

function fmtMs(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "-";
  return `${Math.round(value)}ms`;
}

function loadTone(load1: number | null, cores: number): ToneName {
  if (load1 == null) return "zinc";
  if (load1 > cores) return "red";
  if (load1 >= cores * 0.5) return "amber";
  return "zinc";
}

function apiTone(latency: number | null): ToneName {
  if (latency == null) return "zinc";
  if (latency > 1500) return "red";
  if (latency >= 500) return "amber";
  return "emerald";
}

interface Props {
  proposals: Proposal[];
  proposalsLastUpdated?: number | null;
}

/**
 * SystemView (S1-Fusion) — der eine Leitstand-Kopf über Druck (Pressure),
 * Ops Radar und dem 48h-Puls. Ein geteilter Status-Kopf, dann die Sektionen
 * Druck / Zugang / Druckquellen (Pressure) · Worktrees / Akteure (Ops) ·
 * Ereignisse (Puls) — dieselben Live-Ströme, aber als eine Ansicht gerahmt.
 */
export function SystemView({ proposals, proposalsLastUpdated }: Props) {
  const pressure = usePressureStatus();
  const inventory = useOperatorInventory();
  const pulse = usePulseData({ proposals, proposalsLastUpdated });

  const p = pressure.data;
  const inv = inventory.data;
  const summary = inv?.summary;

  const overall: PressureOverall = p?.overall ?? "unknown";
  const load1 = p?.host.load_avg[0] ?? null;
  const load5 = p?.host.load_avg[1] ?? null;
  const cores = Math.max(1, p?.host.cpu_count || 1);
  const apiLatency = p?.access.api_latency_ms ?? null;
  const tailnet: TailnetPressureState = p?.access.tailnet ?? "unknown";
  const mismatchCount = summary
    ? summary.worktrees_dirty + summary.worktrees_orphaned + summary.worktrees_status_unknown
    : 0;

  // Kopf-Ton: Sättigung dominiert (rot), sonst Busy oder ein Mismatch → amber,
  // sonst ruhig (grün) — der eine Ampelwert für die ganze Ansicht.
  const headTone: ToneName =
    overall === "saturated" ? "red" : overall === "busy" || mismatchCount > 0 ? "amber" : "emerald";

  const opsMeta = summary
    ? `${summary.worktrees_total} Worktrees · ${summary.actors_total} Akteure`
    : undefined;

  return (
    <div className="space-y-5">
      <Card surface="raised" tone={headTone} className="overflow-hidden p-0" ariaLabel="System-Status">
        <div className="flex flex-col gap-3 border-b border-[var(--hc-border)] px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <p className="hc-eyebrow">System</p>
            <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
              <StatusPill tone={overallTone[overall]} label={`Druck: ${overallLabel[overall]}`} dot={overallDot[overall]} size="md" />
              {inv ? (
                <StatusPill
                  tone={mismatchCount > 0 ? "rose" : "emerald"}
                  label={mismatchCount > 0 ? `${mismatchCount} Mismatch` : "Sync"}
                  dot={mismatchCount > 0 ? "error" : "ready"}
                />
              ) : (
                <StatusPill tone="zinc" label="Ops unklar" dot="idle" />
              )}
              <Text as="h2" variant="subtitle" className="truncate text-white">Puls des Homeservers</Text>
            </div>
          </div>
          <div className="flex flex-wrap justify-end gap-1.5">
            <StaleBadge isStale={pressure.isStale} lastUpdated={pressure.lastUpdated} error={pressure.error} />
            <StaleBadge isStale={inventory.isStale} lastUpdated={inventory.lastUpdated} error={inventory.error} />
          </div>
        </div>

        {/* Chips: die kombinierte Metrik-Zeile über beide Datenquellen. */}
        <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-3 lg:grid-cols-6">
          <StatusChip icon={Gauge} label="Last" value={`${fmtDecimal(load1)} / ${cores}`} hint={load5 != null ? `5m ${fmtDecimal(load5)}` : "Load"} tone={loadTone(load1, cores)} />
          <StatusChip icon={Cpu} label="CPU" value={fmtNumber(p?.host.cpu_percent, "%")} hint="Host" tone={overall === "saturated" ? "red" : overall === "busy" ? "amber" : "zinc"} />
          <StatusChip icon={Activity} label="API" value={fmtMs(apiLatency)} hint="p95" tone={apiTone(apiLatency)} />
          <StatusChip icon={Network} label="Tailnet" value={tailnetLabel[tailnet]} hint={p?.access.detail ?? undefined} tone={tailnetTone[tailnet]} />
          <StatusChip icon={GitBranch} label="Worktrees" value={summary ? `${summary.worktrees_total} total` : "-"} hint={summary ? `${summary.worktrees_locked} locked · ${summary.worktrees_dirty} dirty` : undefined} tone={summary && (summary.worktrees_dirty || summary.worktrees_orphaned) ? "amber" : "zinc"} />
          <StatusChip icon={Bot} label="Akteure" value={summary ? String(summary.actors_total) : "-"} hint={summary ? `${summary.actors_canonical} kanonisch` : undefined} tone={summary && summary.actors_total ? "cyan" : "zinc"} />
        </div>

        {/* Kacheln: die 48h-Bilanz des Puls (dritte Datenquelle im Kopf). */}
        <div className="border-t border-[var(--hc-border)] px-4 py-3">
          <PulseTally summary={pulse.summary} />
        </div>
      </Card>

      <SectionHeader label="Druck · Zugang · Druckquellen" meta={p ? overallLabel[overall] : undefined} />
      <PressureContent embedded data={pressure.data} lastUpdated={pressure.lastUpdated} isStale={pressure.isStale} error={pressure.error} />

      <SectionHeader label="Worktrees · Akteure" meta={opsMeta} />
      <OpsRadarContent embedded data={inventory.data} lastUpdated={inventory.lastUpdated} isStale={inventory.isStale} error={inventory.error} />

      <SectionHeader label="Ereignisse" meta={`letzte ${pulse.windowHours}h`} />
      <PulseTimeline data={pulse} />
    </div>
  );
}
