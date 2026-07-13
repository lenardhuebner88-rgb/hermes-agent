/**
 * Heute-Subtab (Lagezeile + KPI-Panel + Worker-Karten + PlanSpec-Karten).
 *
 * Aus FleetView.tsx extrahiert — reine Zerlegung, kein Verhalten geändert.
 * Enthält die Heute-lokalen Präsentationsbausteine (Lagezeile-Formatter,
 * Worker-Karte, PlanSpec-Karte, Fertig-24h-Sparkline).
 */
import { useMemo, useState } from "react";
import { ArrowRight } from "lucide-react";
import type { DotKind } from "../../lib/tones";
import {
  buildLagezeile,
  runProgressFraction,
  heartbeatAge,
  fmtSeconds,
  deriveKpi,
  fmtTokens,
  planSpecHasParkedSignedChain,
  planSpecWaitsForOperator,
  profileInitial,
  profileColorClass,
  premiumLaneMarker,
  deriveSparklinePoints,
  type SparklinePoint,
  type PlanSpecActionState,
  type PendingItem,
} from "../../lib/fleetHub";
import { de } from "../../i18n/de";
import type { Worker } from "../../lib/types";
import type { CostBucket, CostProfileRow, RunsCostsResponse, RunsDailyResponse } from "../../lib/schemas";
import type { PlanSpecRecord } from "./shared";
import { LaneQuickSwitch } from "./LaneQuickSwitch";
import { SignalChip, type SignalTone } from "../../components/leitstand";
import { Led } from "../../components/atoms";
import { profileLabel } from "../../lib/tones";
import { elapsedSeconds } from "../../lib/derive";
import { BoardBadge } from "../../components/fleet/BoardIdentity";

/** Ziel-Subtabs, zu denen der Heute-Handlungsblock und Karten navigieren. */
type HeuteNavTarget = "worker" | "plan" | "risiko";

// ─── Heute-Subtab ────────────────────────────────────────────────────────────

interface HeuteTabProps {
  allWorkers: Worker[];
  activeWorkers: Worker[];
  blockedCount: number;
  pendingApprovals: number;
  allPlanspecs: PlanSpecRecord[];
  costs: RunsCostsResponse | null;
  daily: RunsDailyResponse | null;
  now: number;
  /** Wartende Freigaben + Operator-Halts (aus FleetView), für den Handlungsblock. */
  pendingItems: PendingItem[];
  onWorkerClick: (w: Worker) => void;
  onPlanSpecClick: (ps: PlanSpecRecord) => void;
  onNavigate: (target: HeuteNavTarget) => void;
}

