/**
 * StatistikView (/control/statistik) — Leitstand-Reskin (Rule 11+12): dieselbe
 * dunkle Statistik wie Fleet/Start statt des Broadsheet-Vorgängers (ST3/ST4/
 * ST5). Skin via [data-statistik] + statistik.css (Full-Bleed-Regeln über
 * `.hc-page:has([data-statistik])`, selbst-genügsam statt von einer
 * ControlShell-Route-Klasse abzuhängen — Pattern-Parität mit Fleet/Start);
 * Flächen aus den Leitstand-Tokens (surface-0/1/2, ink/ink-2/ink-3, line,
 * live, status-trio). IA/Funktion + alle Sektionen bleiben unverändert
 * gegenüber dem Broadsheet-Vorgänger — nur die Präsentation wechselt von einer
 * bedruckten Papier-Spalte auf Leitstand-Karten/KPI-Pods.
 *
 * Masthead: seit W3-3 (2026-07-10) rendert Statistik KEIN eigenes Masthead
 * mehr — die Shell-Puls-Leiste (ControlShell) trägt Label "Statistik" +
 * Instrumente + die NotificationBridge-Glocke (schließt denselben P2 "Glocke
 * unsichtbar", den Fleet in W3-1a und Start in W3-2 schon hatten). Die alte
 * Brand-Zeile ("Hermes"/"Statistik") + der LIVE/SYNC-Punkt dopplten nur das
 * Shell-Label bzw. trugen kein eigenständiges Signal — ersatzlos entfernt
 * statt verschoben. Die "Akzeptanzrate"-Kachel (`StatsMasthead` unten) bleibt
 * unverändert: das ist echter Inhalt (die Kern-KPI der View), kein Chrome.
 *
 * Mobil-first: die Spalte liest sich bei 390px top-to-bottom ohne horizontales
 * Scrollen.
 */
