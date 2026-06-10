/**
 * StatistikView (/control/statistik) — Phase 3 des Operator-Vertrags.
 *
 * Vier Fragen, vier Sektionen, alles aus echten Endpoints:
 *   Durchsatz   — /runs/daily   (gelieferte Roots/Tasks pro Tag)
 *   Kosten      — /runs/daily   (gemessene $ + Token-Burn; $0 = Subscription)
 *   Cycle-Time  — /runs/daily + /runs/summary (p50/p90)
 *   Reliability — /runs/reliability (pro Profil, min-n-gedämpft, 30d-Baseline)
 * Charts sind handgebaute SVG-Primitives (components/charts) — keine Lib.
 */
import { useMemo } from "react";
import { de } from "../i18n/de";
import { fmtDur, nowSec } from "../lib/derive";
import { profileLabel } from "../lib/tones";
import {
  useHermesReliability,
  useHermesRunsDaily,
  useHermesRunSummary,
} from "../hooks/useControlData";
import type { ReliabilityProfile, RunsDailyPoint } from "../lib/schemas";
import { Hero } from "../components/Hero";
import { ToneCallout } from "../components/atoms";
import { SkeletonCard } from "../components/primitives";
import { FleetPod, FleetPanel, FleetEmptyState } from "../components/fleet/atoms";
import { RunSummaryTile } from "../components/RunSummaryTile";
import { DayBars, RateBar, Sparkline, type SeriesPoint } from "../components/charts/charts";