export function HeuteTab({ allWorkers, activeWorkers, blockedCount, pendingApprovals, allPlanspecs, costs, daily, now, pendingItems, onWorkerClick, onPlanSpecClick, onNavigate }: HeuteTabProps) {
  const [costDrawerOpen, setCostDrawerOpen] = useState(false);
  const lagezeile = buildLagezeile({ workers: allWorkers, blockedCount, pendingApprovals });
  const kpi = deriveKpi(
    allWorkers,
    blockedCount,
    costs?.today.actual_cost_usd ?? null,
    costs?.today.runs ?? null,
    costs?.today.cost_usd_equivalent ?? null,
  );
  // 7-Tage-Sparkline aus der bestehenden runs/daily-Serie (kein neuer Endpoint).
  // Liefert null bei <2 Punkten → keine Sparkline (kein Fake, keine Platzhalter).
  const sparklinePts = useMemo(() => deriveSparklinePoints(daily), [daily]);
  const activeProfileBreakdown = useMemo(() => formatActiveProfileBreakdown(activeWorkers), [activeWorkers]);
  const costAverageDimension = useMemo(
    () => formatCostAverageDimension(costs, kpi.kosten24hEquiv),
    [costs, kpi.kosten24hEquiv],
  );
  const showActiveKpi = kpi.aktiv > 0 && activeProfileBreakdown !== null;
  const showCostKpi = kpi.kosten24h != null && costAverageDimension !== null;
  const kpiTileCount = 2 + Number(showActiveKpi) + Number(showCostKpi);

  // Handlungsblock (AC-1/AC-5): wartende Freigaben + Operator-Halts als
  // antippbare Zeilen, plus eine Sammelzeile für sonst blockierte Aufgaben.
  const actionRows = useMemo(
    () => buildActionRows(pendingItems, blockedCount),
    [pendingItems, blockedCount],
  );

  // PlanSpecs nach aktueller Relevanz statt beliebiger erster fünf (AC-6).
  const rankedPlanspecs = useMemo(() => rankPlanSpecsByRelevance(allPlanspecs).slice(0, 5), [allPlanspecs]);

  // Operativer Lage-Hero (AC-R1): Blockade dominiert, sonst ruhiger Zustand.
  const hero = deriveHeroState(blockedCount, actionRows, activeWorkers.length);

  return (
    <>
      {/* Operativer Lage-Hero — die aktuelle Lage ist der visuelle Einstieg.
          Blockiert = klarer Incident-Hero mit Aktion; sonst ruhiger. */}
      <section className={`fleet-hero fleet-hero-${hero.tone}`} aria-label="Aktuelle Lage">
        <div className="fleet-hero-main">
          <span className="fleet-hero-eyebrow">
            {hero.tone !== "idle" ? <Led kind={hero.led} size={7} /> : null}
            {hero.eyebrow}
          </span>
          <h2 className="fleet-hero-headline">
            <LagezeileFormatted text={lagezeile} />
          </h2>
        </div>
        {hero.cta ? (
          <button
            type="button"
            className={`fleet-hero-cta${hero.cta.ghost ? " fleet-hero-cta-ghost" : ""}`}
            onClick={() => onNavigate(hero.cta!.target)}
          >
            {hero.cta.label}
            <ArrowRight className="h-4 w-4 shrink-0" aria-hidden="true" />
          </button>
        ) : null}
      </section>

      {/* 1. Handlungsbedarf/Blocker — antippbare Detailzeilen unter dem Hero.
          Kein leerer Platzhalter, wenn nichts wartet. */}
      {actionRows.length > 0 ? (
        <div className="fleet-actionlist" aria-label="Handlungsbedarf">
          {actionRows.map((row, index) => (
            <button
              key={row.key}
              type="button"
              className="fleet-actionrow"
              onClick={() => onNavigate(row.target)}
              aria-label={row.label}
              // Primärer Handlungs-Callout wird — wie zuvor die globale PendingBar —
              // höflich angekündigt; nur die erste Zeile ist die Live-Region.
              aria-live={index === 0 ? "polite" : undefined}
            >
              <Led kind="warn" />
              <span className="fleet-actionrow-label">{row.label}</span>
              <ArrowRight className="h-4 w-4 shrink-0 opacity-70" aria-hidden="true" />
            </button>
          ))}
        </div>
      ) : null}

      {/* 2. Aktive Worker und laufende Arbeit — vor den KPIs und PlanSpecs.
          Bei null aktiven Workern ein kompakter Idle-Zustand statt Leere. */}
      <p className="fleet-section-eyebrow">Jetzt</p>
      {activeWorkers.length === 0 ? (
        <div className="fleet-idle">Keine Worker aktiv — Board ruht.</div>
      ) : (
        activeWorkers.map((w) => (
          <WorkerCard key={`${w.board_slug ?? "current"}:${w.run_id}`} worker={w} now={now} onClick={() => onWorkerClick(w)} />
        ))
      )}

      {/* 3. KPI-Panel — höchstens vier belastbare Tages-KPIs. */}
      <div className="fleet-kpanel" style={{ "--fleet-kp-count": kpiTileCount } as React.CSSProperties}>
        {showActiveKpi ? (
          <div className="fleet-kp fleet-kp-aktiv">
            <div className="fleet-kp-num">{kpi.aktiv}</div>
            <div className="fleet-kp-label">{de.fleet.kpiAktiv}</div>
            <div className="fleet-kp-dim" title={activeProfileBreakdown}>{activeProfileBreakdown}</div>
          </div>
        ) : null}
        <div className="fleet-kp">
          <div className="fleet-kp-num">{kpi.blockiert}</div>
          <div className="fleet-kp-label">{de.fleet.kpiBlockiert}</div>
        </div>
        <div className="fleet-kp">
          <div className="fleet-kp-num">{kpi.fertig24h ?? "—"}</div>
          <div className="fleet-kp-label">{de.fleet.kpiFertig}</div>
          {sparklinePts && <FleetSparkline points={sparklinePts} />}
        </div>
        {showCostKpi ? (
          <button
            type="button"
            className="fleet-kp fleet-kp-button"
            onClick={() => setCostDrawerOpen(true)}
            aria-label="Kosten-Details öffnen"
          >
            <div className="fleet-kp-num">
              {kpi.kosten24h!.toFixed(1).replace(".", ",")}
              <small>$</small>
              {kpi.kosten24hEquiv ? <small> äquiv.</small> : null}
            </div>
            <div className="fleet-kp-label">{de.fleet.kpiKosten}</div>
            <div className="fleet-kp-dim" title={costAverageDimension}>{costAverageDimension}</div>
          </button>
        ) : null}
      </div>

      {costDrawerOpen ? (
        <CostDrawer costs={costs} daily={daily} onClose={() => setCostDrawerOpen(false)} />
      ) : null}

      {/* 4. Relevante offene/laufende PlanSpecs — nach Relevanz, nicht Reihenfolge.
          Planung ist visuell sekundär zur laufenden Arbeit (AC-4). */}
      {rankedPlanspecs.length > 0 ? (
        <>
          <p className="fleet-section-eyebrow fleet-section-eyebrow-plan">Planung</p>
          {rankedPlanspecs.map((ps) => (
            <PlanSpecCard key={ps.path} ps={ps} onClick={() => onPlanSpecClick(ps)} />
          ))}
        </>
      ) : null}

      {/* 5. Sekundäre Lane-/Modellkonfiguration — zuletzt, initial eingeklappt.
          Stabiler Anker: Der Disclosure hält seinen lokalen open/aria-expanded-Zustand
          nur, solange React diesen Knoten über Live-Poll-Rerender hinweg wiederverwendet.
          Der explizite Key macht die Reconciliation key- statt index-basiert, sodass ein
          Einfügen/Entfernen konditionaler Geschwister davor (Worker-Karten, PlanSpecs,
          Handlungszeilen) den Knoten selbst dann nicht remountet, wenn sich die Slot-Zahl
          durch spätere Umbauten ändert. Invariante ist per Regressionstest gesichert
          (HeuteTab.test.tsx → "Disclosure-Stabilität bei Live-Polling"). */}
      <LaneQuickSwitch key="lane-config" />
    </>
  );
}

