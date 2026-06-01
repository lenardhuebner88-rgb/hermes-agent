// Board-intelligence helpers for the Orchestrator backlog: dependency readiness.
// Pure logic (no React / no fetch) so it stays unit-tested in isolation
// (orchestration.test.ts). The view layers i18n/rendering on top of these.

export type DepState = "done" | "pending" | "missing";

type ItemRef = { id: string; status: string };

/** State of a single dependency relative to the loaded board. */
export function depState(depId: string, items: ReadonlyArray<ItemRef>): DepState {
  const found = items.find((it) => it.id === depId);
  if (!found) return "missing";
  return found.status === "done" ? "done" : "pending";
}

export type Readiness =
  | { state: "ready"; blockedBy: [] }
  | { state: "blocked"; blockedBy: string[] }
  | { state: "neutral"; blockedBy: [] };

/**
 * Readiness of a backlog item to be started. Only `todo` items carry a
 * ready/blocked signal — anything already running/done/queued is `neutral`.
 * A `todo` item is `ready` when every dependency is `done`, otherwise
 * `blocked` and `blockedBy` lists the dependency ids that are not yet done
 * (a missing dependency counts as not-done).
 */
export function readiness(
  item: { status: string; dependsOn?: string[] },
  items: ReadonlyArray<ItemRef>,
): Readiness {
  if (item.status !== "todo") return { state: "neutral", blockedBy: [] };
  const blockedBy = (item.dependsOn ?? []).filter((id) => depState(id, items) !== "done");
  if (blockedBy.length > 0) return { state: "blocked", blockedBy };
  return { state: "ready", blockedBy: [] };
}
