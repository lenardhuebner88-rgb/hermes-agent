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
import { useId, useMemo } from "react";
import { de } from "../i18n/de";
import { fmtDur, fmtTokens, nowSec } from "../lib/derive";
import { profileLabel } from "../lib/tones";
import {
  useEpics,
  useHermesReliability,
  useHermesRunsCosts,
  useHermesRunsDaily,
  useHermesRunSummary,
} from "../hooks/useControlData";
import type { CostBucket, CostProfileRow, Epic, ReliabilityProfile, RunsCostsResponse, RunsDailyPoint } from "../lib/schemas";
import { Hero } from "../components/Hero";
import { StaleBadge, ToneCallout } from "../components/atoms";
import { SkeletonCard } from "../components/primitives";
import { FleetPod, FleetPanel, FleetEmptyState } from "../components/fleet/atoms";
import { RunSummaryTile } from "../components/RunSummaryTile";
import { DayBars, RateBar, Sparkline, type SeriesPoint } from "../components/charts/charts";

const fmtUsd = (v: number) => `$ ${v.toFixed(2)}`;
const fmtPct = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)} %`);
const dayLabel = (iso: string) => iso.slice(5); // MM-DD reicht im Tooltip

// F4: echte $ zuerst, das Subscription-API-Äquivalent klar als ≈ daneben —
// nie addieren (ehrliche $0 der Max-Abo-Lanes bleiben sichtbar ehrlich).
const fmtCostPair = (b: CostBucket) => {
  const real = fmtUsd(b.cost_usd ?? 0);
  return b.cost_usd_equivalent != null ? `${real} · ≈ ${fmtUsd(b.cost_usd_equivalent)}` : real;
};

type SubscriptionTokenBucket = {
  key: "chatgpt" | "claude" | "kimi";
  label: string;
  runs: number;
  inputTokens: number;
  outputTokens: number;
  costEquivalent: number;
};

const SUBSCRIPTION_BUCKETS: SubscriptionTokenBucket[] = [
  { key: "chatgpt", label: "ChatGPT/Codex Abo", runs: 0, inputTokens: 0, outputTokens: 0, costEquivalent: 0 },
  { key: "claude", label: "Claude Max Abo", runs: 0, inputTokens: 0, outputTokens: 0, costEquivalent: 0 },
  { key: "kimi", label: "Kimi Abo", runs: 0, inputTokens: 0, outputTokens: 0, costEquivalent: 0 },
];

function subscriptionTokenBuckets(rows: CostProfileRow[]): SubscriptionTokenBucket[] {
  const buckets = new Map(SUBSCRIPTION_BUCKETS.map((b) => [b.key, { ...b }]));
  for (const row of rows) {
    // Bucket strictly on the server-resolved subscription lane (grounded in the
    // profile's provider/runtime config). API-billed lanes carry null and are
    // intentionally excluded — they are not subscriptions.
    if (!row.subscription) continue;
    const bucket = buckets.get(row.subscription);
    if (!bucket) continue;
    bucket.runs += row.runs;
    bucket.inputTokens += row.input_tokens ?? 0;
    bucket.outputTokens += row.output_tokens ?? 0;
    bucket.costEquivalent += row.cost_usd_equivalent ?? 0;
  }
  return SUBSCRIPTION_BUCKETS
    .map((b) => buckets.get(b.key) ?? b)
    .filter((b) => b.runs > 0 || b.inputTokens > 0 || b.outputTokens > 0);
}

function points(series: RunsDailyPoint[], pick: (p: RunsDailyPoint) => number | null): SeriesPoint[] {
  return series.map((p) => ({ label: dayLabel(p.date), value: pick(p) ?? 0 }));
}

/** Δ der Abschluss-Rate gegen die 30d-Baseline, gerundet auf Prozentpunkte. */
function completedDelta(current: ReliabilityProfile, baseline: ReliabilityProfile | undefined): number | null {
  if (!baseline || current.completed_rate == null || baseline.completed_rate == null) return null;
  return Math.round((current.completed_rate - baseline.completed_rate) * 100);
}

// Epic-Kompaktübersicht: eine Zeile pro OFFENEM Epic — Fortschritt (done/total,
// RateBar wiederverwendet) + Token-Burn + ggf. gemessene $. Bewusst keine neuen
// Diagrammtypen (Grill-Entscheid 2); die Anlage/Zuordnung lebt im Flow-Board.
function EpicRows({ epics }: { epics: Epic[] }) {
  return (
    <ul className="space-y-1.5">
      {epics.map((e) => {
        const tokens = (e.input_tokens ?? 0) + (e.output_tokens ?? 0);
        const rate = e.task_count > 0 ? e.done_tasks / e.task_count : null;
        return (
          <li key={e.id} className="flex flex-wrap items-center gap-2 rounded-md border border-[var(--hc-border)] px-2.5 py-2">
            <span className="min-w-0 flex-1 basis-40 truncate hc-type-label text-white">{e.title || e.id}</span>
            <span className="hc-mono w-24 shrink-0 hc-type-label hc-soft">
              {e.task_count > 0 ? de.stats.epicProgress(e.done_tasks, e.task_count) : de.stats.epicNoTasks}
            </span>
            <div className="w-20 shrink-0"><RateBar rate={rate} /></div>
            <span className="hc-mono shrink-0 hc-type-label hc-dim">
              {tokens > 0 ? fmtTokens(tokens) : de.stats.epicNoTokens}
              {e.cost_usd != null && e.cost_usd > 0 ? ` · ${fmtUsd(e.cost_usd)}` : ""}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

// F4: Kosten heute / Fenster + Top-Profile nach Kosten (Backend sortiert nach
// Burn = $ + Äquivalent). Exportiert für den Render-Test.
export function CostBreakdownPanel({ data }: { data: RunsCostsResponse | null }) {
  if (!data) return <SkeletonCard rows={3} />;
  const top = data.profiles.slice(0, 6);
  return (
    <FleetPanel eyebrow={de.stats.topProfiles} meta={de.stats.topProfilesHint}>
      <div className="grid grid-cols-2 gap-2">
        <FleetPod label={de.stats.costToday} value={fmtCostPair(data.today)} />
        <FleetPod label={de.stats.costWindow(data.days)} value={fmtCostPair(data.window)} />
      </div>
      {top.length ? (
        <ul className="mt-3 space-y-1.5">
          {top.map((p) => {
            const tokens = (p.input_tokens ?? 0) + (p.output_tokens ?? 0);
            return (
              <li key={p.profile} className="flex flex-wrap items-center gap-2 rounded-md border border-[var(--hc-border)] px-2.5 py-2">
                <span className="min-w-0 flex-1 basis-32 truncate hc-type-label text-white">{profileLabel[p.profile] ?? p.profile}</span>
                <span className="hc-mono w-16 shrink-0 hc-type-label hc-dim">{de.stats.costRuns(p.runs)}</span>
                <span className="hc-mono w-16 shrink-0 hc-type-label text-white">{p.cost_usd != null ? fmtUsd(p.cost_usd) : "—"}</span>
                <span className="hc-mono w-20 shrink-0 hc-type-label hc-soft">{p.cost_usd_equivalent != null ? `≈ ${fmtUsd(p.cost_usd_equivalent)}` : ""}</span>
                <span className="hc-mono shrink-0 hc-type-label hc-dim">{tokens > 0 ? fmtTokens(tokens) : "—"}</span>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="mt-3 hc-type-label hc-dim">{de.stats.costNoData}</p>
      )}
    </FleetPanel>
  );
}

export function AboTokenPanel({ data }: { data: RunsCostsResponse | null }) {
  if (!data) return <SkeletonCard rows={3} />;
  const buckets = subscriptionTokenBuckets(data.profiles);
  return (
    <FleetPanel eyebrow={de.stats.subscriptionTokens} meta={de.stats.subscriptionTokensHint}>
      {buckets.length ? (
        <div className="grid gap-2 sm:grid-cols-3">
          {buckets.map((b) => {
            const total = b.inputTokens + b.outputTokens;
            return (
              <div key={b.key} className="rounded-xl border border-[var(--hc-border)] bg-white/[.035] p-3">
                <span className="hc-eyebrow">{b.label}</span>
                <strong className="mt-2 block hc-mono text-lg text-[var(--hc-text)]">{fmtTokens(total)}</strong>
                <span className="mt-1 block hc-type-label hc-dim">
                  {de.stats.subscriptionInputOutput(fmtTokens(b.inputTokens), fmtTokens(b.outputTokens))}
                </span>
                <span className="mt-1 block hc-type-label hc-soft">
                  {de.stats.costRuns(b.runs)}{b.costEquivalent > 0 ? ` · ${de.stats.subscriptionEquivalent(fmtUsd(b.costEquivalent))}` : ""}
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <p className="hc-type-label hc-dim">{de.stats.subscriptionNoData}</p>
      )}
    </FleetPanel>
  );
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
                  {p.low_sample ? <span className="ml-2 rounded-full border border-[var(--hc-border)] px-1.5 py-0.5 hc-eyebrow">{de.stats.lowSample}</span> : null}
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
                    <span className="hc-type-label hc-dim">{de.stats.noJudgements}</span>
                  ) : (
                    <span className="hc-type-label">
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

type WeekTotals = {
  roots: number;
  tasks: number;
  outTokens: number;
  measuredCost: number;
  hasMeasuredCost: boolean;
};

function weekTotals(points: RunsDailyPoint[]): WeekTotals {
  const initial: WeekTotals = { roots: 0, tasks: 0, outTokens: 0, measuredCost: 0, hasMeasuredCost: false };
  return points.reduce<WeekTotals>(
    (acc, p) => ({
      roots: acc.roots + p.done_roots,
      tasks: acc.tasks + p.done_tasks,
      outTokens: acc.outTokens + (p.output_tokens ?? 0),
      measuredCost: acc.measuredCost + (p.cost_usd ?? 0),
      hasMeasuredCost: acc.hasMeasuredCost || p.cost_usd != null,
    }),
    initial,
  );
}

function signedNumberDelta(delta: number) {
  return `${delta >= 0 ? "+" : "−"}${Math.abs(delta)}`;
}

function signedTokenDelta(delta: number) {
  return `${delta >= 0 ? "+" : "−"}${fmtTokens(Math.abs(delta))}`;
}

function signedUsdDelta(delta: number) {
  return `${delta >= 0 ? "+" : "−"} ${fmtUsd(Math.abs(delta))}`;
}

function percentDelta(current: number, previous: number): string {
  if (previous === 0) return de.stats.weekCompareNoPrior;
  const delta = (current - previous) / previous;
  return `${delta >= 0 ? "+" : "−"}${Math.abs(Math.round(delta * 100))} %`;
}

function profileCompletedRuns(profile: ReliabilityProfile): number {
  const explicit = profile.outcomes.completed;
  if (typeof explicit === "number") return explicit;
  return profile.completed_rate == null ? 0 : Math.round(profile.completed_rate * profile.runs);
}

function weightedCompletedRate(profiles: ReliabilityProfile[]): number | null {
  const runs = profiles.reduce((acc, p) => acc + p.runs, 0);
  if (runs <= 0) return null;
  const completed = profiles.reduce((acc, p) => acc + profileCompletedRuns(p), 0);
  return completed / runs;
}

function fmtRoots(n: number) {
  return `${n} Roots`;
}

function fmtTasks(n: number) {
  return `${n} Tasks`;
}

export function StatsSignalPanel({ last7, reliabilityProfiles }: { last7: RunsDailyPoint[]; reliabilityProfiles: ReliabilityProfile[] }) {
  const gradientId = useId().replace(/:/g, "");
  const ringId = `${gradientId}-stats-signal-ring`;
  const fillId = `${gradientId}-stats-signal-fill`;
  const roots = last7.reduce((acc, p) => acc + p.done_roots, 0);
  const tasks = last7.reduce((acc, p) => acc + p.done_tasks, 0);
  const tokens = last7.reduce((acc, p) => acc + (p.output_tokens ?? 0), 0);
  const completedRate = weightedCompletedRate(reliabilityProfiles);
  const completedPct = completedRate == null ? null : Math.round(completedRate * 100);
  const ring = completedPct == null ? 0 : Math.max(0, Math.min(100, completedPct));
  const dash = `${ring} ${100 - ring}`;
  const activeDays = last7.filter((p) => p.done_roots > 0 || p.done_tasks > 0).length;

  return (
    <FleetPanel eyebrow={de.stats.signal} meta={de.stats.signalHint} className="hc-stats-signal overflow-hidden">
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.15fr)_minmax(220px,.85fr)] lg:items-center">
        <div className="space-y-3">
          <p className="hc-hero-statement max-w-2xl text-2xl text-[var(--hc-text)] lg:text-3xl">
            {de.stats.signalStatement(roots, tasks)}
          </p>
          <div className="grid grid-cols-3 gap-2">
            <div className="rounded-xl border border-[var(--hc-border)] bg-white/[.035] p-3">
              <span className="hc-eyebrow">{de.stats.signalRoots}</span>
              <strong className="mt-2 block hc-mono text-lg text-[var(--hc-text)]">{fmtRoots(roots)}</strong>
            </div>
            <div className="rounded-xl border border-[var(--hc-border)] bg-white/[.035] p-3">
              <span className="hc-eyebrow">{de.stats.signalTasks}</span>
              <strong className="mt-2 block hc-mono text-lg text-[var(--hc-text)]">{fmtTasks(tasks)}</strong>
            </div>
            <div className="rounded-xl border border-[var(--hc-border)] bg-white/[.035] p-3">
              <span className="hc-eyebrow">{de.stats.signalTokens}</span>
              <strong className="mt-2 block hc-mono text-lg text-[var(--hc-text)]">{fmtTokens(tokens)} Tokens</strong>
            </div>
          </div>
        </div>
        <div className="relative mx-auto aspect-square w-full max-w-[17rem]">
          <svg viewBox="0 0 120 120" className="h-full w-full" role="img" aria-label={de.stats.signalAria(completedPct)}>
            <defs>
              <linearGradient id={ringId} x1="10" y1="10" x2="110" y2="110" gradientUnits="userSpaceOnUse">
                <stop offset="0%" stopColor="var(--hc-accent-strong)" />
                <stop offset="55%" stopColor="var(--hc-accent)" />
                <stop offset="100%" stopColor="var(--hc-cyan)" />
              </linearGradient>
              <radialGradient id={fillId} cx="50%" cy="42%" r="58%">
                <stop offset="0%" stopColor="rgba(46,69,212,.18)" />
                <stop offset="100%" stopColor="rgba(10,138,166,.02)" />
              </radialGradient>
            </defs>
            <circle cx="60" cy="60" r="42" fill={`url(#${fillId})`} stroke="var(--hc-border)" strokeWidth="1" />
            <circle cx="60" cy="60" r="46" fill="none" stroke="var(--hc-border)" strokeWidth="7" opacity=".75" />
            <circle cx="60" cy="60" r="46" fill="none" stroke={`url(#${ringId})`} strokeWidth="7" strokeLinecap="round" pathLength="100" strokeDasharray={dash} transform="rotate(-90 60 60)" />
            {last7.map((p, i) => {
              const angle = (-120 + i * 40) * (Math.PI / 180);
              const radius = 28 + Math.min(18, p.done_roots * 4 + p.done_tasks * 0.8);
              const x = 60 + Math.cos(angle) * radius;
              const y = 60 + Math.sin(angle) * radius;
              return <circle key={`${p.date}-${i}`} cx={x.toFixed(1)} cy={y.toFixed(1)} r={p.done_roots > 0 ? 2.6 : 1.4} fill={p.done_roots > 0 ? "var(--hc-accent)" : "var(--hc-border-strong)"}><title>{`${dayLabel(p.date)} · ${p.done_roots} Roots · ${p.done_tasks} Tasks`}</title></circle>;
            })}
            <text x="60" y="57" textAnchor="middle" className="hc-mono" fill="var(--hc-text)" fontSize="18" fontWeight="700">{completedPct == null ? "—" : `${completedPct}%`}</text>
            <text x="60" y="73" textAnchor="middle" className="hc-eyebrow" fill="var(--hc-text-dim)">{de.stats.signalRateShort}</text>
          </svg>
          <div className="pointer-events-none absolute inset-x-0 bottom-4 text-center hc-type-label hc-soft">
            {de.stats.signalActiveDays(activeDays)}
          </div>
        </div>
      </div>
    </FleetPanel>
  );
}

