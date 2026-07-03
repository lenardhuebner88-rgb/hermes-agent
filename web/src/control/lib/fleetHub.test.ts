import { describe, expect, it } from "vitest";
import {
  buildLagezeile,
  etaFraction,
  heartbeatAge,
  fmtSeconds,
  deriveKpi,
  fmtTokens,
  fmtUsd,
  planSpecWaitsForOperator,
  profileInitial,
  buildChainChips,
  buildSegments,
  pickFocusNode,
  chainProgress,
  chainTotalCostUsd,
  budgetTone,
  derivePlanLanes,
  buildApproveRequest,
  fmtResetAt,
  derivePendingItems,
  pendingCount,
  type ChainChipState,
} from "./fleetHub";
import type { Worker, ChainGraphResponse } from "./types";

// ─── Worker-Fixtures (echtes Response-Shape von WorkerSchema) ────────────────

const NOW = 1_780_700_000;

function makeWorker(overrides: Partial<Worker> = {}): Worker {
  return {
    run_id: "r1",
    task_id: "t_aabbcc00",
    task_title: "Testtask",
    task_status: "running",
    task_assignee: "hermes",
    profile: "coder",
    worker_pid: 1234,
    started_at: NOW - 600,
    claim_lock: "lock",
    claim_expires: NOW + 3000,
    last_heartbeat_at: NOW - 9,
    max_runtime_seconds: 7200,
    run_status: "running",
    run_outcome: null,
    last_heartbeat_note: null,
    last_heartbeat_note_at: null,
    eta_p50_seconds: null,
    eta_p90_seconds: null,
    step_key: null,
    model_override: null,
    effective_model: "claude-sonnet-4-5",
    input_tokens: 61400,
    output_tokens: 12200,
    ...overrides,
  };
}

// ─── buildLagezeile ──────────────────────────────────────────────────────────

describe("buildLagezeile", () => {
  it("keine Worker, nichts blockiert", () => {
    const result = buildLagezeile({ workers: [], blockedCount: 0, pendingApprovals: 0 });
    expect(result).toBe("Keine Worker aktiv, nichts blockiert.");
  });

  it("ein Worker läuft, nichts blockiert", () => {
    const result = buildLagezeile({ workers: [makeWorker()], blockedCount: 0, pendingApprovals: 0 });
    expect(result).toBe("Ein Worker läuft, nichts blockiert.");
  });

  it("zwei Worker laufen, nichts blockiert", () => {
    const result = buildLagezeile({ workers: [makeWorker(), makeWorker({ run_id: "r2" })], blockedCount: 0, pendingApprovals: 0 });
    expect(result).toBe("2 Worker laufen, nichts blockiert.");
  });

  it("ein Worker läuft, eine blockierte Aufgabe", () => {
    const result = buildLagezeile({ workers: [makeWorker()], blockedCount: 1, pendingApprovals: 0 });
    expect(result).toBe("Ein Worker läuft — eine Aufgabe blockiert.");
  });

  it("zwei Worker laufen, zwei blockierte Aufgaben", () => {
    const result = buildLagezeile({ workers: [makeWorker(), makeWorker({ run_id: "r2" })], blockedCount: 2, pendingApprovals: 0 });
    expect(result).toBe("2 Worker laufen — 2 Aufgaben blockiert.");
  });

  it("ein Worker läuft, ein Plan wartet auf Freigabe", () => {
    // "nichts blockiert" erscheint immer wenn blockedCount === 0 — auch wenn Pläne warten.
    const result = buildLagezeile({ workers: [makeWorker()], blockedCount: 0, pendingApprovals: 1 });
    expect(result).toBe("Ein Worker läuft, nichts blockiert — ein Plan wartet auf deine Freigabe.");
  });

  it("zwei Worker laufen, mehrere Pläne warten", () => {
    const result = buildLagezeile({ workers: [makeWorker(), makeWorker({ run_id: "r2" })], blockedCount: 0, pendingApprovals: 3 });
    expect(result).toBe("2 Worker laufen, nichts blockiert — 3 Pläne warten auf deine Freigabe.");
  });

  it("blockiert + ausstehende Freigaben kombiniert", () => {
    const result = buildLagezeile({ workers: [makeWorker()], blockedCount: 1, pendingApprovals: 2 });
    expect(result).toBe("Ein Worker läuft — eine Aufgabe blockiert — 2 Pläne warten auf deine Freigabe.");
  });

  it("zählt nur laufende Worker (nicht done/blocked)", () => {
    const doneWorker = makeWorker({ run_id: "r2", run_status: "done" });
    const result = buildLagezeile({ workers: [makeWorker(), doneWorker], blockedCount: 0, pendingApprovals: 0 });
    // Nur ein laufender
    expect(result).toBe("Ein Worker läuft, nichts blockiert.");
  });
});

