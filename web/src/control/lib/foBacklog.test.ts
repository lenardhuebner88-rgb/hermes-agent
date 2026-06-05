import { describe, it, expect } from "vitest";
import {
  computeNextFoTaskId,
  buildFoAuditPrompt,
  buildFoCommissionPrompt,
  filterFoItems,
  foHealthStripCounts,
  nextActionForFoItem,
  ownerLoadSummary,
  matchesFoQuickView,
  qualityFlagsForFoItem,
  queueStateForFoItem,
  rankFoItems,
  rankedQueueWithReasons,
  readinessForFoItem,
  reasonCodesForFoItem,
  sortFoItems,
  staleSignalForFoItem,
} from "./foBacklog";
import type { BacklogItem, BacklogDetail } from "./schemas";

function item(overrides: Partial<BacklogItem> & { id: string }): BacklogItem {
  return {
    title: `Task ${overrides.id}`,
    status: "next",
    owner: "claude",
    risk: "low",
    area: "lists",
    updated: "2026-06-01",
    lane: null,
    result: null,
    stale: false,
    excerpt: undefined,
    ...overrides,
  };
}

function detail(overrides: Partial<BacklogDetail> & { id: string }): BacklogDetail {
  return {
    title: `Task ${overrides.id}`,
    status: "now",
    owner: "claude",
    risk: "low",
    area: "lists",
    updated: "2026-06-01",
    lane: null,
    result: null,
    stale: false,
    body: "",
    decision: [],
    acceptance_criteria: [],
    proofs: [],
    blockers: [],
    next_action: "",
    source_path: "",
    source_ref: "",
    links: [],
    ...overrides,
  };
}

// --- computeNextFoTaskId ---

describe("computeNextFoTaskId", () => {
  it("returns null for empty list", () => {
    expect(computeNextFoTaskId([])).toBeNull();
  });

  it("picks the only now item", () => {
    const items = [item({ id: "0001", status: "now", updated: "2026-06-01" })];
    expect(computeNextFoTaskId(items)).toBe("0001");
  });

  it("prefers now over next", () => {
    const items = [
      item({ id: "0002", status: "next", updated: "2026-01-01" }),
      item({ id: "0001", status: "now", updated: "2026-06-01" }),
    ];
    expect(computeNextFoTaskId(items)).toBe("0001");
  });

  it("among multiple now items picks oldest updated", () => {
    const items = [
      item({ id: "0002", status: "now", updated: "2026-06-02" }),
      item({ id: "0001", status: "now", updated: "2026-05-01" }),
    ];
    expect(computeNextFoTaskId(items)).toBe("0001");
  });

  it("falls back to next when no now items", () => {
    const items = [
      item({ id: "0003", status: "later", updated: "2026-01-01" }),
      item({ id: "0002", status: "next", updated: "2026-06-01" }),
      item({ id: "0001", status: "next", updated: "2026-05-01" }),
    ];
    expect(computeNextFoTaskId(items)).toBe("0001");
  });

  it("skips done/later/in_progress for next-pick", () => {
    const items = [
      item({ id: "0001", status: "done" }),
      item({ id: "0002", status: "later" }),
      item({ id: "0003", status: "in_progress" }),
    ];
    expect(computeNextFoTaskId(items)).toBeNull();
  });

  it("prefers non-stale over stale within same status", () => {
    const items = [
      item({ id: "0001", status: "now", stale: true, updated: "2020-01-01" }),
      item({ id: "0002", status: "now", stale: false, updated: "2026-06-01" }),
    ];
    expect(computeNextFoTaskId(items)).toBe("0002");
  });

  it("uses queueState semantics and ignores unknown-status drift", () => {
    const items = [
      item({ id: "0001", status: "readyish" as BacklogItem["status"], updated: "2026-01-01" }),
      item({ id: "0002", status: "next", updated: "2026-06-01" }),
    ];
    expect(queueStateForFoItem(items[0]).state).toBe("drift");
    expect(computeNextFoTaskId(items)).toBe("0002");
  });
});

