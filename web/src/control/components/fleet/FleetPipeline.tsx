/**
 * FleetPipeline — kompakte Stufen-Übersicht (Capture → Plan → Execute →
 * Verify → Ship) mit echten per-Stage-Zahlen aus GET /board.
 *
 * Die frühere "wartet auf Operator"-Karten-Liste ist raus (2026-07-02): sie
 * duplizierte Recovery-Strip, aktive Ketten und Einzeltasks weiter unten im
 * Flow-Tab (jede Karte erschien doppelt/dreifach, besonders auf Mobil) und
 * ihr Dispatch-Pfad umging die Held-Guards der FlowRunCards. Aktionen leben
 * in Stufe 2 (Execution) auf den Ketten-/Task-Karten.
 */
import { useMemo } from "react";
import { de } from "../../i18n/de";
import { TONE_HEX } from "../../lib/tones";
import { buildPipeline } from "../../lib/fleet";
import type { BoardTask } from "../../lib/types";
import { Eyebrow } from "../primitives";
import { FleetPanel } from "./atoms";

export function FleetPipeline({ tasks }: { tasks: BoardTask[] }) {
  const pipeline = useMemo(() => buildPipeline(tasks), [tasks]);
  const maxCount = Math.max(1, ...pipeline.buckets.map((b) => b.count));
  const blockedMeta = pipeline.blockedCount > 0 ? de.fleet.pipelineBlocked(pipeline.blockedCount) : `${pipeline.total} Aufgaben`;

  return (
    <FleetPanel eyebrow={de.fleet.pipelineEyebrow} meta={blockedMeta}>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        {pipeline.buckets.map((bucket) => {
          const color = TONE_HEX[bucket.meta.tone];
          const fill = bucket.count > 0 ? Math.min(1, bucket.count / maxCount) : 0;
          return (
            <div key={bucket.stage} className="hc-fleet-pod">
              <Eyebrow>{bucket.meta.label}</Eyebrow>
              <div className="hc-fleet-pod-value mt-1.5" style={{ fontSize: "1.4rem" }}>{bucket.count}</div>
              <div className="hc-stage-rail mt-2" style={{ "--hc-role": color } as React.CSSProperties}>
                <i style={{ width: `${fill * 100}%` }} />
              </div>
              {/* Zweck-Text nur ab sm — auf dem Handy tragen Label + Zahl die
                  Aussage; die Erklärzeilen machten aus 5 Pods eine halbe Seite. */}
              <p className="mt-1.5 hidden hc-type-label hc-dim leading-tight sm:block">{bucket.meta.purpose}</p>
            </div>
          );
        })}
      </div>
    </FleetPanel>
  );
}