// ─── etaFraction ─────────────────────────────────────────────────────────────

describe("etaFraction", () => {
  it("gibt null zurück wenn kein eta", () => {
    expect(etaFraction(NOW - 300, null, NOW)).toBeNull();
    expect(etaFraction(NOW - 300, 0, NOW)).toBeNull();
  });

  it("50 % wenn Hälfte des ETA abgelaufen", () => {
    const frac = etaFraction(NOW - 300, 600, NOW);
    expect(frac).toBeCloseTo(0.5);
  });

  it("gedeckelt auf 0.95 wenn über ETA", () => {
    const frac = etaFraction(NOW - 1200, 600, NOW);
    expect(frac).toBe(0.95);
  });

  it("0 wenn gerade gestartet", () => {
    const frac = etaFraction(NOW, 600, NOW);
    expect(frac).toBe(0);
  });
});

// ─── heartbeatAge ────────────────────────────────────────────────────────────

describe("heartbeatAge", () => {
  it("gibt null zurück wenn kein Heartbeat", () => {
    expect(heartbeatAge(null, NOW)).toBeNull();
    expect(heartbeatAge(0, NOW)).toBeNull();
  });

  it("gibt korrekte Sekunden zurück", () => {
    expect(heartbeatAge(NOW - 9, NOW)).toBe(9);
  });

  it("mindestens 0", () => {
    expect(heartbeatAge(NOW + 5, NOW)).toBe(0);
  });
});

// ─── fmtSeconds ──────────────────────────────────────────────────────────────

describe("fmtSeconds", () => {
  it("unter 60 → Sekunden", () => {
    expect(fmtSeconds(9)).toBe("9 s");
    expect(fmtSeconds(59)).toBe("59 s");
  });

  it("60–3599 → Minuten", () => {
    expect(fmtSeconds(60)).toBe("1 min");
    expect(fmtSeconds(90)).toBe("2 min");
    expect(fmtSeconds(660)).toBe("11 min");
  });

  it("≥3600 → Stunden", () => {
    expect(fmtSeconds(3600)).toBe("1 h");
    expect(fmtSeconds(7200)).toBe("2 h");
  });
});

// ─── deriveKpi ───────────────────────────────────────────────────────────────

describe("deriveKpi", () => {
  it("zählt nur laufende Worker als aktiv", () => {
    const workers = [
      makeWorker({ run_status: "running" }),
      makeWorker({ run_id: "r2", run_status: "done" }),
      makeWorker({ run_id: "r3", run_status: "running" }),
    ];
    const kpi = deriveKpi(workers, 2, 4.1, 21);
    expect(kpi.aktiv).toBe(2);
    expect(kpi.blockiert).toBe(2);
    expect(kpi.kosten24h).toBe(4.1);
    expect(kpi.fertig24h).toBe(21);
  });

  it("null Kosten wenn Quelle fehlt", () => {
    const kpi = deriveKpi([], 0, null, null);
    expect(kpi.kosten24h).toBeNull();
    expect(kpi.fertig24h).toBeNull();
  });
});

// ─── fmtTokens ───────────────────────────────────────────────────────────────

describe("fmtTokens", () => {
  it("null → —", () => {
    expect(fmtTokens(null)).toBe("—");
    expect(fmtTokens(undefined)).toBe("—");
  });

  it("unter 1000 → Zahl", () => {
    expect(fmtTokens(500)).toBe("500");
  });

  it("ab 1000 → k mit Komma", () => {
    expect(fmtTokens(61400)).toBe("61,4k");
    expect(fmtTokens(1000)).toBe("1,0k");
  });

  it("ab 1M → M", () => {
    expect(fmtTokens(1_200_000)).toBe("1,2M");
  });
});

// ─── fmtUsd ──────────────────────────────────────────────────────────────────

