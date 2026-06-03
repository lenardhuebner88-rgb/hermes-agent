// Board-intelligence helpers for the Orchestrator backlog: dependency readiness,
// next-task selection, commission prompt, filtering and sorting.
// Pure logic (no React / no fetch) — unit-tested in orchestration.test.ts.

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

// ── Commission / dispatch helpers ────────────────────────────────────────────

const PRIORITY_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 };

/**
 * Pick the single best next task: status=todo, all dependsOn either done or
 * missing (missing ≠ blocking per spec), ranked high>medium>low then oldest
 * `created`. Returns null when no candidate exists.
 */
export function computeNextTaskId(
  items: ReadonlyArray<{ id: string; status: string; priority: string; created: string; dependsOn?: string[] }>,
): string | null {
  const candidates = items
    .filter((it) => it.status === "todo")
    .filter((it) =>
      (it.dependsOn ?? []).every((depId) => {
        const s = depState(depId, items);
        return s === "done" || s === "missing";
      }),
    );
  if (candidates.length === 0) return null;
  const sorted = [...candidates].sort((a, b) => {
    const pa = PRIORITY_ORDER[a.priority] ?? 1;
    const pb = PRIORITY_ORDER[b.priority] ?? 1;
    if (pa !== pb) return pa - pb;
    return a.created.localeCompare(b.created);
  });
  return sorted[0]?.id ?? null;
}

/** Build the standard dispatch prompt from a loaded detail (title, id, root, gate). */
export function buildCommissionPrompt(detail: {
  title: string;
  id: string;
  root: string;
  gate: string;
}): string {
  const { title, id, root, gate } = detail;
  return `Du bist eine Orchestrator-Session auf dem Homeserver mit vollem Zugriff. Arbeite GENAU EINEN Backlog-Task ab.
TASK: ${title}   (id: ${id})
SPEC: ~/orchestration/backlog/${id}.md  ← ZUERST vollständig lesen (root, gate, dependsOn, Constraints, ## Subtasks)
ROOT: ${root}   GATE: ${gate}
1) Preflight: cd ${root} + \`git status\` (additiv, fremde uncommittete Arbeit in Ruhe).
2) planGate:true → NICHT bauen: kurzer Subtask-Plan, zurück zu mir für Go. STOPP.
3) sonst Task umsetzen (orchestrate-Skill / Workflow-Harness erlaubt).
4) Gate fahren: ${gate} — WIRKLICH grün (Mocks = Regressions-Wächter, kein Erstbeweis).
5) E2E-Browser-Check root-aware: FO→\`npm run gate:e2e\` · ~/.hermes/hermes-agent→nach Build+Restart UI am
   Tailnet-Dashboard via \`chromium-shot\`/\`verify\` sichten · kein UI-Task→skip+vermerken.
6) NUR bei grün: in ~/orchestration/backlog/${id}.md status:todo→done + \`## Receipt\` (Commit-Hash·getestet·
   deployed). Commit im Task-Repo (KEIN origin-Push; piet-fork/lokal) + im Backlog-Repo.
7) root=~/.hermes/hermes-agent + UI/Backend → web build → \`systemctl --user restart hermes-dashboard.service\`
   (koordiniert) → live-verify.
8) Discord-Report (nie nur Telegram): Status + Commit + Receipt.
ABBRUCH (stop & melde, NICHT loopen/raten): Gate 2–3× rot · DB-Migration/destruktiv · Spec mehrdeutig ·
etwas außerhalb des Task-Scopes müsste geändert werden.`;
}

/** Derive a human-friendly project name from a root path. */
export function projectFromRoot(root?: string): string {
  if (!root) return "Orchestration";
  if (root.includes("hermes-agent")) return "Dashboard";
  if (root.includes("family-organizer")) return "Family Organizer";
  if (root.includes("orchestration")) return "Orchestration";
  const parts = root.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? root;
}

// ── Filter / Sort ─────────────────────────────────────────────────────────────

type FilterableItem = {
  id: string;
  title: string;
  status: string;
  priority: string;
  created: string;
  planGate: boolean;
  dependsOn?: string[];
  root?: string;
  excerpt?: string;
};

export type SortKey = "priority" | "age" | "readiness";

const READINESS_ORDER: Record<string, number> = { ready: 0, neutral: 1, blocked: 2 };

/** Filter items by free-text query and optional field filters. */
export function filterItems<T extends FilterableItem>(
  items: ReadonlyArray<T>,
  q: string,
  filters: {
    priority?: string;
    project?: string;
    planGate?: string;
    readiness?: string;
  } = {},
  allItems: ReadonlyArray<ItemRef> = [],
): T[] {
  let result: T[] = [...items];

  const qLow = q.trim().toLowerCase();
  if (qLow) {
    result = result.filter(
      (it) =>
        it.title.toLowerCase().includes(qLow) ||
        it.id.toLowerCase().includes(qLow) ||
        (it.excerpt ?? "").toLowerCase().includes(qLow),
    );
  }

  if (filters.priority) result = result.filter((it) => it.priority === filters.priority);
  if (filters.planGate) result = result.filter((it) => it.planGate === (filters.planGate === "true"));
  if (filters.project) result = result.filter((it) => projectFromRoot(it.root) === filters.project);
  if (filters.readiness) {
    result = result.filter((it) => readiness(it, allItems).state === filters.readiness);
  }

  return result;
}

/** Sort items in-place (returns new array). */
export function sortItems<T extends FilterableItem>(
  items: ReadonlyArray<T>,
  key: SortKey,
  allItems: ReadonlyArray<ItemRef> = [],
): T[] {
  return [...items].sort((a, b) => {
    if (key === "priority") {
      return (PRIORITY_ORDER[a.priority] ?? 1) - (PRIORITY_ORDER[b.priority] ?? 1);
    }
    if (key === "age") {
      return a.created.localeCompare(b.created);
    }
    if (key === "readiness") {
      const ra = readiness(a, allItems).state;
      const rb = readiness(b, allItems).state;
      return (READINESS_ORDER[ra] ?? 1) - (READINESS_ORDER[rb] ?? 1);
    }
    return 0;
  });
}
