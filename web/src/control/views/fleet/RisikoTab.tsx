/**
 * Risiko-Subtab (Operator-Entscheidungen, blockierte Tasks, Zuverlässigkeit, System-Puls).
 *
 * Aus FleetView.tsx extrahiert — reine Zerlegung, kein Verhalten geändert.
 */
import { planSpecWaitsForOperator } from "../../lib/fleetHub";
import { de } from "../../i18n/de";
import type { ReliabilityResponse } from "../../lib/schemas";
import type { SystemHealthResponse, PressureStatusResponse } from "../../lib/types";
import type { PlanSpecRecord } from "./shared";
import { FleetTaskActions } from "./TaskActions";

// ─── Risiko-Subtab ────────────────────────────────────────────────────────────

interface RisikoBlockedTask {
  id: string;
  title: string;
  status: string;
  block_reason?: string | null;
  root_id?: string | null;
}

interface RisikoTabProps {
  allPlanspecs: PlanSpecRecord[];
  blockedTasks: RisikoBlockedTask[];
  reliability: ReliabilityResponse | null;
  systemHealth: SystemHealthResponse | null;
  pressureStatus: PressureStatusResponse | null;
  onNavigateToPlan: () => void;
  /** Board nach einer Steuerungs-Aktion (Unblock/Retry/Cancel) neu laden. */
  onTaskChanged?: () => void | Promise<void>;
}

/** Ketten-Root eines Blocked-Tasks für "Kette abbrechen" — nur wenn der Task
 *  Kind einer Kette ist (root_id gesetzt und ≠ eigener id). */
function rowChainRootId(t: RisikoBlockedTask): string | null {
  return t.root_id && t.root_id !== t.id ? t.root_id : null;
}