describe("FO queue intelligence", () => {
  it("classifies all canonical queue states without remapping unknown statuses", () => {
    expect(queueStateForFoItem(item({ id: "n", status: "now" })).state).toBe("now");
    expect(queueStateForFoItem(item({ id: "x", status: "readyish" as BacklogItem["status"] }))).toEqual({
      state: "drift",
      reason: "unknown_status",
    });
  });

  it("marks stale proof/update separately from fresh and missing update states", () => {
    expect(staleSignalForFoItem(item({ id: "old", status: "in_progress", updated: "2026-05-20" }), 1780524000).state).toBe("stale");
    expect(staleSignalForFoItem(item({ id: "none", status: "next", updated: "" }), 1780524000).state).toBe("missing_update");
    expect(staleSignalForFoItem(item({ id: "done", status: "done", updated: "2026-05-20" }), 1780524000).state).toBe("fresh");
  });

  it("summarizes active owner load with high-risk and stale pressure", () => {
    const summary = ownerLoadSummary([
      // next + unassigned = ruhige Queue → NICHT unready (vereinheitlichtes Claim-Modell).
      item({ id: "a", owner: "unassigned", risk: "high", status: "next" }),
      // in_progress + unassigned = aktiv ohne Owner → unready.
      item({ id: "d", owner: "unassigned", risk: "low", status: "in_progress" }),
      item({ id: "b", owner: "claude", risk: "medium", status: "in_progress", stale: true }),
      item({ id: "c", owner: "claude", risk: "low", status: "done" }),
    ]);
    expect(summary).toEqual([
      { owner: "unassigned", total: 2, highRisk: 1, stale: 0, unready: 1 },
      { owner: "claude", total: 1, highRisk: 0, stale: 1, unready: 0 },
    ]);
  });

  it("ranks risk, age, and business impact ahead of low-impact later work (deterministic with nowSec)", () => {
    const nowSec = Math.floor(Date.parse("2026-06-10T00:00:00Z") / 1000);
    const ranked = rankFoItems([
      item({ id: "low", status: "later", risk: "low", area: "process", updated: "2026-06-01" }),
      item({ id: "high", status: "next", risk: "high", area: "db", updated: "2026-06-03" }),
      item({ id: "old", status: "next", risk: "medium", area: "lists", updated: "2026-05-01" }),
    ], nowSec);
    expect(ranked.map((it) => it.id)).toEqual(["high", "old", "low"]);
  });

  it("ranking is stable for a fixed nowSec regardless of wall-clock", () => {
    const items = [
      item({ id: "a", status: "next", risk: "high", area: "db", updated: "2026-06-03" }),
      item({ id: "b", status: "later", risk: "low", area: "process", updated: "2026-06-01" }),
    ];
    const a = rankFoItems(items, 1_750_000_000).map((it) => it.id);
    const b = rankFoItems(items, 1_750_000_000).map((it) => it.id);
    expect(a).toEqual(b);
  });

  it("rankScore prefers the server age_days fact over the client clock", () => {
    const nowSec = Math.floor(Date.parse("2026-06-10T00:00:00Z") / 1000);
    // Two next items, equal except one carries a large server-provided age_days.
    const [aged, fresh] = rankFoItems([
      item({ id: "fresh", status: "next", risk: "low", area: "lists", updated: "2026-06-09", age_days: 1 }),
      item({ id: "aged", status: "next", risk: "low", area: "lists", updated: "2026-06-09", age_days: 30 }),
    ], nowSec);
    // The item with the bigger server age_days outranks its otherwise-identical twin.
    expect([aged.id, fresh.id]).toEqual(["aged", "fresh"]);
  });

  it("derives next action and task-quality flags without mutating backlog data", () => {
    const weak = item({
      id: "0009",
      title: "Fix",
      owner: "unassigned",
      status: "in_progress", // aktiv ohne Owner → unclear_owner feuert
      updated: "2026-05-01",
      stale: true,
    });
    const d = detail({
      id: "0009",
      title: "Fix",
      owner: "unassigned",
      status: "next",
      body: "## Kontext\n\n" + "- Punkt\n".repeat(12),
    });

    expect(nextActionForFoItem(weak, d)).toBe("Akzeptanzkriterien und konkreten nächsten Schritt klären.");
    expect(qualityFlagsForFoItem(weak, d).map((flag) => flag.kind)).toEqual([
      "weak_title",
      "missing_acceptance",
      "unclear_owner",
      "stale_update",
      "large_scope",
      "missing_next_action",
    ]);
  });

  it("computes health strip counts from items plus endpoint contract health", () => {
    const counts = foHealthStripCounts(
      [
        item({ id: "n", status: "now", risk: "low" }),
        item({ id: "x", status: "next", risk: "high", owner: "unassigned" }),
        item({ id: "b", status: "blocked", risk: "medium", stale: true }),
      ],
      {
        source_count: 3,
        counted_sum: 2,
        unknown_statuses: [{ status: "readyish", count: 1, ids: ["d"] }],
        invalid_risk_count: 1,
        invalid_owner_count: 0,
        unowned_count: 1,
        stale_count: 1,
        missing_acceptance_count: 2,
        missing_next_action_count: 1,
        invalid_area_count: 0,
      },
    );

    expect(counts).toEqual({
      now: 1,
      nextReady: 1,
      blocked: 1,
      unowned: 1,
      stale: 1,
      highRisk: 1,
      contractDrift: 2,
      missingAcceptance: 2,
    });
  });
});

