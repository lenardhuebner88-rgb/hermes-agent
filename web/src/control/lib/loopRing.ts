/**
 * Pure Ableitungs-Logik für den Loop-Ring (Signatur-Element des Loops-Tabs) —
 * eigenes Modul statt LoopsView.tsx, damit die Funktionen direkt testbar sind
 * ohne die react-refresh/only-export-components-Regel der View zu verletzen.
 */

import type { LoopHeartbeatHistoryEntry, LoopPackSummary } from "./types";

export interface LoopRingSegment {
  key: string;
  state: "current" | "done" | "pending";
  /** nur bei state==="current": 0..1 Anteil von heartbeat.current.timeout;
   *  null = kein Timeout bekannt → unbestimmt (Segment füllt sich ganz, der
   *  Atem-Glow trägt die "läuft noch"-Aussage). */
  progress?: number | null;
}

export interface LoopRingTicks {
  total: number;
  done: number;
  currentActive: boolean;
}

/** Pipeline-Phasen sind vom Runner fest verdrahtet (`loops/runner.py::_run_pipeline`
 *  ruft immer genau plan→build→verify auf) — deshalb hier als feste Reihenfolge,
 *  nicht aus `pack.phases`-Keys abgeleitet. */
const PIPELINE_PHASE_ORDER = ["plan", "build", "verify"] as const;

/** Fenster der AKTUELLEN Runde aus der rollierenden Historie: `heartbeat.last`
 *  trägt keine Runden-/Run-IDs (loops/runner.py hält ein 20er-Fenster über
 *  Rundengrenzen hinweg), aber jede Pipeline-Runde beginnt mit `plan` — alles
 *  vor dem letzten `plan`-Eintrag gehört zu früheren Runden und darf nicht als
 *  Fortschritt der aktuellen erscheinen. Läuft gerade `plan`, beginnt eine neue
 *  Runde und die gesamte Historie ist Vergangenheit; ohne `plan` im Fenster
 *  (rotiert/fremd) bleibt konservativ alles "pending". */
function currentRoundWindow(
  last: LoopHeartbeatHistoryEntry[],
  currentPhase: string | null,
): LoopHeartbeatHistoryEntry[] {
  if (currentPhase === "plan") return [];
  const lastPlanIdx = last.map((e) => e.phase).lastIndexOf("plan");
  if (lastPlanIdx < 0) return [];
  return last.slice(lastPlanIdx);
}

export function deriveRingSegments(pack: LoopPackSummary, nowMs: number): LoopRingSegment[] {
  const current = pack.heartbeat?.current ?? null;
  const windowed = currentRoundWindow(pack.heartbeat?.last ?? [], current?.phase ?? null);
  return PIPELINE_PHASE_ORDER.map((phase): LoopRingSegment => {
    if (current && current.phase === phase) {
      const startedMs = Date.parse(current.started_at);
      const elapsedSec = Number.isFinite(startedMs) ? Math.max(0, (nowMs - startedMs) / 1000) : 0;
      const progress = current.timeout > 0 ? Math.min(1, elapsedSec / current.timeout) : null;
      return { key: phase, state: "current", progress };
    }
    const found = [...windowed].reverse().find((e) => e.phase === phase);
    if (found) return { key: phase, state: found.rc === 0 ? "done" : "pending" };
    return { key: phase, state: "pending" };
  });
}

export function deriveRingTicks(pack: LoopPackSummary): LoopRingTicks {
  const maxRounds = pack.stop.max_rounds ?? 0;
  const total = Math.max(1, Math.min(24, maxRounds || 1));
  const last = pack.heartbeat?.last ?? [];
  // Nur der hintere zusammenhängende `round`-Block zählt: davor liegende
  // Einträge (Pipeline-Phasen, ältere Runs) sind nicht "diese" Sweep-Serie.
  // Näherung — direkt aufeinanderfolgende Sweep-RUNS bleiben ununterscheidbar,
  // Autorität über echte Runden bleibt das Logbuch.
  let doneRounds = 0;
  for (let i = last.length - 1; i >= 0; i--) {
    if (last[i].phase !== "round") break;
    if (last[i].rc === 0) doneRounds++;
  }
  const currentActive = pack.heartbeat?.current?.phase === "round";
  return { total, done: Math.min(doneRounds, total), currentActive };
}
