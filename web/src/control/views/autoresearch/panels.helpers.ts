// Ausgelagert aus panels.tsx (react-refresh/only-export-components).
import type { ToneName } from "../../lib/types";

export function reviewStepToneClass(tone: ToneName): string {
  switch (tone) {
    case "emerald": return "border-emerald-500/20 bg-emerald-500/10";
    case "cyan": return "border-cyan-500/20 bg-cyan-500/10";
    case "amber": return "border-amber-500/20 bg-amber-500/10";
    case "violet": return "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]";
    case "red": return "border-red-500/20 bg-red-500/10";
    default: return "border-white/10 bg-black/20";
  }
}