// --- buildFoCommissionPrompt ---

describe("buildFoCommissionPrompt", () => {
  it("includes task title and id in the prompt", () => {
    const d = detail({ id: "0001", title: "Add shopping list feature" });
    const prompt = buildFoCommissionPrompt(d);
    expect(prompt).toContain("0001");
    expect(prompt).toContain("Add shopping list feature");
  });

  it("references the correct spec path", () => {
    const d = detail({ id: "0042", title: "X", source_path: "backlog/items/0042-x.md" });
    const prompt = buildFoCommissionPrompt(d);
    expect(prompt).toContain("~/projects/family-organizer/backlog/items/0042-x.md");
  });

  it("includes the gate command", () => {
    const d = detail({ id: "0001", title: "X" });
    const prompt = buildFoCommissionPrompt(d);
    // Default-Gate ist das schnelle (lint+test+build), nicht das Browser-E2E-Gate.
    expect(prompt).toContain("GATE: npm run gate\n");
    expect(prompt).toContain("npm run gate:e2e");
  });

  it("is a non-empty string", () => {
    const d = detail({ id: "0001", title: "X" });
    expect(buildFoCommissionPrompt(d).length).toBeGreaterThan(50);
  });
});

// --- buildFoAuditPrompt ---

describe("buildFoAuditPrompt", () => {
  it("targets the FO project root and is read-first (no code)", () => {
    const prompt = buildFoAuditPrompt();
    expect(prompt).toContain("~/projects/family-organizer");
    expect(prompt).toContain("READ-FIRST AUDIT");
    expect(prompt).toContain("KEIN Code");
  });

  it("asks for new backlog item proposals and a next-task recommendation", () => {
    const prompt = buildFoAuditPrompt();
    expect(prompt).toContain("NEUE Backlog-Items");
    expect(prompt).toContain("EMPFEHLUNG");
  });

  it("embeds the live board summary when provided", () => {
    const prompt = buildFoAuditPrompt({ active: 14, done: 99, stale: 2, unowned: 1, highRisk: 3, missingAcceptance: 4, contractDrift: 0 });
    expect(prompt).toContain("AKTUELLER STAND");
    expect(prompt).toContain("14 aktiv");
    expect(prompt).toContain("99 erledigt");
  });

  it("omits the board summary line when no counts are passed", () => {
    expect(buildFoAuditPrompt()).not.toContain("AKTUELLER STAND");
  });

  it("is a non-empty string", () => {
    expect(buildFoAuditPrompt().length).toBeGreaterThan(50);
  });
});

// --- filterFoItems ---

describe("filterFoItems", () => {
  const items = [
    item({ id: "0001", title: "Groceries list", owner: "piet", risk: "low", area: "kitchen" }),
    item({ id: "0002", title: "Calendar sync", owner: "claude", risk: "medium", area: "planning" }),
    item({ id: "0003", title: "School schedule", owner: "piet", risk: "high", area: "education", stale: true }),
  ];

  it("returns all when no filter", () => {
    expect(filterFoItems(items, "", {})).toHaveLength(3);
  });

  it("filters by query against title", () => {
    expect(filterFoItems(items, "groceries", {})).toHaveLength(1);
    expect(filterFoItems(items, "groceries", {})[0].id).toBe("0001");
  });

  it("filters by query against owner", () => {
    expect(filterFoItems(items, "claude", {})).toHaveLength(1);
  });

  it("filters by query against area", () => {
    expect(filterFoItems(items, "planning", {})).toHaveLength(1);
  });

  it("filters by owner", () => {
    expect(filterFoItems(items, "", { owner: "piet" })).toHaveLength(2);
  });

  it("filters by risk", () => {
    expect(filterFoItems(items, "", { risk: "high" })).toHaveLength(1);
  });

  it("filters by stale=true", () => {
    expect(filterFoItems(items, "", { stale: true })).toHaveLength(1);
    expect(filterFoItems(items, "", { stale: true })[0].id).toBe("0003");
  });

  it("combines query and filter", () => {
    expect(filterFoItems(items, "schedule", { owner: "piet" })).toHaveLength(1);
    expect(filterFoItems(items, "schedule", { owner: "claude" })).toHaveLength(0);
  });
});