describe("fmtUsd", () => {
  it("null → —", () => {
    expect(fmtUsd(null)).toBe("—");
  });

  it("USD-Betrag → $x,xx", () => {
    expect(fmtUsd(4.1)).toBe("$4,10");
    expect(fmtUsd(0.71)).toBe("$0,71");
  });
});

// ─── planSpecWaitsForOperator ────────────────────────────────────────────────

describe("planSpecWaitsForOperator", () => {
  it("true wenn freigabe=operator und queued", () => {
    expect(planSpecWaitsForOperator("operator", "queued")).toBe(true);
  });

  it("true wenn freigabe=operator und not_ingested", () => {
    expect(planSpecWaitsForOperator("operator", "not_ingested")).toBe(true);
  });

  it("false wenn freigabe=auto", () => {
    expect(planSpecWaitsForOperator("auto", "queued")).toBe(false);
  });

  it("false wenn state=running", () => {
    expect(planSpecWaitsForOperator("operator", "running")).toBe(false);
  });
});

// ─── profileInitial ──────────────────────────────────────────────────────────

describe("profileInitial", () => {
  it("coder → C", () => {
    expect(profileInitial("coder")).toBe("C");
  });

  it("premium → P", () => {
    expect(profileInitial("premium")).toBe("P");
  });

  it("reviewer → R", () => {
    expect(profileInitial("reviewer")).toBe("R");
  });

  it("Fallback auf erstes Zeichen", () => {
    expect(profileInitial("sonnet")).toBe("S");
  });
});

// ─── Ketten-Subtab Fixture ───────────────────────────────────────────────────

type BoardTaskMini = {
  id: string;
  title: string;
  root_id?: string | null;
  status: string;
  completed_at?: number | null;
};

function makeBoardTask(overrides: Partial<BoardTaskMini> & { id: string }): BoardTaskMini {
  return {
    title: `Task ${overrides.id}`,
    root_id: null,
    status: "running",
    completed_at: null,
    ...overrides,
  };
}

type ChainNode = ChainGraphResponse["nodes"][number];

function makeChainNode(overrides: Partial<ChainNode> & { id: string }): ChainNode {
  return {
    title: `Node ${overrides.id}`,
    status: "running",
    assignee: null,
    level: 0,
    parents: [],
    children: [],
    created_at: NOW,
    started_at: null,
    completed_at: null,
    last_heartbeat_at: null,
    runtime_seconds: null,
    progress: null,
    latest_run: null,
    cost_usd: 0,
    input_tokens: 0,
    output_tokens: 0,
    cost_usd_equivalent: 0,
    cost_effective_usd: 0,
    ...overrides,
  };
}

// ─── buildChainChips ─────────────────────────────────────────────────────────