import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { ChevronRight, TriangleAlert } from "lucide-react";
import { de } from "../i18n/de";
import { fmtClock, fmtClockTime, fmtDur, fmtTokens, nowSec, formatEffectiveCost } from "../lib/derive";
import { profileLabel } from "../lib/tones";
import {
  useAccountUsage,
  useBoardStats,
  useChainCompletion,
  useHermesReliability,
  useHermesRunsCosts,
  useHermesRunsCostSeries,
  useHermesRunsDaily,
  useHermesRunsIssues,
  useHermesRunSummary,
  useHermesWindowedRollup,
  useHermesSubscriptionBurn,
  useStatsConfig,
} from "../hooks/useControlData";
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
import { Eyebrow } from "../components/primitives";
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
  rootRuns,
  rosterProfiles,
  sortedLedgerRoots,
  subscriptionBurnBreakdown,
  windowCostSummary,
  workerCost,
  workerTokens,
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
      <div className="st-mast" data-testid="stats-masthead-figure">
        <span className="st-mast-value">{acc.rate == null ? "—" : pctText(acc.rate)}</span>
        {acc.rate != null ? <small>%</small> : null}
      </div>
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
        <FleetEmptyState title={de.stats.leaderEmpty} desc={de.stats.leaderEmpty} ok />
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
    verdict = <FleetEmptyState title={de.stats.errEmpty} desc={de.stats.errEmpty} ok />;
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
        <FleetEmptyState title={de.stats.budgetEmpty} desc={de.stats.budgetEmpty} ok />
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
              footRight={r.resetAt ? de.stats.budgetReset(fmtClockTime(r.resetAt)) : undefined}
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
        <FleetEmptyState title="Review zurück offen" desc="Review zurück offen" ok />
      </div>
    );
  }
  if (chartRows.length === 0) {
    return (
      <div className="st-we-map st-we-map-empty">
        <FleetEmptyState title="Token/min offen" desc="Token/min offen" ok />
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
        <span className="st-we-lever st-we-lever-empty">Noch kein belastbarer Hebel</span>
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
        <FleetEmptyState title="Worker-Daten laden" desc="Worker-Daten laden" ok />
      ) : error && empty ? (
        <StNote tone="warn">Worker-Daten nicht frisch</StNote>
      ) : empty ? (
        <FleetEmptyState title="Noch keine Worker-Daten im Fenster." desc="Noch keine Worker-Daten im Fenster." ok />
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
        <FleetEmptyState title={de.stats.burnEmpty} desc={de.stats.burnEmpty} ok />
      ) : (
        <div className="st-panel space-y-1.5 p-2">
          {lanes.map((l, i) => {
            const cost = formatEffectiveCost({
              cost_usd: l.costUsd ?? 0,
              cost_effective_usd: l.costEquivalent ?? 0,
              tokens: l.tokens,
            });
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
                      <span title={de.ketten.costEstimatedTooltip}>{cost.text}</span>
                    ) : (
                      cost.text
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
        <FleetEmptyState title={de.stats.reviewValueEmpty} desc={de.stats.reviewValueEmpty} ok />
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
  const maxTokens = Math.max(1, ...series.map((row) => row.total_tokens ?? 0));
  const maxCost = Math.max(1, ...series.map((row) => row.api_equivalent_usd ?? row.cost_usd_equivalent ?? 0));
  const hasTrend = series.some((row) => (row.runs ?? 0) > 0 || (row.total_tokens ?? 0) > 0 || (row.api_equivalent_usd ?? row.cost_usd_equivalent ?? 0) > 0);
  const laneRows = burn ? subscriptionBurnBreakdown(burn, 8).topLanes : [];
  const hasDrilldown = profiles.length > 0 || laneRows.length > 0;

  return (
    <section className="space-y-2">
      <SectionHeader label={de.stats.secCostTrend} meta={de.stats.secCostTrendMeta} />
      {loading ? (
        <FleetEmptyState title={de.stats.costTrendLoading} desc={de.stats.costTrendLoading} ok />
      ) : error ? (
        <StNote tone="warn">{de.stats.costTrendError} {error}</StNote>
      ) : !hasTrend ? (
        <FleetEmptyState title={de.stats.costTrendEmpty} desc={de.stats.costTrendEmpty} ok />
      ) : (
        <>
          <div className="st-trend" data-testid="runs-costs-series-trend">
            {series.map((row) => {
              const tokens = row.total_tokens ?? 0;
              const cost = row.api_equivalent_usd ?? row.cost_usd_equivalent ?? 0;
              return (
                <div key={row.day} className="st-trend-row">
                  <span className="st-mono">{row.day.slice(5)}</span>
                  <div className="st-trend-bars" aria-label={`${row.day}: ${fmtTokens(tokens)} tokens, ${usdText(cost)}, ${row.runs ?? 0} runs`}>
                    <span className="st-trend-token" style={{ "--st-w": `${Math.max(3, Math.round((tokens / maxTokens) * 100))}%` } as CSSProperties} />
                    <span className="st-trend-money" style={{ "--st-w": `${Math.max(3, Math.round((cost / maxCost) * 100))}%` } as CSSProperties} />
                  </div>
                  <span className="st-mono">{fmtTokens(tokens)}</span>
                  <span className="st-mono">{usdText(cost)}</span>
                  <span className="st-mono">{row.runs ?? 0}×</span>
                </div>
              );
            })}
          </div>
          <p className="st-note"><b>{de.stats.costTrendSource}:</b> {de.stats.costTrendSourceCopy}</p>
          <button type="button" className="st-linkbutton" onClick={() => setDrawerOpen(true)}>
            {de.stats.modelLaneDrilldown}<ChevronRight className="h-3.5 w-3.5" />
          </button>
        </>
      )}
      {drawerOpen ? (
        <DrawerShell title={de.stats.modelLaneDrilldown} ariaLabel={de.stats.modelLaneDrilldown} onClose={() => setDrawerOpen(false)}>
          {!hasDrilldown ? (
            <FleetEmptyState title={de.stats.modelDrilldownEmpty} desc={de.stats.modelDrilldownEmpty} ok />
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
                    <b>{fmtTokens(row.total_tokens ?? ((row.input_tokens ?? 0) + (row.output_tokens ?? 0) + (row.cached_tokens ?? 0)))}</b>
                    <span>{usdText(row.api_equivalent_usd ?? row.cost_usd_equivalent ?? row.cost_usd)}</span>
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
        <FleetEmptyState title={de.stats.burnLoading} desc={de.stats.burnLoading} ok />
      ) : error ? (
        <StNote tone="warn">{error}</StNote>
      ) : !hasBurn ? (
        <FleetEmptyState title={de.stats.subscriptionBurnEmpty} desc={de.stats.subscriptionBurnEmpty} ok />
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
  return value != null && value > 0 ? `$${value.toFixed(2)}` : "—";
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
  cost_usd_equivalent?: number | null;
  cost_effective_usd?: number | null;
  provider?: string | null;
  providers?: string[];
  model?: string | null;
  billing_mode?: string | null;
  runtime_seconds?: number | null;
}): string {
  const provider = item.provider ?? item.providers?.join(", ") ?? null;
  const model = item.model ?? null;
  return [
    `${de.stats.motherLedgerColAbo}: ${fmtMaybeUsd(item.cost_usd_equivalent)} ${de.stats.motherLedgerAboMarker}`,
    `${de.stats.motherLedgerColReal}: ${fmtMaybeUsd(item.cost_usd)}`,
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
          <b className="st-mono">{fmtTokens((runner.input_tokens ?? 0) + (runner.output_tokens ?? 0))}</b>
          <b className="st-mono st-ledger-abo">{fmtMaybeUsd(runner.cost_usd_equivalent)} <small>{de.stats.motherLedgerAboMarker}</small></b>
          <b className="st-mono st-ledger-real">{(runner.cost_usd ?? 0) > 0 ? fmtMaybeUsd(runner.cost_usd) : "—"}</b>
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
  const windowCost = useMemo(() => windowCostSummary(roots), [roots]);
  const topAbo = roots.reduce((top, root) => Math.max(top, chainCost(root).abo ?? 0), 0);
  const showStaleNotice = Boolean((rollup.error || rollup.isStale) && rollup.data);
  const windowLabel = windowHours === 168 ? "7T" : "24Std";
  const ratioText = windowCost.echtUsd > 0
    ? de.stats.motherLedgerHeroRatio
        .replace("{real}", fmtUsd(windowCost.echtUsd))
        .replace("{abo}", fmtUsd(windowCost.aboUsd))
        .replace("{ratio}", Math.round(windowCost.aboUsd / windowCost.echtUsd).toLocaleString("de-DE"))
    : de.stats.motherLedgerHeroRatioNoReal.replace("{abo}", fmtUsd(windowCost.aboUsd));
  const metaText = `${windowLabel}${showStaleNotice ? ` · ${de.stats.motherLedgerStaleNotice}` : ""}`;
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
        <FleetEmptyState title={de.stats.burnLoading} desc={de.stats.burnLoading} ok />
      ) : rollup.error && !rollup.data ? (
        <StNote tone="warn">{de.ketten.chainCostsLoadError}</StNote>
      ) : roots.length === 0 ? (
        <FleetEmptyState title={de.ketten.chainCostsEmpty} desc={de.ketten.chainCostsEmpty} ok />
      ) : (
        <div className="st-ledger" data-ledger-viewport={isMobileLedger ? "mobile" : "desktop"}>
          <div className="st-ledger-hero">
            <div className="st-ledger-hero-primary">
              <span>{de.stats.motherLedgerHeroAbo.replace("{window}", windowLabel)}</span>
              <b className="st-mono">{fmtUsd(windowCost.aboUsd)} <small>{de.stats.motherLedgerAboMarker}</small></b>
              <small>{de.stats.motherLedgerHeroAboSub}</small>
            </div>
            <div className="st-ledger-hero-real">
              <span>{de.stats.motherLedgerHeroReal.replace("{window}", windowLabel)}</span>
              <b className="st-mono">{fmtUsd(windowCost.echtUsd)}</b>
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
                      <span className="st-ledger-abo"><b className="st-mono">{fmtMaybeUsd(money.abo)}</b><small>{de.stats.motherLedgerAboMarker}</small></span>
                      <span className={(money.echt ?? 0) > 0 ? "st-ledger-real is-positive" : "st-ledger-real"}>{de.stats.motherLedgerRealShort} {(money.echt ?? 0) > 0 ? fmtMaybeUsd(money.echt) : "—"}</span>
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
                        return (
                          <div key={key} className="st-ledger-worker-block">
                            <button type="button" className="st-ledger-worker-row" onClick={() => toggleWorker(root.id, worker.profile)} aria-expanded={openWorker} title={ledgerDetailTitle(worker)}>
                              <span className="st-ledger-worker-label"><LaneLabel profile={worker.profile} /><small>{worker.model ?? worker.provider ?? "—"}</small></span>
                              <span className="st-mono" data-label={de.stats.motherLedgerColTokens}>{fmtTokens(workerTokens(worker))}</span>
                              <span className="st-mono st-ledger-abo" data-label={de.stats.motherLedgerAboMobile}>{fmtMaybeUsd(cost.abo)}</span>
                              <span className={(cost.echt ?? 0) > 0 ? "st-mono st-ledger-real is-positive" : "st-mono st-ledger-real"} data-label={de.stats.motherLedgerRealMobile}>{(cost.echt ?? 0) > 0 ? fmtMaybeUsd(cost.echt) : "—"}</span>
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

export function StatistikView() {
  const reliability = useHermesReliability();
  const summary = useHermesRunSummary();
  const daily = useHermesRunsDaily();
  const issues = useHermesRunsIssues();
  const accountUsage = useAccountUsage();
  const statsConfig = useStatsConfig();
  const costs = useHermesRunsCosts();
  const costSeries = useHermesRunsCostSeries();
  const subscriptionBurn = useHermesSubscriptionBurn();
  const workerRollup = useHermesWindowedRollup({ hours: 168, limit: 20 });
  const chain = useChainCompletion();
  const board = useBoardStats();

  const now = reliability.data?.now ?? nowSec();
  const profiles = useMemo(() => reliability.data?.profiles ?? [], [reliability.data]);
  const baseline = useMemo(() => reliability.data?.baseline ?? [], [reliability.data]);
  const last7 = useMemo(() => (daily.data?.series ?? []).slice(-7), [daily.data]);
  const issueGroups = useMemo(() => issues.data?.issues ?? [], [issues.data]);
  const providers = useMemo(() => accountUsage.data?.providers ?? [], [accountUsage.data]);
  const costProfiles = useMemo(() => costs.data?.profiles ?? [], [costs.data]);
  const reviewValue = useMemo(() => costs.data?.review_value ?? [], [costs.data]);

  const stale = reliability.isStale || daily.isStale;
  const hasLoadError = Boolean(reliability.error || daily.error);

  return (
    <div data-statistik className="space-y-5">
      {/* A7: AccountUsageTile bleibt der primäre Live-Cockpit-Block. */}
      <AccountUsageTile
        usage={accountUsage.data}
        loading={accountUsage.loading && !accountUsage.data}
        error={accountUsage.error}
        config={statsConfig.data ?? DEFAULT_STATS_CONFIG}
      />

      {hasLoadError ? (
        <StLead tone="warn">
          <b>{de.stats.loadError}</b>
        </StLead>
      ) : null}

      <WorkerEfficiencySection
        roots={workerRollup.data?.roots ?? []}
        profiles={profiles}
        minReviewSample={reliability.data?.min_n ?? DEFAULT_MIN_REVIEW_SAMPLE}
        loading={workerRollup.loading && !workerRollup.data}
        error={workerRollup.error}
        stale={workerRollup.isStale}
      />

      <section className="st-panel p-4">
        <StatsMasthead profiles={profiles} baseline={baseline} series={last7} now={now} stale={stale} />
      </section>

      <LatencySection
        p50={summary.data?.cycle_time_p50_seconds ?? null}
        p90={summary.data?.cycle_time_p90_seconds ?? null}
      />

      <ReliabilitySection profiles={profiles} />

      <ErrorTaxonomySection issues={issueGroups} />

      {/* ST5: Budget-Ledger bleibt unter Details — AccountUsageTile oben ist die
          primäre Ansicht; das Ledger-Format bleibt für den ausführlichen Blick. */}
      <details className="st-details">
        <summary className="min-h-12 text-xs font-medium tracking-wide text-live hover:brightness-110">
          {de.stats.secBudget} — {de.stats.budgetLedgerDetailLabel}
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

      <SubscriptionBurnSection
        burn={subscriptionBurn.data ?? null}
        loading={subscriptionBurn.loading && !subscriptionBurn.data}
        error={subscriptionBurn.error}
      />

      <EffizienzSection
        profiles={profiles}
        costs={costProfiles}
        reviewValue={reviewValue}
        chainRate={chain.data?.chain_completion_rate ?? null}
        queueWaitSeconds={board.data?.queue_wait_p50_seconds ?? null}
      />

      {/* ── Kosten pro Kette ────────────────────────────────────────────────── */}
      <MotherLedgerSection />

      <div className="st-foot">
        <span>{de.stats.footLeft(fmtClock(now))}</span>
        <span>/control/statistik</span>
      </div>
    </div>
  );
}
