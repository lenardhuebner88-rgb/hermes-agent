/**
 * Heute-Subtab (Lagezeile + KPI-Panel + Worker-Karten + PlanSpec-Karten).
 *
 * Aus FleetView.tsx extrahiert — reine Zerlegung, kein Verhalten geändert.
 * Enthält die Heute-lokalen Präsentationsbausteine (Lagezeile-Formatter,
 * Worker-Karte, PlanSpec-Karte, Fertig-24h-Sparkline).
 */
import { useMemo, useState } from "react";
import {
  buildLagezeile,
  runProgressFraction,
  heartbeatAge,
  fmtSeconds,
  deriveKpi,
  fmtTokens,
  planSpecWaitsForOperator,
  profileInitial,
  profileColorClass,
  deriveSparklinePoints,
  type SparklinePoint,
} from "../../lib/fleetHub";
import { de } from "../../i18n/de";
import type { Worker } from "../../lib/types";
import type { CostBucket, CostProfileRow, RunsCostsResponse, RunsDailyResponse } from "../../lib/schemas";
import type { PlanSpecRecord } from "./shared";

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
  onWorkerClick: (w: Worker) => void;
  onPlanSpecClick: (ps: PlanSpecRecord) => void;
}

export function HeuteTab({ allWorkers, activeWorkers, blockedCount, pendingApprovals, allPlanspecs, costs, daily, now, onWorkerClick, onPlanSpecClick }: HeuteTabProps) {
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

  return (
    <>
      {/* Lagezeile */}
      <p className="fleet-lage">
        <LagezeileFormatted text={lagezeile} />
      </p>

      {/* KPI-Panel */}
      <div className="fleet-kpanel">
        <div className={`fleet-kp${kpi.aktiv > 0 ? " fleet-kp-aktiv" : ""}`}>
          <div className="fleet-kp-num">{kpi.aktiv}</div>
          <div className="fleet-kp-label">{de.fleet.kpiAktiv}</div>
        </div>
        <div className="fleet-kp">
          <div className="fleet-kp-num">{kpi.blockiert}</div>
          <div className="fleet-kp-label">{de.fleet.kpiBlockiert}</div>
        </div>
        <div className="fleet-kp">
          <div className="fleet-kp-num">{kpi.fertig24h ?? "—"}</div>
          <div className="fleet-kp-label">{de.fleet.kpiFertig}</div>
          {sparklinePts && <FleetSparkline points={sparklinePts} />}
        </div>
        <button
          type="button"
          className="fleet-kp fleet-kp-button"
          onClick={() => setCostDrawerOpen(true)}
          aria-label="Kosten-Details öffnen"
        >
          <div className="fleet-kp-num">
            {kpi.kosten24h != null ? (
              <>
                {kpi.kosten24h.toFixed(1).replace(".", ",")}
                <small>$</small>
                {kpi.kosten24hEquiv ? <small> äquiv.</small> : null}
              </>
            ) : "—"}
          </div>
          <div className="fleet-kp-label">{de.fleet.kpiKosten}</div>
        </button>
      </div>

      {costDrawerOpen ? (
        <CostDrawer costs={costs} daily={daily} onClose={() => setCostDrawerOpen(false)} />
      ) : null}

      {/* Worker-Karten */}
      {activeWorkers.length === 0 ? null : (
        activeWorkers.map((w) => (
          <WorkerCard key={w.run_id} worker={w} now={now} onClick={() => onWorkerClick(w)} />
        ))
      )}

      {/* PlanSpec-Karten */}
      {allPlanspecs.slice(0, 5).map((ps) => (
        <PlanSpecCard key={ps.path} ps={ps} onClick={() => onPlanSpecClick(ps)} />
      ))}
    </>
  );
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
              <td>{profile.runs}</td>
              <td>{fmtUsd(profile.actual_cost_usd)}</td>
              <td>{fmtUsd(profile.api_equivalent_usd)}</td>
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
  const elapsedSec = Math.max(0, now - w.started_at);
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
        <div className={`fleet-avatar ${colorCls}`}>{initial}</div>
        <div className="fleet-wk-name">
          {w.profile}
          <span>{w.task_id.slice(0, 10)}</span>
        </div>
        {isLive && hbAge != null ? (
          <div className="fleet-led">
            <span className="fleet-led-dot" />
            ♥ {fmtSeconds(hbAge)}
          </div>
        ) : null}
      </div>

      {/* Task-Titel */}
      <div className="fleet-wk-task">{w.task_title}</div>

      {/* Heartbeat-Notiz */}
      {w.last_heartbeat_note ? (
        <div className="fleet-wk-note">{w.last_heartbeat_note}</div>
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
          <span className="fleet-meta-right">ETA ~{fmtSeconds(w.eta_p50_seconds - elapsedSec > 0 ? w.eta_p50_seconds - elapsedSec : 0)}</span>
        ) : null}
      </div>
    </button>
  );
}

// ─── PlanSpec-Karte ───────────────────────────────────────────────────────────

function PlanSpecCard({ ps, onClick }: { ps: PlanSpecRecord; onClick: () => void }) {
  const fraction = ps.kanban_child_total > 0 ? ps.kanban_child_done / ps.kanban_child_total : null;
  const waitsForOp = planSpecWaitsForOperator(ps.freigabe, ps.kanban_state);
  const isRunning = ps.kanban_state === "running";

  let badgeClass = "fleet-ps-badge-gruen";
  let badgeLabel = ps.status;
  if (waitsForOp) {
    badgeClass = "fleet-ps-badge-amber";
    badgeLabel = de.fleet.psWaitsForOperator;
  } else if (isRunning) {
    badgeClass = "fleet-ps-badge-lauf";
    badgeLabel = `läuft${ps.kanban_child_total > 0 ? ` · ${ps.kanban_child_done}/${ps.kanban_child_total}` : ""}`;
  }

  return (
    <button type="button" className="fleet-ps" onClick={onClick}>
      <div className="fleet-ps-top">
        <span className="fleet-ps-name">{ps.topic || ps.filename}</span>
        <span className={`fleet-ps-badge ${badgeClass}`}>{badgeLabel}</span>
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
