// Ausgelagert aus panels.tsx (react-refresh/only-export-components).
import type { ToneName } from "../../lib/types";

export function reviewStepToneClass(tone: ToneName): string {
  switch (tone) {
    case "emerald": return "border-status-ok/20 bg-status-ok/10";
    case "cyan": return "border-live/20 bg-live/10";
    case "amber": return "border-status-warn/20 bg-status-warn/10";
    case "violet": return "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]";
    case "red": return "border-status-alert/20 bg-status-alert/10";
    default: return "border-white/10 bg-black/20";
  }
}
