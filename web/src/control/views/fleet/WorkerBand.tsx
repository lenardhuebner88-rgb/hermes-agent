/**
 * WorkerBand — die Zeitachsen-Laufband-Darstellung eines laufenden Workers
 * (Puls-Leitstand Variante B). Füllung = elapsed gegen das p90-Fenster, eine
 * p50-Marke, Heartbeat-Ticks als Punkte und das step_key-Label. In drei Größen
 * wiederverwendet: Swimlane (`lane`), Drawer-Fokus (`big`), Andere-Lanes (`mini`).
 *
 * Rein präsentational: Geometrie kommt aus computeBandGeometry (lib/fleetHub),
 * Farben ausschließlich aus fleet.css-Klassen (keine rohen Hex in TSX, DESIGN.md
 * Regel 8). Cyan (--fleet-puls) trägt nur die Live-Füllung + das aktuelle
 * step_key — beides „live", konform mit Regel 1.
 */
import { computeBandGeometry, fmtDurationClock } from "../../lib/fleetHub";
import { elapsedSeconds } from "../../lib/derive";
import type { Worker } from "../../lib/types";

type BandSize = "lane" | "big" | "mini";

export function WorkerBand({
  worker,
  now,
  size = "lane",
}: {
  worker: Worker;
  now: number;
  size?: BandSize;
}) {
  const geo = computeBandGeometry(worker, now);
  const elapsed = elapsedSeconds(worker.started_at, now) ?? Number.NaN;
  const step = worker.step_key?.trim() || "läuft";
  const p50 = worker.eta_p50_seconds && worker.eta_p50_seconds > 0 ? worker.eta_p50_seconds : null;
  const p90 = worker.eta_p90_seconds && worker.eta_p90_seconds > 0 ? worker.eta_p90_seconds : null;

  const cls =
    size === "big"
      ? "fleet-band fleet-band-big"
      : size === "mini"
        ? "fleet-band fleet-band-mini"
        : "fleet-band";

  // Meta: kompakt in der Mini-Lane (nur elapsed), sonst elapsed / p50 (+ p90 im
  // vergrößerten Fokus-Band).
  const meta =
    size === "mini"
      ? fmtDurationClock(elapsed)
      : `${fmtDurationClock(elapsed)}${p50 ? ` / p50 ${fmtDurationClock(p50)}` : ""}${
          size === "big" && p90 ? ` / p90 ${fmtDurationClock(p90)}` : ""
        }`;

  return (
    <div className={cls}>
      <div className="fleet-band-ghost" />
      <div className="fleet-band-fill" style={{ width: `${Math.round(geo.fillFraction * 100)}%` }} />
      {geo.p50Fraction != null ? (
        <div className="fleet-band-p50" style={{ left: `${Math.round(geo.p50Fraction * 100)}%` }} />
      ) : null}
      {size !== "mini" && geo.tickFractions.length > 0 ? (
        <div className="fleet-band-ticks">
          {geo.tickFractions.map((f, i) => (
            <span key={`${i}-${f.toFixed(3)}`} className="fleet-band-tick" style={{ left: `${(f * 100).toFixed(1)}%` }} />
          ))}
        </div>
      ) : null}
      <span className="fleet-band-step" title={step}>{step}</span>
      <span className="fleet-band-meta">{meta}</span>
    </div>
  );
}