// ─── Operativer Lage-Hero (AC-R1) ─────────────────────────────────────────────

type HeroTone = "alert" | "warn" | "live" | "idle";

interface HeroState {
  tone: HeroTone;
  led: DotKind;
  eyebrow: string;
  cta: { label: string; target: HeuteNavTarget; ghost: boolean } | null;
}

/**
 * deriveHeroState: übersetzt die Flottenlage in einen dominanten Hero-Zustand.
 * Blockade schlägt wartende Freigabe schlägt laufende Arbeit schlägt Ruhe.
 * Nur aus Live-Daten (blockierte Aufgaben, Handlungszeilen, aktive Worker) —
 * keine erfundene Kennzahl. Die eindeutige Aktion zeigt in den Ziel-Subtab.
 */
function deriveHeroState(blockedCount: number, actionRows: ActionRow[], activeCount: number): HeroState {
  if (blockedCount > 0) {
    return {
      tone: "alert",
      led: "error",
      eyebrow: "Blockade",
      cta: { label: "Im Risiko-Tab lösen", target: "risiko", ghost: false },
    };
  }
  const approval = actionRows.find((row) => row.target === "plan");
  if (approval) {
    return {
      tone: "warn",
      led: "warn",
      eyebrow: "Wartet auf dich",
      cta: { label: "Im Plan-Tab freigeben", target: "plan", ghost: false },
    };
  }
  if (activeCount > 0) {
    return {
      tone: "live",
      led: "live",
      eyebrow: "Aktuelle Lage",
      cta: { label: "Worker ansehen", target: "worker", ghost: true },
    };
  }
  return { tone: "idle", led: "idle", eyebrow: "Aktuelle Lage", cta: null };
}