export function RisikoTab({
  allPlanspecs,
  blockedTasks,
  reliability,
  systemHealth,
  pressureStatus,
  onNavigateToPlan,
  onTaskChanged,
}: RisikoTabProps) {
  // (a) Wartende Freigaben
  const pendingApprovals = allPlanspecs.filter((ps) => planSpecWaitsForOperator(ps.freigabe, ps.kanban_state));

  // (a) Blockierte Tasks — Operator-Halts vs. sonstige blockierte
  const operatorHalts = blockedTasks.filter((t) => {
    const r = (t.block_reason ?? "").toLowerCase();
    return r.includes("operator");
  });
  const otherBlocked = blockedTasks.filter((t) => {
    const r = (t.block_reason ?? "").toLowerCase();
    return !r.includes("operator");
  });

  // Gesamte blockierte Tasks für den Leer-Zustand
  const totalBlockedCount = blockedTasks.length;
  const totalBoardTasks = 0; // Wir haben keinen Gesamtcount leicht verfügbar, daher weglassen

  // (b) Zuverlässigkeit je Lane
  const profiles = reliability?.profiles ?? [];

  // (c) System-Puls
  const gateway = systemHealth?.subsystems?.gateway;
  const dispatcher = systemHealth?.subsystems?.kanban_dispatcher;
  const host = pressureStatus?.host;
  const tokenPressure = pressureStatus?.token_pressure;

  const hasAnything = pendingApprovals.length > 0 || operatorHalts.length > 0 || otherBlocked.length > 0;

  return (
    <>
      {/* Lagezeile */}
      <p className="fleet-lage">
        {!hasAnything
          ? <>{de.fleet.risikoLageNichtsBlockiert}</>
          : <span className="fleet-amber">{de.fleet.risikoLageBlockiert(pendingApprovals.length + operatorHalts.length)}</span>
        }
      </p>

      {/* (a) Operator-Entscheidungen: wartende Freigaben */}
      {pendingApprovals.length > 0 ? (
        <>
          <div className="fleet-risiko-sec">{de.fleet.risikoFreigabenTitle}</div>
          {pendingApprovals.map((ps) => (
            <div key={ps.path} className="fleet-risiko-approval">
              <div className="fleet-risiko-approval-n">
                {ps.topic || ps.filename}
                <span className="fleet-ps-badge fleet-ps-badge-amber" style={{ marginLeft: "auto" }}>
                  freigabe: operator
                </span>
              </div>
              {ps.freigabe ? (
                <div className="fleet-plan-kopf-meta" style={{ marginTop: 2 }}>
                  {ps.kanban_child_total > 0 ? <span>{de.fleet.kartenGeplant(ps.kanban_child_total)}</span> : null}
                  {ps.binding ? <span>binding</span> : null}
                </div>
              ) : null}
              {/* Konfiguration gehört ins Plan-Subtab-Cockpit — kein Blind-Approve hier */}
              <button
                type="button"
                className="fleet-btn fleet-btn-primar"
                style={{ marginTop: 2, alignSelf: "flex-start", padding: "8px 14px", minHeight: 36 }}
                onClick={onNavigateToPlan}
                aria-label={`${ps.topic || ps.filename} im Plan-Subtab konfigurieren`}
              >
                {de.fleet.risikoFreigabeZumPlan}
              </button>
            </div>
          ))}
        </>
      ) : null}

      {/* (a) Blockierte Tasks: Operator-Halts */}
      {operatorHalts.length > 0 ? (
        <>
          <div className="fleet-risiko-sec">{de.fleet.risikoBlockiertTitle}</div>
          {operatorHalts.map((t) => (
            <div key={t.id} className="fleet-risiko-blocked" aria-label={`Blockierter Task: ${t.title}`}>
              <div className="fleet-risiko-blocked-n">
                {t.title}
                <span className="fleet-ps-badge fleet-ps-badge-amber" style={{ marginLeft: "auto" }}>
                  {de.fleet.risikoOperatorHalt}
                </span>
              </div>
              {t.block_reason ? (
                <div style={{ font: "400 11px/1.4 var(--hc-font-mono)", color: "var(--fleet-t3)", paddingLeft: 0 }}>
                  {t.block_reason}
                </div>
              ) : null}
              <FleetTaskActions
                taskId={t.id}
                status={t.status}
                chainRootId={rowChainRootId(t)}
                onChanged={onTaskChanged}
              />
            </div>
          ))}
        </>
      ) : null}

      {/* Sonstige blockierte Tasks (keine Operator-Halts) — kompakt, mit Aktionen */}
      {otherBlocked.length > 0 ? (
        <div className="fleet-risiko-rel" style={{ gap: 10 }}>
          {otherBlocked.slice(0, 5).map((t) => (
            <div key={t.id}>
              <div className="fleet-risiko-rel-row">
                <span className="fleet-risiko-rel-lane" style={{ width: "auto", flex: 1 }}>{t.title}</span>
                {t.block_reason ? (
                  <span className="fleet-risiko-rel-val" style={{ flex: "none", fontSize: 10, color: "var(--fleet-t3)" }}>
                    {t.block_reason.slice(0, 40)}
                  </span>
                ) : null}
              </div>
              <FleetTaskActions
                taskId={t.id}
                status={t.status}
                chainRootId={rowChainRootId(t)}
                onChanged={onTaskChanged}
              />
            </div>
          ))}
        </div>
      ) : null}

      {/* (b) Zuverlässigkeit je Lane */}
      {profiles.length > 0 ? (
        <>
          <div className="fleet-risiko-sec">{de.fleet.risikoZuverlässigkeitTitle}</div>
          <div className="fleet-risiko-rel" aria-label="Zuverlässigkeit je Profil">
            {profiles.map((p) => {
              const isLowSample = p.low_sample || p.runs < 5;
              const completedPct = p.completed_rate != null ? Math.round(p.completed_rate * 100) : null;
              const failedPct = p.failed_rate != null ? Math.round(p.failed_rate * 100) : null;

              return (
                <div key={p.profile} className="fleet-risiko-rel-row">
                  <span className="fleet-risiko-rel-lane">{p.profile}</span>
                  {isLowSample ? (
                    <span className="fleet-risiko-low-sample" aria-label="Wenig Daten — kein sicheres Urteil möglich">
                      {de.fleet.risikoWenigDaten}
                    </span>
                  ) : (
                    <span className="fleet-risiko-rel-val">
                      {completedPct != null ? `${de.fleet.risikoAbschlussRate} ${completedPct} %` : "—"}
                      {failedPct != null && failedPct > 0 ? ` · ${de.fleet.risikoFailed} ${failedPct} %` : ""}
                      {p.retries > 0 ? ` · ${de.fleet.risikoRetries} ${p.retries}` : ""}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </>
      ) : null}

      {/* (c) System-Puls */}
      <div className="fleet-risiko-sec">{de.fleet.risikoSystemPulsTitle}</div>
      <div className="fleet-puls-table" aria-label="System-Puls">
        {/* Gateway */}
        <div className="fleet-puls-row">
          <span className="fleet-puls-label">{de.fleet.risikoGateway}</span>
          <span className={`fleet-puls-val ${gateway?.heartbeat_age_s != null && gateway.heartbeat_age_s < 30 ? "fleet-puls-val-gruen" : gateway ? "fleet-puls-val-warn" : "fleet-puls-val-normal"}`}>
            {gateway?.heartbeat_age_s != null
              ? de.fleet.risikoHeartbeatFrisch(Math.round(gateway.heartbeat_age_s))
              : gateway?.status === "healthy"
              ? de.fleet.risikoGrün
              : de.fleet.risikoHeartbeatNichtVerfügbar}
          </span>
        </div>

        {/* Dispatcher */}
        <div className="fleet-puls-row">
          <span className="fleet-puls-label">{de.fleet.risikoDispatcher}</span>
          <span className={`fleet-puls-val ${dispatcher?.heartbeat_age_s != null && dispatcher.heartbeat_age_s < 30 ? "fleet-puls-val-gruen" : dispatcher ? "fleet-puls-val-warn" : "fleet-puls-val-normal"}`}>
            {dispatcher?.heartbeat_age_s != null
              ? de.fleet.risikoHeartbeatFrisch(Math.round(dispatcher.heartbeat_age_s))
              : dispatcher?.status === "healthy"
              ? de.fleet.risikoGrün
              : de.fleet.risikoHeartbeatNichtVerfügbar}
          </span>
        </div>

        {/* CPU / RAM */}
        <div className="fleet-puls-row">
          <span className="fleet-puls-label">{de.fleet.risikoCpuRam}</span>
          <span className="fleet-puls-val fleet-puls-val-normal">
            {host?.cpu_percent != null || host?.memory_percent != null
              ? `${host.cpu_percent != null ? Math.round(host.cpu_percent) : "—"} % · ${host.memory_percent != null ? Math.round(host.memory_percent) : "—"} %`
              : "—"}
          </span>
        </div>

        {/* Token-Pressure */}
        <div className="fleet-puls-row">
          <span className="fleet-puls-label">{de.fleet.risikoTokenPressure}</span>
          <span className={`fleet-puls-val ${tokenPressure?.class === "normal" ? "fleet-puls-val-gruen" : tokenPressure ? "fleet-puls-val-warn" : "fleet-puls-val-normal"}`}>
            {tokenPressure?.class ?? "—"}
          </span>
        </div>
      </div>

      {/* (d) Gepflegter Leerzustand */}
      {!hasAnything ? (
        <div className="fleet-risiko-leer">
          <div className="fleet-risiko-leer-title">{de.fleet.risikoLeerState}</div>
          <div className="fleet-risiko-leer-sub">
            {totalBlockedCount === 0
              ? "Alle Karten sauber durch."
              : de.fleet.risikoLeerStateSub(totalBoardTasks)}
          </div>
        </div>
      ) : null}
    </>
  );
}