// --- sortFoItems ---

describe("sortFoItems", () => {
  const items = [
    item({ id: "0001", risk: "low", updated: "2026-06-01", status: "next" }),
    item({ id: "0002", risk: "high", updated: "2026-03-01", status: "now" }),
    item({ id: "0003", risk: "medium", updated: "2026-01-01", status: "later" }),
  ];

  it("sort by risk: high first", () => {
    const sorted = sortFoItems(items, "risk");
    expect(sorted[0].id).toBe("0002");
    expect(sorted[1].id).toBe("0003");
    expect(sorted[2].id).toBe("0001");
  });

  it("sort by age: oldest updated first", () => {
    const sorted = sortFoItems(items, "age");
    expect(sorted[0].id).toBe("0003");
    expect(sorted[2].id).toBe("0001");
  });

  it("sort by status: now < next < later", () => {
    const sorted = sortFoItems(items, "status");
    expect(sorted[0].id).toBe("0002"); // now
    expect(sorted[1].id).toBe("0001"); // next
    expect(sorted[2].id).toBe("0003"); // later
  });

  it("does not mutate original array", () => {
    const orig = [...items];
    sortFoItems(items, "risk");
    expect(items.map((it) => it.id)).toEqual(orig.map((it) => it.id));
  });
});

// --- v2: prefer server-computed facts, with graceful client fallback ---

const NOW_SEC = Math.floor(Date.parse("2026-06-10T00:00:00Z") / 1000);

describe("prefer-server facts", () => {
  it("qualityFlagsForFoItem uses the server taxonomy when present (server is authoritative)", () => {
    const withServer = item({
      id: "0001",
      title: "Fix",          // would be a client weak_title
      owner: "unassigned",   // would be a client unclear_owner
      quality_issues: [
        { code: "missing_acceptance", severity: "risk" },
        { code: "large_scope", severity: "warn" },
      ],
    });
    // Only the server-listed issues surface — client heuristics do not re-fire.
    expect(qualityFlagsForFoItem(withServer).map((f) => f.kind)).toEqual([
      "missing_acceptance",
      "large_scope",
    ]);
  });

  it("qualityFlagsForFoItem falls back to client heuristics when the server field is absent (v1)", () => {
    const v1 = item({ id: "0001", title: "Fix", owner: "unassigned", status: "in_progress" });
    const kinds = qualityFlagsForFoItem(v1).map((f) => f.kind);
    expect(kinds).toContain("weak_title");
    expect(kinds).toContain("unclear_owner");
  });

  it("staleSignalForFoItem prefers server freshness/age_days", () => {
    const stale = item({ id: "x", status: "in_progress", updated: "2026-06-09", freshness: "stale", age_days: 12 });
    expect(staleSignalForFoItem(stale, NOW_SEC)).toEqual({ state: "stale", label: "12 Tage ohne Beleg" });

    const aging = item({ id: "y", status: "next", updated: "2026-06-05", freshness: "aging", age_days: 5 });
    expect(staleSignalForFoItem(aging, NOW_SEC).state).toBe("aging");

    const noProof = item({ id: "z", status: "next", updated: "", freshness: "no_proof", age_days: null });
    expect(staleSignalForFoItem(noProof, NOW_SEC).state).toBe("missing_update");
  });

  it("staleSignalForFoItem falls back to client logic when freshness is absent (v1)", () => {
    const v1 = item({ id: "a", status: "in_progress", updated: "2026-05-01", stale: true });
    expect(staleSignalForFoItem(v1, NOW_SEC).state).toBe("stale");
  });
});

// --- v2: reason codes + ranked queue ---

describe("reasonCodesForFoItem", () => {
  it("explains a high-priority candidate via the server facts", () => {
    const candidate = item({
      id: "r",
      status: "in_progress", // aktiv ohne Owner → penalty_unowned feuert
      risk: "high",
      area: "db",
      owner: "unassigned",
      updated: "2026-05-20",
      quality_issues: [{ code: "missing_acceptance", severity: "risk" }],
      readiness: "needs_grooming",
    });
    const codes = reasonCodesForFoItem(candidate, NOW_SEC);
    expect(codes).toEqual(
      expect.arrayContaining([
        "in_progress",
        "high_risk",
        "high_impact_area",
        "aged",
        "penalty_unowned",
        "missing_acceptance",
        "needs_grooming",
      ]),
    );
  });
});

