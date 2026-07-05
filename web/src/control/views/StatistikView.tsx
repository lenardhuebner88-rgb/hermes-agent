/**
 * StatistikView (/control/statistik) — Richtung B · Broadsheet (PlanSpec
 * 2026-06-17, ST4). An editorial flotten-report printed on paper instead of a
 * wall of dark cards. The shell + primitives are ST3 (components/broadsheet);
 * this module binds the live numbers.
 *
 * ST4 owns the top of the sheet:
 *   Masthead   — Akzeptanzrate (verifier verdicts) + 3 Stütz-KPIs
 *                (Autonomie · Kosten je Lieferung · Nutzerwert).
 *   Latenz     — p50 / p90 als Zwei-Zahl-Karte (/runs/summary).
 *   Verläss.   — Leaderboard je Profil (/runs/reliability, phantom-gefiltert).
 *   Taxonomie  — wiederkehrende Fehler gebucketet (/runs/issues); Befund:
 *                alles Harness-Lifecycle.
 * ST5 inserts the Budget-Ledger + Effizienz sections at the marked seam below.
 *
 * Mobil-first: the column is capped at 27rem and reads top-to-bottom at 390px.
 */
import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { de } from "../i18n/de";
import { fmtClock, fmtClockTime, fmtDur, fmtTokens, nowSec, formatEffectiveCost } from "../lib/derive";
import { profileLabel } from "../lib/tones";
import {
  useAccountUsage,
  useBoardStats,
  useChainCompletion,
  useHermesReliability,
  useHermesRunsCosts,
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
  SubscriptionTokenBurnResponse,
  WindowedRollupRoot,
  WindowedRollupWorker,
} from "../lib/schemas";
import { AccountUsageTile } from "../components/AccountUsageTile";
import { DEFAULT_STATS_CONFIG } from "../lib/statsFields";
import {
  BroadsheetShell,
  BroadsheetFooter,
  EngpassLead,
  ErrorBar,
  ErrorLegend,
  ErrorLegendItem,
  LeaderRow,
  LedgerRow,
  Masthead,
  SectionRule,
  SupportingStat,
  SupportingStats,
  TwinStat,
  TwinStats,
  Verdict,
} from "../components/broadsheet/Broadsheet";
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
  type LedgerEntry,
  type MotherLedgerSortKey,
} from "../lib/statsBroadsheet";

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
  return { "--sb-lane": LANE_COLORS[profile] ?? "var(--color-ink-3)" } as CSSProperties;
}

