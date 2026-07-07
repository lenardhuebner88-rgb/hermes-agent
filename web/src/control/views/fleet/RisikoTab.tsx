/**
 * Risiko-Subtab — "Autonomie-Kontrollzentrum" (★ FINAL, Design-Board c_2103a234).
 *
 * Greenfield-Redesign (2026-07-08) gegen den bindenden Mockup
 * `risiko-final-hybrid.html`: 4 Zonen — Hero Auto-Mode Cockpit,
 * "Braucht dich" (Ausnahmen, die die Autonomie eskaliert hat), Autonome
 * Aktivität (Deploy/Rollback-Quittung) und System-Puls (kompakt, tap-to-detail).
 *
 * Gecuttet ggü. dem Vorgänger: Plan-Freigaben (gehören ins Plan-Subtab),
 * die Zuverlässigkeits-Disclosure-Tabelle (ersetzt durch eine schlanke
 * Lane-Health-Zeile in RisikoPulse) und die (nie gebaute) Per-Profil-Matrix.
 * "Sonstige blockierte Tasks" (weder Release-Gate noch Operator-Frage) sind
 * ebenfalls raus: die Retry-Sweep-Klassifikation (isOperatorQuestion mirrort
 * _AUTO_RETRY_QUESTION_RE) sagt, dass genau diese Tasks vom System selbst
 * behandelt werden — keine echte Eskalation, gehört nicht auf diesen Tab
 * (Designprinzip aus dem Handoff: "was ein gesunder autonomer Lauf selbst
 * erledigt, gehört NICHT auf den Tab"). Fehler-Triage (TriageStrip) bleibt:
 * gescheiterte Runs mit Ein-Klick-Eskalation SIND eine echte Ausnahme.
 */
import { de } from "../../i18n/de";
import { buildReliabilityRiskModel } from "../../lib/fleetRisk";
import type { ReliabilityResponse, LanesCatalogResponse, KanbanDecision } from "../../lib/schemas";
import type { SystemHealthResponse, PressureStatusResponse, Worker } from "../../lib/types";
import { FleetTaskActions } from "./TaskActions";
import { AnswerQuestion } from "./AnswerQuestion";
import { isOperatorQuestion } from "../../lib/fleet";
import { TriageStrip } from "../../components/TriageStrip";
import { ReleaseGateButton } from "../../components/ReleaseGateButton";
import { useReleaseGateExecute } from "../../hooks/useControlData";
import type { ReleaseStatusResponse } from "../../lib/schemas";
import { RisikoHero } from "./RisikoHero";
import { RisikoActivity } from "./RisikoActivity";
import { RisikoPulse } from "./RisikoPulse";
import "./risiko-v4.css";

// ─── Risiko-Subtab ────────────────────────────────────────────────────────────

interface RisikoBlockedTask {
  id: string;
  title: string;
  status: string;
  block_reason?: string | null;
  root_id?: string | null;
}

interface RisikoTabProps {
  blockedTasks: RisikoBlockedTask[];
  reliability: ReliabilityResponse | null;
  systemHealth: SystemHealthResponse | null;
  pressureStatus: PressureStatusResponse | null;
  activeWorkers: Worker[];
  lanesCatalog: LanesCatalogResponse | null;
  /** Geparkte Release-Gates (kind === "release_gate_parked") — aus dem
   *  /control-Postfach hierher verschoben, einziges Zuhause der Aktion. */
  releaseGateDecisions: KanbanDecision[];
  /** kanban.max_in_progress — GET /workers `cap` (F4), null = unconfiguriert. */
  cap: number | null;
  /** GET /release-status — Autonomie-Kill-Switch-State + Aktivitäts-Timeline. */
  releaseStatus: ReleaseStatusResponse | null;
  /** Board nach einer Steuerungs-Aktion (Unblock/Retry/Cancel) neu laden. */
  onTaskChanged?: () => void | Promise<void>;
}

/** Ketten-Root eines Blocked-Tasks für "Kette abbrechen" — nur wenn der Task
 *  Kind einer Kette ist (root_id gesetzt und ≠ eigener id). */
function rowChainRootId(t: RisikoBlockedTask): string | null {
  return t.root_id && t.root_id !== t.id ? t.root_id : null;
}

