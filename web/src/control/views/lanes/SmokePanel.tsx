import { Activity } from "lucide-react";
import { cn } from "@/lib/utils";
import { KpiTile, ListRow, SignalChip } from "../../components/leitstand";
import {
  filterSinnvoll,
  probeKey,
  UNREACHABLE_PROBE_STATUSES,
  type LaneModelOption,
  type ModelProbeResult,
} from "./api";
import { providerDot } from "./providerColors";
import { PROBE_STATUS_LABEL, probeTone, t } from "./strings";

// SmokePanel („Rauch") — reachability/latency evidence for the curated model
// set. KPI tiles (erreichbar X/Y · p50 Latenz · blockiert), one primary CTA
// that batch-probes the sinnvoll set (sequential, capped — moderate cost), and
// a result feed. Per-row probes triggered from the matrix land in the same
// shared probe map, so they show up here too. No polling — probes are explicit.

function effectiveProbe(
  model: LaneModelOption,
  probes: Record<string, ModelProbeResult>,
): ModelProbeResult | null {
  return probes[probeKey(model.provider, model.id)] ?? model.probe ?? null;
}

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor((sorted.length - 1) / 2)];
}

function priceToken(model: LaneModelOption | undefined): string | null {
  if (!model || (model.price_in_per_mtok_usd == null && model.price_out_per_mtok_usd == null)) return null;
  const combined = (model.price_in_per_mtok_usd ?? 0) + (model.price_out_per_mtok_usd ?? 0);
  return `$${combined.toFixed(2)}/1M`;
}

export function SmokePanel({
  models,
  probes,
  busy,
  batchRunning,
  onCatalogProbe,
}: {
  models: LaneModelOption[];
  probes: Record<string, ModelProbeResult>;
  busy: boolean;
  batchRunning: boolean;
  onCatalogProbe: () => void;
}) {
  const probeable = filterSinnvoll(models).filter((model) => model.runtime === "hermes");
  const probesFor = probeable.map((m) => effectiveProbe(m, probes));

  const reachable = probesFor.filter((p) => p && (p.status === "ok" || p.status === "fallback")).length;
  const blocked = probesFor.filter((p) => p && UNREACHABLE_PROBE_STATUSES.has(p.status)).length;
  const p50 = median(
    probesFor
      .filter((p) => p && (p.status === "ok" || p.status === "fallback") && (p.duration_ms ?? 0) > 0)
      .map((p) => p!.duration_ms as number),
  );

  const feed = Object.values(probes).sort((a, b) => (b.at ?? 0) - (a.at ?? 0));

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-2">
        <KpiTile label={t.erreichbar} value={t.of(reachable, probeable.length)} />
        <KpiTile label={t.p50} value={p50 != null ? `${p50}` : t.noProbeData} suffix={p50 != null ? "ms" : undefined} />
        <KpiTile label={t.blockiert} value={String(blocked)} dot={blocked > 0 ? "error" : "idle"} />
      </div>

      <button
        type="button"
        disabled={busy || batchRunning || probeable.length === 0}
        onClick={onCatalogProbe}
        className="flex min-h-12 w-full items-center justify-center gap-2 rounded-card border border-live bg-live px-3 text-sec font-semibold text-surface-0 transition-colors duration-150 hover:bg-bronze-hi disabled:cursor-not-allowed disabled:opacity-40"
      >
        <Activity className="h-4 w-4" />
        {batchRunning ? t.measuring : t.katalogMessen(probeable.length)}
      </button>

      {feed.length === 0 ? (
        <div className="rounded-card border border-dashed border-line p-4">
          <p className="text-sec text-ink-2">{t.smokeEmptyTitle}</p>
          <p className="mt-1 text-micro text-ink-3">{t.smokeEmptyEval}</p>
          <p className="mt-1 text-micro text-ink-3">{t.smokeEmptyAction}</p>
        </div>
      ) : (
        <div className="space-y-1.5">
          {feed[0]?.at ? (
            <p className="font-data text-micro tabular-nums text-ink-3">
              {t.zuletztGemessen(
                new Date(feed[0].at * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }),
              )}
            </p>
          ) : null}
          {feed.map((probe) => {
            const model =
              models.find((m) => m.id === probe.model && (m.provider ?? "") === (probe.provider ?? "")) ??
              models.find((m) => m.id === probe.model);
            const price = priceToken(model);
            const meta = [
              probe.duration_ms != null && probe.duration_ms > 0 ? `${probe.duration_ms} ms` : null,
              price,
            ]
              .filter(Boolean)
              .join(" · ");
            return (
              <ListRow
                key={`${probe.provider}::${probe.model}::${probe.at ?? 0}`}
                leading={
                  <span className={cn("pdot", providerDot(probe.provider, probe.model))} aria-hidden />
                }
                title={
                  <span className="flex items-center gap-2">
                    <SignalChip tone={probeTone(probe.status)} label={PROBE_STATUS_LABEL[probe.status]} />
                    <span className="font-data text-micro">{probe.provider || "—"}/{probe.model}</span>
                  </span>
                }
                meta={meta || undefined}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}
