// Ausgelagert aus RunTimelineView.tsx (react-refresh/only-export-components).
import type { ToneName } from "../lib/types";
import type { TimelineItem } from "./RunTimelineView";

const RED_KINDS = new Set([
  "error", "crashed", "timed_out", "spawn_failed", "gave_up", "stale",
  "completion_blocked_hallucination", "suspected_hallucinated_references",
  "iteration_budget_exhausted",
]);
const AMBER_KINDS = new Set([
  "reclaimed", "claim_rejected", "claim_extended", "respawn_guarded",
  "budget_held", "role_fit_held", "auto_continuation_scheduled",
  "auto_continuation_exhausted", "auto_continuation_disabled",
]);
const GREEN_KINDS = new Set([
  "completed", "spawned", "claimed", "promoted", "unblocked",
  "run_started", "submitted_for_review", "workflow_step_advanced",
]);

/** Farbcode laut Plan: grün=ok · rot=error · gelb=retry · grau=blocked/neutral. */
export function eventTone(item: Pick<TimelineItem, "kind" | "payload">): ToneName {
  const kind = item.kind;
  if (kind === "run_ended") {
    const outcome = String(
      (item.payload as Record<string, unknown> | null)?.outcome ?? "",
    ).toLowerCase();
    if (outcome === "completed") return "emerald";
    if (outcome === "blocked" || outcome === "") return "zinc";
    return "red";
  }
  if (RED_KINDS.has(kind)) return "red";
  if (AMBER_KINDS.has(kind)) return "amber";
  if (kind === "blocked") return "zinc";
  if (GREEN_KINDS.has(kind)) return "emerald";
  return "zinc";
}