function LaneLabel({ profile, label = profile }: { profile: string; label?: ReactNode }) {
  return (
    <span className="sb-lane-label" style={laneStyle(profile)}>
      <span className="sb-lane-dot" aria-hidden="true" />
      <span>{label}</span>
    </span>
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
  const deltaStatus = delta == null ? "neutral" : delta >= 0 ? "ok" : "crit";

  return (
    <>
      <Masthead
        kicker={de.stats.mastKicker}
        meta={meta}
        label={de.stats.mastLabel}
        value={acc.rate == null ? "—" : pctText(acc.rate)}
        unit={acc.rate == null ? undefined : "%"}
        note={note}
        delta={deltaNode}
        deltaStatus={deltaStatus}
      />
      <SupportingStats>
        <SupportingStat
          value={pctText(aut)}
          unit={aut == null ? undefined : "%"}
          label={de.stats.suppAutonomie}
          accent
        />
        <SupportingStat value={usdText(cpd)} label={de.stats.suppCost} />
        <SupportingStat value={String(nutzer)} label={de.stats.suppNutzer} />
      </SupportingStats>
    </>
  );
}

// ── Latenz (Zwei-Zahl-Karte) ─────────────────────────────────────────────────
export function LatencySection({ p50, p90 }: { p50: number | null; p90: number | null }) {
  return (
    <>
      <SectionRule title={de.stats.secLatency} meta={de.stats.secLatencyMeta} />
      <TwinStats>
        <TwinStat label={de.stats.latP50} value={p50 == null ? "—" : fmtDur(p50)} />
        <TwinStat label={de.stats.latP90} value={p90 == null ? "—" : fmtDur(p90)} />
      </TwinStats>
    </>
  );
}

// ── Verlässlichkeit (Leaderboard) ────────────────────────────────────────────
export function ReliabilitySection({ profiles }: { profiles: ReliabilityProfile[] }) {
  const rows = useMemo(() => leaderboard(profiles), [profiles]);
  return (
    <>
      <SectionRule title={de.stats.secReliability} meta={de.stats.secReliabilityMeta} />
      {rows.length === 0 ? (
        <Verdict tone="calm">{de.stats.leaderEmpty}</Verdict>
      ) : (
        rows.map((r, i) => (
          <LeaderRow
            key={r.profile}
            rank={i + 1}
            name={<LaneLabel profile={r.profile} label={r.label} />}
            score={r.rate == null ? "—" : `${Math.round(r.rate * 100)} %`}
            status={r.status}
            latency={de.stats.leaderRuns(r.runs)}
          />
        ))
      )}
    </>
  );
}

// ── Fehler-Taxonomie ─────────────────────────────────────────────────────────
export function ErrorTaxonomySection({ issues }: { issues: IssueGroup[] }) {
  const tax = useMemo(() => errorTaxonomy(issues), [issues]);
  let verdict: ReactNode;
  if (tax.buckets.length === 0) {
    verdict = <Verdict tone="calm">{de.stats.errEmpty}</Verdict>;
  } else if (tax.allLifecycle) {
    verdict = (
      <Verdict tone="calm">
        {de.stats.verdictPre}
        <b>{de.stats.verdictBold}</b>
        {de.stats.verdictPost}
      </Verdict>
    );
  } else {
    verdict = <Verdict tone="warn">{de.stats.verdictMixed}</Verdict>;
  }
  return (
    <>
      <SectionRule title={de.stats.secErrors} meta={de.stats.secErrorsMeta} />
      {tax.buckets.length > 0 ? (
        <>
          <ErrorBar segments={tax.buckets.map((b) => ({ pct: b.pct, color: b.color, key: b.key }))} />
          <ErrorLegend>
            {tax.buckets.map((b) => (
              <ErrorLegendItem key={b.key} color={b.color} label={ERROR_LABEL[b.key] ?? b.key} count={b.count} />
            ))}
          </ErrorLegend>
        </>
      ) : null}
      {verdict}
      <a href="/control/issues" className="sb-kick sb-accent mt-4 inline-block min-h-9">
        {de.stats.issuesLink}
      </a>
    </>
  );
}

// ── Budget-Ledger (Provider-Limits, Engpass zuerst) ──────────────────────────
// GET /api/account-usage: je Provider die knappste Auslastung, Engpass-Zeile
// oben. Claude/ChatGPT = echter OAuth-Fetch je window_key (session/weekly);
// Kimi ist geschätzt (kein Provider-Limit) und so getaggt.
function ledgerFoot(r: LedgerEntry): string {
  if (!r.available) return r.unavailableReason ?? de.stats.budgetUnavailable;
  if (r.window) return r.window;
  return r.estimated ? de.stats.budgetNoLimit : de.stats.budgetNoWindow;
}

export function BudgetLedgerSection({ providers }: { providers: AccountUsageProvider[] }) {
  const rows = useMemo(() => budgetLedger(providers), [providers]);
  // Engpass-Lead: die knappste Zeile, sofern sie schon spürbar (≠ ok) ist.
  const lead = rows.find((r) => r.usedPercent != null && r.status !== "ok") ?? null;
  return (
    <>
      <SectionRule title={de.stats.secBudget} meta={de.stats.secBudgetMeta} />
      {lead && lead.usedPercent != null ? (
        <EngpassLead tone={lead.status === "crit" ? "crit" : "warn"}>
          {de.stats.budgetLeadPre}
          <b>{de.stats.budgetLead(lead.label, lead.window, Math.round(lead.usedPercent))}</b>
        </EngpassLead>
      ) : null}
      {rows.length === 0 ? (
        <Verdict tone="calm">{de.stats.budgetEmpty}</Verdict>
      ) : (
        rows.map((r) => (
          <LedgerRow
            key={r.provider}
            name={r.label}
            tag={r.estimated ? de.stats.budgetEstimated : undefined}
            figure={r.usedPercent == null ? "—" : `${Math.round(r.usedPercent)} %`}
            status={r.status}
            pct={r.usedPercent ?? null}
            footLeft={ledgerFoot(r)}
            footRight={r.resetAt ? de.stats.budgetReset(fmtClockTime(r.resetAt)) : undefined}
          />
        ))
      )}
    </>
  );
}

// ── Flotten-Effizienz (Durchsatz, Gate, Token-Burn je Lane) ──────────────────
// Drei Effizienz-KPIs (Ketten-Abschluss · Queue-Wartezeit · Gate-Quote) plus
// Token-Burn je Lane. Bewusst KEINE Vanity-Metriken (Out-Tokens/Tag, roher
// Abo-Verbrauch) — nur handlungsleitende Effizienz.
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
  // S1B: nur Stufen mit Läufen im Fenster zeigen; leere Stufen sind Rauschen.
  const stages = useMemo(() => reviewValue.filter((r) => r.runs > 0), [reviewValue]);
  const gate = gateEffectiveness(profiles);
  return (
    <>
      <SectionRule title={de.stats.secEffizienz} meta={de.stats.secEffizienzMeta} />
      <SupportingStats>
        <SupportingStat
          value={pctText(chainRate)}
          unit={chainRate == null ? undefined : "%"}
          label={de.stats.effChain}
          accent
        />
        <SupportingStat
          value={queueWaitSeconds == null ? "—" : fmtDur(queueWaitSeconds)}
          label={de.stats.effQueue}
        />
        <SupportingStat
          value={pctText(gate)}
          unit={gate == null ? undefined : "%"}
          label={de.stats.effGate}
        />
      </SupportingStats>
      <SectionRule title={de.stats.secBurn} meta={de.stats.secBurnMeta} />
      {lanes.length === 0 ? (
        <Verdict tone="calm">{de.stats.burnEmpty}</Verdict>
      ) : (
        lanes.map((l, i) => {
          // Tokens bleiben die führende Zahl (score); der $-Gegenwert ist
          // sekundär und steht neben der Run-Zahl. estimated → "gesch." +
          // Tooltip, abgeleitet aus costUsd === 0 && costEquivalent > 0.
          const cost = formatEffectiveCost({
            cost_usd: l.costUsd ?? 0,
            cost_effective_usd: l.costEquivalent ?? 0,
            tokens: l.tokens,
          });
          return (
            <LeaderRow
              key={l.profile}
              rank={i + 1}
              name={<LaneLabel profile={l.profile} label={l.label} />}
              score={fmtTokens(l.tokens)}
              status="neutral"
              latency={
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
        })
      )}
      {/* S1B: Review-Wert je Stufe — Funde & Kosten je Fund. Ergänzt die
          Kosten-Sicht ($/Lane) um den WERT je Review-Stufe. */}
      <SectionRule title={de.stats.secReviewValue} meta={de.stats.secReviewValueMeta} />
      {stages.length === 0 ? (
        <Verdict tone="calm">{de.stats.reviewValueEmpty}</Verdict>
      ) : (
        stages.map((r, i) => {
          const name = <LaneLabel profile={r.profile} label={profileLabel[r.profile] ?? r.profile} />;
          // Scout ist read-only Recon, keine Verdikt-Stufe: sein Wert ist die
          // gelesene Evidenz (read_items als Score) plus Kosten je Item — es
          // gibt weder Quote noch Funde. read_items ist NULL, wenn kein Lauf
          // Read-Metadaten trug (Altbestand) → "—".
          if (r.profile === "scout") {
            return (
              <LeaderRow
                key={r.profile}
                rank={i + 1}
                name={name}
                score={r.read_items == null ? "—" : String(r.read_items)}
                status="neutral"
                latency={
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
          // findings_* sind gekoppelt NULL (Altbestand): keine Fund-Erfassung.
          const findings =
            r.findings_blocking == null || r.findings_observations == null
              ? null
              : r.findings_blocking + r.findings_observations;
          return (
            <LeaderRow
              key={r.profile}
              rank={i + 1}
              name={name}
              score={findings == null ? "—" : String(findings)}
              status={findings != null && findings > 0 ? "warn" : "neutral"}
              latency={
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
        })
      )}
    </>
  );
}

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
    <>
      <SectionRule title={de.stats.secSubscriptionBurn} meta={de.stats.secSubscriptionBurnMeta} />
      {loading ? (
        <Verdict tone="calm">{de.stats.burnLoading}</Verdict>
      ) : error ? (
        <Verdict tone="warn">{error}</Verdict>
      ) : !hasBurn ? (
        <Verdict tone="calm">{de.stats.subscriptionBurnEmpty}</Verdict>
      ) : (
        <div className="sb-subburn" data-testid="subscription-burn-breakdown">
          <div className="sb-subburn-hero">
            <span className="sb-kick">{de.stats.subscriptionBurnWindow(burn?.days ?? 7)}</span>
            <strong className="sb-disp">{fmtTokens(detail.totals.total_tokens)}</strong>
            <span>{de.stats.subscriptionBurnHero(detail.totals.runs, detail.subscriptionCount)}</span>
          </div>
          <div className="sb-subburn-grid">
            <div>
              <p className="sb-kick">{de.stats.subscriptionBurnTop}</p>
              {detail.topLanes.map((row) => (
                <div key={`${row.subscription}:${row.profile}`} className="sb-subburn-row" style={laneStyle(row.profile)}>
                  <span aria-label={`${row.profile} · ${row.subscription}`}><LaneLabel profile={row.profile} /> · {row.subscription}</span>
                  <i style={{ "--sb-share": `${Math.max(2, Math.round(row.share * 100))}%` } as CSSProperties} />
                  <b className="sb-mono">{fmtTokens(row.total_tokens)}</b>
                  <small>{Math.round(row.share * 100)}%</small>
                </div>
              ))}
            </div>
            <div>
              <p className="sb-kick">{de.stats.subscriptionBurnClasses}</p>
              {detail.classes.map((row) => (
                <div key={`${row.subscription}:${row.value_class}`} className="sb-subburn-row">
                  <span>{row.value_class} · {row.subscription}</span>
                  <i />
                  <b className="sb-mono">{fmtTokens(row.total_tokens)}</b>
                  <small className="sb-mono">{Math.round(row.share * 100)} %</small>
                </div>
              ))}
            </div>
          </div>
          <div className="sb-subburn-flags" aria-label={de.stats.subscriptionBurnFlags}>
            {detail.flags.map((flag) => (
              <span key={`${flag.kind}:${flag.title}`} className={flag.kind === "anti" ? "sb-subburn-flag sb-subburn-anti" : "sb-subburn-flag"}>
                <b>{flag.kind === "anti" ? de.stats.subscriptionBurnAnti : de.stats.subscriptionBurnTopFlag}</b>
                {flag.title} · {flag.detail}
              </span>
            ))}
          </div>
          {detail.trend.length > 0 && (
            <div data-testid="subscription-burn-trend">
              <p className="sb-kick">{de.stats.subscriptionBurnTrend}</p>
              {detail.trend.map((row) => (
                <div key={row.date} className="sb-subburn-trend-row">
                  <span className="sb-mono" style={{ fontSize: "11px", color: "var(--sb-ink2)" }}>{row.date}</span>
                  <div className="sb-subburn-trend-bar">
                    <i style={{ width: `${Math.round(row.share * 100)}%` }} />
                  </div>
                  <b className="sb-mono" style={{ fontSize: "12px" }}>{fmtTokens(row.total_tokens)}</b>
                  <small className="sb-mono" style={{ color: "var(--sb-ink3)" }}>{Math.round(row.share * 100)} %</small>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </>
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
    return <div className="sb-ledger-runners sb-mono">{de.stats.motherLedgerNoRunners}</div>;
  }
  return (
    <div className="sb-ledger-runners">
      {runners.map((runner) => (
        <div key={runner.id} className="sb-ledger-runner" title={ledgerDetailTitle(runner)}>
          <span className="sb-mono">#{runner.id}</span>
          <span>{runner.provider ?? "Provider n/a"}{runner.model ? ` · ${runner.model}` : ""}</span>
          <b className="sb-mono">{fmtTokens((runner.input_tokens ?? 0) + (runner.output_tokens ?? 0))}</b>
          <b className="sb-mono sb-ledger-abo">{fmtMaybeUsd(runner.cost_usd_equivalent)} <small>{de.stats.motherLedgerAboMarker}</small></b>
          <b className="sb-mono sb-ledger-real">{(runner.cost_usd ?? 0) > 0 ? fmtMaybeUsd(runner.cost_usd) : "—"}</b>
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
    <>
      <SectionRule title={de.stats.motherLedgerTitle} meta={metaText} />
      <div className="sb-ledger-controls" aria-label="MotherLedger Controls">
        <div className="sb-chipset" aria-label="Fenster">
          <button type="button" className={windowHours === 168 ? "is-active" : ""} onClick={() => setWindowHours(168)}>7T</button>
          <button type="button" className={windowHours === 24 ? "is-active" : ""} onClick={() => setWindowHours(24)}>24Std</button>
        </div>
        <div className="sb-chipset" aria-label="Sortierung">
          <button type="button" className={sortKey === "usd" ? "is-active" : ""} onClick={() => setSortKey("usd")}>{de.stats.motherLedgerSortAbo}</button>
          <button type="button" className={sortKey === "tokens" ? "is-active" : ""} onClick={() => setSortKey("tokens")}>Tokens</button>
          <button type="button" className={sortKey === "runs" ? "is-active" : ""} onClick={() => setSortKey("runs")}>Runs</button>
        </div>
      </div>
      {rollup.loading && !rollup.data ? (
        <Verdict tone="calm">{de.stats.burnLoading}</Verdict>
      ) : rollup.error && !rollup.data ? (
        <Verdict tone="warn">{de.ketten.chainCostsLoadError}</Verdict>
      ) : roots.length === 0 ? (
        <Verdict tone="calm">{de.ketten.chainCostsEmpty}</Verdict>
      ) : (
        <div className="sb-ledger" data-ledger-viewport={isMobileLedger ? "mobile" : "desktop"}>
          <div className="sb-ledger-hero">
            <div className="sb-ledger-hero-primary">
              <span>{de.stats.motherLedgerHeroAbo.replace("{window}", windowLabel)}</span>
              <b className="sb-mono">{fmtUsd(windowCost.aboUsd)} <small>{de.stats.motherLedgerAboMarker}</small></b>
              <small>{de.stats.motherLedgerHeroAboSub}</small>
            </div>
            <div className="sb-ledger-hero-real">
              <span>{de.stats.motherLedgerHeroReal.replace("{window}", windowLabel)}</span>
              <b className="sb-mono">{fmtUsd(windowCost.echtUsd)}</b>
            </div>
          </div>
          <p className="sb-ledger-honesty">{ratioText}</p>
          {showStaleNotice ? (
            <div className="sb-kick" role="status" title={rollup.error ?? undefined}>
              {de.stats.motherLedgerStaleNotice}
            </div>
          ) : null}
          <div className="sb-ledger-chains" aria-label="Kettenkosten">
            {roots.map((root) => {
              const openRoot = openRootId === root.id;
              const money = chainCost(root);
              const share = chainShare(root, topAbo);
              const providerText = root.providers.length ? root.providers.join(", ") : "—";
              return (
                <article key={root.id} className="sb-ledger-chain" title={ledgerDetailTitle(root)}>
                  <button type="button" className="sb-ledger-chain-head" onClick={() => toggleRoot(root.id)} aria-expanded={openRoot}>
                    <span className="sb-ledger-chain-main">
                      <b>{root.title ?? root.id}</b>
                      <small className="sb-mono">{root.id} · {fmtRuntime(root.runtime_seconds)} · {rootRuns(root)} Runs · {providerText}</small>
                    </span>
                    <span className="sb-ledger-chain-money">
                      <span className="sb-ledger-abo"><b className="sb-mono">{fmtMaybeUsd(money.abo)}</b><small>{de.stats.motherLedgerAboMarker}</small></span>
                      <span className={(money.echt ?? 0) > 0 ? "sb-ledger-real is-positive" : "sb-ledger-real"}>{de.stats.motherLedgerRealShort} {(money.echt ?? 0) > 0 ? fmtMaybeUsd(money.echt) : "—"}</span>
                    </span>
                  </button>
                  <div className="sb-ledger-meter" aria-hidden="true"><span style={{ width: `${Math.round(share * 100)}%` }} /></div>
                  {openRoot ? (
                    <div className="sb-ledger-workers" role="table" aria-label="Worker-Aufschlüsselung">
                      <div className="sb-ledger-workers-head" role="row">
                        <span>{de.stats.motherLedgerColWorker}</span><span>{de.stats.motherLedgerColTokens}</span><span>{de.stats.motherLedgerColAbo}</span><span>{de.stats.motherLedgerColReal}</span>
                      </div>
                      {root.workers.map((worker) => {
                        const key = `${root.id}:${worker.profile}`;
                        const openWorker = openWorkerKey === key;
                        const cost = workerCost(worker);
                        return (
                          <div key={key} className="sb-ledger-worker-block">
                            <button type="button" className="sb-ledger-worker-row" onClick={() => toggleWorker(root.id, worker.profile)} aria-expanded={openWorker} title={ledgerDetailTitle(worker)}>
                              <span className="sb-ledger-worker-label"><LaneLabel profile={worker.profile} /><small>{worker.model ?? worker.provider ?? "—"}</small></span>
                              <span className="sb-mono" data-label={de.stats.motherLedgerColTokens}>{fmtTokens(workerTokens(worker))}</span>
                              <span className="sb-mono sb-ledger-abo" data-label={de.stats.motherLedgerAboMobile}>{fmtMaybeUsd(cost.abo)}</span>
                              <span className={(cost.echt ?? 0) > 0 ? "sb-mono sb-ledger-real is-positive" : "sb-mono sb-ledger-real"} data-label={de.stats.motherLedgerRealMobile}>{(cost.echt ?? 0) > 0 ? fmtMaybeUsd(cost.echt) : "—"}</span>
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
    </>
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
  const subscriptionBurn = useHermesSubscriptionBurn();
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
    <>
      {/* A7: AccountUsageTile lives OUTSIDE BroadsheetShell so its dark hc-*
          tokens render in the dashboard context, not on the light paper sheet.
          This is the PRIMARY live cockpit — bottleneck callout, session+weekly
          bars, details collapse. */}
      <AccountUsageTile
        usage={accountUsage.data}
        loading={accountUsage.loading && !accountUsage.data}
        error={accountUsage.error}
        config={statsConfig.data ?? DEFAULT_STATS_CONFIG}
      />

      <BroadsheetShell>
        {hasLoadError ? (
          <EngpassLead tone="warn">
            <b>{de.stats.loadError}</b>
          </EngpassLead>
        ) : null}

        <StatsMasthead profiles={profiles} baseline={baseline} series={last7} now={now} stale={stale} />

        <LatencySection
          p50={summary.data?.cycle_time_p50_seconds ?? null}
          p90={summary.data?.cycle_time_p90_seconds ?? null}
        />

        <ReliabilitySection profiles={profiles} />

        <ErrorTaxonomySection issues={issueGroups} />

        {/* ── ST5: Budget-Ledger (Provider-Limits) + Flotten-Effizienz ───────── */}
        {/* A4: BudgetLedgerSection is a broadsheet-styled summary of the same
            source. Collapsed under a Details element so the primary cockpit
            (AccountUsageTile above) is unambiguously the operative view; the
            ledger format stays available for a printed/broadsheet read. */}
        <details style={{ marginTop: "8px" }}>
          <summary className="sb-kick sb-accent" style={{ cursor: "pointer", display: "flex", gap: "8px", alignItems: "center", listStyle: "none", minHeight: "44px" }}>
            {de.stats.secBudget} — {de.stats.budgetLedgerDetailLabel}
          </summary>
          <BudgetLedgerSection providers={providers} />
        </details>

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

        <BroadsheetFooter left={de.stats.footLeft(fmtClock(now))} right="/control/statistik" />
      </BroadsheetShell>
    </>
  );
}