const fmtUsd = (v: number) => `$ ${v.toFixed(2)}`;
const fmtPct = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)} %`);
const fmtTokens = (v: number) =>
  v >= 1_000_000 ? `${(v / 1_000_000).toFixed(1)} M` : v >= 1_000 ? `${Math.round(v / 1_000)} k` : String(v);
const dayLabel = (iso: string) => iso.slice(5); // MM-DD reicht im Tooltip

function points(series: RunsDailyPoint[], pick: (p: RunsDailyPoint) => number | null): SeriesPoint[] {
  return series.map((p) => ({ label: dayLabel(p.date), value: pick(p) ?? 0 }));
}

/** Δ der Abschluss-Rate gegen die 30d-Baseline, gerundet auf Prozentpunkte. */
function completedDelta(current: ReliabilityProfile, baseline: ReliabilityProfile | undefined): number | null {
  if (!baseline || current.completed_rate == null || baseline.completed_rate == null) return null;
  return Math.round((current.completed_rate - baseline.completed_rate) * 100);
}

function ReliabilityTable({ profiles, baseline, minN }: {
  profiles: ReliabilityProfile[]; baseline: ReliabilityProfile[]; minN: number;
}) {
  const baseByProfile = useMemo(() => new Map(baseline.map((p) => [p.profile, p])), [baseline]);
  if (!profiles.length) return <FleetEmptyState title={de.stats.empty} desc="" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[34rem] border-separate border-spacing-0 text-sm">
        <thead>
          <tr className="text-left">
            {[de.stats.colProfile, de.stats.colRuns, de.stats.colCompleted, de.stats.colRetry, de.stats.colVerdicts, de.stats.colDelta].map((h) => (
              <th key={h} className="hc-eyebrow border-b border-[var(--hc-border)] px-2 pb-2 font-semibold">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {profiles.map((p) => {
            const delta = completedDelta(p, baseByProfile.get(p.profile));
            return (
              <tr key={p.profile} className="align-middle">
                <td className="border-b border-[var(--hc-border)] px-2 py-2.5">
                  <span className="font-medium text-white">{profileLabel[p.profile] ?? p.profile}</span>
                  {p.low_sample ? <span className="ml-2 rounded-full border border-[var(--hc-border)] px-1.5 py-0.5 text-[0.64rem] hc-dim">{de.stats.lowSample}</span> : null}
                </td>
                <td className="hc-mono border-b border-[var(--hc-border)] px-2 py-2.5">{p.runs}</td>
                <td className="border-b border-[var(--hc-border)] px-2 py-2.5">
                  <div className="flex items-center gap-2">
                    <span className="hc-mono w-12 shrink-0">{fmtPct(p.completed_rate)}</span>
                    <div className="w-20"><RateBar rate={p.completed_rate} /></div>
                  </div>
                </td>
                <td className="hc-mono border-b border-[var(--hc-border)] px-2 py-2.5">{fmtPct(p.retry_rate)}</td>
                <td className="border-b border-[var(--hc-border)] px-2 py-2.5">
                  {p.judged === 0 ? (
                    <span className="hc-dim text-[0.78rem]">{de.stats.noJudgements}</span>
                  ) : (
                    <span className="text-[0.82rem]">
                      <span className="text-emerald-300">{de.stats.judgedLine(p.approved, p.rejected)}</span>
                      <span className="hc-mono ml-2 hc-soft">{p.approve_rate != null ? fmtPct(p.approve_rate) : `n<${minN}`}</span>
                    </span>
                  )}
                </td>
                <td className="hc-mono border-b border-[var(--hc-border)] px-2 py-2.5">
                  {delta == null ? <span className="hc-dim">—</span> : (
                    <span className={delta > 0 ? "text-emerald-300" : delta < 0 ? "text-red-300" : "hc-soft"}>
                      {delta > 0 ? "▲" : delta < 0 ? "▼" : "•"} {Math.abs(delta)} pp
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function StatistikView() {
  const daily = useHermesRunsDaily();
  const reliability = useHermesReliability();
  const summary = useHermesRunSummary();
  const now = nowSec();

  const series = useMemo(() => daily.data?.series ?? [], [daily.data]);
  const last7 = series.slice(-7);
  const today = series[series.length - 1];
  const rootsWeek = last7.reduce((acc, p) => acc + p.done_roots, 0);
  const costSeries = useMemo(() => points(series, (p) => p.cost_usd), [series]);
  const hasCost = series.some((p) => (p.cost_usd ?? 0) > 0);
  const hasTokens = series.some((p) => (p.output_tokens ?? 0) > 0);
  const loadingFirst = daily.loading && daily.data == null;
  const isEmpty = !loadingFirst && !series.some((p) => p.done_tasks > 0 || p.runs_completed > 0);

  return (
    <div className="space-y-4">
      <Hero
        eyebrow={de.stats.eyebrow}
        title={de.stats.title}
        subtitle={de.stats.subtitle}
        count={loadingFirst ? "—" : rootsWeek}
        countHint={de.stats.rootsWeek}
        tone="violet"
      >
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <FleetPod label={de.stats.podToday} value={loadingFirst ? "—" : (today?.done_roots ?? 0)} />
          <FleetPod label={de.stats.podCycleP50} value={summary.data?.cycle_time_p50_seconds != null ? fmtDur(summary.data.cycle_time_p50_seconds) : "—"} />
          <FleetPod label={de.stats.podCost} value={summary.data?.total_cost_usd != null ? fmtUsd(summary.data.total_cost_usd) : "—"} />
          <FleetPod label={de.stats.podTokens} value={today?.output_tokens != null ? fmtTokens(today.output_tokens) : "—"} />
        </div>
      </Hero>

      {daily.error ? <ToneCallout tone="red">{de.stats.loadError}<br />{daily.error}</ToneCallout> : null}

      {loadingFirst ? (
        <div className="grid gap-3 lg:grid-cols-2"><SkeletonCard rows={4} /><SkeletonCard rows={4} /></div>
      ) : isEmpty ? (
        <FleetEmptyState title={de.stats.empty} desc="" />
      ) : (
        <>
          <div className="grid gap-3 lg:grid-cols-2">
            <FleetPanel eyebrow={de.stats.throughput} meta={de.stats.throughputHint}>
              <DayBars points={points(series, (p) => p.done_roots)} />
              <p className="mt-3 hc-type-label hc-dim">{de.stats.tasksLine}</p>
              <Sparkline points={points(series, (p) => p.done_tasks)} stroke="var(--hc-cyan)" />
            </FleetPanel>

            <FleetPanel eyebrow={de.stats.costs} meta={de.stats.costsHint}>
              {hasCost ? (
                <DayBars points={costSeries} color="var(--hc-amber)" valueFmt={(v) => fmtUsd(v)} />
              ) : null}
              <p className="mt-2 hc-type-label hc-soft">{de.stats.costNote}</p>
              {hasTokens ? (
                <>
                  <p className="mt-3 hc-type-label hc-dim">{de.stats.tokensLine}</p>
                  <Sparkline points={points(series, (p) => p.output_tokens)} stroke="var(--hc-accent-2)" valueFmt={(v) => fmtTokens(v)} />
                </>
              ) : null}
            </FleetPanel>
          </div>

          <div className="grid gap-3 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <FleetPanel eyebrow={de.stats.cycle} meta={de.stats.cycleHint}>
              <Sparkline points={points(series, (p) => p.cycle_time_p50_seconds)} stroke="var(--hc-emerald)" valueFmt={(v) => fmtDur(v)} />
              <div className="mt-3 grid grid-cols-2 gap-2">
                <FleetPod label="p50 · 24h" value={summary.data?.cycle_time_p50_seconds != null ? fmtDur(summary.data.cycle_time_p50_seconds) : "—"} />
                <FleetPod label="p90 · 24h" value={summary.data?.cycle_time_p90_seconds != null ? fmtDur(summary.data.cycle_time_p90_seconds) : "—"} />
              </div>
            </FleetPanel>

            <FleetPanel eyebrow={de.stats.reliability} meta={de.stats.reliabilityHint(reliability.data?.min_n ?? 5)}>
              {reliability.error ? <ToneCallout tone="red">{reliability.error}</ToneCallout> : reliability.loading && reliability.data == null ? (
                <SkeletonCard rows={4} />
              ) : (
                <ReliabilityTable
                  profiles={reliability.data?.profiles ?? []}
                  baseline={reliability.data?.baseline ?? []}
                  minN={reliability.data?.min_n ?? 5}
                />
              )}
            </FleetPanel>
          </div>

          {/* Letzte Lieferungen — der wiederverwendete Root-Summary-Block */}
          <RunSummaryTile data={summary.data ?? null} now={now} error={summary.error} loading={summary.loading} />
        </>
      )}
    </div>
  );
}