// Wochenvergleich: rollierende letzte 7 Kalendertage gegen die 7 Tage davor.
// Frontend-only aus /runs/daily; keine Kalenderwochen, keine Backend-Erweiterung.
export function WochenvergleichPanel({ series }: { series: RunsDailyPoint[] }) {
  const current = weekTotals(series.slice(-7));
  const previous = weekTotals(series.slice(-14, -7));
  const showMeasuredCost = current.hasMeasuredCost || previous.hasMeasuredCost;

  return (
    <FleetPanel eyebrow={de.stats.weekCompare} meta={de.stats.weekCompareHint}>
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <FleetPod
          label={de.stats.weekCompareRoots}
          value={current.roots}
          suffix={`${signedNumberDelta(current.roots - previous.roots)} · ${percentDelta(current.roots, previous.roots)}`}
        />
        <FleetPod
          label={de.stats.weekCompareTasks}
          value={current.tasks}
          suffix={signedNumberDelta(current.tasks - previous.tasks)}
        />
        <FleetPod
          label={de.stats.weekCompareOutTokens}
          value={fmtTokens(current.outTokens)}
          suffix={signedTokenDelta(current.outTokens - previous.outTokens)}
        />
        {showMeasuredCost ? (
          <FleetPod
            label={de.stats.weekCompareMeasuredCost}
            value={fmtUsd(current.measuredCost)}
            suffix={signedUsdDelta(current.measuredCost - previous.measuredCost)}
          />
        ) : null}
      </div>
    </FleetPanel>
  );
}