describe("buildChainChips", () => {
  it("leere Eingabe → leeres Ergebnis", () => {
    expect(buildChainChips([])).toEqual([]);
  });

  it("aktive Kette erkannt (running)", () => {
    const tasks = [
      makeBoardTask({ id: "r1", title: "Root", status: "done", root_id: null }),
      makeBoardTask({ id: "t2", title: "Subtask", status: "running", root_id: "r1" }),
    ];
    const chips = buildChainChips(tasks);
    // Beide Tasks sind in derselben Gruppe (root_id = "r1")
    const chip = chips.find((c) => c.rootId === "r1");
    expect(chip).toBeDefined();
    expect(chip!.state).toBe<ChainChipState>("active");
  });

  it("Kette ohne running und alle done → completed", () => {
    const tasks = [
      makeBoardTask({ id: "r2", title: "Root", status: "done", root_id: null }),
      makeBoardTask({ id: "t3", title: "Subtask", status: "done", root_id: "r2" }),
    ];
    const chips = buildChainChips(tasks);
    const chip = chips.find((c) => c.rootId === "r2");
    expect(chip!.state).toBe<ChainChipState>("completed");
  });

  it("active vor pending vor completed (Reihenfolge)", () => {
    const tasks = [
      // Fertige Kette (completed)
      makeBoardTask({ id: "rA", title: "Fertig", status: "done", root_id: null, completed_at: NOW - 100 }),
      makeBoardTask({ id: "tA1", title: "Subtask", status: "done", root_id: "rA" }),
      // Aktive Kette (active)
      makeBoardTask({ id: "rB", title: "Aktiv", status: "done", root_id: null }),
      makeBoardTask({ id: "tB1", title: "Subtask", status: "running", root_id: "rB" }),
      // Wartende Kette (pending: todo-Kind, nichts aktiv)
      makeBoardTask({ id: "rC", title: "Wartet", status: "done", root_id: null }),
      makeBoardTask({ id: "tC1", title: "Subtask", status: "todo", root_id: "rC" }),
    ];
    const chips = buildChainChips(tasks);
    expect(chips[0].rootId).toBe("rB"); // active zuerst
    expect(chips[1].rootId).toBe("rC"); // pending danach
    expect(chips[2].rootId).toBe("rA"); // completed zuletzt
  });

  it("Fortschritt korrekt berechnet (2 von 4 done)", () => {
    const tasks = [
      makeBoardTask({ id: "r3", title: "Root", status: "done", root_id: null }),
      makeBoardTask({ id: "t4", status: "done", root_id: "r3" }),
      makeBoardTask({ id: "t5", status: "running", root_id: "r3" }),
      makeBoardTask({ id: "t6", status: "scheduled", root_id: "r3" }),
    ];
    const chips = buildChainChips(tasks);
    const chip = chips[0];
    // "r3" ist done, "t4" ist done → 2 von 4
    expect(chip.done).toBe(2);
    expect(chip.total).toBe(4);
    expect(chip.progress).toBeCloseTo(0.5);
  });

  it("Solo-Task (kein Kind) wird nicht als Kette angezeigt (Chips-Pollution Fix)", () => {
    // Ein einzelner Task ohne Kind-Tasks darf keinen Chip erzeugen.
    const tasks = [
      makeBoardTask({ id: "solo", title: "Allein", status: "running", root_id: null }),
    ];
    const chips = buildChainChips(tasks);
    expect(chips).toHaveLength(0);
  });

  it("Root-only-running: nur fertiges Kind → completed (nicht active)", () => {
    // Root läuft selbst, aber hat nur ein fertiges Kind → alle Members done? Nein, Root läuft.
    // Root=running, Kind=done → nicht alle done, kein aktives Kind → pending.
    const tasks = [
      makeBoardTask({ id: "rX", title: "Root", status: "running", root_id: null }),
      makeBoardTask({ id: "cX", title: "Kind", status: "done", root_id: "rX" }),
    ];
    const chips = buildChainChips(tasks);
    const chip = chips.find((c) => c.rootId === "rX");
    expect(chip).toBeDefined();
    // Root selbst running zählt nicht als "aktives Kind" → kein active, nicht alle done → pending
    expect(chip!.state).toBe<ChainChipState>("pending");
  });

  it("Root running + Kind running → active", () => {
    const tasks = [
      makeBoardTask({ id: "rY", title: "Root", status: "running", root_id: null }),
      makeBoardTask({ id: "cY", title: "Kind", status: "running", root_id: "rY" }),
    ];
    const chips = buildChainChips(tasks);
    const chip = chips.find((c) => c.rootId === "rY");
    expect(chip!.state).toBe<ChainChipState>("active");
  });

  // ─── Drei-Zustands-Fixtures ────────────────────────────────────────────────

  it("[state=active] mind. 1 Kind running/scheduled/blocked", () => {
    const tasks = [
      makeBoardTask({ id: "ra", title: "Root", status: "done", root_id: null }),
      makeBoardTask({ id: "ca1", status: "done", root_id: "ra" }),
      makeBoardTask({ id: "ca2", status: "scheduled", root_id: "ra" }),
    ];
    const chip = buildChainChips(tasks).find((c) => c.rootId === "ra");
    expect(chip!.state).toBe<ChainChipState>("active");
  });

  it("[state=active] blocked-Kind gilt als aktiv", () => {
    const tasks = [
      makeBoardTask({ id: "rb", title: "Root", status: "done", root_id: null }),
      makeBoardTask({ id: "cb1", status: "blocked", root_id: "rb" }),
    ];
    const chip = buildChainChips(tasks).find((c) => c.rootId === "rb");
    expect(chip!.state).toBe<ChainChipState>("active");
  });

  it("[state=pending] todo/ready-Kinder ohne aktives Kind → pending NICHT completed (Regressionsfall)", () => {
    // Das ist der Original-Blocker: vorher wurde alles Nicht-Aktive als ✓ gerendert.
    const tasks = [
      makeBoardTask({ id: "rp", title: "Root", status: "done", root_id: null }),
      makeBoardTask({ id: "cp1", status: "todo", root_id: "rp" }),
      makeBoardTask({ id: "cp2", status: "ready", root_id: "rp" }),
    ];
    const chip = buildChainChips(tasks).find((c) => c.rootId === "rp");
    expect(chip!.state).toBe<ChainChipState>("pending");
  });

  it("[state=completed] alle Members done/archived → completed", () => {
    const tasks = [
      makeBoardTask({ id: "rc", title: "Root", status: "done", root_id: null }),
      makeBoardTask({ id: "cc1", status: "done", root_id: "rc" }),
      makeBoardTask({ id: "cc2", status: "archived", root_id: "rc" }),
    ];
    const chip = buildChainChips(tasks).find((c) => c.rootId === "rc");
    expect(chip!.state).toBe<ChainChipState>("completed");
  });
});