// ─── Handlungsblock-Ableitung ─────────────────────────────────────────────────

interface ActionRow {
  key: string;
  label: string;
  target: HeuteNavTarget;
}

/**
 * buildActionRows: wartende Freigaben + Operator-Halts als antippbare Zeilen,
 * plus eine Sammelzeile für sonst blockierte Aufgaben (ohne Doppelzählung der
 * bereits als Operator-Halt gelisteten). Rein aus Live-Daten — kein Fake-CTA.
 */
function buildActionRows(pendingItems: PendingItem[], blockedCount: number): ActionRow[] {
  const rows: ActionRow[] = pendingItems.map((item, index) => ({
    key: `pi-${index}`,
    label: item.kind === "approval" ? `Freigabe wartet: ${item.topic}` : `Operator-Halt: ${item.topic}`,
    target: item.targetSubtab,
  }));
  const holdCount = pendingItems.filter((item) => item.kind === "blocked").length;
  const otherBlocked = blockedCount - holdCount;
  if (otherBlocked > 0) {
    rows.push({
      key: "blocked-rest",
      label: otherBlocked === 1
        ? "Eine Aufgabe blockiert — im Risiko-Tab lösen"
        : `${otherBlocked} Aufgaben blockiert — im Risiko-Tab lösen`,
      target: "risiko",
    });
  }
  return rows;
}

// ─── PlanSpec-Relevanz-Ranking (AC-6) ─────────────────────────────────────────

/**
 * rankPlanSpecsByRelevance: sortiert PlanSpecs nach aktueller Handlungsrelevanz
 * statt Katalog-Reihenfolge. Reine Funktion über die vorhandenen kanban-Felder
 * (keine neue API): wartet-auf-Operator > startbare signierte Kette > laufend >
 * eingereiht > sonst offen. Sekundär: mehr aktive/blockierte Kinder zuerst.
 * Stabil (behält Eingangsreihenfolge bei Gleichstand).
 */
function planSpecRelevanceRank(ps: PlanSpecRecord): number {
  const state: PlanSpecActionState = {
    freigabe: ps.freigabe,
    kanban_state: ps.kanban_state,
    kanban_root_status: ps.kanban_root_status,
    kanban_root_task_id: ps.kanban_root_task_id,
    kanban_child_total: ps.kanban_child_total,
    kanban_child_done: ps.kanban_child_done,
    kanban_child_running: ps.kanban_child_running,
    kanban_child_blocked: ps.kanban_child_blocked,
  };
  if (planSpecWaitsForOperator(ps.freigabe, ps.kanban_state)) return 5;
  if (planSpecHasParkedSignedChain(state)) return 4;
  const kanbanState = String(ps.kanban_state ?? "").toLowerCase();
  if (kanbanState === "running" || (ps.kanban_child_running ?? 0) > 0) return 3;
  if (kanbanState === "queued") return 2;
  if (ps.open) return 1;
  return 0;
}