// T5 (Wert-Bilanz): Wochenbilanz nach Klasse — zeigt erstmals, WOFÜR die
// Flotte gearbeitet hat (Nutzer-Feature aus dem Demand-Funnel vs. Härtung vs.
// Meta). Klasse kommt v1 aus created_by (bewusst unscharf, kein Schema-Touch).
export function WertBilanzPanel({ last7 }: { last7: RunsDailyPoint[] }) {
  const sums = last7.reduce(
    (acc, p) => ({
      nutzer: acc.nutzer + (p.done_roots_by_class?.nutzer ?? 0),
      haertung: acc.haertung + (p.done_roots_by_class?.haertung ?? 0),
      meta: acc.meta + (p.done_roots_by_class?.meta ?? 0),
    }),
    { nutzer: 0, haertung: 0, meta: 0 },
  );
  return (
    <FleetPanel eyebrow={de.stats.valueBalance} meta={de.stats.valueBalanceHint}>
      <div className="grid grid-cols-3 gap-2">
        <FleetPod label={de.stats.classNutzer} value={sums.nutzer} />
        <FleetPod label={de.stats.classHaertung} value={sums.haertung} />
        <FleetPod label={de.stats.classMeta} value={sums.meta} />
      </div>
    </FleetPanel>
  );
}

