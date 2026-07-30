/**
 * /control/statistik owns volume, latency, token, cache, and cost analysis.
 * Langfuse is a server-side data source only; worker quality stays exclusively
 * in /control/scorecard. The leading observability deck is mobile-first and
 * fails soft to the local Usage Facts ledger when Langfuse is unavailable.
 */
import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { ChevronRight, TriangleAlert } from "lucide-react";
import { de } from "../i18n/de";
import { fmtClock, fmtClockTime, fmtDur, fmtTokens, nowSec, formatEffectiveCost } from "../lib/derive";
import { profileLabel } from "../lib/tones";
import { useAccountUsage, useHermesRunsCosts, useHermesRunsCostSeries, useHermesSubscriptionBurn } from "../hooks/costsUsage";
import { useHermesRunsIssues, useHermesWindowedRollup } from "../hooks/runsDigestRollup";
import { useStatsConfig } from "../hooks/stats";
import { useObservabilityStats } from "../hooks/observability";
import type { ObservabilityStatsResponse, ObservabilityUsageOrigin } from "../lib/schemas/observability";
import type {
  AccountUsageProvider,
  CostProfileRow,
  IssueGroup,
  ReliabilityProfile,
  ReviewValueRow,
  RunsDailyPoint,
  RunsCostsSeriesResponse,
  SubscriptionTokenBurnResponse,
  WindowedRollupRoot,
  WindowedRollupWorker,
} from "../lib/schemas";
import { AccountUsageTile } from "../components/AccountUsageTile";
import { Eyebrow, SkeletonCard } from "../components/primitives";
import { DEFAULT_STATS_CONFIG } from "../lib/statsFields";
import { DrawerShell, KpiTile, SectionHeader, FleetEmptyState } from "../components/leitstand";
import { cn } from "@/lib/utils";
import {
  acceptance,
  acceptanceDelta,
  autonomy,
  budgetLedger,
  chainCost,
  chainShare,
  costPerDelivery,
  errorTaxonomy,
  gateEffectiveness,
  germanDate,
  laneBurn,
  leaderboard,
  nutzerwert,
  profileCostFact,
  rootRuns,
  rosterProfiles,
  sortedLedgerRoots,
  subscriptionBurnBreakdown,
  windowCostSummary,
  workerCost,
  workerTokenDisplay,
  type FigureStatus,
  type LedgerEntry,
  type MotherLedgerSortKey,
} from "../lib/statsBroadsheet";
import {
  DEFAULT_MIN_REVIEW_SAMPLE,
  WORKER_EFFICIENCY_BENCH_PROFILES,
  buildWorkerEfficiencyRows,
  workerEfficiencyLevers,
  type WorkerEfficiencyRow,
} from "../lib/workerEfficiency";
import "./statistik.css";