// ─── buildSegments ────────────────────────────────────────────────────────────

describe("buildSegments", () => {
  it("leere Nodes → leeres Ergebnis", () => {
    expect(buildSegments([])).toEqual([]);
  });

  it("running → active, done → done, scheduled → open", () => {
    const nodes = [
      makeChainNode({ id: "n1", level: 0, status: "done" }),
      makeChainNode({ id: "n2", level: 1, status: "running" }),
      makeChainNode({ id: "n3", level: 2, status: "scheduled" }),
    ];
    const segs = buildSegments(nodes);
    expect(segs).toEqual(["done", "active", "open"]);
  });

  it("archived → done", () => {
    const nodes = [makeChainNode({ id: "n1", level: 0, status: "archived" })];
    expect(buildSegments(nodes)).toEqual(["done"]);
  });
});

// ─── pickFocusNode ───────────────────────────────────────────────────────────

describe("pickFocusNode", () => {
  it("leere Nodes → null", () => {
    expect(pickFocusNode([])).toBeNull();
  });

  it("bevorzugt running vor scheduled", () => {
    const nodes = [
      makeChainNode({ id: "n1", level: 0, status: "done" }),
      makeChainNode({ id: "n2", level: 1, status: "running" }),
      makeChainNode({ id: "n3", level: 2, status: "scheduled" }),
    ];
    expect(pickFocusNode(nodes)!.id).toBe("n2");
  });

  it("kein running → ersten scheduled/ready/todo", () => {
    const nodes = [
      makeChainNode({ id: "n1", level: 0, status: "done" }),
      makeChainNode({ id: "n2", level: 1, status: "todo" }),
      makeChainNode({ id: "n3", level: 2, status: "scheduled" }),
    ];
    // Level 0 ist done, level 1 ist todo → wählt level 1
    expect(pickFocusNode(nodes)!.id).toBe("n2");
  });

  it("alles done → letzten fertigen Node", () => {
    const nodes = [
      makeChainNode({ id: "n1", level: 0, status: "done" }),
      makeChainNode({ id: "n2", level: 1, status: "done" }),
    ];
    expect(pickFocusNode(nodes)!.id).toBe("n2");
  });
});

// ─── chainProgress ────────────────────────────────────────────────────────────

describe("chainProgress", () => {
  it("leere Nodes → 0%", () => {
    expect(chainProgress([])).toEqual({ pct: 0, done: 0, total: 0 });
  });

  it("2 von 4 done → 50%", () => {
    const nodes = [
      makeChainNode({ id: "n1", status: "done" }),
      makeChainNode({ id: "n2", status: "done" }),
      makeChainNode({ id: "n3", status: "running" }),
      makeChainNode({ id: "n4", status: "scheduled" }),
    ];
    expect(chainProgress(nodes)).toEqual({ pct: 50, done: 2, total: 4 });
  });

  it("alle done → 100%", () => {
    const nodes = [
      makeChainNode({ id: "n1", status: "done" }),
      makeChainNode({ id: "n2", status: "archived" }),
    ];
    expect(chainProgress(nodes)).toEqual({ pct: 100, done: 2, total: 2 });
  });
});

// ─── chainTotalCostUsd ────────────────────────────────────────────────────────

