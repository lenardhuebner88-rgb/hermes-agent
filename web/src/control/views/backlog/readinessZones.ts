// Pure helper: categorise FO backlog items into the three readiness zones.
// Lives in its own module so BacklogSections.tsx (component-only) keeps
// the react-refresh constraint and this function stays independently testable.
import { readinessForFoItem, EXCLUDED_STATUSES } from "../../lib/foBacklog";
import type { BacklogItem } from "../../lib/schemas";

export function partitionReadinessZones(items: BacklogItem[]): {
  ready: BacklogItem[];
  grooming: BacklogItem[];
  ideas: BacklogItem[];
} {
  const ready: BacklogItem[] = [];
  const grooming: BacklogItem[] = [];
  const ideas: BacklogItem[] = [];
  for (const item of items) {
    // Items with excluded statuses (later, done, in_progress, blocked) are always
    // Ideenspeicher/Rohmaterial — regardless of what readinessForFoItem computes.
    // The v1 client fallback returns "ready" for any clean item (including later),
    // which would otherwise place raw idea-storage in the Bereit zone.
    if (EXCLUDED_STATUSES.has(item.status)) {
      ideas.push(item);
      continue;
    }
    const r = readinessForFoItem(item);
    if (r === "ready") {
      ready.push(item);
    } else if (r === "needs_grooming" || r === "drift") {
      grooming.push(item);
    } else {
      ideas.push(item);
    }
  }
  return { ready, grooming, ideas };
}