const pctText = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)}`);
const usdText = (v: number | null) => (v == null ? "—" : `$ ${v.toFixed(2)}`);

const ERROR_LABEL: Record<string, string> = {
  dead: de.stats.errDead,
  timeout: de.stats.errTimeout,
  budget: de.stats.errBudget,
  other: de.stats.errOther,
};

const LANE_COLORS: Record<string, string> = {
  coder: "var(--color-brand)",
  "coder-frontend": "var(--color-data-2)",
  "coder-claude": "var(--color-ink-3)",
  premium: "var(--color-brand)",
  reviewer: "var(--color-status-ok)",
  verifier: "var(--color-status-ok)",
  critic: "var(--color-status-alert)",
  scout: "var(--color-status-warn)",
  research: "var(--color-brand)",
  admin: "var(--color-ink-3)",
};
function laneStyle(profile: string): CSSProperties {
  return { "--st-lane": LANE_COLORS[profile] ?? "var(--color-ink-3)" } as CSSProperties;
}

function LaneLabel({ profile, label = profile }: { profile: string; label?: ReactNode }) {
  return (
    <span className="st-lane-label" style={laneStyle(profile)}>
      <span className="st-lane-dot" aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}

// ── Leitstand-Primitive für die Statistik (lokal, DESIGN.md-token-only) ──────
const FIG_CLASS: Record<FigureStatus, string> = {
  ok: "text-status-ok",
  warn: "text-status-warn",
  crit: "text-status-alert",
  neutral: "",
};

/** Ruhiger Verdikt-Absatz — calm bleibt still, warn/crit tragen die Status-Tönung. */
function StNote({ children, tone = "calm" }: { children: ReactNode; tone?: "calm" | "warn" | "crit" }) {
  return (
    <p className={cn("st-note", tone === "warn" && "text-status-warn", tone === "crit" && "text-status-alert")}>
      {children}
    </p>
  );
}

/** Der eine dringende Engpass-Callout — status-getönt, nie stumm. */
function StLead({ children, tone }: { children: ReactNode; tone: "crit" | "warn" }) {
  return (
    <div className={cn("st-lead", tone === "crit" ? "st-lead-crit" : "st-lead-warn")} role="alert" aria-live="polite">
      {children}
    </div>
  );
}

function fmtWorkerTokens(value: number | null): string {
  return value == null ? "—" : fmtTokens(Math.round(value));
}

/**
 * Latenzen kommen in Millisekunden und liegen bei TTFT praktisch immer unter
 * einer Sekunde. `fmtDur` schneidet auf ganze Sekunden ab (`Math.floor`), also
 * las sich ein gemessenes TTFT von 250 ms dort als "0s" — eine echte Zahl, die
 * als Null erscheint. Unterhalb einer Sekunde bleibt die Einheit deshalb ms.
 */
function fmtMillis(value: number | null): string {
  if (value == null) return "—";
  if (value < 1000) return `${Math.round(value).toLocaleString("de-DE")} ms`;
  return `${(value / 1000).toLocaleString("de-DE", { maximumFractionDigits: 1 })} s`;
}

function fmtWorkerRate(value: number | null): string {
  if (value == null) return "—";
  if (value >= 1000) return fmtTokens(Math.round(value));
  return Math.round(value).toLocaleString("de-DE");
}

function fmtWorkerUsd(value: number | null): string {
  return value == null ? "—" : `$${value.toFixed(2)}`;
}

function fmtWorkerPct(value: number | null): string {
  return value == null ? "—" : `${Math.round(value * 100)} %`;
}

function metricShare(value: number | null, max: number): string {
  if (value == null || max <= 0) return "0%";
  return `${Math.max(4, Math.min(100, Math.round((value / max) * 100)))}%`;
}

/** Leaderboard-Zeile: Rang · mono Lane-Label mit Status-Dot · großer mono Score
 *  rechts · ruhige Meta-Zeile darunter. */
function StLeaderRow({ rank, name, score, status, meta }: {
  rank: ReactNode;
  name: ReactNode;
  score: ReactNode;
  status: FigureStatus;
  meta?: ReactNode;
}) {
  return (
    <div className="st-lr">
      <span className="st-lr-rank">{rank}</span>
      <span className="st-lr-name min-w-0">{name}</span>
      <span className={cn("st-lr-score", FIG_CLASS[status])}>{score}</span>
      {meta != null ? <span className="st-lr-meta">{meta}</span> : null}
    </div>
  );
}

/** Ledger-Zeile: Name, mono Figur (status-getönt), dünner Meter, Fuß-Meta. */
function StLedgerRow({ name, tag, figure, status, pct, footLeft, footRight }: {
  name: ReactNode;
  tag?: ReactNode;
  figure: ReactNode;
  status: FigureStatus;
  pct: number | null;
  footLeft?: ReactNode;
  footRight?: ReactNode;
}) {
  return (
    <div className="st-led-row">
      <div className="st-led-top">
        <span className="st-led-name">
          {name}
          {tag != null ? <span className="st-tag">{tag}</span> : null}
        </span>
        <span className={cn("st-fig", FIG_CLASS[status])}>{figure}</span>
      </div>
      {pct != null ? (
        <div className="st-led-meter">
          <i className={`st-meter-${status}`} style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} />
        </div>
      ) : null}
      {footLeft != null || footRight != null ? (
        <div className="st-led-foot">
          <span>{footLeft}</span>
          <span>{footRight}</span>
        </div>
      ) : null}
    </div>
  );
}

// ── Masthead ────────────────────────────────────────────────────────────────
// Acceptance headline + the three supporting KPIs. `profiles`/`baseline` are the
// raw reliability windows; the masthead phantom-filters them itself (the same
// roster gate the leaderboard applies) so the headline acceptance, autonomy and
// Δ count only real configured workers — never a "w"/"(ohne profil)" sentinel.
// `series` is the last-7 daily slice.
export function StatsMasthead({
  profiles,
  baseline,
  series,
  now,
  stale = false,
}: {
  profiles: ReliabilityProfile[];
  baseline: ReliabilityProfile[];
  series: RunsDailyPoint[];
  now: number;
  stale?: boolean;
}) {
  const roster = useMemo(() => rosterProfiles(profiles), [profiles]);
  const rosterBaseline = useMemo(() => rosterProfiles(baseline), [baseline]);
  const acc = acceptance(roster);
  const delta = acceptanceDelta(roster, rosterBaseline);
  const aut = autonomy(roster);
  const cpd = costPerDelivery(series);
  const nutzer = nutzerwert(series);

  const meta = `${germanDate(now)} · ${de.stats.mastWindow}${stale ? ` · ${de.stats.mastStale}` : ""}`;
  const note = acc.rate == null ? de.stats.mastNoteEmpty : de.stats.mastNote(acc.approved, acc.rejected);
  const deltaNode = delta == null ? de.stats.mastDeltaNone : `${delta >= 0 ? "▲" : "▼"} ${Math.abs(delta)} ${de.stats.mastDeltaUnit}`;
  const deltaTone = delta == null ? "neutral" : delta >= 0 ? "up" : "down";

  return (
    <div className="space-y-3">
      <div className="st-mast-head">
        <div>
          {/* Kicker "Hermes · Statistik" entfernt (W3-3): reines Brand-Echo unter
              der Shell-Puls-Leiste, die bereits STATISTIK trägt (Ein-Masthead-Doktrin). */}
          <Eyebrow className="text-brand">{de.stats.mastLabel}</Eyebrow>
        </div>
        <span className="st-mast-meta">{meta}</span>
      </div>
      {/* Kein Balken-Torso im Leerzustand (Empty-States-Doktrin W4-7): die
          große Figur rendert nur mit echter Akzeptanzrate — bei 0 Urteilen
          trägt die Note-Zeile darunter den Zustand allein. */}
      {acc.rate != null ? (
        <div className="st-mast" data-testid="stats-masthead-figure">
          <span className="st-mast-value">{pctText(acc.rate)}</span>
          <small>%</small>
        </div>
      ) : null}
      <div className="st-mast-foot">
        <span className="st-note">{note}</span>
        <span className={cn("st-mast-delta", deltaTone === "up" ? "text-status-ok" : deltaTone === "down" ? "text-status-alert" : "text-ink-3")}>
          {deltaNode}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        <KpiTile
          label={de.stats.suppAutonomie}
          value={pctText(aut)}
          suffix={aut == null ? undefined : "%"}
          deltaTone="neutral"
          dot="live"
          delta={aut == null ? de.stats.suppAutonomieEmpty : undefined}
        />
        <KpiTile
          label={de.stats.suppCost}
          value={usdText(cpd)}
          delta={cpd == null ? de.stats.suppCostEmpty : undefined}
        />
        <KpiTile
          label={de.stats.suppNutzer}
          value={String(nutzer)}
          delta={nutzer === 0 ? de.stats.suppNutzerEmpty : undefined}
        />
      </div>
    </div>
  );
}

// ── Latenz (zwei KpiTiles) ───────────────────────────────────────────────────
export function LatencySection({ p50, p90 }: { p50: number | null; p90: number | null }) {
  return (
    <section className="space-y-2">
      <SectionHeader label={de.stats.secLatency} meta={de.stats.secLatencyMeta} />
      <div className="grid grid-cols-2 gap-2">
        <KpiTile label={de.stats.latP50} value={p50 == null ? "—" : fmtDur(p50)} />
        <KpiTile label={de.stats.latP90} value={p90 == null ? "—" : fmtDur(p90)} />
      </div>
    </section>
  );
}

// ── Verlässlichkeit (Leaderboard) ────────────────────────────────────────────
export function ReliabilitySection({ profiles }: { profiles: ReliabilityProfile[] }) {
  const rows = useMemo(() => leaderboard(profiles), [profiles]);
  return (
    <section className="space-y-2">
      <SectionHeader label={de.stats.secReliability} meta={de.stats.secReliabilityMeta} />
      {rows.length === 0 ? (
        <FleetEmptyState title={de.stats.leaderEmpty} desc={de.stats.leaderEmptyDesc} />
      ) : (
        <div className="st-panel space-y-1.5 p-2">
          {rows.map((r, i) => (
            <StLeaderRow
              key={r.profile}
              rank={i + 1}
              name={<LaneLabel profile={r.profile} label={r.label} />}
              score={r.rate == null ? "—" : `${Math.round(r.rate * 100)} %`}
              status={r.status}
              meta={de.stats.leaderRuns(r.runs)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

// ── Fehler-Taxonomie ─────────────────────────────────────────────────────────
export function ErrorTaxonomySection({ issues }: { issues: IssueGroup[] }) {
  const tax = useMemo(() => errorTaxonomy(issues), [issues]);
  let verdict: ReactNode;
  if (tax.buckets.length === 0) {
    verdict = <FleetEmptyState title={de.stats.errEmpty} desc={de.stats.errEmptyDesc} ok />;
  } else if (tax.allLifecycle) {
    verdict = (
      <StNote tone="calm">
        {de.stats.verdictPre}
        <b>{de.stats.verdictBold}</b>
        {de.stats.verdictPost}
      </StNote>
    );
  } else {
    verdict = <StNote tone="warn">{de.stats.verdictMixed}</StNote>;
  }
  return (
    <section className="space-y-2">
      <SectionHeader label={de.stats.secErrors} meta={de.stats.secErrorsMeta} />
      {tax.buckets.length > 0 ? (
        <>
          <div className="st-estack">
            {tax.buckets.map((b) => (
              <i key={b.key} style={{ width: `${Math.max(0, Math.min(100, b.pct))}%`, background: b.color }} />
            ))}
          </div>
          <div className="st-legend">
            {tax.buckets.map((b) => (
              <div key={b.key} className="st-legend-item">
                <span className="st-legend-sw" style={{ background: b.color }} />
                {ERROR_LABEL[b.key] ?? b.key}
                <b>{b.count}</b>
              </div>
            ))}
          </div>
        </>
      ) : null}
      {verdict}
      <a href="/control/issues" className="st-link mt-2 inline-flex min-h-12 items-center text-xs text-live hover:brightness-110">
        {de.stats.issuesLink}
      </a>
    </section>
  );
}

// ── Budget-Ledger (Provider-Limits, Engpass zuerst) ──────────────────────────
function ledgerFoot(r: LedgerEntry): string {
  if (!r.available) return r.unavailableReason ?? de.stats.budgetUnavailable;
  if (r.window) return r.window;
  return r.estimated ? de.stats.budgetNoLimit : de.stats.budgetNoWindow;
}

export function BudgetLedgerSection({ providers }: { providers: AccountUsageProvider[] }) {
  const rows = useMemo(() => budgetLedger(providers), [providers]);
  const lead = rows.find((r) => r.usedPercent != null && r.status !== "ok") ?? null;
  return (
    <section className="space-y-2">
      <SectionHeader label={de.stats.secBudget} meta={de.stats.secBudgetMeta} />
      {lead && lead.usedPercent != null ? (
        <StLead tone={lead.status === "crit" ? "crit" : "warn"}>
          {de.stats.budgetLeadPre}
          <b>{de.stats.budgetLead(lead.label, lead.window, Math.round(lead.usedPercent))}</b>
        </StLead>
      ) : null}
      {rows.length === 0 ? (
        <FleetEmptyState title={de.stats.budgetEmpty} desc={de.stats.budgetEmptyDesc} />
      ) : (
        <div className="st-panel space-y-2 p-3">
          {rows.map((r) => (
            <StLedgerRow
              key={r.provider}
              name={r.label}
              tag={r.estimated ? de.stats.budgetEstimated : undefined}
              figure={r.usedPercent == null ? "—" : `${Math.round(r.usedPercent)} %`}
              status={r.status}
              pct={r.usedPercent ?? null}
              footLeft={ledgerFoot(r)}
              footRight={r.staleLabel ?? (r.resetAt ? de.stats.budgetReset(fmtClockTime(r.resetAt)) : undefined)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function WorkerEfficiencyMap({ rows }: { rows: WorkerEfficiencyRow[] }) {
  const chartRows = rows.filter((row) => row.token_per_min != null && row.review_return_rate != null);
  const hasReviewSignal = rows.some((row) => row.reviewed_outputs != null && row.reviewed_outputs > 0);
  if (!hasReviewSignal) {
    return (
      <div className="st-we-map st-we-map-empty">
        <FleetEmptyState title="Keine Review-Daten im Fenster." desc="Die Rücklaufquote ist noch nicht bewertbar." />
      </div>
    );
  }
  if (chartRows.length === 0) {
    return (
      <div className="st-we-map st-we-map-empty">
        <FleetEmptyState title="Keine Laufzeitdaten im Fenster." desc="Token/min ist noch nicht bewertbar." />
      </div>
    );
  }

  const maxTokenPerMin = Math.max(1, ...chartRows.map((row) => row.token_per_min ?? 0));
  const maxReview = Math.max(0.05, ...chartRows.map((row) => row.review_return_rate ?? 0));
  const maxCost = Math.max(0.01, ...chartRows.map((row) => row.cost_per_task ?? 0));

  return (
    <div className="st-we-map" role="img" aria-label={de.stats.workerEfficiencyMap}>
      <span className="st-we-axis st-we-axis-x">Token/min</span>
      <span className="st-we-axis st-we-axis-y">Review zurück</span>
      <div className="st-we-grid" aria-hidden="true" />
      {chartRows.map((row) => {
        const x = 12 + Math.min(1, (row.token_per_min ?? 0) / maxTokenPerMin) * 76;
        const y = 12 + Math.min(1, (row.review_return_rate ?? 0) / maxReview) * 74;
        const size = 38 + Math.min(1, (row.cost_per_task ?? 0) / maxCost) * 28;
        const title = `${row.label}: ${fmtWorkerRate(row.token_per_min)} Token/min · ${fmtWorkerPct(row.review_return_rate)} Review zurück · ${fmtWorkerUsd(row.cost_per_task)} $/Task`;
        return (
          <span
            key={row.profile}
            className="st-we-bubble"
            style={{
              ...laneStyle(row.profile),
              "--we-x": `${x}%`,
              "--we-y": `${y}%`,
              "--we-size": `${size}px`,
            } as CSSProperties}
            title={title}
          >
            <b>{row.label}</b>
            <small>{fmtWorkerUsd(row.cost_per_task)}</small>
          </span>
        );
      })}
    </div>
  );
}

function WorkerMetricBar({ value, max }: { value: number | null; max: number }) {
  return (
    <span className="st-we-metric-bar" aria-hidden="true">
      <i style={{ width: metricShare(value, max) }} />
    </span>
  );
}

function WorkerComparison({ rows }: { rows: WorkerEfficiencyRow[] }) {
  const byProfile = new Map(rows.map((row) => [row.profile, row]));
  const coder = byProfile.get("coder") ?? null;
  const premium = byProfile.get("premium") ?? null;
  const metrics = [
    { key: "token_per_task", label: "Token/Task", format: fmtWorkerTokens },
    { key: "token_per_min", label: "Token/min", format: fmtWorkerRate },
    { key: "cost_per_task", label: "$/Task", format: fmtWorkerUsd },
    { key: "review_return_rate", label: "Review zurück", format: fmtWorkerPct },
  ] as const;

  return (
    <div className="st-we-compare">
      <div className="st-we-compare-head">
        <span />
        <b>coder</b>
        <b>premium</b>
      </div>
      {metrics.map((metric) => {
        const coderValue = coder?.[metric.key] ?? null;
        const premiumValue = premium?.[metric.key] ?? null;
        const max = Math.max(coderValue ?? 0, premiumValue ?? 0);
        return (
          <div key={metric.key} className="st-we-compare-row">
            <span>{metric.label}</span>
            <span>
              <b>{metric.format(coderValue)}</b>
              <WorkerMetricBar value={coderValue} max={max} />
            </span>
            <span>
              <b>{metric.format(premiumValue)}</b>
              <WorkerMetricBar value={premiumValue} max={max} />
            </span>
          </div>
        );
      })}
    </div>
  );
}

function WorkerLevers({ rows }: { rows: WorkerEfficiencyRow[] }) {
  const levers = workerEfficiencyLevers(rows);
  return (
    <div className="st-we-levers">
      {levers.length === 0 ? (
        <span className="st-we-lever st-we-lever-empty">Kein belastbarer Hebel aus den aktuellen Daten.</span>
      ) : (
        levers.map((lever) => (
          <span key={lever.key} className="st-we-lever">
            <b>{lever.label}</b>
            <small>{lever.detail}</small>
          </span>
        ))
      )}
    </div>
  );
}

function emptyWorkerEfficiencyRow(profile: string): WorkerEfficiencyRow {
  return {
    profile,
    label: profileLabel[profile] ?? profile,
    tasks: 0,
    runs: 0,
    tokens_total: 0,
    runtime_seconds: null,
    cost_effective_usd: null,
    token_per_task: null,
    token_per_min: null,
    cost_per_task: null,
    reviewed_outputs: null,
    review_request_changes: null,
    review_return_rate: null,
    data_quality: "partial",
  };
}

function WorkerBench({ rows }: { rows: WorkerEfficiencyRow[] }) {
  const byProfile = new Map(rows.map((row) => [row.profile, row]));
  const benchRows = WORKER_EFFICIENCY_BENCH_PROFILES.map((profile) => byProfile.get(profile) ?? emptyWorkerEfficiencyRow(profile));

  return (
    <div className="st-we-bench" role="table" aria-label="Worker Bench">
      <div className="st-we-bench-head" role="row">
        <span>Worker</span>
        <span>Tok/Task</span>
        <span>Tok/min</span>
        <span>$/Task</span>
        <span>Zurück</span>
      </div>
      {benchRows.map((row) => (
        <div key={row.profile} className="st-we-bench-row" role="row" style={laneStyle(row.profile)}>
          <span><LaneLabel profile={row.profile} label={row.label} /></span>
          <b>{fmtWorkerTokens(row.token_per_task)}</b>
          <b>{fmtWorkerRate(row.token_per_min)}</b>
          <b>{fmtWorkerUsd(row.cost_per_task)}</b>
          <b>{fmtWorkerPct(row.review_return_rate)}</b>
        </div>
      ))}
    </div>
  );
}

export function WorkerEfficiencySection({
  roots,
  profiles,
  minReviewSample = DEFAULT_MIN_REVIEW_SAMPLE,
  loading = false,
  error = null,
  stale = false,
}: {
  roots: WindowedRollupRoot[];
  profiles: ReliabilityProfile[];
  minReviewSample?: number;
  loading?: boolean;
  error?: string | null;
  stale?: boolean;
}) {
  const rows = useMemo(
    () => buildWorkerEfficiencyRows(roots, profiles, minReviewSample),
    [roots, profiles, minReviewSample],
  );
  const levers = useMemo(() => workerEfficiencyLevers(rows), [rows]);
  const leadLever = levers[0] ?? null;
  const empty = rows.length === 0;

  return (
    <section className="st-we" data-testid="worker-efficiency">
      <div className="st-we-hero st-panel">
        <div className="st-we-hero-top">
          <div className="st-we-chips" aria-label="Worker Efficiency Dimensionen">
            <span>Worker</span>
            <span>Kosten</span>
            <span>Review</span>
            <span>Ketten</span>
          </div>
          <span className="st-mast-meta">{stale ? "veraltet" : "7 Tage"}</span>
        </div>
        <div className="st-we-title">
          <div>
            <Eyebrow>{de.stats.workerEfficiencyMap}</Eyebrow>
            <h2>Worker vergleichen</h2>
            <p><b>Token</b><span>Geld</span><b>Rework</b></p>
          </div>
          {leadLever ? (
            <span className="st-we-lead-chip">
              größter Hebel: <b>{leadLever.label}</b>
            </span>
          ) : null}
        </div>
      </div>

      {loading && empty ? (
        <SkeletonCard rows={4} />
      ) : error && empty ? (
        <StNote tone="warn">Worker-Daten nicht frisch</StNote>
      ) : empty ? (
        <FleetEmptyState title="Noch keine Worker-Daten im Fenster." desc="Der Vergleich ist noch nicht belastbar." />
      ) : (
        <>
          <WorkerEfficiencyMap rows={rows} />
          <WorkerComparison rows={rows} />
          <WorkerLevers rows={rows} />
          <WorkerBench rows={rows} />
        </>
      )}
    </section>
  );
}

// ── Flotten-Effizienz (Durchsatz, Gate, Token-Burn je Lane) ──────────────────
export function LaneBurnSection({ costs }: { costs: CostProfileRow[] }) {
  const lanes = useMemo(() => laneBurn(costs), [costs]);
  return (
    <section className="space-y-3">
      <SectionHeader label="Lane-Burn" meta="Tokens · effektive Kosten · Läufe" />
      {lanes.length === 0 ? (
        <FleetEmptyState title={de.stats.burnEmpty} desc={de.stats.burnEmptyDesc} />
      ) : (
        <div className="st-panel space-y-1.5 p-2">
          {lanes.map((lane, index) => {
            const cost = formatEffectiveCost({
              cost_usd: lane.costUsd ?? 0,
              cost_effective_usd: lane.costEquivalent ?? 0,
              tokens: lane.tokens,
            });
            const partialPrefix = lane.costPartial && cost.text !== "—" ? "≥" : "";
            return (
              <StLeaderRow
                key={lane.profile}
                rank={index + 1}
                name={<LaneLabel profile={lane.profile} label={lane.label} />}
                score={fmtTokens(lane.tokens)}
                status="neutral"
                meta={
                  <>
                    {cost.estimated ? (
                      <span title={de.ketten.costEstimatedTooltip}>{partialPrefix}{cost.text}</span>
                    ) : (
                      `${partialPrefix}${cost.text}`
                    )}
                    {" · "}
                    {de.stats.leaderRuns(lane.runs)}
                  </>
                }
              />
            );
          })}
        </div>
      )}
    </section>
  );
}


export function EffizienzSection({
  profiles,
  costs,
  reviewValue,
  chainRate,
  queueWaitSeconds,
}: {
  profiles: ReliabilityProfile[];
  costs: CostProfileRow[];
  reviewValue: ReviewValueRow[];
  chainRate: number | null;
  queueWaitSeconds: number | null;
}) {
  const lanes = useMemo(() => laneBurn(costs), [costs]);
  const stages = useMemo(() => reviewValue.filter((r) => r.runs > 0), [reviewValue]);
  const gate = gateEffectiveness(profiles);
  return (
    <section className="space-y-4">
      <SectionHeader label={de.stats.secEffizienz} meta={de.stats.secEffizienzMeta} />
      <div className="grid grid-cols-3 gap-2">
        <KpiTile label={de.stats.effChain} value={pctText(chainRate)} suffix={chainRate == null ? undefined : "%"} dot="live" />
        <KpiTile label={de.stats.effQueue} value={queueWaitSeconds == null ? "—" : fmtDur(queueWaitSeconds)} />
        <KpiTile label={de.stats.effGate} value={pctText(gate)} suffix={gate == null ? undefined : "%"} />
      </div>

      <SectionHeader label={de.stats.secBurn} meta={de.stats.secBurnMeta} rule={false} />
      {lanes.length === 0 ? (
        <FleetEmptyState title={de.stats.burnEmpty} desc={de.stats.burnEmptyDesc} />
      ) : (
        <div className="st-panel space-y-1.5 p-2">
          {lanes.map((l, i) => {
            const cost = formatEffectiveCost({
              cost_usd: l.costUsd ?? 0,
              cost_effective_usd: l.costEquivalent ?? 0,
              tokens: l.tokens,
            });
            const partialPrefix = l.costPartial && cost.text !== "—" ? "≥" : "";
            return (
              <StLeaderRow
                key={l.profile}
                rank={i + 1}
                name={<LaneLabel profile={l.profile} label={l.label} />}
                score={fmtTokens(l.tokens)}
                status="neutral"
                meta={
                  <>
                    {cost.estimated ? (
                      <span title={de.ketten.costEstimatedTooltip}>{partialPrefix}{cost.text}</span>
                    ) : (
                      `${partialPrefix}${cost.text}`
                    )}
                    {" · "}
                    {de.stats.leaderRuns(l.runs)}
                  </>
                }
              />
            );
          })}
        </div>
      )}

      <SectionHeader label={de.stats.secReviewValue} meta={de.stats.secReviewValueMeta} rule={false} />
      {stages.length === 0 ? (
        <FleetEmptyState title={de.stats.reviewValueEmpty} desc={de.stats.reviewValueEmptyDesc} />
      ) : (
        <div className="st-panel space-y-1.5 p-2">
          {stages.map((r, i) => {
            const name = <LaneLabel profile={r.profile} label={profileLabel[r.profile] ?? r.profile} />;
            if (r.profile === "scout") {
              return (
                <StLeaderRow
                  key={r.profile}
                  rank={i + 1}
                  name={name}
                  score={r.read_items == null ? "—" : String(r.read_items)}
                  status="neutral"
                  meta={
                    <>
                      {de.stats.leaderRuns(r.runs)}
                      {" · "}
                      {r.tokens_per_read_item == null
                        ? "—"
                        : `${fmtTokens(r.tokens_per_read_item)} ${de.stats.reviewPerRead}`}
                    </>
                  }
                />
              );
            }
            const judged = r.approved + r.request_changes;
            const quote = judged > 0 ? Math.round((r.approved / judged) * 100) : null;
            const findings =
              r.findings_blocking == null || r.findings_observations == null
                ? null
                : r.findings_blocking + r.findings_observations;
            return (
              <StLeaderRow
                key={r.profile}
                rank={i + 1}
                name={name}
                score={findings == null ? "—" : String(findings)}
                status={findings != null && findings > 0 ? "warn" : "neutral"}
                meta={
                  <>
                    {de.stats.leaderRuns(r.runs)}
                    {" · "}
                    {de.stats.reviewQuote}{" "}
                    {quote == null ? "—" : `${quote} %`}
                    {" · "}
                    {r.tokens_per_finding == null
                      ? "—"
                      : `${fmtTokens(r.tokens_per_finding)} ${de.stats.reviewPerFinding}`}
                  </>
                }
              />
            );
          })}
        </div>
      )}
    </section>
  );
}

// ── Kosten-/Token-Trend + Drill-down ───────────────────────────────────────
function CostTrendSection({
  costs,
  loading = false,
  error = null,
  profiles,
  burn,
}: {
  costs: RunsCostsSeriesResponse | null;
  loading?: boolean;
  error?: string | null;
  profiles: CostProfileRow[];
  burn: SubscriptionTokenBurnResponse | null;
}) {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const series = costs?.series ?? [];
  const rowTokens = (row: RunsCostsSeriesResponse["series"][number]) =>
    row.total_tokens ?? null;
  const rowTokensPartial = (row: RunsCostsSeriesResponse["series"][number]) => {
    if (
      row.token_state === "empty"
      && row.token_known_runs === 0
      && row.token_total_runs === 0
    ) return false;
    return row.token_state !== "complete"
      || row.token_total_runs == null
      || row.token_known_runs == null
      || row.token_known_runs !== row.token_total_runs;
  };
  const rowEquivalentCostPartial = (row: RunsCostsSeriesResponse["series"][number]) =>
    row.equivalent_cost_state !== "complete"
    || row.equivalent_cost_total_runs == null
    || row.equivalent_cost_known_runs == null
    || row.equivalent_cost_known_runs !== row.equivalent_cost_total_runs;
  const maxTokens = Math.max(1, ...series.map((row) => rowTokens(row) ?? 0));
  const maxCost = Math.max(1, ...series.map((row) => row.api_equivalent_usd ?? row.cost_usd_equivalent ?? 0));
  const hasTrend = series.some((row) => row.runs > 0 || (rowTokens(row) ?? 0) > 0 || (row.api_equivalent_usd ?? row.cost_usd_equivalent ?? 0) > 0);
  const laneRows = burn ? subscriptionBurnBreakdown(burn, 8).topLanes : [];
  const hasDrilldown = profiles.length > 0 || laneRows.length > 0;

  return (
    <section className="space-y-2">
      <SectionHeader label={de.stats.secCostTrend} meta={de.stats.secCostTrendMeta} />
      {loading ? (
        <SkeletonCard rows={4} />
      ) : error ? (
        <StNote tone="warn">{de.stats.costTrendError} {error}</StNote>
      ) : !hasTrend ? (
        <FleetEmptyState title={de.stats.costTrendEmpty} desc={de.stats.costTrendEmptyDesc} />
      ) : (
        <>
          <div className="st-trend" data-testid="runs-costs-series-trend">
            {series.map((row) => {
              const tokens = rowTokens(row);
              const tokensPartial = rowTokensPartial(row);
              const cost = row.api_equivalent_usd ?? row.cost_usd_equivalent;
              const costPartial = rowEquivalentCostPartial(row);
              const costLabel = cost == null
                ? "Kosten unbekannt"
                : `${costPartial ? "mindestens " : ""}${usdText(cost)}`;
              const tokenCoverage = (row.token_total_runs ?? 0) > 0
                ? `, Coverage ${row.token_known_runs ?? 0}/${row.token_total_runs} Runs`
                : "";
              const costCoverage = (row.equivalent_cost_total_runs ?? 0) > 0
                ? `, Kosten-Coverage ${row.equivalent_cost_known_runs ?? 0}/${row.equivalent_cost_total_runs} Runs`
                : "";
              return (
                <div key={row.day} className="st-trend-row">
                  <span className="st-mono">{row.day.slice(5)}</span>
                  <div className="st-trend-bars" aria-label={`${row.day}: ${tokens == null ? "Tokens unbekannt" : `${tokensPartial ? "mindestens " : ""}${fmtTokens(tokens)} tokens${tokenCoverage}`}, ${costLabel}${costCoverage}, ${row.runs} runs`}>
                    {tokens != null && tokens > 0 ? <span className="st-trend-token" style={{ "--st-w": `${Math.max(3, Math.round((tokens / maxTokens) * 100))}%` } as CSSProperties} /> : null}
                    {cost != null && cost > 0 ? <span className="st-trend-money" style={{ "--st-w": `${Math.max(3, Math.round((cost / maxCost) * 100))}%` } as CSSProperties} /> : null}
                  </div>
                  <span className="st-mono">{tokens == null ? "—" : `${tokensPartial ? "≥" : ""}${fmtTokens(tokens)}`}</span>
                  <span className="st-mono">{cost == null ? "—" : `${costPartial ? "≥" : ""}${usdText(cost)}`}</span>
                  <span className="st-mono">{row.runs}×</span>
                </div>
              );
            })}
          </div>
          <p className="st-note"><b>{de.stats.costTrendSource}:</b> {de.stats.costTrendSourceCopy}</p>
          <button type="button" className="st-linkbutton min-h-11" onClick={() => setDrawerOpen(true)}>
            {de.stats.modelLaneDrilldown}<ChevronRight className="h-3.5 w-3.5" />
          </button>
        </>
      )}
      {drawerOpen ? (
        <DrawerShell title={de.stats.modelLaneDrilldown} ariaLabel={de.stats.modelLaneDrilldown} onClose={() => setDrawerOpen(false)}>
          {!hasDrilldown ? (
            <FleetEmptyState title={de.stats.modelDrilldownEmpty} desc={de.stats.modelDrilldownEmptyDesc} />
          ) : (
            /* Host-Wrapper traegt den Query-Container: ein Element ist per Spec
               nie sein eigener @container (css-contain-3) — Reviewer-P1 W3-3. */
            <div className="st-drilldown-host">
            <div className="st-drilldown-grid">
              <div>
                <Eyebrow>Profile / Modelle</Eyebrow>
                {profiles.map((row) => (
                  <div key={`${row.profile}:${row.subscription ?? "api"}`} className="st-drilldown-row">
                    <span>{profileLabel[row.profile] ?? row.profile} · {row.subscription ?? "api"}</span>
                    <b>{row.total_tokens != null
                      ? fmtTokens(row.total_tokens)
                      : row.input_tokens != null && row.output_tokens != null
                        ? `${row.cached_tokens == null ? "≥" : ""}${fmtTokens(row.input_tokens + row.output_tokens + (row.cached_tokens ?? 0))}`
                        : "—"}</b>
                    <ProfileCostValue row={row} />
                  </div>
                ))}
              </div>
              <div>
                <Eyebrow>Lanes</Eyebrow>
                {laneRows.map((row) => (
                  <div key={`${row.subscription}:${row.profile}`} className="st-drilldown-row">
                    <span>{row.subscription} · {profileLabel[row.profile] ?? row.profile}</span>
                    <b>{fmtTokens(row.total_tokens)}</b>
                    <span>{row.runs} Runs</span>
                  </div>
                ))}
              </div>
            </div>
            </div>
          )}
        </DrawerShell>
      ) : null}
    </section>
  );
}

export function ProfileCostValue({ row }: { row: CostProfileRow }) {
  const fact = profileCostFact(row);
  const tokens = (row.input_tokens ?? 0) + (row.output_tokens ?? 0);
  const cost = formatEffectiveCost({
    cost_usd: row.cost_usd ?? 0,
    cost_effective_usd: fact.effectiveUsd ?? 0,
    tokens,
  });
  const partialPrefix = fact.partial && cost.text !== "—" ? "≥" : "";
  return (
    <span title={cost.estimated ? de.ketten.costEstimatedTooltip : undefined}>
      {partialPrefix}{cost.text}
    </span>
  );
}

// ── Subscription-Burn (Abo-Token-Realität) ─────────────────────────────────
export function SubscriptionBurnSection({
  burn,
  loading = false,
  error = null,
}: {
  burn: SubscriptionTokenBurnResponse | null;
  loading?: boolean;
  error?: string | null;
}) {
  const detail = useMemo(() => subscriptionBurnBreakdown(burn), [burn]);
  const hasBurn = detail.totals.total_tokens > 0;
  return (
    <section className="space-y-2">
      <SectionHeader label={de.stats.secSubscriptionBurn} meta={de.stats.secSubscriptionBurnMeta} />
      {loading ? (
        <SkeletonCard rows={4} />
      ) : error ? (
        <StNote tone="warn">{error}</StNote>
      ) : !hasBurn ? (
        <FleetEmptyState title={de.stats.subscriptionBurnEmpty} desc={de.stats.subscriptionBurnEmptyDesc} />
      ) : (
        <div className="st-panel st-subburn space-y-3 p-3" data-testid="subscription-burn-breakdown">
          <div className="st-subburn-hero">
            <Eyebrow>{de.stats.subscriptionBurnWindow(burn?.days ?? 7)}</Eyebrow>
            <strong className="st-subburn-disp">{fmtTokens(detail.totals.total_tokens)}</strong>
            <span className="st-note">{de.stats.subscriptionBurnHero(detail.totals.runs, detail.subscriptionCount)}</span>
          </div>
          <div className="st-subburn-grid">
            <div>
              <Eyebrow>{de.stats.subscriptionBurnTop}</Eyebrow>
              {detail.topLanes.map((row) => (
                <div key={`${row.subscription}:${row.profile}`} className="st-subburn-row" style={laneStyle(row.profile)}>
                  <span aria-label={`${row.profile} · ${row.subscription}`}><LaneLabel profile={row.profile} /> · {row.subscription}</span>
                  <i style={{ "--st-share": `${Math.max(2, Math.round(row.share * 100))}%` } as CSSProperties} />
                  <b className="st-mono">{fmtTokens(row.total_tokens)}</b>
                  <small>{Math.round(row.share * 100)}%</small>
                </div>
              ))}
            </div>
            <div>
              <Eyebrow>{de.stats.subscriptionBurnClasses}</Eyebrow>
              {detail.classes.map((row) => (
                <div key={`${row.subscription}:${row.value_class}`} className="st-subburn-row">
                  <span>{row.value_class} · {row.subscription}</span>
                  <i />
                  <b className="st-mono">{fmtTokens(row.total_tokens)}</b>
                  <small className="st-mono">{Math.round(row.share * 100)} %</small>
                </div>
              ))}
            </div>
          </div>
          <div className="st-subburn-flags" aria-label={de.stats.subscriptionBurnFlags}>
            {detail.flags.map((flag) => (
              <span key={`${flag.kind}:${flag.title}`} className={flag.kind === "anti" ? "st-flag st-flag-anti" : "st-flag"}>
                <b>{flag.kind === "anti" ? de.stats.subscriptionBurnAnti : de.stats.subscriptionBurnTopFlag}</b>
                {flag.title} · {flag.detail}
              </span>
            ))}
          </div>
          {detail.trend.length > 0 && (
            <div data-testid="subscription-burn-trend">
              <Eyebrow>{de.stats.subscriptionBurnTrend}</Eyebrow>
              {detail.trend.map((row) => (
                <div key={row.date} className="st-subburn-trend-row">
                  <span className="st-mono text-ink-3" style={{ fontSize: "11px" }}>{row.date}</span>
                  <div className="st-subburn-trend-bar">
                    <i style={{ width: `${Math.round(row.share * 100)}%` }} />
                  </div>
                  <b className="st-mono" style={{ fontSize: "12px" }}>{fmtTokens(row.total_tokens)}</b>
                  <small className="st-mono text-ink-3">{Math.round(row.share * 100)} %</small>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// ── Kosten pro Kette — Helper functions shared by MotherLedgerSection ────────

function fmtUsd(value: number | null | undefined): string {
  return value == null ? "—" : `$${value.toFixed(2)}`;
}

function fmtRuntime(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const rem = seconds % 60;
  if (mins < 60) return rem ? `${mins}m ${rem}s` : `${mins}m`;
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  return remMins ? `${hours}h ${remMins}m` : `${hours}h`;
}

function fmtMaybeUsd(value: number | null | undefined): string {
  return fmtUsd(value ?? null);
}

function ledgerDetailTitle(item: {
  cost_usd?: number | null;
  cost_usd_coverage?: number | null;
  cost_usd_equivalent?: number | null;
  cost_usd_equivalent_coverage?: number | null;
  cost_effective_usd?: number | null;
  provider?: string | null;
  providers?: string[];
  model?: string | null;
  billing_mode?: string | null;
  runtime_seconds?: number | null;
}): string {
  const provider = item.provider ?? item.providers?.join(", ") ?? null;
  const model = item.model ?? null;
  const coveredCost = (value: number | null | undefined, coverage: number | null | undefined) =>
    value == null ? "—" : `${coverage === 1 ? "" : "≥"}${fmtMaybeUsd(value)}`;
  return [
    `${de.stats.motherLedgerColAbo}: ${coveredCost(item.cost_usd_equivalent, item.cost_usd_equivalent_coverage)} ${de.stats.motherLedgerAboMarker}`,
    `${de.stats.motherLedgerColReal}: ${coveredCost(item.cost_usd, item.cost_usd_coverage)}`,
    `Provider/Model: ${provider ?? "—"}${model ? ` · ${model}` : ""}`,
    `billing_mode: ${item.billing_mode ?? "—"}`,
    `Laufzeit: ${fmtRuntime(item.runtime_seconds)}`,
    `${de.stats.motherLedgerNeuralwatt}`,
  ].join(" · ");
}

function useMediaQuery(query: string): boolean {
  const readMatch = () => {
    if (typeof globalThis.matchMedia !== "function") return false;
    return globalThis.matchMedia(query).matches;
  };
  const [matches, setMatches] = useState(readMatch);

  useEffect(() => {
    if (typeof globalThis.matchMedia !== "function") return undefined;
    const media = globalThis.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, [query]);

  return matches;
}

export function LedgerWorkerRunners({ root, worker }: { root: WindowedRollupRoot; worker: WindowedRollupWorker }) {
  const runners = root.runners.filter((runner) => runner.profile === worker.profile);
  if (runners.length === 0) {
    return <div className="st-ledger-runners st-mono">{de.stats.motherLedgerNoRunners}</div>;
  }
  return (
    <div className="st-ledger-runners">
      {runners.map((runner) => (
        <div key={runner.id} className="st-ledger-runner" title={ledgerDetailTitle(runner)}>
          <span className="st-mono">#{runner.id}</span>
          <span>{runner.provider ?? "Provider n/a"}{runner.model ? ` · ${runner.model}` : ""}</span>
          <b className="st-mono">{runner.input_tokens == null || runner.output_tokens == null ? "—" : `≥${fmtTokens(runner.input_tokens + runner.output_tokens)}`}</b>
          <b className="st-mono st-ledger-abo">{runner.cost_usd_equivalent == null ? "—" : `${runner.cost_usd_equivalent_coverage === 1 ? "" : "≥"}${fmtMaybeUsd(runner.cost_usd_equivalent)}`} <small>{de.stats.motherLedgerAboMarker}</small></b>
          <b className={cn("st-mono st-ledger-real", runner.cost_usd != null && runner.cost_usd > 0 && "is-positive")}>{runner.cost_usd == null ? "—" : `${runner.cost_usd_coverage === 1 ? "" : "≥"}${fmtMaybeUsd(runner.cost_usd)}`}</b>
          <small>{runner.billing_mode ?? "—"} · {fmtRuntime(runner.runtime_seconds)} · {de.stats.motherLedgerNeuralwatt}</small>
        </div>
      ))}
    </div>
  );
}

export function MotherLedgerSection() {
  const [windowHours, setWindowHours] = useState<24 | 168>(168);
  const [sortKey, setSortKey] = useState<MotherLedgerSortKey>("usd");
  const [openRootId, setOpenRootId] = useState<string | null>(null);
  const [openWorkerKey, setOpenWorkerKey] = useState<string | null>(null);
  const isMobileLedger = useMediaQuery("(max-width: 760px)");
  const rollup = useHermesWindowedRollup({ hours: windowHours, limit: 20 });
  const roots = useMemo(
    () => sortedLedgerRoots(rollup.data?.roots ?? [], sortKey),
    [rollup.data, sortKey],
  );
  const totalRoots = rollup.data?.totals?.root_count
    ?? rollup.data?.completed_roots
    ?? roots.length;
  const windowCost = useMemo(
    () => rollup.data?.totals
      ? {
          echtUsd: rollup.data.totals.cost_usd,
          aboUsd: rollup.data.totals.cost_usd_equivalent,
        }
      : windowCostSummary(roots),
    [rollup.data, roots],
  );
  const knownAboRoots = rollup.data?.totals?.cost_usd_equivalent_known_roots
    ?? roots.filter(
      (root) =>
        root.cost_usd_equivalent != null
        && (root.cost_usd_equivalent_coverage ?? 0) >= 1,
    ).length;
  const applicableAboRoots = rollup.data?.totals?.cost_usd_equivalent_applicable_roots
    ?? (rollup.data?.totals?.cost_usd_equivalent_state === "not_applicable" ? 0 : totalRoots);
  const knownRealRoots = rollup.data?.totals?.cost_usd_known_roots
    ?? roots.filter(
      (root) =>
        root.cost_usd != null
        && (root.cost_usd_coverage ?? 0) >= 1,
    ).length;
  const windowMoney = (value: number, known: number, denominator: number) => {
    if (denominator === 0) return "n/a";
    if (known === 0) return "—";
    return `${known < denominator ? "≥" : ""}${fmtUsd(value)}`;
  };
  const topAbo = roots.reduce((top, root) => Math.max(top, chainCost(root).abo ?? 0), 0);
  const showStaleNotice = Boolean((rollup.error || rollup.isStale) && rollup.data);
  const windowLabel = windowHours === 168 ? "7T" : "24Std";
  const aboComplete = knownAboRoots === applicableAboRoots;
  const realComplete = knownRealRoots === totalRoots;
  const costsComplete = aboComplete && realComplete;
  const ratioText = applicableAboRoots === 0 && realComplete
    ? `Abo-Gegenwert nicht anwendbar · Echt ${fmtUsd(windowCost.echtUsd)} (${knownRealRoots}/${totalRoots})`
    : costsComplete && windowCost.echtUsd > 0
    ? de.stats.motherLedgerHeroRatio
        .replace("{real}", fmtUsd(windowCost.echtUsd))
        .replace("{abo}", fmtUsd(windowCost.aboUsd))
        .replace("{ratio}", Math.round(windowCost.aboUsd / windowCost.echtUsd).toLocaleString("de-DE"))
    : costsComplete
      ? de.stats.motherLedgerHeroRatioNoReal.replace("{abo}", fmtUsd(windowCost.aboUsd))
      : `Kosten-Untergrenzen · Abo ${windowMoney(windowCost.aboUsd, knownAboRoots, applicableAboRoots)} (${knownAboRoots}/${applicableAboRoots} anwendbar) · Echt ${windowMoney(windowCost.echtUsd, knownRealRoots, totalRoots)} (${knownRealRoots}/${totalRoots})`;
  const sampleText = roots.length < totalRoots ? ` · Auswahl ${roots.length}/${totalRoots} Ketten` : "";
  const metaText = `${windowLabel}${sampleText}${showStaleNotice ? ` · ${de.stats.motherLedgerStaleNotice}` : ""}`;
  const toggleRoot = (rootId: string) => {
    setOpenRootId((prev) => (prev === rootId ? null : rootId));
    setOpenWorkerKey(null);
  };
  const toggleWorker = (rootId: string, profile: string) => {
    const key = `${rootId}:${profile}`;
    setOpenWorkerKey((prev) => (prev === key ? null : key));
  };

  return (
    <section className="space-y-2">
      <SectionHeader label={de.stats.motherLedgerTitle} meta={metaText} />
      <div className="st-ledger-controls" aria-label="MotherLedger Controls">
        <div className="st-chipset" aria-label="Fenster">
          <button type="button" className={cn("inline-flex min-h-12 items-center justify-center", windowHours === 168 && "is-active")} onClick={() => setWindowHours(168)}>7T</button>
          <button type="button" className={cn("inline-flex min-h-12 items-center justify-center", windowHours === 24 && "is-active")} onClick={() => setWindowHours(24)}>24Std</button>
        </div>
        <div className="st-chipset" aria-label="Sortierung">
          <button type="button" className={cn("inline-flex min-h-12 items-center justify-center", sortKey === "usd" && "is-active")} onClick={() => setSortKey("usd")}>{de.stats.motherLedgerSortAbo}</button>
          <button type="button" className={cn("inline-flex min-h-12 items-center justify-center", sortKey === "tokens" && "is-active")} onClick={() => setSortKey("tokens")}>Tokens</button>
          <button type="button" className={cn("inline-flex min-h-12 items-center justify-center", sortKey === "runs" && "is-active")} onClick={() => setSortKey("runs")}>Runs</button>
        </div>
      </div>
      {rollup.loading && !rollup.data ? (
        <SkeletonCard rows={5} />
      ) : rollup.error && !rollup.data ? (
        <StNote tone="warn">{de.ketten.chainCostsLoadError}</StNote>
      ) : roots.length === 0 ? (
        <FleetEmptyState title={de.ketten.chainCostsEmpty} desc={de.ketten.chainCostsEmptyDesc} />
      ) : (
        <div className="st-ledger" data-ledger-viewport={isMobileLedger ? "mobile" : "desktop"}>
          <div className="st-ledger-hero">
            <div className="st-ledger-hero-primary">
              <span>{de.stats.motherLedgerHeroAbo.replace("{window}", windowLabel)}</span>
              <b className="st-mono">{windowMoney(windowCost.aboUsd, knownAboRoots, applicableAboRoots)} <small>{de.stats.motherLedgerAboMarker}</small></b>
              <small>{de.stats.motherLedgerHeroAboSub}</small>
            </div>
            <div className="st-ledger-hero-real">
              <span>{de.stats.motherLedgerHeroReal.replace("{window}", windowLabel)}</span>
              <b className="st-mono">{windowMoney(windowCost.echtUsd, knownRealRoots, totalRoots)}</b>
            </div>
          </div>
          <p className="st-note">{ratioText}</p>
          {showStaleNotice ? (
            <div className="st-note flex items-center gap-1.5 text-status-warn" role="status" title={rollup.error ?? undefined}>
              <TriangleAlert aria-hidden className="h-3 w-3 shrink-0" />
              {de.stats.motherLedgerStaleNotice}
            </div>
          ) : null}
          <div className="st-ledger-chains" aria-label="Kettenkosten">
            {roots.map((root) => {
              const openRoot = openRootId === root.id;
              const money = chainCost(root);
              const aboText = money.abo == null
                ? "—"
                : `${root.cost_usd_equivalent_coverage === 1 ? "" : "≥"}${fmtMaybeUsd(money.abo)}`;
              const realText = money.echt == null
                ? "—"
                : `${root.cost_usd_coverage === 1 ? "" : "≥"}${fmtMaybeUsd(money.echt)}`;
              const share = chainShare(root, topAbo);
              const providerText = root.providers.length ? root.providers.join(", ") : "—";
              return (
                <article key={root.id} className="st-ledger-chain" title={ledgerDetailTitle(root)}>
                  <button type="button" className="st-ledger-chain-head" onClick={() => toggleRoot(root.id)} aria-expanded={openRoot}>
                    <span className="st-ledger-chain-main min-w-0">
                      <b>{root.title ?? root.id}</b>
                      <small className="st-mono">{root.id} · {fmtRuntime(root.runtime_seconds)} · {rootRuns(root)} Runs · {providerText}</small>
                    </span>
                    <span className="st-ledger-chain-money">
                      <span className="st-ledger-abo"><b className="st-mono">{aboText}</b><small>{de.stats.motherLedgerAboMarker}</small></span>
                      <span className={money.echt != null && money.echt > 0 ? "st-ledger-real is-positive" : "st-ledger-real"}>{de.stats.motherLedgerRealShort} {realText}</span>
                    </span>
                  </button>
                  <div className="st-ledger-meter" aria-hidden="true"><span style={{ width: `${Math.round(share * 100)}%` }} /></div>
                  {openRoot ? (
                    <div className="st-ledger-workers" role="table" aria-label="Worker-Aufschlüsselung">
                      <div className="st-ledger-workers-head" role="row">
                        <span>{de.stats.motherLedgerColWorker}</span><span>{de.stats.motherLedgerColTokens}</span><span>{de.stats.motherLedgerColAbo}</span><span>{de.stats.motherLedgerColReal}</span>
                      </div>
                      {root.workers.map((worker) => {
                        const key = `${root.id}:${worker.profile}`;
                        const openWorker = openWorkerKey === key;
                        const cost = workerCost(worker);
                        const tokenDisplay = workerTokenDisplay(worker);
                        const workerRealText = cost.echt == null
                          ? "—"
                          : `${worker.cost_usd_coverage === 1 ? "" : "≥"}${fmtMaybeUsd(cost.echt)}`;
                        const workerAboText = cost.abo == null
                          ? "—"
                          : `${worker.cost_usd_equivalent_coverage === 1 ? "" : "≥"}${fmtMaybeUsd(cost.abo)}`;
                        return (
                          <div key={key} className="st-ledger-worker-block">
                            <button type="button" className="st-ledger-worker-row" onClick={() => toggleWorker(root.id, worker.profile)} aria-expanded={openWorker} title={ledgerDetailTitle(worker)}>
                              <span className="st-ledger-worker-label"><LaneLabel profile={worker.profile} /><small>{worker.model ?? worker.provider ?? "—"}</small></span>
                              <span className="st-mono" data-label={de.stats.motherLedgerColTokens}>{tokenDisplay.tokens == null ? "—" : `${tokenDisplay.partial ? "≥" : ""}${fmtTokens(tokenDisplay.tokens)}`}</span>
                              <span className="st-mono st-ledger-abo" data-label={de.stats.motherLedgerAboMobile}>{workerAboText}</span>
                              <span className={cost.echt != null && cost.echt > 0 ? "st-mono st-ledger-real is-positive" : "st-mono st-ledger-real"} data-label={de.stats.motherLedgerRealMobile}>{workerRealText}</span>
                            </button>
                            {openWorker ? <LedgerWorkerRunners root={root} worker={worker} /> : null}
                          </div>
                        );
                      })}
                    </div>
                  ) : null}
                </article>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}

const ORIGIN_COLORS = [
  "var(--color-data-1)",
  "var(--color-data-4)",
  "var(--color-data-5)",
  "var(--color-data-3)",
  "var(--color-data-7)",
  "var(--color-live)",
  "var(--color-status-warn)",
];

const ratio = (observed: number, denominator: number) =>
  denominator > 0 ? observed / denominator : null;

const metricValue = (
  data: ObservabilityStatsResponse,
  name: string,
) => data.langfuse.metrics[name]?.value ?? null;

const compactUsd = (value: number | string | null) => {
  if (value == null) return "—";
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed)
    ? parsed.toLocaleString("de-DE", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 2,
    })
    : "—";
};

function CoverageRow({
  label,
  observed,
  denominator,
}: {
  label: string;
  observed: number;
  denominator: number;
}) {
  const value = ratio(observed, denominator);
  return (
    <div className="grid grid-cols-[minmax(92px,1fr)_minmax(100px,2fr)_84px] items-center gap-2 border-b border-line-soft py-2.5 last:border-0">
      <span className="text-[11px] font-semibold text-ink-2">{label}</span>
      <span className="h-1.5 overflow-hidden rounded-full bg-surface-3">
        <i
          className={cn(
            "block h-full rounded-full",
            value != null && value >= 0.8 ? "bg-status-ok" : "bg-status-warn",
          )}
          style={{ width: `${Math.max(value == null ? 0 : 2, (value ?? 0) * 100)}%` }}
        />
      </span>
      <b className="text-right font-data text-[10px]">
        {observed.toLocaleString("de-DE")} / {denominator.toLocaleString("de-DE")}
      </b>
    </div>
  );
}

function ImportActivityChart({
  rows,
}: {
  rows: ObservabilityStatsResponse["usage"]["daily_imports"];
}) {
  if (!rows.length) {
    return <FleetEmptyState title="Keine Importe" desc="Im gewählten Fenster wurden keine Usage Facts importiert." />;
  }
  const peak = Math.max(1, ...rows.map((row) => row.fact_rows));
  const scale = (value: number) =>
    Math.max(2, (Math.log1p(value) / Math.log1p(peak)) * 100);
  return (
    <div className="mt-4">
      <div className="flex h-44 items-end gap-1.5 border-b border-line px-1 sm:gap-3" aria-label="Importierte Usage Facts je Tag, logarithmisch skaliert">
        {rows.map((row) => (
          <div key={row.date} className="relative flex h-full min-w-0 flex-1 items-end pt-8">
            <i
              className="block w-full rounded-t bg-gradient-to-b from-data-1 to-data-1/20"
              style={{ height: `${scale(row.fact_rows)}%` }}
              title={`${row.fact_rows.toLocaleString("de-DE")} Usage Facts`}
            />
            <b className="absolute left-1/2 top-1 -translate-x-1/2 text-[8px] font-data text-ink-2 sm:text-[10px]">
              {fmtTokens(row.fact_rows)}
            </b>
            <span className="absolute -bottom-4 left-1/2 -translate-x-1/2 text-[8px] font-semibold text-ink-3">
              {row.date.slice(8)}
            </span>
          </div>
        ))}
      </div>
      <div className="mt-5 flex justify-between gap-3 text-[9px] font-semibold text-ink-3">
        <span>Usage Facts je Importtag</span>
        <span>logarithmisch · kein Worker-Durchsatz</span>
      </div>
    </div>
  );
}

function OriginDistribution({
  rows,
  total,
}: {
  rows: ObservabilityUsageOrigin[];
  total: number;
}) {
  const visible = rows.slice(0, 6);
  const visibleRows = visible.length < rows.length
    ? [
      ...visible,
      {
        name: "weitere",
        fact_rows: rows.slice(6).reduce((sum, row) => sum + row.fact_rows, 0),
        context_input_tokens: rows.slice(6).reduce((sum, row) => sum + row.context_input_tokens, 0),
        output_tokens: rows.slice(6).reduce((sum, row) => sum + row.output_tokens, 0),
        cache_read_tokens: rows.slice(6).reduce((sum, row) => sum + row.cache_read_tokens, 0),
        cache_write_tokens: rows.slice(6).reduce((sum, row) => sum + row.cache_write_tokens, 0),
        model_count: rows.slice(6).reduce((sum, row) => sum + row.model_count, 0),
      },
    ]
    : visible;
  return (
    <>
      <div className="mt-4 flex h-4 overflow-hidden rounded-full bg-surface-3" aria-label="Herkunftsanteile">
        {visibleRows.map((row, index) => (
          <i
            key={row.name}
            style={{
              width: `${total > 0 ? (row.fact_rows / total) * 100 : 0}%`,
              backgroundColor: ORIGIN_COLORS[index % ORIGIN_COLORS.length],
            }}
            title={`${row.name}: ${total > 0 ? ((row.fact_rows / total) * 100).toFixed(1) : 0} %`}
          />
        ))}
      </div>
      <div className="mt-4">
        {visibleRows.map((row, index) => (
          <div key={row.name} className="grid min-h-11 grid-cols-[12px_minmax(100px,1fr)_72px_76px] items-center gap-2 border-b border-line-soft text-[11px] last:border-0">
            <i
              className="size-2.5 rounded-full"
              style={{ backgroundColor: ORIGIN_COLORS[index % ORIGIN_COLORS.length] }}
            />
            <span className="min-w-0 break-words font-semibold text-ink-2">{row.name}</span>
            <b className="text-right font-data">
              {total > 0
                ? `${((row.fact_rows / total) * 100).toLocaleString("de-DE", { maximumFractionDigits: 1 })} %`
                : "—"}
            </b>
            <small className="text-right text-ink-3">{fmtTokens(row.fact_rows)} Facts</small>
          </div>
        ))}
      </div>
    </>
  );
}

function LangfuseSignals({ data }: { data: ObservabilityStatsResponse }) {
  if (!data.langfuse.available) {
    return (
      <div className="mt-4 rounded-xl border border-status-warn/30 bg-status-warn/5 p-4">
        <b className="text-sm text-status-warn">Noch keine Langfuse-Signale</b>
        <p className="mt-2 text-xs text-ink-2">
          Usage Facts bleiben sichtbar. Für Modellkosten, Generation-Latenz und TTFT fehlen aktuell Server-Zugang oder laufende Worker-Traces.
        </p>
      </div>
    );
  }
  const models = data.langfuse.models.slice(0, 6);
  const partial = data.langfuse.state === "partial";
  const lowerBound = partial ? "≥" : "";
  const generationCalls = metricValue(data, "generation_calls");
  return (
    <div className="mt-3">
      {partial ? (
        <p className="mb-3 rounded-lg border border-status-warn/30 bg-status-warn/5 px-3 py-2 text-[10px] font-semibold text-status-warn">
          Ausschnitt · Summen sind Untergrenzen, p50 aus Stichprobe
        </p>
      ) : null}
      {models.map((row) => (
        <div key={row.name} className="grid min-h-12 grid-cols-[minmax(110px,1fr)_56px_72px_72px] items-center gap-2 border-b border-line-soft text-[10px] last:border-0">
          <span className="min-w-0 break-words font-semibold text-ink-2">{row.name}</span>
          <b className="text-right font-data">{lowerBound}{row.calls.toLocaleString("de-DE")}</b>
          <span className="text-right text-ink-3">{fmtMillis(row.latency_p50_ms)}</span>
          <span className="text-right text-ink-3">{fmtMillis(row.ttft_p50_ms)}</span>
        </div>
      ))}
      <div className="mt-3 grid grid-cols-2 gap-2 text-[10px] font-semibold text-ink-2">
        <span className="rounded-lg bg-surface-2 p-2">Generationen <b className="float-right font-data">{generationCalls == null ? "—" : `${lowerBound}${fmtTokens(generationCalls)}`}</b></span>
        <span className="rounded-lg bg-surface-2 p-2">Kosten <b className="float-right font-data">{lowerBound}{compactUsd(metricValue(data, "known_cost_usd"))}</b></span>
      </div>
    </div>
  );
}

export function ObservabilityDeck({
  data,
  loading,
  error,
}: {
  data: ObservabilityStatsResponse | null;
  loading: boolean;
  error: string | null;
}) {
  if (loading && !data) return <SkeletonCard rows={6} />;
  if (!data) {
    return (
      <section className="hc-surface-card p-4">
        <FleetEmptyState title="Observability nicht verfügbar" desc={error ?? "Der Hermes-Read-Model-Endpunkt liefert derzeit keine Daten."} />
      </section>
    );
  }
  const summary = data.usage.summary;
  // Faellt der Ledger-Lesepfad aus, liefert das Backend `summary: {}` und das
  // Zod-Schema fuellt jedes fehlende Token-Feld mit 0 auf. "0 Context Tokens"
  // liest sich dann wie "keine Worker-Aktivitaet" statt wie "nicht lesbar".
  // Canon 2026-07-27 (Kosten-SSOT), Regel 3: Unbekanntes bleibt unbekannt und
  // wird nie als 0 gezeigt.
  const usageKnown = data.usage.available;
  const coverage = data.usage.coverage;
  const linkCoverage = ratio(coverage.exact_task_run_links, coverage.fact_rows);
  const dataGap = !data.langfuse.available
    || coverage.exact_task_run_links === 0
    || summary.price_status !== "complete";
  const priceStatus = summary.price_status === "complete"
    ? "vollständig"
    : summary.price_status === "partial"
      ? "teilweise"
      : "unbekannt";
  const checkedAt = new Date(data.checked_at * 1000).toLocaleString("de-DE");
  return (
    <section className="space-y-4">
      <header className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <p className="hc-type-label hc-dim">VOLUMEN · GESCHWINDIGKEIT · KOSTEN · {data.window_days} TAGE</p>
          <h1 className="hc-type-display-tight mt-2">Observability</h1>
          <p className="hc-dim mt-2 text-sm">Worker-Daten in Hermes. Langfuse bleibt unsichtbare Datenquelle.</p>
        </div>
        <div className={cn(
          "inline-flex min-h-10 w-fit items-center gap-2 rounded-full border px-3 text-[11px] font-semibold",
          data.langfuse.state === "fresh"
            ? "border-status-ok/40 text-status-ok"
            : "border-status-warn/40 text-status-warn",
        )}>
          <span className="size-2 rounded-full bg-current" aria-hidden />
          {data.langfuse.state === "fresh" ? "Langfuse fresh" : `Langfuse ${data.langfuse.state}`} · {checkedAt}
        </div>
      </header>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <KpiTile
          label="Usage Facts"
          value={usageKnown ? fmtTokens(summary.fact_rows) : "—"}
          delta={`${data.window_days} Tage · ${data.usage.state}`}
          dot="live"
        />
        <KpiTile
          label="Context Tokens"
          value={usageKnown ? fmtTokens(summary.context_input_tokens) : "—"}
          delta={usageKnown
            ? `${fmtTokens(summary.cache_read_tokens)} Cache Read`
            : `unbekannt · ${data.usage.reason ?? "Ledger nicht lesbar"}`}
        />
        <KpiTile
          label="Run Duration p50"
          value={data.run_duration.p50_seconds == null ? "—" : fmtDur(data.run_duration.p50_seconds)}
          delta={`p95 ${data.run_duration.p95_seconds == null ? "—" : fmtDur(data.run_duration.p95_seconds)} · n ${data.run_duration.count.toLocaleString("de-DE")}`}
        />
        <KpiTile
          label="Exact Links"
          value={linkCoverage == null ? "—" : `${(linkCoverage * 100).toLocaleString("de-DE", { maximumFractionDigits: 1 })} %`}
          delta={`${coverage.exact_task_run_links.toLocaleString("de-DE")} / ${coverage.fact_rows.toLocaleString("de-DE")} Facts`}
          deltaTone={linkCoverage != null && linkCoverage < 0.8 ? "down" : "neutral"}
        />
      </div>

      {dataGap ? (
        <div className="flex flex-col gap-1 rounded-xl border border-status-warn/30 bg-status-warn/5 px-4 py-3 text-xs sm:flex-row sm:items-center sm:justify-between" role="status">
          <b className="text-status-warn">Datenlücke</b>
          <span className="text-ink-2">
            {coverage.exact_task_run_links === 0 ? "Run↔Trace-Korrelation fehlt" : "Korrelation teilweise"}
            {" · "}
            {data.langfuse.available ? "Langfuse verbunden" : "Langfuse nicht erreichbar"}
            {" · "}
            Metered-Preise {priceStatus}
          </span>
        </div>
      ) : null}

      <div className="grid gap-4 2xl:grid-cols-[minmax(0,1.35fr)_minmax(360px,.65fr)]">
        <article className="hc-surface-card p-4 md:p-5">
          <SectionHeader label="Import-Verlauf" meta="Usage Facts · nicht Durchsatz" />
          <ImportActivityChart rows={data.usage.daily_imports} />
        </article>
        <article className="hc-surface-card p-4 md:p-5">
          <SectionHeader label="Herkunft" meta={`echter Anteil · n ${summary.fact_rows.toLocaleString("de-DE")}`} />
          <OriginDistribution rows={data.usage.origins} total={summary.fact_rows} />
        </article>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <article className="hc-surface-card p-4 md:p-5">
          <SectionHeader label="Datenabdeckung" meta="größte Hebel zuerst" />
          <div className="mt-2">
            <CoverageRow label="Exact Links" observed={coverage.exact_task_run_links} denominator={coverage.fact_rows} />
            <CoverageRow label="Modell" observed={coverage.model_known} denominator={coverage.fact_rows} />
            <CoverageRow label="Laufzeit" observed={coverage.duration_known} denominator={coverage.fact_rows} />
            <CoverageRow label="TTFT" observed={coverage.ttft_known} denominator={coverage.fact_rows} />
          </div>
          {coverage.missing_columns.length ? (
            <p className="mt-3 text-[10px] font-semibold text-status-warn">
              Fehlende Adapterfelder: {coverage.missing_columns.join(", ")}
            </p>
          ) : null}
        </article>
        <article className="hc-surface-card p-4 md:p-5">
          <SectionHeader label="Langfuse-Signale" meta={data.langfuse.available ? "Calls · Latenz p50 · TTFT p50" : "fail-soft"} />
          <LangfuseSignals data={data} />
        </article>
      </div>
    </section>
  );
}

export function StatistikView() {
  const observability = useObservabilityStats(7);
  const issues = useHermesRunsIssues();
  const accountUsage = useAccountUsage();
  const statsConfig = useStatsConfig();
  const costs = useHermesRunsCosts();
  const costSeries = useHermesRunsCostSeries();
  const subscriptionBurn = useHermesSubscriptionBurn();
  const now = observability.data?.checked_at ?? nowSec();
  const issueGroups = useMemo(() => issues.data?.issues ?? [], [issues.data]);
  const providers = useMemo(() => accountUsage.data?.providers ?? [], [accountUsage.data]);
  const costProfiles = useMemo(() => costs.data?.profiles ?? [], [costs.data]);

  return (
    <div data-statistik className="mx-auto w-full max-w-[2200px] space-y-5 p-4 pb-20 md:p-6">
      <ObservabilityDeck
        data={observability.data}
        loading={observability.loading}
        error={observability.error}
      />

      {issues.error ? (
        <StLead tone="warn">
          <b>{de.stats.loadError}</b>
        </StLead>
      ) : null}

      <ErrorTaxonomySection issues={issueGroups} />

      <AccountUsageTile
        usage={accountUsage.data}
        loading={accountUsage.loading && !accountUsage.data}
        error={accountUsage.error}
        config={statsConfig.data ?? DEFAULT_STATS_CONFIG}
      />

      <details className="st-details">
        <summary className="min-h-12 text-xs font-medium tracking-wide text-live hover:brightness-110">
          Provider-Budget im Detail
        </summary>
        <BudgetLedgerSection providers={providers} />
      </details>

      <CostTrendSection
        costs={costSeries.data ?? null}
        loading={costSeries.loading && !costSeries.data}
        error={costSeries.error}
        profiles={costProfiles}
        burn={subscriptionBurn.data ?? null}
      />

      <LaneBurnSection costs={costProfiles} />

      <SubscriptionBurnSection
        burn={subscriptionBurn.data ?? null}
        loading={subscriptionBurn.loading && !subscriptionBurn.data}
        error={subscriptionBurn.error}
      />

      <details className="st-details">
        <summary className="min-h-12 text-xs font-medium tracking-wide text-live hover:brightness-110">
          Kettenkosten im Detail
        </summary>
        <MotherLedgerSection />
      </details>

      <div className="st-foot">
        <span>{de.stats.footLeft(fmtClock(now))}</span>
        <span>/control/statistik</span>
      </div>
    </div>
  );
}
