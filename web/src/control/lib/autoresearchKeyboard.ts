import { KEYMAP } from "./keymap";

export type AutoresearchKeyboardAction = "select-top" | "select-visible" | "clear-selection" | null;

export function getAutoresearchKeyboardAction(input: {
  key: string;
  hasTopProposal: boolean;
  hasVisibleProposals: boolean;
  hasSelection: boolean;
}): AutoresearchKeyboardAction {
  const key = input.key.toLowerCase();
  if (KEYMAP.autoresearch.selectTop.includes(key as "t") && input.hasTopProposal) return "select-top";
  if (KEYMAP.autoresearch.selectVisible.includes(key as "v") && input.hasVisibleProposals) return "select-visible";
  if (KEYMAP.autoresearch.clearSelection.includes(input.key as "Escape") && input.hasSelection) return "clear-selection";
  return null;
}
