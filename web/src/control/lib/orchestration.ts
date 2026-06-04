// Board-intelligence helpers for the Orchestrator backlog: dependency readiness,
// next-task selection, commission prompt, filtering and sorting.
// Pure logic (no React / no fetch) — unit-tested in orchestration.test.ts.

export type DepState = "done" | "pending" | "missing";

type ItemRef = { id: string; status: string };

export const KNOWN_STATUSES = ["backlog", "todo", "doing", "review", "done"] as const;
export type KnownStatus = (typeof KNOWN_STATUSES)[number];

const KNOWN_STATUS_SET = new Set<string>(KNOWN_STATUSES);

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

export function isKnownStatus(status: string): status is KnownStatus {
  return KNOWN_STATUS_SET.has(status);
}

export function ageDays(created: string, nowSec: number): number | null {
  if (!created) return null;
  const t = Date.parse(`${created}T00:00:00Z`);
  if (Number.isNaN(t)) return null;
  return Math.floor((nowSec * 1000 - t) / 86_400_000);
}

export function ageLabel(created: string, nowSec: number): string {
  const days = ageDays(created, nowSec);
  if (days === null) return created || "-";
  if (days <= 0) return "heute";
  if (days === 1) return "gestern";
  if (days < 7) return `vor ${days} T`;
  if (days < 30) return `vor ${Math.floor(days / 7)} Wo`;
  return `vor ${Math.floor(days / 30)} Mon`;
}

type QueueSignalItem = {
  id: string;
  status: string;
  priority: string;
  created: string;
  owner?: string;
  lastProof?: string;
  dependsOn?: string[];
  planGate?: boolean;
};

type ContractHealthRef = {
  source_count?: number;
  counted_sum?: number;
  unknown_statuses?: ReadonlyArray<{ count?: number }>;
  invalid_priority_count?: number;
  missing_dep_count?: number;
};

export function isQueueVisible(status: string): boolean {
  return status !== "done";
}

export function isStaleProof(item: QueueSignalItem, nowSec: number, staleDays = 3): boolean {
  if (!isQueueVisible(item.status)) return false;
  if ((item.lastProof ?? "").trim()) return false;
  const days = ageDays(item.created, nowSec);
  return days !== null && days >= staleDays;
}

export function contractDriftCount(health?: ContractHealthRef | null): number {
  if (!health) return 0;
  const unknownCount = (health.unknown_statuses ?? []).reduce((sum, entry) => sum + (entry.count ?? 0), 0);
  const countGap = Math.max(0, (health.source_count ?? 0) - (health.counted_sum ?? 0));
  return Math.max(unknownCount, countGap) + (health.invalid_priority_count ?? 0) + (health.missing_dep_count ?? 0);
}

export function deriveQueueSignals(
  items: ReadonlyArray<QueueSignalItem>,
  health: ContractHealthRef | null | undefined,
  nowSec: number,
) {
  const visible = items.filter((item) => isQueueVisible(item.status));
  return {
    ready: visible.filter((item) => readiness(item, items).state === "ready").length,
    blocked: visible.filter((item) => readiness(item, items).state === "blocked").length,
    unowned: visible.filter((item) => !(item.owner ?? "").trim()).length,
    staleProof: visible.filter((item) => isStaleProof(item, nowSec)).length,
    highRisk: visible.filter((item) => item.priority === "high").length,
    contractDrift: contractDriftCount(health),
  };
}

export function nextActionForItem(item: QueueSignalItem, items: ReadonlyArray<ItemRef>): string {
  if (!isKnownStatus(item.status)) return "Status klären";
  const r = readiness(item, items);
  if (r.state === "blocked") return `Dependency klären: ${r.blockedBy.slice(0, 2).join(", ")}`;
  if (item.status === "todo" && item.planGate) return "Plan-Gate entscheiden";
  if (r.state === "ready") return "Beauftragen";
  if (item.status === "doing") return "Fortschritt prüfen";
  if (item.status === "review") return "Proof prüfen";
  if (item.status === "backlog") return "Priorisieren";
  if (item.status === "done") return "Receipt prüfen";
  return "Einordnen";
}

// ── Commission / dispatch helpers ────────────────────────────────────────────

const PRIORITY_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 };

/**
 * Pick the single best next task: status=todo and readiness=ready, ranked
 * high>medium>low then oldest `created`. Missing dependencies are blocking,
 * matching readiness().
 */
export function computeNextTaskId(
  items: ReadonlyArray<{ id: string; status: string; priority: string; created: string; dependsOn?: string[] }>,
): string | null {
  const candidates = items
    .filter((it) => it.status === "todo")
    .filter((it) => readiness(it, items).state === "ready");
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
