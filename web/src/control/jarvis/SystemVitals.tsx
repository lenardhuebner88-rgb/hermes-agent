/**
 * SystemVitals — G4: Glas-Pille unten links in der Graph-Zone.
 *
 * Zeigt echte 10-Minuten-Microsparks für CPU und RAM plus aktuelle
 * Prozentwerte. Daten aus useSystemHistory (Endpoint-Vorrang, sonst
 * Client-Ringpuffer über den 15s-Poll). Fehlen Daten komplett (frischer
 * Start, <2 Samples und Endpoint 404): dezenter Sammel-Hinweis statt
 * Fake-Flachlinie. Niemals erfundene Kurven.
 */
import { sparkPathFromSeries, useSystemHistory } from "./useSystemHistory";

function VitalSpark({ series, label }: { series: number[]; label: string }) {
  return (
    <svg
      className="jv-vital-spark"
      viewBox="0 0 100 24"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <path d={sparkPathFromSeries(series)} fill="none" stroke="currentColor" strokeWidth="1.5" />
      <title>{label}</title>
    </svg>
  );
}

export function SystemVitals() {
  const { cpu, mem, source, cpuNow, memNow } = useSystemHistory();

  // Sammel-Zustand: Ringpuffer mit <2 Samples und kein Endpoint.
  const collecting = source === "ring" && cpu.length < 2 && mem.length < 2;

  if (collecting) {
    return (
      <div className="jv-vitals" role="status">
        <span className="jv-vitals-hint">Vitals sammeln …</span>
      </div>
    );
  }

  return (
    <div className="jv-vitals" role="status" aria-label="System-Vitals">
      <div className="jv-vital">
        <VitalSpark series={cpu} label="CPU-Verlauf" />
        <span className="jv-vital-val">{cpuNow !== null ? `${Math.round(cpuNow)}%` : "–"}</span>
        <span className="jv-vital-label">CPU</span>
      </div>
      <div className="jv-vital">
        <VitalSpark series={mem} label="RAM-Verlauf" />
        <span className="jv-vital-val">{memNow !== null ? `${Math.round(memNow)}%` : "–"}</span>
        <span className="jv-vital-label">RAM</span>
      </div>
    </div>
  );
}
