import type { UsageFactsGroup } from "../hooks/usageFacts";

// Reine Faltung des S6-Payloads über die Hebel-Achsen origin und model
// (Brief §3a). Keine Neu-Aggregation quellennativer Token: es werden nur die
// vom Backend normalisierten Gruppenfelder aufsummiert — dieselbe Regel wie
// `_sum_normalized_tokens` im Read-Pfad (fehlende Buckets zählen als 0 und
// setzen ein Lower-Bound-Flag, sie erfinden keine Null-Aussage).

/** Origin der Aux-Calls (Kompression/Verdichtung) — Vertragsvokabular aus
 *  `origin-input.v1`, kein Modellname. Der Aux-Anteil lebt auf Call-Ebene im
 *  eigenen Origin, nicht auf Run-Ebene (Brief §2.3). */
export const AUX_ORIGIN = "hermes_aux";

export interface OriginFold {
  origin: string;
  factRows: number;
  contextInput: number;
  newInput: number;
  cacheRead: number;
  output: number;
  /** Mindestens eine Gruppe meldet context/new_input nicht als `exact`. */
  hasLowerBounds: boolean;
}

export interface ModelFold {
  label: string;
  factRows: number;
  contextInput: number;
  hasLowerBounds: boolean;
}

export interface AuxFold {
  factRows: number;
  contextInput: number;
  hasLowerBounds: boolean;
}

const isExact = (field: { status: string }) => field.status === "exact";

/** Gruppen → eine Zeile je Origin, absteigend nach Kontext-Input. */
export function foldGroupsByOrigin(groups: UsageFactsGroup[]): OriginFold[] {
  const byOrigin = new Map<string, OriginFold>();
  for (const group of groups) {
    const origin = group.key.origin;
    const fold = byOrigin.get(origin) ?? {
      origin,
      factRows: 0,
      contextInput: 0,
      newInput: 0,
      cacheRead: 0,
      output: 0,
      hasLowerBounds: false,
    };
    fold.factRows += group.fact_rows;
    fold.contextInput += group.tokens.context_input.tokens ?? 0;
    fold.newInput += group.tokens.new_input.tokens ?? 0;
    fold.cacheRead += group.tokens.cache_read_tokens ?? 0;
    fold.output += group.tokens.output_tokens ?? 0;
    fold.hasLowerBounds =
      fold.hasLowerBounds ||
      !isExact(group.tokens.context_input) ||
      !isExact(group.tokens.new_input);
    byOrigin.set(origin, fold);
  }
  return [...byOrigin.values()].sort((a, b) => b.contextInput - a.contextInput);
}

/**
 * Gruppen → eine Zeile je Modell, absteigend nach Kontext-Input, auf `limit`
 * Zeilen gekappt. Gruppen ohne Modell (`key.model === null`) bleiben hier
 * bewusst draußen — sie sind der voraggregierte `unattributed`-Topf des
 * Payloads und werden dort gezeigt, nicht doppelt.
 */
export function foldGroupsByModel(
  groups: UsageFactsGroup[],
  limit = 10,
): { rows: ModelFold[]; restRows: number; restContextInput: number } {
  const byModel = new Map<string, ModelFold>();
  for (const group of groups) {
    if (group.key.model === null) continue;
    const label = group.key.model_label;
    const fold = byModel.get(label) ?? {
      label,
      factRows: 0,
      contextInput: 0,
      hasLowerBounds: false,
    };
    fold.factRows += group.fact_rows;
    fold.contextInput += group.tokens.context_input.tokens ?? 0;
    fold.hasLowerBounds =
      fold.hasLowerBounds || !isExact(group.tokens.context_input);
    byModel.set(label, fold);
  }
  const sorted = [...byModel.values()].sort(
    (a, b) => b.contextInput - a.contextInput,
  );
  const rows = sorted.slice(0, limit);
  const rest = sorted.slice(limit);
  return {
    rows,
    restRows: rest.length,
    restContextInput: rest.reduce((sum, fold) => sum + fold.contextInput, 0),
  };
}

/** Kompressions-Overhead: die Aux-Gruppen des Payloads (Call-Ebene). */
export function foldAuxGroups(groups: UsageFactsGroup[]): AuxFold {
  const fold: AuxFold = { factRows: 0, contextInput: 0, hasLowerBounds: false };
  for (const group of groups) {
    if (group.key.origin !== AUX_ORIGIN) continue;
    fold.factRows += group.fact_rows;
    fold.contextInput += group.tokens.context_input.tokens ?? 0;
    fold.hasLowerBounds =
      fold.hasLowerBounds || !isExact(group.tokens.context_input);
  }
  return fold;
}

/** Anteil als 0..1, `null` bei leerem Nenner — nie eine erfundene 0 %. */
export function share(part: number, whole: number): number | null {
  return whole > 0 ? part / whole : null;
}
