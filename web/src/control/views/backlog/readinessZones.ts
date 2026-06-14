// Pure helper: categorise FO backlog items into the three readiness zones.
// Lives in its own module so BacklogSections.tsx (component-only) keeps
// the react-refresh constraint and this function stays independently testable.
import { readinessForFoItem } from "../../lib/foBacklog";
import type { BacklogItem } from "../../lib/schemas";

// Statuses that structurally cannot be "Bereit" regardless of readiness:
// - in_progress: already running.
// - done: finished, already out.
// `later` is NOT excluded: `later`+`readiness==="ready"` = a parked-but-done task
// that the backend has promoted to workable — it belongs in Bereit.
const BEREIT_BLOCKED_STATUSES = new Set(["in_progress", "done"]);

export function partitionReadinessZones(items: BacklogItem[]): {
  ready: BacklogItem[];
  grooming: BacklogItem[];
  ideas: BacklogItem[];
} {
  const ready: BacklogItem[] = [];
  const grooming: BacklogItem[] = [];
  const ideas: BacklogItem[] = [];
  for (const item of items) {
    const r = readinessForFoItem(item);
    // Bereit: server (or v1 fallback) says ready, and the status is not structurally
    // ineligible (in_progress is already running; done is finished).
    if (r === "ready" && !BEREIT_BLOCKED_STATUSES.has(item.status)) {
      ready.push(item);
      continue;
    }
    // Schleifen: needs attention — grooming gaps, contract drift, or externally blocked.
    // `blocked` belongs here: it has a concrete blocker the operator can act on, not raw
    // idea-storage.
    if (r === "needs_grooming" || r === "drift" || r === "blocked") {
      grooming.push(item);
      continue;
    }
    // Ideenspeicher: the genuine rest (done, in_progress, raw parked without a clear signal).
    ideas.push(item);
  }
  return { ready, grooming, ideas };
}