export function StatistikView() {
  const daily = useHermesRunsDaily();
  const reliability = useHermesReliability();
  const summary = useHermesRunSummary();
  const costs = useHermesRunsCosts();
  const epics = useEpics();
  const now = nowSec();
  const openEpics = useMemo(() => (epics.data?.epics ?? []).filter((e) => e.status === "open"), [epics.data]);

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
        action={
          <div className="flex flex-wrap justify-end gap-1.5">
            <StaleBadge isStale={daily.isStale} lastUpdated={daily.lastUpdated} errorObj={daily.errorObj} error={daily.error} now={now} />
            <StaleBadge isStale={summary.isStale} lastUpdated={summary.lastUpdated} errorObj={summary.errorObj} error={summary.error} now={now} />
            <StaleBadge isStale={costs.isStale} lastUpdated={costs.lastUpdated} errorObj={costs.errorObj} error={costs.error} now={now} />
            <StaleBadge isStale={reliability.isStale} lastUpdated={reliability.lastUpdated} errorObj={reliability.errorObj} error={reliability.error} now={now} />
          </div>
        }
      >
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <FleetPod label={de.stats.podToday} value={loadingFirst ? "—" : (today?.done_roots ?? 0)} />
          <FleetPod label={de.stats.podCycleP50} value={summary.data?.cycle_time_p50_seconds != null ? fmtDur(summary.data.cycle_time_p50_seconds) : "—"} />
          <FleetPod label={de.stats.costToday} value={costs.data ? fmtCostPair(costs.data.today) : summary.data?.total_cost_usd != null ? fmtUsd(summary.data.total_cost_usd) : "—"} />
          <FleetPod label={de.stats.podTokens} value={today?.output_tokens != null ? fmtTokens(today.output_tokens) : "—"} />
        </div>
      </Hero>

      {daily.error ? <ToneCallout tone="red">{de.stats.loadError}<br />{daily.error}</ToneCallout> : null}

      {/* Offene Epics — die Vorhaben-Ebene über den Tages-Charts. Nur gezeigt,
          wenn es offene Epics gibt (kein Rauschen für den Nicht-Nutzer). */}
      {openEpics.length ? (
        <FleetPanel eyebrow={de.stats.epics} meta={de.stats.epicsHint}>
          {epics.error ? <ToneCallout tone="red">{epics.error}</ToneCallout> : <EpicRows epics={openEpics} />}
        </FleetPanel>
      ) : null}

      {loadingFirst ? (
        <div className="grid gap-3 lg:grid-cols-2"><SkeletonCard rows={4} /><SkeletonCard rows={4} /></div>
      ) : isEmpty ? (
        <FleetEmptyState title={de.stats.empty} desc="" />
      ) : (
        <>
          <StatsSignalPanel last7={last7} reliabilityProfiles={reliability.data?.profiles ?? []} />
          <WertBilanzPanel last7={last7} />
          <WochenvergleichPanel series={series} />

          <div className="grid gap-3 lg:grid-cols-2">
            <FleetPanel eyebrow={de.stats.throughput} meta={de.stats.throughputHint}>
              <DayBars points={points(series, (p) => p.done_roots)} />
              <p className="mt-3 hc-type-label hc-dim">{de.stats.tasksLine}</p>
              <Sparkline points={points(series, (p) => p.done_tasks)} stroke="var(--hc-cyan)" />
            </FleetPanel>

            <div className="space-y-3">
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
              {costs.error ? <ToneCallout tone="red">{costs.error}</ToneCallout> : <CostBreakdownPanel data={costs.data ?? null} />}
              <AboTokenPanel data={costs.data ?? null} />
            </div>
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
              {/* F6: Absprung ins Issue-Board (Detail-Seite der Statistik). */}
              <a
                href="/control/issues"
                className="mt-3 inline-flex min-h-9 items-center rounded-md border border-white/10 px-2.5 py-1 hc-type-label hc-soft hover:bg-white/5"
              >
                {de.stats.issuesLink}
              </a>
            </FleetPanel>
          </div>

          {/* Letzte Lieferungen — der wiederverwendete Root-Summary-Block */}
          <RunSummaryTile data={summary.data ?? null} now={now} error={summary.error} loading={summary.loading} />
        </>
      )}
    </div>
  );
}
