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
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { de } from "../i18n/de";
import { fmtClock, fmtClockTime, fmtDur, fmtTokens, nowSec, formatEffectiveCost } from "../lib/derive";
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
  ChainCostsResponse,
  CostProfileRow,
  IssueGroup,
  ReliabilityProfile,
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
  costPerDelivery,
  errorTaxonomy,
  gateEffectiveness,
  germanDate,
  laneBurn,
  leaderboard,
  nutzerwert,
  rosterProfiles,
  subscriptionBurnBreakdown,
  type LedgerEntry,
} from "../lib/statsBroadsheet";

const pctText = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)}`);
const usdText = (v: number | null) => (v == null ? "—" : `$ ${v.toFixed(2)}`);

const ERROR_LABEL: Record<string, string> = {
  dead: de.stats.errDead,
  timeout: de.stats.errTimeout,
  budget: de.stats.errBudget,
  other: de.stats.errOther,
};

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
            name={r.label}
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
            pct={r.usedPercent ?? 0}
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
  chainRate,
  queueWaitSeconds,
}: {
  profiles: ReliabilityProfile[];
  costs: CostProfileRow[];
  chainRate: number | null;
  queueWaitSeconds: number | null;
}) {
  const lanes = useMemo(() => laneBurn(costs), [costs]);
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
              name={l.label}
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
    </>
  );
}

export function SubscriptionBurnSection({ burn }: { burn: SubscriptionTokenBurnResponse | null }) {
  const detail = useMemo(() => subscriptionBurnBreakdown(burn), [burn]);
  const hasBurn = detail.totals.total_tokens > 0;
  return (
    <>
      <SectionRule title={de.stats.secSubscriptionBurn} meta={de.stats.secSubscriptionBurnMeta} />
      {!hasBurn ? (
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
                <div key={`${row.subscription}:${row.profile}`} className="sb-subburn-row">
                  <span>{row.profile} · {row.subscription}</span>
                  <i />
                  <b className="sb-mono">{fmtTokens(row.total_tokens)}</b>
                  <small className="sb-mono">{Math.round(row.share * 100)} %</small>
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

// ── Kosten pro Kette ─────────────────────────────────────────────────────────
// Detailtabelle für API-Antworten aus chain-costs; bleibt als kleine
// Anzeige-Hilfe für Tests/Diagnose, der Statistik-Tab nutzt das MotherLedger.

function fmtUsd(value: number | null | undefined): string {
  return value != null && value > 0 ? `$${value.toFixed(2)}` : "—";
}

function fmtKwh(value: number | null | undefined): string {
  return value != null && value > 0 ? value.toFixed(4) : "—";
}

function blendedRate(kwh: number, cost: number): number | null {
  return kwh > 0 ? cost / kwh : null;
}

export function ChainCostsLaneTable({ costs }: { costs: ChainCostsResponse }) {
  const { by_lane, totals } = costs;
  if (by_lane.length === 0) return null;
  const totalRate = blendedRate(totals.billing_neuralwatt_kwh, totals.billing_neuralwatt_cost_usd);
  return (
    <div className="mt-2 space-y-1">
      <p className="text-[10px] uppercase tracking-wider text-[var(--hc-text-dim)]">
        {de.ketten.chainCostsLaneTitle}
      </p>
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="border-b border-[var(--hc-border)] text-left text-[10px] text-[var(--hc-text-dim)]">
            <th className="py-1 pr-2 font-normal">Lane</th>
            <th className="py-1 pr-2 text-right font-normal">{de.ketten.chainCostsColTokens}</th>
            <th className="py-1 pr-2 text-right font-normal">{de.ketten.chainCostsColCost}</th>
            <th className="py-1 text-right font-normal">{de.ketten.chainCostsColRuns}</th>
          </tr>
        </thead>
        <tbody>
          {by_lane.map((l) => {
            const apiEquivalent = l.api_equivalent_usd;
            const rate = blendedRate(l.billing_neuralwatt_kwh, l.billing_neuralwatt_cost_usd);
            return (
              <tr key={l.profile} className="border-b border-[var(--hc-border)] last:border-0">
                <td className="hc-mono py-1 pr-2 text-[var(--hc-text)]">{l.profile}</td>
                <td className="hc-mono py-1 pr-2 text-right tabular-nums text-[var(--hc-text-soft)]">
                  {fmtTokens(l.input_tokens + l.output_tokens)}
                </td>
                <td className="py-1 pr-2 text-right tabular-nums">
                  <div className="hc-mono text-[var(--hc-text)]">{fmtUsd(l.actual_cost_usd)}</div>
                  {apiEquivalent > 0 ? (
                    <div className="hc-mono text-[10px] text-[var(--hc-text-dim)]">
                      {de.ketten.chainCostsApiEquivalent} {fmtUsd(apiEquivalent)}
                    </div>
                  ) : null}
                  {l.billing_neuralwatt_kwh > 0 && rate != null ? (
                    <div className="hc-mono text-[10px] text-[var(--hc-text-dim)]">
                      {de.ketten.chainCostsNeuralwattBasis} {fmtKwh(l.billing_neuralwatt_kwh)} kWh × ${rate.toFixed(2)}/kWh
                    </div>
                  ) : null}
                </td>
                <td className="hc-mono py-1 text-right tabular-nums text-[var(--hc-text-dim)]">{l.run_count}</td>
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr className="border-t-2 border-[var(--hc-border-strong)] text-xs font-semibold">
            <td className="py-1 pr-2 text-[var(--hc-text-dim)]">Gesamt</td>
            <td className="hc-mono py-1 pr-2 text-right tabular-nums text-[var(--hc-text-soft)]">
              {fmtTokens(totals.input_tokens + totals.output_tokens)}
            </td>
            <td className="py-1 pr-2 text-right tabular-nums">
              <div className="hc-mono text-[var(--hc-text)]">{fmtUsd(totals.actual_cost_usd)}</div>
              {totals.api_equivalent_usd > 0 ? (
                <div className="hc-mono text-[10px] text-[var(--hc-text-dim)]">
                  {de.ketten.chainCostsApiEquivalent} {fmtUsd(totals.api_equivalent_usd)}
                </div>
              ) : null}
              {totals.billing_neuralwatt_kwh > 0 && totalRate != null ? (
                <div className="hc-mono text-[10px] text-[var(--hc-text-dim)]">
                  {de.ketten.chainCostsNeuralwattBasis} {fmtKwh(totals.billing_neuralwatt_kwh)} kWh × ${totalRate.toFixed(2)}/kWh
                </div>
              ) : null}
            </td>
            <td className="hc-mono py-1 text-right tabular-nums text-[var(--hc-text-dim)]">{totals.run_count}</td>
          </tr>
        </tfoot>
      </table>
      {/* Legende: erklärt "gesch." und Abo-Semantik */}
      <p className="mt-2 text-[10px] leading-snug text-[var(--hc-text-dim)]">
        {de.ketten.chainCostsCostLegend}
      </p>
    </div>
  );
}

type MotherLedgerSortKey = "usd" | "tokens" | "runs";

function workerTokens(worker: WindowedRollupWorker): number {
  return worker.input_tokens + worker.output_tokens;
}

function rootRuns(root: WindowedRollupRoot): number {
  return root.workers.reduce((sum, worker) => sum + worker.run_count, 0);
}

function rootTokens(root: WindowedRollupRoot): number {
  return root.workers.reduce((sum, worker) => sum + workerTokens(worker), 0);
}

function rootUsd(root: WindowedRollupRoot): number {
  return root.cost_effective_usd ?? root.cost_usd ?? 0;
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

function estimateSuffix(provider: string | null | undefined, billingMode: string | null | undefined): string {
  const source = `${provider ?? ""} ${billingMode ?? ""}`.toLowerCase();
  return source.includes("openrouter") || source.includes("metered") ? " (gesch.)" : "";
}

function ledgerDetailTitle(item: {
  cost_usd?: number | null;
  cost_effective_usd?: number | null;
  provider?: string | null;
  providers?: string[];
  model?: string | null;
  billing_mode?: string | null;
  runtime_seconds?: number | null;
}): string {
  const provider = item.provider ?? item.providers?.join(", ") ?? null;
  const model = item.model ?? null;
  const billingMode = item.billing_mode ?? null;
  return [
    `USD effektiv: ${fmtUsd(item.cost_effective_usd ?? item.cost_usd)}`,
    `USD echt/metered: ${fmtUsd(item.cost_usd)}${estimateSuffix(provider, billingMode)}`,
    `Provider/Model: ${provider ?? "—"}${model ? ` · ${model}` : ""}`,
    `billing_mode: ${billingMode ?? "—"}`,
    `Laufzeit: ${fmtRuntime(item.runtime_seconds)}`,
    "Neuralwatt: —",
  ].join(" · ");
}

function sortedLedgerRoots(roots: WindowedRollupRoot[], sortKey: MotherLedgerSortKey): WindowedRollupRoot[] {
  return [...roots].sort((a, b) => {
    const byMetric = sortKey === "tokens"
      ? rootTokens(b) - rootTokens(a)
      : sortKey === "runs"
        ? rootRuns(b) - rootRuns(a)
        : rootUsd(b) - rootUsd(a);
    return byMetric || (b.completed_at ?? 0) - (a.completed_at ?? 0) || a.id.localeCompare(b.id);
  });
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
    return <div className="sb-ledger-runners sb-mono">keine Läuferdaten</div>;
  }
  return (
    <div className="sb-ledger-runners">
      {runners.map((runner) => (
        <div key={runner.id} className="sb-ledger-runner" title={ledgerDetailTitle(runner)}>
          <span className="sb-mono">#{runner.id}</span>
          <span>{runner.provider ?? "Provider n/a"}{runner.model ? ` · ${runner.model}` : ""}</span>
          <b className="sb-mono">{fmtTokens((runner.input_tokens ?? 0) + (runner.output_tokens ?? 0))}</b>
          <b className="sb-mono">{fmtUsd(runner.cost_effective_usd ?? runner.cost_usd)}{estimateSuffix(runner.provider, runner.billing_mode)}</b>
          <small>{runner.billing_mode ?? "—"} · {fmtRuntime(runner.runtime_seconds)} · Neuralwatt —</small>
        </div>
      ))}
    </div>
  );
}

export function MotherLedgerSection() {
  const [windowHours, setWindowHours] = useState<24 | 168>(168);
  const [sortKey, setSortKey] = useState<MotherLedgerSortKey>("usd");
  const [openKey, setOpenKey] = useState<string | null>(null);
  const isMobileLedger = useMediaQuery("(max-width: 760px)");
  const rollup = useHermesWindowedRollup({ hours: windowHours, limit: 20 });
  const roots = useMemo(
    () => sortedLedgerRoots(rollup.data?.roots ?? [], sortKey),
    [rollup.data, sortKey],
  );
  const totalUsd = roots.reduce((sum, root) => sum + rootUsd(root), 0);
  const showStaleNotice = Boolean((rollup.error || rollup.isStale) && rollup.data);
  const metaText = `${windowHours === 168 ? "7T" : "24Std"} · USD inkl. Cache · ${fmtUsd(totalUsd)}${showStaleNotice ? " · Letzte Daten angezeigt" : ""}`;
  const toggleWorker = (rootId: string, profile: string) => {
    const key = `${rootId}:${profile}`;
    setOpenKey((prev) => (prev === key ? null : key));
  };

  return (
    <>
      <SectionRule title="MotherLedger" meta={metaText} />
      <div className="sb-ledger-controls" aria-label="MotherLedger Controls">
        <div className="sb-chipset" aria-label="Fenster">
          <button type="button" className={windowHours === 168 ? "is-active" : ""} onClick={() => setWindowHours(168)}>7T</button>
          <button type="button" className={windowHours === 24 ? "is-active" : ""} onClick={() => setWindowHours(24)}>24Std</button>
        </div>
        <div className="sb-chipset" aria-label="Sortierung">
          <button type="button" className={sortKey === "usd" ? "is-active" : ""} onClick={() => setSortKey("usd")}>USD</button>
          <button type="button" className={sortKey === "tokens" ? "is-active" : ""} onClick={() => setSortKey("tokens")}>Tokens</button>
          <button type="button" className={sortKey === "runs" ? "is-active" : ""} onClick={() => setSortKey("runs")}>Runs</button>
        </div>
      </div>
      {rollup.loading && !rollup.data ? (
        <Verdict tone="calm">lädt …</Verdict>
      ) : rollup.error && !rollup.data ? (
        <Verdict tone="warn">{de.ketten.chainCostsLoadError}</Verdict>
      ) : roots.length === 0 ? (
        <Verdict tone="calm">{de.ketten.chainCostsEmpty}</Verdict>
      ) : (
        <div className="sb-ledger" data-ledger-viewport={isMobileLedger ? "mobile" : "desktop"}>
          {showStaleNotice ? (
            <div className="sb-kick" role="status" title={rollup.error ?? undefined}>
              Letzte Daten angezeigt
            </div>
          ) : null}
          <div className="sb-ledger-table" role="table" aria-label="MotherLedger Desktop" aria-hidden={isMobileLedger}>
            <div className="sb-ledger-head" role="row">
              <span>Mother</span><span>Worker</span><span>Runs</span><span>Tokens</span><span>USD <small>inkl. Cache</small></span>
            </div>
            {roots.map((root) => root.workers.map((worker, index) => {
              const key = `${root.id}:${worker.profile}`;
              const open = openKey === key;
              return (
                <div key={key} className="sb-ledger-pair">
                  <button type="button" className="sb-ledger-row" onClick={() => toggleWorker(root.id, worker.profile)} aria-expanded={open} title={index === 0 ? ledgerDetailTitle(root) : undefined}>
                    <span className="sb-ledger-mother">{index === 0 ? (root.title ?? root.id) : ""}</span>
                    <span>{worker.profile}</span>
                    <span className="sb-mono">{worker.run_count}</span>
                    <span className="sb-mono">{fmtTokens(workerTokens(worker))}</span>
                    <span className="sb-mono sb-ledger-usd"><b>{fmtUsd(worker.cost_effective_usd ?? worker.cost_usd)}</b><small>inkl. Cache</small></span>
                  </button>
                  {open ? <LedgerWorkerRunners root={root} worker={worker} /> : null}
                </div>
              );
            }))}
          </div>
          <div className="sb-ledger-cards" aria-label="MotherLedger Mobile" aria-hidden={!isMobileLedger}>
            {roots.map((root) => (
              <article key={root.id} className="sb-ledger-card">
                <div className="sb-kick">Mother</div>
                <h3>{root.title ?? root.id}</h3>
                {root.workers.map((worker) => {
                  const key = `${root.id}:${worker.profile}`;
                  const open = openKey === key;
                  return (
                    <div key={key} className="sb-ledger-worker-card">
                      <button type="button" onClick={() => toggleWorker(root.id, worker.profile)} aria-expanded={open}>
                        <span>{worker.profile}</span>
                        <b className="sb-mono">{fmtUsd(worker.cost_effective_usd ?? worker.cost_usd)}</b>
                        <small>Runs {worker.run_count} · {fmtTokens(workerTokens(worker))} Tokens · USD inkl. Cache</small>
                      </button>
                      {open ? <LedgerWorkerRunners root={root} worker={worker} /> : null}
                    </div>
                  );
                })}
              </article>
            ))}
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

  const stale = reliability.isStale || daily.isStale;
  const hasLoadError = Boolean(reliability.error || daily.error);

  return (
    <BroadsheetShell>
      {hasLoadError ? (
        <EngpassLead tone="warn">
          <b>{de.stats.loadError}</b>
        </EngpassLead>
      ) : null}

      <StatsMasthead profiles={profiles} baseline={baseline} series={last7} now={now} stale={stale} />

      {/* Abo-Limits-Cockpit — Provider-Tokenverbrauch stays first-class in /control/statistik. */}
      <AccountUsageTile
        usage={accountUsage.data}
        loading={accountUsage.loading && !accountUsage.data}
        error={accountUsage.error}
        config={statsConfig.data ?? DEFAULT_STATS_CONFIG}
      />

      <LatencySection
        p50={summary.data?.cycle_time_p50_seconds ?? null}
        p90={summary.data?.cycle_time_p90_seconds ?? null}
      />

      <ReliabilitySection profiles={profiles} />

      <ErrorTaxonomySection issues={issueGroups} />

      {/* ── ST5: Budget-Ledger (Provider-Limits) + Flotten-Effizienz ───────── */}
      <BudgetLedgerSection providers={providers} />

      <SubscriptionBurnSection burn={subscriptionBurn.data ?? null} />

      <EffizienzSection
        profiles={profiles}
        costs={costProfiles}
        chainRate={chain.data?.chain_completion_rate ?? null}
        queueWaitSeconds={board.data?.queue_wait_p50_seconds ?? null}
      />

      {/* ── Kosten pro Kette ────────────────────────────────────────────────── */}
      <MotherLedgerSection />

      <BroadsheetFooter left={de.stats.footLeft(fmtClock(now))} right="/control/statistik" />
    </BroadsheetShell>
  );
}
