import { describe, expect, it } from "vitest";
import { BoardResponseSchema, parseOrThrow } from "./schemas";
import { buildChains, type ChainModel } from "./fleet";
import type { BoardTask } from "./types";
import { chainListBoardFixture } from "./chainListBoardFixture";
import {
  buildChainListEntries,
  chainDisplayTitle,
  chainListCounts,
  filterChainListEntries,
  matchesChainSearch,
  paginateChainListEntries,
  CHAIN_LIST_DONE_PAGE_SIZE,
  CHAIN_LIST_GROUP_ORDER,
} from "./chainList";

// The fixture is a real (trimmed) GET /api/plugins/kanban/board response —
// parsing it through the production zod schema + buildChains() exercises the
// exact same pipeline ChainVizView runs on the live board. See
// chainListBoardFixture.ts for capture provenance + the two documented
// status flips (no live chain was running/blocked at capture time).
const parsed = parseOrThrow(BoardResponseSchema, chainListBoardFixture, "chainListBoardFixture");
const allTasks = parsed.columns.flatMap((c) => c.tasks);
const board = buildChains(allTasks);
const entries = buildChainListEntries(board);

describe("buildChainListEntries (real fixture)", () => {
  it("groups running before waiting before done, fixed order", () => {
    expect(entries.map((e) => e.group)).toEqual(["running", "waiting", "done", "done"]);
    expect(CHAIN_LIST_GROUP_ORDER).toEqual(["running", "waiting", "done"]);
  });

  it("puts the real running chain (flipped root status) first", () => {
    expect(entries[0].chain.rootId).toBe("t_d3eb22f2");
    expect(entries[0].title).toBe("Upstream Sync 2026-06-22 Slice 1 — Gateway approval-prompt credential redaction");
  });

  it("puts the real waiting/held chain (flipped member status → blocked) second", () => {
    expect(entries[1].chain.rootId).toBe("t_3731875b");
    expect(entries[1].title).toBe("Review/Einsteuerung PlanSpec A: Worker Review Lifecycle Contract");
  });

  it("sorts the done chains newest-completed-first", () => {
    expect(entries[2].chain.rootId).toBe("t_0e5ca6d6");
    expect(entries[3].chain.rootId).toBe("t_5fb20eb6");
  });

  it("counts each group correctly", () => {
    expect(chainListCounts(entries)).toEqual({ running: 1, waiting: 1, done: 2 });
  });
});

describe("chainDisplayTitle — unnamed/zombie chain fallback", () => {
  it("falls back to the orphaned member's title, not the bare root id (the real t_0e5ca6d6 case from the S1 ticket)", () => {
    const zombie = entries.find((e) => e.chain.rootId === "t_0e5ca6d6")!;
    expect(zombie.chain.root).toBeNull();
    expect(zombie.title).toBe("Agent-Terminals: Gates reparieren und Handoff-PlanSpec einreichen (Build plus Test)");
    expect(zombie.title).not.toBe("t_0e5ca6d6");
  });

  it("picks the stage/priority-sorted first member's title when the root is missing and there are several members", () => {
    const zombie = entries.find((e) => e.chain.rootId === "t_5fb20eb6")!;
    expect(zombie.chain.root).toBeNull();
    expect(zombie.title).toBe("FO-0134 Coder retry: Reverify/Repair /kitchen Termin-Schnellerfassung");
  });

  it("uses the root's own title directly when the root is present", () => {
    const named = entries.find((e) => e.chain.rootId === "t_3731875b")!;
    expect(named.chain.root).not.toBeNull();
    expect(named.title).toBe(named.chain.root!.title);
  });

  // No real chain in the live snapshot has every member title blank — this
  // boundary is exercised with a minimal hand-built ChainModel instead of
  // the real fixture (see the module doc-comment for why the real-data
  // requirement is satisfied above, not here).
  it("falls back to '<rootId> · N Tasks' when every title is blank too", () => {
    const blank: ChainModel<BoardTask> = {
      rootId: "t_blank0000",
      root: null,
      members: [{ title: "  " } as BoardTask, { title: "" } as BoardTask],
      total: 2,
      doneCount: 0,
      stageCounts: { capture: 0, plan: 0, execute: 0, verify: 0, ship: 0 },
      blockedCount: 0,
      runningCount: 0,
      reviewCount: 0,
      isDone: false,
      latestCompletedAt: null,
      tenant: null,
      epicId: null,
    };
    expect(chainDisplayTitle(blank)).toBe("t_blank0000 · 2 Tasks");
  });
});

describe("matchesChainSearch / filterChainListEntries", () => {
  it("matches by title, case-insensitively", () => {
    const hit = entries.find((e) => e.chain.rootId === "t_5fb20eb6")!;
    expect(matchesChainSearch(hit, "kitchen")).toBe(true);
    expect(matchesChainSearch(hit, "KITCHEN")).toBe(true);
    expect(matchesChainSearch(hit, "no-such-term")).toBe(false);
  });

  it("matches by root id substring", () => {
    const results = filterChainListEntries(entries, { search: "t_3731875b" });
    expect(results.map((e) => e.chain.rootId)).toEqual(["t_3731875b"]);
  });

  it("a blank query matches everything", () => {
    expect(filterChainListEntries(entries, { search: "   " })).toHaveLength(entries.length);
  });

  it("status-filter chips isolate a single group, preserving order", () => {
    expect(filterChainListEntries(entries, { filter: "running" }).map((e) => e.chain.rootId)).toEqual(["t_d3eb22f2"]);
    expect(filterChainListEntries(entries, { filter: "waiting" }).map((e) => e.chain.rootId)).toEqual(["t_3731875b"]);
    expect(filterChainListEntries(entries, { filter: "done" }).map((e) => e.chain.rootId)).toEqual(["t_0e5ca6d6", "t_5fb20eb6"]);
    expect(filterChainListEntries(entries, { filter: "all" })).toHaveLength(entries.length);
  });

  it("combines the status filter with the search term", () => {
    const results = filterChainListEntries(entries, { filter: "done", search: "kitchen" });
    expect(results.map((e) => e.chain.rootId)).toEqual(["t_5fb20eb6"]);
  });
});

describe("paginateChainListEntries", () => {
  const doneEntries = filterChainListEntries(entries, { filter: "done" });

  it("default page size is 20", () => {
    expect(CHAIN_LIST_DONE_PAGE_SIZE).toBe(20);
  });

  it("slices to the requested count and reports how many remain", () => {
    const page = paginateChainListEntries(doneEntries, 1);
    expect(page.visible.map((e) => e.chain.rootId)).toEqual(["t_0e5ca6d6"]);
    expect(page.hasMore).toBe(true);
    expect(page.remaining).toBe(1);
  });

  it("hasMore is false once every entry is visible", () => {
    const page = paginateChainListEntries(doneEntries, doneEntries.length);
    expect(page.hasMore).toBe(false);
    expect(page.remaining).toBe(0);
  });

  it("a visibleCount above the total just returns everything", () => {
    const page = paginateChainListEntries(doneEntries, 999);
    expect(page.visible).toHaveLength(doneEntries.length);
    expect(page.hasMore).toBe(false);
  });
});