describe("rankedQueueWithReasons", () => {
  it("excludes done items, ranks active work, and attaches reason codes + score", () => {
    const ranked = rankedQueueWithReasons([
      item({ id: "done1", status: "done" }),
      item({ id: "hi", status: "now", risk: "high", area: "db", updated: "2026-06-09" }),
      item({ id: "lo", status: "later", risk: "low", area: "process", updated: "2026-06-09" }),
    ], NOW_SEC);

    expect(ranked.map((c) => c.item.id)).toEqual(["hi", "lo"]);
    expect(ranked[0].reasonCodes).toContain("now_status");
    expect(ranked.every((c) => typeof c.score === "number")).toBe(true);
  });
});

describe("readiness + quick views", () => {
  it("readinessForFoItem prefers the server fact, falls back to client heuristics", () => {
    expect(readinessForFoItem(item({ id: "a", readiness: "needs_grooming" }))).toBe("needs_grooming");
    expect(readinessForFoItem(item({ id: "b", status: "blocked" }))).toBe("blocked");
    expect(readinessForFoItem(item({ id: "c", status: "weird" }))).toBe("drift");
    // v1 fallback: in_progress + unassigned owner → risk flag → needs_grooming
    expect(readinessForFoItem(item({ id: "d", owner: "unassigned", status: "in_progress" }))).toBe("needs_grooming");
    // ruhige Queue (next) + unassigned → kein Owner-Risk-Flag mehr → ready
    expect(readinessForFoItem(item({ id: "d2", owner: "unassigned", status: "next" }))).toBe("ready");
    expect(readinessForFoItem(item({ id: "e", owner: "claude", quality_issues: [] }))).toBe("ready");
  });

  it("matchesFoQuickView selects the right working set", () => {
    const ready = item({ id: "r", readiness: "ready" });
    const groom = item({ id: "g", readiness: "needs_grooming" });
    const stale = item({ id: "s", freshness: "stale" });
    const unowned = item({ id: "u", owner: "unassigned", status: "in_progress" });
    const quietUnowned = item({ id: "qu", owner: "unassigned", status: "later" });

    expect(matchesFoQuickView(ready, "all")).toBe(true);
    expect(matchesFoQuickView(ready, "ready")).toBe(true);
    expect(matchesFoQuickView(groom, "ready")).toBe(false);
    expect(matchesFoQuickView(groom, "groom")).toBe(true);
    expect(matchesFoQuickView(stale, "stale")).toBe(true);
    expect(matchesFoQuickView(ready, "stale")).toBe(false);
    // unowned quick-view zeigt nur aktiv-ohne-Owner (in_progress), nicht die ruhige Queue.
    expect(matchesFoQuickView(unowned, "unowned")).toBe(true);
    expect(matchesFoQuickView(quietUnowned, "unowned")).toBe(false);
  });

  it("owner-gap signals only fire for in_progress, never the quiet queue (unified claim model)", () => {
    const active = item({ id: "ip", owner: "unassigned", status: "in_progress" });
    const quiet = item({ id: "lt", owner: "unassigned", status: "later" });

    // Client-Heuristik (v1-Fallback): unclear_owner nur bei in_progress.
    expect(qualityFlagsForFoItem(active).map((f) => f.kind)).toContain("unclear_owner");
    expect(qualityFlagsForFoItem(quiet).map((f) => f.kind)).not.toContain("unclear_owner");

    // Reason-Code-Penalty nur bei in_progress.
    expect(reasonCodesForFoItem(active, NOW_SEC)).toContain("penalty_unowned");
    expect(reasonCodesForFoItem(quiet, NOW_SEC)).not.toContain("penalty_unowned");

    // HealthStrip-Fallback (kein Server-Count) zaehlt nur in_progress-ohne-Owner.
    const counts = foHealthStripCounts([active, quiet]);
    expect(counts.unowned).toBe(1);

    // Server-Pfad: ein ungegatetes unclear_owner vom Server wird bei ruhiger Queue gefiltert.
    const quietWithServerIssue = item({
      id: "lt2",
      owner: "unassigned",
      status: "later",
      quality_issues: [{ code: "unclear_owner", severity: "risk" }],
    });
    expect(qualityFlagsForFoItem(quietWithServerIssue).map((f) => f.kind)).not.toContain("unclear_owner");
  });
});