function rankPlanSpecsByRelevance(planspecs: PlanSpecRecord[]): PlanSpecRecord[] {
  return planspecs
    .map((ps, index) => ({
      ps,
      index,
      rank: planSpecRelevanceRank(ps),
      activity: (ps.kanban_child_running ?? 0) + (ps.kanban_child_blocked ?? 0),
    }))
    .sort((a, b) => b.rank - a.rank || b.activity - a.activity || a.index - b.index)
    .map((entry) => entry.ps);
}

function CostDrawer({ costs, daily, onClose }: { costs: RunsCostsResponse | null; daily: RunsDailyResponse | null; onClose: () => void }) {
  const trendPoints = daily?.series.slice(-7) ?? [];

  return (
    <div className="fleet-cost-scrim" role="presentation" onClick={onClose}>
      <section
        className="fleet-cost-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="Kosten-Details"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="fleet-cost-head">
          <div>
            <p className="fleet-cost-eyebrow">GET /runs/costs · 7 Tage</p>
            <h2>Kosten-Details</h2>
          </div>
          <button type="button" className="fleet-cost-close" onClick={onClose} aria-label="Kosten-Details schließen">×</button>
        </div>

        {costs ? (
          <>
            <div className="fleet-cost-buckets">
              <CostBucketCard title="Kosten heute" bucket={costs.today} />
              <CostBucketCard title={`Kosten ${costs.days} Tage`} bucket={costs.window} />
            </div>

            <div className="fleet-cost-note">
              $0 ist bei Abo-Lanes Grenzpreis, nicht kostenlos — Tokenverbrauch und API-Äquivalent zeigen den Verbrauch.
            </div>

            {trendPoints.length > 0 ? <CostTrend points={trendPoints} /> : null}
            <CostProfileTable profiles={costs.profiles} />
          </>
        ) : (
          <p className="fleet-cost-empty">Noch keine Kostendaten geladen.</p>
        )}
      </section>
    </div>
  );
}

function CostBucketCard({ title, bucket }: { title: string; bucket: CostBucket }) {
  return (
    <div className="fleet-cost-bucket">
      <h3>{title}</h3>
      <div className="fleet-cost-values">
        <span>Ist: {fmtUsd(bucket.actual_cost_usd)}</span>
        <span>≈ API: {fmtUsd(bucket.api_equivalent_usd)}</span>
      </div>
      <div className="fleet-cost-meta">
        <span>{bucket.runs} Runs</span>
        <span>{fmtTokens(sumTokens(bucket.input_tokens, bucket.output_tokens))} Token</span>
      </div>
    </div>
  );
}

