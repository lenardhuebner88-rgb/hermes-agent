/**
 * Ketten-Liste (S1) — pure grouping/sort/filter/search over the ChainModel[]
 * `buildChains()` (fleet.ts) already produces from the board tasks. Replaces
 * the flat 236-entry `<select>` dropdown with three fixed-order groups:
 * "Läuft jetzt" (running) → "Wartet/Gehalten" (waiting) → "Fertig" (done).
 *
 * Framework-neutral so the grouping/title/search/pagination maths is
 * unit-tested without React (mirrors the fleet.ts convention).
 */
import type { ChainModel, ChainTaskLite } from "./fleet";

export type ChainListGroup = "running" | "waiting" | "done";

/** Fixed display order — never re-derive this from data. */
export const CHAIN_LIST_GROUP_ORDER: ChainListGroup[] = ["running", "waiting", "done"];

export interface ChainListEntry<T extends ChainTaskLite> {
  chain: ChainModel<T>;
  group: ChainListGroup;
  /** Resolved display title — see {@link chainDisplayTitle}. */
  title: string;
}

/**
 * Display title for a chain, with a two-step fallback for unnamed/zombie
 * chains (root task missing or archived, so `chain.root` is null):
 *  1. the root task's own title, if present and non-blank;
 *  2. the first non-blank title among the chain's members (stage-sorted, so
 *     effectively the earliest/most-root-like task still on the board) —
 *     this is what turns a chain like the real `t_0e5ca6d6` zombie (root
 *     missing, one orphaned member) into a readable title instead of the
 *     bare id;
 *  3. `<rootId> · <N> Task(s)`, only when every title is blank too.
 */
export function chainDisplayTitle<T extends ChainTaskLite>(chain: ChainModel<T>): string {
  const rootTitle = chain.root?.title?.trim();
  if (rootTitle) return rootTitle;
  const memberTitle = chain.members.map((m) => m.title?.trim()).find((t) => !!t);
  if (memberTitle) return memberTitle;
  return `${chain.rootId} · ${chain.total} ${chain.total === 1 ? "Task" : "Tasks"}`;
}

/** Which of the three fixed groups a chain belongs to. `active` chains
 *  (from buildChains) split into running/waiting by `runningCount`; `done`
 *  chains are always "done". */
export function chainListGroup<T extends ChainTaskLite>(chain: ChainModel<T>): ChainListGroup {
  if (chain.isDone) return "done";
  return chain.runningCount > 0 ? "running" : "waiting";
}

/**
 * Builds the ordered entry list from the {active, done} arrays `buildChains`
 * already produces. `active` is urgency-sorted (running chains rank above
 * review/blocked/plan chains — see `chainUrgency` in fleet.ts), so filtering
 * it into running/waiting preserves that order; `done` is already sorted
 * newest-completed-first. The three groups are then concatenated in the
 * fixed running → waiting → done order.
 */
export function buildChainListEntries<T extends ChainTaskLite>(
  board: { active: ChainModel<T>[]; done: ChainModel<T>[] },
): ChainListEntry<T>[] {
  const toEntry = (group: ChainListGroup) => (chain: ChainModel<T>): ChainListEntry<T> => ({
    chain,
    group,
    title: chainDisplayTitle(chain),
  });
  const running = board.active.filter((c) => c.runningCount > 0).map(toEntry("running"));
  const waiting = board.active.filter((c) => c.runningCount === 0).map(toEntry("waiting"));
  const done = board.done.map(toEntry("done"));
  return [...running, ...waiting, ...done];
}

/** Per-group counts — feeds the filter-chip badges. */
export function chainListCounts<T extends ChainTaskLite>(
  entries: ChainListEntry<T>[],
): Record<ChainListGroup, number> {
  const counts: Record<ChainListGroup, number> = { running: 0, waiting: 0, done: 0 };
  for (const entry of entries) counts[entry.group] += 1;
  return counts;
}

/** Status-filter value: a specific group, or "all" (no filtering). */
export type ChainListFilter = "all" | ChainListGroup;

/** Case-insensitive substring match over the resolved title or the raw
 *  root id — a blank query always matches. */
export function matchesChainSearch<T extends ChainTaskLite>(entry: ChainListEntry<T>, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return entry.title.toLowerCase().includes(q) || entry.chain.rootId.toLowerCase().includes(q);
}

/** Applies the status-filter chip and the text search, in that order,
 *  without disturbing the fixed group order the entries already carry. */
export function filterChainListEntries<T extends ChainTaskLite>(
  entries: ChainListEntry<T>[],
  options: { filter?: ChainListFilter; search?: string } = {},
): ChainListEntry<T>[] {
  const filter = options.filter ?? "all";
  const search = options.search ?? "";
  return entries.filter((entry) => (filter === "all" || entry.group === filter) && matchesChainSearch(entry, search));
}

/** Default page size for the collapsed/paginated "Fertig" group. */
export const CHAIN_LIST_DONE_PAGE_SIZE = 20;

export interface ChainListPage<T extends ChainTaskLite> {
  visible: ChainListEntry<T>[];
  hasMore: boolean;
  remaining: number;
}

/** Slices a (already-filtered) entry list to `visibleCount` — used to collapse
 *  the "Fertig" group behind a "Mehr laden" button instead of rendering
 *  hundreds of finished chains at once. */
export function paginateChainListEntries<T extends ChainTaskLite>(
  entries: ChainListEntry<T>[],
  visibleCount: number,
): ChainListPage<T> {
  const visible = entries.slice(0, Math.max(0, visibleCount));
  const remaining = Math.max(0, entries.length - visible.length);
  return { visible, hasMore: remaining > 0, remaining };
}
