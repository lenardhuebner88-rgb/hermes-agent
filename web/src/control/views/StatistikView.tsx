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
import { useMemo, type ReactNode } from "react";
import { de } from "../i18n/de";
import { fmtClock, fmtClockTime, fmtDur, fmtTokens, nowSec } from "../lib/derive";
import {
  useAccountUsage,
  useBoardStats,
  useChainCompletion,
  useHermesReliability,
  useHermesRunsCosts,
  useHermesRunsDaily,
  useHermesRunsIssues,
  useHermesRunSummary,
} from "../hooks/useControlData";
import type {
  AccountUsageProvider,
  CostProfileRow,
  IssueGroup,
  ReliabilityProfile,
  RunsDailyPoint,
} from "../lib/schemas";
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
        lanes.map((l, i) => (
          <LeaderRow
            key={l.profile}
            rank={i + 1}
            name={l.label}
            score={fmtTokens(l.tokens)}
            status="neutral"
            latency={de.stats.leaderRuns(l.runs)}
          />
        ))
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
  const costs = useHermesRunsCosts();
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

      <LatencySection
        p50={summary.data?.cycle_time_p50_seconds ?? null}
        p90={summary.data?.cycle_time_p90_seconds ?? null}
      />

      <ReliabilitySection profiles={profiles} />

      <ErrorTaxonomySection issues={issueGroups} />

      {/* ── ST5: Budget-Ledger (Provider-Limits) + Flotten-Effizienz ───────── */}
      <BudgetLedgerSection providers={providers} />

      <EffizienzSection
        profiles={profiles}
        costs={costProfiles}
        chainRate={chain.data?.chain_completion_rate ?? null}
        queueWaitSeconds={board.data?.queue_wait_p50_seconds ?? null}
      />

      <BroadsheetFooter left={de.stats.footLeft(fmtClock(now))} right="/control/statistik" />
    </BroadsheetShell>
  );
}
