/** Reine Route-Logik des Loop-Startdialogs: Engine/Modell/Effort → Overrides.
 *
 *  Eigenes Modul statt Helfer in LoopsView.tsx, damit das Verhalten testbar ist,
 *  ohne den ganzen Dialog zu rendern (und ohne Nicht-Komponenten-Exporte in
 *  einer .tsx, die react-refresh anmeckert).
 */

import type { LoopModelsResponse, LoopPackSummary } from "../../lib/types";

/** Route einer Phase im Startdialog: Engine + Modell + optionaler Effort. */
export type PhaseRoute = { engine: string; model: string; effort: string | null };

/** Effort-Stufen, die eine Engine transportieren kann ([] = kein Control).
 *
 *  Quelle ist der Backend-Katalog (`effort_levels`), der ihn seinerseits aus der
 *  Engine-Registrierung zieht — NICHT eine Liste im Frontend. Sonst böte das UI
 *  Stufen an, die das CLI ablehnt (genau der Fehlermodus, den diese Kette
 *  beseitigt: Anzeige sagt medium, Prozess läuft xhigh).
 */
export function effortLevelsFor(
  models: LoopModelsResponse | null,
  engine: string,
): string[] {
  return models?.engines?.[engine]?.effort_levels ?? [];
}

/** Beim Engine-Wechsel gilt das Effort-Set der NEUEN Engine.
 *
 *  `claude: max` gibt es auf xai nicht — würde der alte Wert mitwandern, stürbe
 *  der Lauf fail-closed. Spiegelt exakt, was der Runner beim Auflösen tut.
 */
export function routeAfterEngineChange(
  before: PhaseRoute | undefined,
  next: { engine: string; model: string },
): PhaseRoute {
  return {
    ...next,
    effort: before && before.engine === next.engine ? before.effort : null,
  };
}

export function buildPhaseOverrides(
  pack: LoopPackSummary,
  phaseValues: Record<string, PhaseRoute>,
): Record<string, string> {
  const overrides: Record<string, string> = {};
  for (const phase of Object.keys(pack.phases)) {
    const current = phaseValues[phase];
    if (!current) continue;
    const upper = phase.toUpperCase();
    overrides[`PHASE_${upper}_ENGINE`] = current.engine;
    overrides[`PHASE_${upper}_MODEL`] = current.model;
    // Leerer Effort = "Standard": kein Override, der CLI-Default gilt.
    if (current.effort) overrides[`PHASE_${upper}_EFFORT`] = current.effort;
  }
  return overrides;
}

/** Autoland ist erst scharf, wenn der Zweitschlüssel exakt stimmt.
 *
 *  Ein Klick allein reicht bewusst nicht: sonst wäre hinterher nicht
 *  unterscheidbar, ob die Landung Absicht war oder ein Durchklicken.
 */
export function autolandArmed(
  pack: LoopPackSummary,
  enabled: boolean,
  ack: string,
): boolean {
  if (!enabled) return false;
  if (pack.autoland) return false; // Vertrags-Autoland braucht den Schalter nicht
  if (!pack.autoland_capable) return false;
  return ack.trim() === pack.name;
}