describe("chainTotalCostUsd", () => {
  it("leere Nodes → null", () => {
    expect(chainTotalCostUsd([])).toBeNull();
  });

  it("alle Kosten 0 → null", () => {
    const nodes = [makeChainNode({ id: "n1", cost_usd: 0 })];
    expect(chainTotalCostUsd(nodes)).toBeNull();
  });

  it("Kosten werden summiert", () => {
    const nodes = [
      makeChainNode({ id: "n1", cost_usd: 0.38 }),
      makeChainNode({ id: "n2", cost_usd: 0.64 }),
    ];
    expect(chainTotalCostUsd(nodes)).toBeCloseTo(1.02);
  });
});

// ─── budgetTone ───────────────────────────────────────────────────────────────

describe("budgetTone", () => {
  it("null → null", () => {
    expect(budgetTone(null)).toBeNull();
    expect(budgetTone(undefined)).toBeNull();
  });

  it("0 % → ok", () => {
    expect(budgetTone(0)).toBe("ok");
  });

  it("59 % → ok", () => {
    expect(budgetTone(59)).toBe("ok");
  });

  it("60 % → warn", () => {
    expect(budgetTone(60)).toBe("warn");
  });

  it("84 % → warn", () => {
    expect(budgetTone(84)).toBe("warn");
  });

  it("85 % → danger", () => {
    expect(budgetTone(85)).toBe("danger");
  });

  it("100 % → danger", () => {
    expect(budgetTone(100)).toBe("danger");
  });
});

// ─── derivePlanLanes ──────────────────────────────────────────────────────────

describe("derivePlanLanes", () => {
  it("leere Subtasks → leere Lanes", () => {
    expect(derivePlanLanes([])).toEqual([]);
  });

  it("extrahiert Lanes aus Subtasks (erste Erwähnung gewinnt)", () => {
    const subtasks = [
      { lane: "coder", title: "Fix implementieren" },
      { lane: "reviewer", title: "Diff prüfen" },
      { lane: "coder", title: "Test schreiben" }, // dupliziert: ignorieren
    ];
    const lanes = derivePlanLanes(subtasks);
    expect(lanes).toHaveLength(2);
    expect(lanes[0]).toEqual({ lane: "coder", description: "Fix implementieren" });
    expect(lanes[1]).toEqual({ lane: "reviewer", description: "Diff prüfen" });
  });

  it("Subtask ohne lane wird ignoriert", () => {
    const subtasks = [
      { lane: "", title: "Kein Lane" },
      { lane: "verifier", title: "Smoke-Test" },
    ];
    const lanes = derivePlanLanes(subtasks);
    expect(lanes).toHaveLength(1);
    expect(lanes[0].lane).toBe("verifier");
  });
});

// ─── buildApproveRequest ──────────────────────────────────────────────────────

describe("buildApproveRequest", () => {
  it("nur geänderte Lane-Models werden gesendet", () => {
    const req = buildApproveRequest(
      "t_abc123",
      { coder: "claude-sonnet-4-5", reviewer: "claude-opus-4-5" },
      { coder: "claude-sonnet-4-5", reviewer: "claude-sonnet-4-5" }, // reviewer abweicht
      false,
    );
    expect(req.root_task_id).toBe("t_abc123");
    expect(req.lane_models).toEqual({ reviewer: "claude-opus-4-5" });
    expect(req.inject_scout).toBe(false);
  });

  it("keine Änderungen → leeres lane_models-Objekt", () => {
    const req = buildApproveRequest(
      "t_def456",
      { coder: "sonnet", reviewer: "opus" },
      { coder: "sonnet", reviewer: "opus" },
      false,
    );
    expect(req.lane_models).toEqual({});
  });

  it("inject_scout wird korrekt weitergegeben", () => {
    const req = buildApproveRequest("t_ghi", {}, {}, true);
    expect(req.inject_scout).toBe(true);
  });

  it("leeres Model-Value wird nicht gesendet", () => {
    const req = buildApproveRequest(
      "t_xyz",
      { coder: "" },
      { coder: "sonnet" },
      false,
    );
    // Leerer Wert → Abweichung vom Default, aber leer → wird trotzdem gefiltert
    expect(req.lane_models).toEqual({});
  });
});

// ─── fmtResetAt ──────────────────────────────────────────────────────────────

