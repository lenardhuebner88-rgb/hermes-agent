import { useTwoPaneExpanded } from "../../components/leitstand";

/** Dünner Alias auf den kanonischen TwoPane-Fork-Hook (eine Quelle, W6-Konsolidierung). */
export function useExpandedLibraryPane(): boolean {
  return useTwoPaneExpanded();
}
