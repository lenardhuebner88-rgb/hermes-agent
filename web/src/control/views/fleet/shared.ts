/**
 * Fleet-Subtab-übergreifende Typen und Formatter.
 *
 * Beim Zerlegen von FleetView.tsx in Subtab-Dateien wandern die von mehreren
 * Subtabs geteilten Bausteine hierher — reine Extraktion, keine Verhaltensänderung.
 */
import { fmtUsd, type CostDisplayValue } from "../../lib/fleetHub";
import type { PlanSpecsResponse } from "../../lib/schemas";
import type { ChainGraphResponse } from "../../lib/types";

export type PlanSpecRecord = PlanSpecsResponse["planspecs"][number];

export type ChainNode = ChainGraphResponse["nodes"][number];

export function fmtUsdDisplay(cost: CostDisplayValue): string {
  return cost.value != null ? `${fmtUsd(cost.value)}${cost.isEquivalent ? " äquiv." : ""}` : "—";
}
