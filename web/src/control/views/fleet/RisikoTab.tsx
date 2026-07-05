/**
 * Risiko-Subtab (Operator-Entscheidungen, blockierte Tasks, Zuverlässigkeit, System-Puls).
 *
 * Aus FleetView.tsx extrahiert — reine Zerlegung, kein Verhalten geändert.
 */
import { planSpecAwaitsPlanAction } from "../../lib/fleetHub";
import { de } from "../../i18n/de";
import { Disclosure } from "../../components/primitives";
import { buildReliabilityRiskModel, buildSystemPulseRiskModel } from "../../lib/fleetRisk";
import type { ReliabilityResponse, LanesCatalogResponse } from "../../lib/schemas";
import type { SystemHealthResponse, PressureStatusResponse, Worker } from "../../lib/types";
import type { PlanSpecRecord } from "./shared";
import { FleetTaskActions } from "./TaskActions";
import { AnswerQuestion } from "./AnswerQuestion";
import { isOperatorQuestion } from "../../lib/fleet";

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
  activeWorkers: Worker[];
  lanesCatalog: LanesCatalogResponse | null;
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
  activeWorkers,
  lanesCatalog,
  onNavigateToPlan,
  onTaskChanged,
}: RisikoTabProps) {
  // (a) Wartende Freigaben
  const pendingApprovals = allPlanspecs.filter((ps) => planSpecAwaitsPlanAction(ps));

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

  // (b) Zuverlässigkeit je Lane / (c) System-Puls
  const reliabilityModel = buildReliabilityRiskModel({
    reliability,
    laneCatalogProfiles: lanesCatalog?.profiles ?? [],
    activeWorkerProfiles: activeWorkers.map((w) => w.profile),
  });
  const pulseModel = buildSystemPulseRiskModel({ systemHealth, pressureStatus });

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

      {/* (c) System-Puls */}
      <section className={`fleet-risk-card fleet-risk-card-${pulseModel.overallTone}`} aria-label="System-Puls">
        <div className="fleet-risk-card-head">
          <div>
            <div className="fleet-risiko-sec">{de.fleet.risikoSystemPulsTitle}</div>
            <p className="fleet-risk-headline">{pulseModel.headline}</p>
          </div>
        </div>
        <div className="fleet-puls-grid">
          {pulseModel.rows.map((row) => (
            <div key={row.key} className={`fleet-puls-tile fleet-puls-tile-${row.tone}`}>
              <span className="fleet-puls-label">{row.label}</span>
              <span className="fleet-puls-val">{row.value}</span>
              {row.detail ? <span className="fleet-puls-detail">{row.detail}</span> : null}
            </div>
          ))}
        </div>
      </section>

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
              {isOperatorQuestion(t.block_reason) ? (
                <AnswerQuestion taskId={t.id} />
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
      {reliabilityModel.rows.length > 0 ? (
        <Disclosure
          className="fleet-risk-disclosure"
          defaultOpen={reliabilityModel.defaultOpen}
          summary={
            <span className="fleet-risk-disclosure-summary">
              <span>{de.fleet.risikoZuverlaessigkeitTitle}</span>
              <span className="fleet-risk-disclosure-meta">{reliabilityModel.summary}</span>
            </span>
          }
        >
          <div className="fleet-risiko-rel" aria-label={`Zuverlässigkeit je Profil, ${reliabilityModel.windowLabel}`}>
            {reliabilityModel.rows.map((row) => (
              <div key={row.profile} className={`fleet-risiko-rel-row fleet-risiko-rel-row-${row.tone}`}>
                <span className="fleet-risiko-rel-lane">{row.profile}</span>
                <span className="fleet-risiko-rel-val">
                  {row.sampleLabel ? row.sampleLabel : [
                    row.completedPct != null ? `${de.fleet.risikoAbschlussRate} ${row.completedPct} %` : null,
                    row.failedPct != null && row.failedPct > 0 ? `${de.fleet.risikoFailed} ${row.failedPct} %` : null,
                    row.retries > 0 ? `${de.fleet.risikoRetries} ${row.retries}` : null,
                  ].filter(Boolean).join(" · ") || "—"}
                </span>
              </div>
            ))}
          </div>
          {reliabilityModel.hiddenCount > 0 ? (
            <p className="fleet-risk-hidden-note">
              {de.fleet.risikoProfileAusgeblendet(reliabilityModel.hiddenCount, reliabilityModel.windowLabel)}
            </p>
          ) : null}
        </Disclosure>
      ) : null}

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