function CostTrend({ points }: { points: RunsDailyResponse["series"] }) {
  const maxTokens = Math.max(...points.map((p) => sumTokens(p.input_tokens, p.output_tokens)), 1);
  const maxCost = Math.max(...points.map((p) => p.cost_usd ?? 0), 0);
  const linePoints = points.map((point, index) => {
    const x = points.length === 1 ? 50 : (index / (points.length - 1)) * 100;
    const y = maxCost > 0 ? 100 - (((point.cost_usd ?? 0) / maxCost) * 100) : 100;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  return (
    <div className="fleet-cost-trend" aria-label="7-Tage-Trend Tokenbalken und Dollar-Linie">
      <div className="fleet-cost-trend-title">7-Tage-Trend · Balken = Token, Linie = $ Ist-Kosten</div>
      <div className="fleet-cost-bars">
        <svg className="fleet-cost-line" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
          <polyline points={linePoints.join(" ")} fill="none" />
        </svg>
        {points.map((point) => {
          const tokens = sumTokens(point.input_tokens, point.output_tokens);
          const costUsd = point.cost_usd ?? 0;
          const tokenPct = Math.max(6, Math.round((tokens / maxTokens) * 100));
          const costPct = maxCost > 0 ? Math.round((costUsd / maxCost) * 100) : 0;
          return (
            <div key={point.date} className="fleet-cost-day" title={`${point.date}: ${fmtTokens(tokens)} Token · Ist ${fmtUsd(costUsd)}`}>
              <span className="fleet-cost-line-dot" style={{ bottom: `${costPct}%` }} />
              <span className="fleet-cost-bar" style={{ height: `${tokenPct}%` }} />
              <small>{point.date.slice(5).replace("-", ".")}</small>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function CostProfileTable({ profiles }: { profiles: CostProfileRow[] }) {
  return (
    <div className="fleet-cost-table-wrap">
      <table className="fleet-cost-table">
        <thead>
          <tr>
            <th>Lane</th>
            <th>Abo</th>
            <th>Runs</th>
            <th>Ist</th>
            <th>API-Äquiv.</th>
          </tr>
        </thead>
        <tbody>
          {profiles.map((profile) => (
            <tr key={`${profile.profile}:${profile.subscription ?? "none"}`}>
              <td>{profile.profile}</td>
              <td><span className="fleet-cost-subscription">{formatSubscription(profile.subscription)}</span></td>
              <td className="font-data tabular-nums">{profile.runs}</td>
              <td className="font-data tabular-nums">{fmtUsd(profile.actual_cost_usd)}</td>
              <td className="font-data tabular-nums">{fmtUsd(profile.api_equivalent_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function fmtUsd(value: number | null | undefined) {
  return `$${(value ?? 0).toFixed(2)}`;
}

function sumTokens(input: number | null | undefined, output: number | null | undefined) {
  return (input ?? 0) + (output ?? 0);
}

function formatSubscription(subscription: string | null | undefined) {
  if (subscription === "chatgpt") return "ChatGPT/Codex";
  if (subscription === "claude") return "Claude";
  if (subscription === "api") return "API";
  return subscription ?? "—";
}

function formatActiveProfileBreakdown(activeWorkers: Worker[]): string | null {
  if (activeWorkers.length === 0) return null;
  const counts = new Map<string, number>();
  for (const worker of activeWorkers) {
    const label = profileLabel[worker.profile] ?? worker.profile;
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort(([labelA, countA], [labelB, countB]) => countB - countA || labelA.localeCompare(labelB, "de"))
    .map(([label, count]) => `${label} ${count}`)
    .join(" · ");
}

function formatCostAverageDimension(costs: RunsCostsResponse | null, equivalent: boolean): string | null {
  if (!costs || costs.days <= 0) return null;
  const windowCost = equivalent ? costs.window.cost_usd_equivalent : costs.window.actual_cost_usd;
  if (windowCost == null) return null;
  const average = (windowCost / costs.days).toFixed(1).replace(".", ",");
  return `Ø ${costs.days}T ${average}$${equivalent ? " äquiv." : ""}`;
}

// ─── Lagezeile-Formatter ─────────────────────────────────────────────────────

function LagezeileFormatted({ text }: { text: string }) {
  // Einfaches highlighting: "Freigabe" in amber, "wartet" in puls (em)
  // Wir teilen auf " — " und formatieren den letzten Teil hervor wenn Freigabe.
  const parts = text.split(" — ");
  if (parts.length <= 1) return <>{text}</>;
  return (
    <>
      {parts[0]}
      {parts.slice(1).map((part, i) => {
        const isApproval = part.toLowerCase().includes("freigabe") || part.toLowerCase().includes("warten");
        return (
          <span key={i}>
            {" — "}
            {isApproval ? <span className="fleet-amber">{part}</span> : <em>{part}</em>}
          </span>
        );
      })}
    </>
  );
}

// ─── Worker-Karte ────────────────────────────────────────────────────────────

function WorkerCard({ worker: w, now, onClick }: { worker: Worker; now: number; onClick: () => void }) {
  const hbAge = heartbeatAge(w.last_heartbeat_at, now);
  const fraction = runProgressFraction(w, now);
  const isEstimated = w.run_progress == null && fraction != null;
  const elapsedSec = elapsedSeconds(w.started_at, now) ?? Number.NaN;
  const initial = profileInitial(w.profile);
  const colorCls = profileColorClass(w.profile);
  const isLive = w.run_status === "running";

  return (
    <button
      type="button"
      className={`fleet-wk text-left${isLive ? " fleet-wk-lebt" : ""}`}
      onClick={onClick}
      aria-label={`Worker ${w.profile} öffnen`}
    >
      {/* Top-Zeile: Avatar + Name + LED */}
      <div className="fleet-wk-top">
        <div className={`fleet-avatar ${colorCls}`} {...premiumLaneMarker(w.profile)}>{initial}</div>
        <div className="fleet-wk-name">
          {w.profile}
          <span>{w.task_id.slice(0, 10)}</span>
        </div>
        <BoardBadge slug={w.board_slug} />
        {isLive && hbAge != null ? (
          <div className="fleet-led">
            <span className="fleet-led-dot" />
            ♥ {fmtSeconds(hbAge)}
          </div>
        ) : null}
      </div>

      {/* Task-Titel */}
      <div className="fleet-wk-task" title={w.task_title}>{w.task_title}</div>

      {/* Heartbeat-Notiz */}
      {w.last_heartbeat_note ? (
        <div className="fleet-wk-note" title={w.last_heartbeat_note}>{w.last_heartbeat_note}</div>
      ) : null}

      {/* Progress-Rail — S2: run_progress wenn vorhanden, sonst ETA-Heuristik (~) */}
      {fraction != null ? (
        <div className="fleet-rail" title={isEstimated ? "Fortschritt geschätzt (ETA-Heuristik)" : "Fortschritt (Runtime-Cap)"}>
          <div className="fleet-rail-fill" style={{ width: `${Math.round(fraction * 100)}%` }} />
        </div>
      ) : null}

      {/* Meta-Zeile */}
      <div className="fleet-wk-meta">
        {w.effective_model ? <b>{w.effective_model.replace(/^claude-/, "").split("-").slice(0, 1).join("")}</b> : null}
        <span>{fmtTokens(w.input_tokens)} → {fmtTokens(w.output_tokens)} tok</span>
        <span>seit {fmtSeconds(elapsedSec)}</span>
        {w.eta_p50_seconds ? (
          <span className="fleet-meta-right">ETA ~{fmtSeconds(Number.isFinite(elapsedSec) ? Math.max(0, w.eta_p50_seconds - elapsedSec) : Number.NaN)}</span>
        ) : null}
      </div>
    </button>
  );
}

// ─── PlanSpec-Karte ───────────────────────────────────────────────────────────

function PlanSpecCard({ ps, onClick }: { ps: PlanSpecRecord; onClick: () => void }) {
  const fraction = ps.kanban_child_total > 0 ? ps.kanban_child_done / ps.kanban_child_total : null;
  const waitsForOp = planSpecWaitsForOperator(ps.freigabe, ps.kanban_state);
  const isSignedParkedChain = planSpecHasParkedSignedChain(ps);
  const isRunning = ps.kanban_state === "running";

  let badgeTone = planSpecStatusTone(ps.status, ps.kanban_state);
  let badgeLabel = ps.status;
  if (waitsForOp) {
    badgeTone = "warn";
    badgeLabel = de.fleet.psWaitsForOperator;
  } else if (isSignedParkedChain) {
    badgeTone = "ok";
    badgeLabel = de.fleet.planKetteStarten;
  } else if (isRunning) {
    badgeTone = "ok";
    badgeLabel = `läuft${ps.kanban_child_total > 0 ? ` · ${ps.kanban_child_done}/${ps.kanban_child_total}` : ""}`;
  }

  return (
    <button type="button" className="fleet-ps" onClick={onClick}>
      <div className="fleet-ps-top">
        <span className="fleet-ps-name" title={ps.topic || ps.filename}>{ps.topic || ps.filename}</span>
        <SignalChip
          tone={badgeTone}
          label={badgeLabel}
          title={badgeLabel}
          className="ml-auto min-w-0 max-w-[min(52%,28rem)] shrink overflow-hidden"
        />
      </div>
      {fraction != null ? (
        <div className="fleet-rail">
          <div className="fleet-rail-fill" style={{ width: `${Math.round(fraction * 100)}%` }} />
        </div>
      ) : null}
      <div className="fleet-ps-meta">
        {ps.kanban_child_total > 0 ? (
          <span><b>{ps.kanban_child_done}</b>/{ps.kanban_child_total} Karten</span>
        ) : null}
        <span>{ps.freigabe}</span>
        {ps.live_test_depth ? <span>{ps.live_test_depth}</span> : null}
      </div>
    </button>
  );
}

const PLAN_SPEC_STATUS_TONE: Record<string, SignalTone> = {
  blocked: "alert",
  failed: "alert",
  error: "alert",
  review: "warn",
  waiting: "warn",
  running: "ok",
  completed: "ok",
  done: "ok",
  shipped: "ok",
  deferred: "neutral",
  superseded: "neutral",
  archived: "neutral",
  obsolete: "neutral",
  closed: "neutral",
  open: "neutral",
  ready: "neutral",
  not_ingested: "neutral",
  queued: "neutral",
  unknown: "neutral",
};

function planSpecStatusTone(status: string, kanbanState: string): SignalTone {
  return PLAN_SPEC_STATUS_TONE[status] ?? PLAN_SPEC_STATUS_TONE[kanbanState] ?? "neutral";
}

// ─── FleetSparkline (Fertig-24h 7-Tage-Trend) ─────────────────────────────────
//
// Pure presentational SVG: nimmt SparklinePoint[] aus deriveSparklinePoints()
// und zeichnet eine kleine Polyline. Keine eigene Datenquelle, kein Fetch.
// Bei <2 Punkten wird null geliefert (Caller rendert dann nichts).

interface FleetSparklineProps {
  points: SparklinePoint[];
}

const SPARK_W = 64;
const SPARK_H = 18;
const SPARK_PAD = 2;

function FleetSparkline({ points }: FleetSparklineProps) {
  const n = points.length;
  if (n < 2) return null;

  const values = points.map((p) => p.value);
  const max = Math.max(...values);
  const min = Math.min(...values);
  const span = max - min;
  // Vermeide Division durch 0: wenn alle Werte gleich, horizontale Mittellinie.
  const range = span === 0 ? 1 : span;

  const innerW = SPARK_W - SPARK_PAD * 2;
  const innerH = SPARK_H - SPARK_PAD * 2;

  const coords = points.map((p, i) => {
    const x = SPARK_PAD + (n === 1 ? innerW / 2 : (i / (n - 1)) * innerW);
    // Y invertieren: höherer Wert = weiter oben. min→unten, max→oben.
    const y = SPARK_PAD + innerH - ((p.value - min) / range) * innerH;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  const last = points[n - 1];
  const lastValue = last.value;
  const lastDate = last.date;

  return (
    <svg
      className="fleet-spark"
      width={SPARK_W}
      height={SPARK_H}
      viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`7-Tage-Trend: ${lastValue} erledigt am ${lastDate}`}
    >
      <title>{`Fertig 24h · 7-Tage-Trend (jüngster: ${lastValue} am ${lastDate})`}</title>
      <polyline
        className="fleet-spark-line"
        points={coords.join(" ")}
        fill="none"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle
        className="fleet-spark-dot"
        cx={coords[n - 1].split(",")[0]}
        cy={coords[n - 1].split(",")[1]}
        r={1.4}
      />
    </svg>
  );
}