describe("fmtResetAt", () => {
  it("null → —", () => {
    expect(fmtResetAt(null)).toBe("—");
    expect(fmtResetAt(undefined)).toBe("—");
    expect(fmtResetAt("")).toBe("—");
  });

  it("ungültiges Datum → —", () => {
    expect(fmtResetAt("kein-datum")).toBe("—");
  });

  it("gültiges ISO-Datum → formatierter String", () => {
    // Wir prüfen nur dass es keinen Fehler wirft und einen nicht-leeren String liefert
    const result = fmtResetAt("2026-07-06T03:00:00Z");
    expect(result).not.toBe("—");
    expect(result.length).toBeGreaterThan(3);
  });
});

// ─── derivePendingItems ───────────────────────────────────────────────────────

describe("derivePendingItems", () => {
  it("leer wenn keine PlanSpecs und keine blockierten Tasks", () => {
    expect(derivePendingItems([], [])).toEqual([]);
  });

  it("wartende Freigabe (freigabe: operator, queued) → Plan-Item", () => {
    const items = derivePendingItems(
      [{ freigabe: "operator", kanban_state: "queued", topic: "mein-plan", filename: "mein-plan.md" }],
      [],
    );
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "approval", topic: "mein-plan", targetSubtab: "plan" });
  });

  it("nicht-wartende PlanSpec (kanban_state: running) → kein Item", () => {
    const items = derivePendingItems(
      [{ freigabe: "operator", kanban_state: "running", topic: "laufend", filename: "x.md" }],
      [],
    );
    expect(items).toHaveLength(0);
  });

  it("PlanSpec ohne topic fällt auf filename zurück", () => {
    const items = derivePendingItems(
      [{ freigabe: "operator", kanban_state: "not_ingested", topic: "", filename: "fallback.md" }],
      [],
    );
    expect(items[0]?.topic).toBe("fallback.md");
  });

  it("blockierter Task mit 'operator hold' → Risiko-Item", () => {
    const items = derivePendingItems(
      [],
      [{ id: "t1", title: "Deploy halten", block_reason: "operator hold" }],
    );
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "blocked", topic: "Deploy halten", targetSubtab: "risiko" });
  });

  it("blockierter Task mit 'Operator' (Groß) → Risiko-Item (case-insensitive)", () => {
    const items = derivePendingItems(
      [],
      [{ id: "t2", title: "Live-Backfill", block_reason: "Operator-Freigabe erforderlich" }],
    );
    expect(items[0]?.kind).toBe("blocked");
  });

  it("blockierter Task ohne operator-Grund → kein Item", () => {
    const items = derivePendingItems(
      [],
      [{ id: "t3", title: "Hängt", block_reason: "dependency missing" }],
    );
    expect(items).toHaveLength(0);
  });

  it("blockierter Task ohne block_reason → kein Item", () => {
    const items = derivePendingItems(
      [],
      [{ id: "t4", title: "Unklar", block_reason: null }],
    );
    expect(items).toHaveLength(0);
  });

  it("Reihenfolge: Freigaben vor blockierten Tasks", () => {
    const items = derivePendingItems(
      [{ freigabe: "operator", kanban_state: "queued", topic: "plan-a", filename: "a.md" }],
      [{ id: "t5", title: "Halt", block_reason: "operator hold" }],
    );
    expect(items).toHaveLength(2);
    expect(items[0].kind).toBe("approval");
    expect(items[1].kind).toBe("blocked");
  });

  it("mehrere Freigaben → alle gelistet", () => {
    const items = derivePendingItems(
      [
        { freigabe: "operator", kanban_state: "queued", topic: "plan-a", filename: "a.md" },
        { freigabe: "operator", kanban_state: "not_ingested", topic: "plan-b", filename: "b.md" },
      ],
      [],
    );
    expect(items).toHaveLength(2);
  });
});

// ─── pendingCount ─────────────────────────────────────────────────────────────

describe("pendingCount", () => {
  it("leere Liste → 0", () => {
    expect(pendingCount([])).toBe(0);
  });

  it("n Items → n", () => {
    const items = derivePendingItems(
      [{ freigabe: "operator", kanban_state: "queued", topic: "x", filename: "x.md" }],
      [{ id: "t1", title: "Halt", block_reason: "operator hold" }],
    );
    expect(pendingCount(items)).toBe(2);
  });
});
