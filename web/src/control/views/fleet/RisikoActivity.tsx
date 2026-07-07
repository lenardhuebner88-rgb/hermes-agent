/**
 * RisikoActivity — Zone 3 "Autonome Aktivität" (★ FINAL, Design-Board c_2103a234).
 *
 * Kompakte Deploy/Rollback-Timeline (das Guard-Netz-Quittung), gespeist aus
 * GET /api/plugins/kanban/release-status (`recent` auto_release-Events +
 * `anchors` pre-deploy Git-Tags) — dieselbe Quelle wie AutoReleaseTile.
 *
 * ⚠️ Divergenz ggü. dem Mockup: die HTML-Vorlage zeigt pro Zeile einen
 * beschreibenden Klartext ("operator-cockpit-s7 · live-test grün") UND einen
 * dazugehörigen Anker-Commit ("↳ 8aeb1773f"). Das reale Payload liefert pro
 * Event nur `task_id` + `payload.{outcome,detail,...}` — keine Chain-Titel und
 * keine 1:1-Zuordnung Event↔Anker. Gebaut gegen die echten Felder: Zeile zeigt
 * `payload.detail` falls vorhanden sonst die `task_id`; der letzte Anker steht
 * separat darunter (wie schon in AutoReleaseTile), statt eine Zuordnung zu
 * erfinden, die die API nicht hergibt.
 */
import { fmtAge } from "../../lib/derive";
import type { ReleaseStatusEvent, ReleaseStatusResponse } from "../../lib/schemas";

type RailTone = "ok" | "alert" | "warn" | "neutral";

const OUTCOME_TONE: Record<string, RailTone> = {
  deployed: "ok",
  rolled_back: "alert",
  held_critical: "warn",
  held_live_test: "warn",
};

function outcomeOf(ev: ReleaseStatusEvent): string {
  return typeof ev.payload?.outcome === "string" ? ev.payload.outcome : "unbekannt";
}

function detailOf(ev: ReleaseStatusEvent): string | null {
  return typeof ev.payload?.detail === "string" ? ev.payload.detail : null;
}

export interface RisikoActivityProps {
  releaseStatus: ReleaseStatusResponse | null;
}

export function RisikoActivity({ releaseStatus }: RisikoActivityProps) {
  const recent = (releaseStatus?.recent ?? []).slice(0, 6);
  const anchors = releaseStatus?.anchors ?? [];
  const lastAnchor = anchors.length > 0 ? anchors[anchors.length - 1] : null;

  return (
    <section className="risiko-v4" aria-label="Autonome Aktivität">
      <div className="rk-eyebrow-row"><span className="rk-eyebrow">Autonome Aktivität</span></div>
      {recent.length === 0 ? (
        <p className="rk-rail-empty">Noch keine autonomen Releases.</p>
      ) : (
        <div className="rk-rail">
          {recent.map((ev, i) => {
            const outcome = outcomeOf(ev);
            const tone = OUTCOME_TONE[outcome] ?? "neutral";
            const isLast = i === recent.length - 1;
            return (
              <div key={`${ev.task_id}-${ev.created_at}-${i}`} className={`rk-rail-item${isLast ? " rk-rail-item-last" : ""}`}>
                <span className={`rk-rail-node rk-node-${tone}`} aria-hidden="true" />
                <div className="rk-rail-body">
                  <div className="rk-rail-top">
                    <span className={`rk-rail-badge rk-rb-${tone}`}>{outcome}</span>
                    <span className="rk-rail-age">{`vor ${fmtAge(ev.created_at)}`}</span>
                  </div>
                  <div className="rk-rail-desc">{detailOf(ev) ?? ev.task_id}</div>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {lastAnchor ? <p className="rk-rail-anchor">{`Anker: ${lastAnchor}`}</p> : null}
    </section>
  );
}