export function RisikoTab({
  blockedTasks,
  reliability,
  systemHealth,
  pressureStatus,
  activeWorkers,
  lanesCatalog,
  releaseGateDecisions,
  cap,
  releaseStatus,
  onTaskChanged,
}: RisikoTabProps) {
  const releaseGate = useReleaseGateExecute();

  // Operator-Halts — echte Klassifikation (mirrort backend _AUTO_RETRY_QUESTION_RE);
  // alles andere blockierte behandelt die Retry-Sweep selbst, gehört nicht hierher.
  const operatorHalts = blockedTasks.filter((t) => isOperatorQuestion(t.block_reason));

  const reliabilityModel = buildReliabilityRiskModel({
    reliability,
    laneCatalogProfiles: lanesCatalog?.profiles ?? [],
    activeWorkerProfiles: activeWorkers.map((w) => w.profile),
  });

  const needsYouCount = releaseGateDecisions.length + operatorHalts.length;
  const hasAnything = needsYouCount > 0;

  return (
    <div className="risiko-v4">
      {/* Zone 1: Hero Auto-Mode Cockpit */}
      <RisikoHero releaseStatus={releaseStatus} cap={cap} />
      <p className="rk-console-foot">Reine Deklaration · Rollback ist das Netz · kein Pre-Gate</p>

      {/* Zone 2: Braucht dich — Ausnahmen, die die Autonomie eskaliert hat */}
      <div className="rk-sect-head">
        <span className="rk-eyebrow">{de.fleet.risikoBrauchtDichTitle}</span>
        {hasAnything ? <span className="rk-sect-count">{de.fleet.risikoBrauchtDichCount(needsYouCount)}</span> : null}
      </div>

      {releaseGateDecisions.map((d) => {
        const context = [
          d.release_gate?.root_id ? `Root ${d.release_gate.root_id}` : null,
          d.release_gate?.merge_commit ? `Merge ${d.release_gate.merge_commit}` : null,
        ].filter(Boolean).join(" · ");
        return (
          <div key={d.task_id} className="rk-needcard" aria-label={`Release-Gate: ${d.title}`}>
            <div className="rk-nc-top">
              <div className="rk-nc-glyph rk-glyph-gate" aria-hidden="true">⇧</div>
              <div className="rk-nc-body">
                <div className="rk-nc-titlerow">
                  <span className="rk-nc-title">{d.title}</span>
                  <span className="rk-nc-badge rk-badge-gate">gate</span>
                </div>
                {context ? <div className="rk-nc-meta">{context}</div> : null}
              </div>
            </div>
            <div className="rk-nc-actions">
              <ReleaseGateButton taskId={d.task_id} releaseGate={releaseGate} />
            </div>
          </div>
        );
      })}

      {operatorHalts.map((t) => (
        <div key={t.id} className="rk-needcard rk-needcard-alert" aria-label={`Operator-Halt: ${t.title}`}>
          <div className="rk-nc-top">
            <div className="rk-nc-glyph rk-glyph-op" aria-hidden="true">!</div>
            <div className="rk-nc-body">
              <div className="rk-nc-titlerow">
                <span className="rk-nc-title">{t.title}</span>
                <span className="rk-nc-badge rk-badge-op">operator</span>
              </div>
              {t.block_reason ? <div className="rk-nc-meta">{t.block_reason}</div> : null}
            </div>
          </div>
          {isOperatorQuestion(t.block_reason) ? <AnswerQuestion taskId={t.id} /> : null}
          <div className="rk-nc-actions">
            <FleetTaskActions
              taskId={t.id}
              status={t.status}
              chainRootId={rowChainRootId(t)}
              onChanged={onTaskChanged}
            />
          </div>
        </div>
      ))}

      {!hasAnything ? (
        <p className="rk-leer">{de.fleet.risikoLeerState}</p>
      ) : null}

      {/* Fehler-Triage: gescheiterte Runs der letzten 48h — eine echte
          Ausnahme, mit Ein-Klick-Eskalation. Blendet sich selbst aus, wenn
          nichts zu triagieren ist (kein Rauschen für den Nicht-Nutzer). */}
      <div id="fleet-section-triage"><TriageStrip /></div>

      {/* Zone 3: Autonome Aktivität */}
      <RisikoActivity releaseStatus={releaseStatus} />

      {/* Zone 4: System-Puls */}
      <RisikoPulse pressureStatus={pressureStatus} systemHealth={systemHealth} reliabilityModel={reliabilityModel} />
    </div>
  );
}
