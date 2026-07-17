import { describe, expect, it } from "vitest";
import {
  buildLagezeile,
  etaFraction,
  runProgressFraction,
  heartbeatAge,
  fmtSeconds,
  deriveKpi,
  fmtTokens,
  fmtUsd,
  planSpecHasParkedSignedChain,
  planSpecWaitsForOperator,
  profileInitial,
  profileColorClass,
  premiumLaneMarker,
  buildChainChips,
  buildSegments,
  pickFocusNode,
  chainProgress,
  chainTotalCostUsd,
  chainTotalCostUsdWithSource,
  budgetTone,
  derivePlanLanes,
  buildApproveRequest,
  fmtResetAt,
  derivePendingItems,
  pendingCount,
  deriveEffectivePlanPath,
  normalizeUsageWindowLabel,
  deriveSparklinePoints,
  computeBandGeometry,
  bandWindowSeconds,
  fmtDurationClock,
  laneTint,
  derivePulse,
  formatLiveEvent,
  mergeLiveEvents,
  clamp01,
  type ChainChipState,
} from "./fleetHub";
import type { Worker, ChainGraphResponse, ChainSummary } from "./types";

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

// ─── runProgressFraction (S2) ────────────────────────────────────────────────

describe("runProgressFraction", () => {
  it("bevorzugt run_progress über etaFraction-Heuristik", () => {
    // run_progress=0.3 (echter Runtime-Cap-Fortschritt) schlägt ETA-Heuristik.
    const w = makeWorker({ run_progress: 0.3, eta_p50_seconds: 600, started_at: NOW - 600 });
    expect(runProgressFraction(w, NOW)).toBe(0.3);
  });

  it("fällt auf etaFraction zurück wenn run_progress null (alten Workers / kein Cap)", () => {
    const w = makeWorker({ run_progress: null, eta_p50_seconds: 600, started_at: NOW - 300 });
    // elapsed 300s / eta 600s → 0.5
    expect(runProgressFraction(w, NOW)).toBeCloseTo(0.5);
  });

  it("gibt null zurück wenn weder run_progress noch eta vorhanden", () => {
    const w = makeWorker({ run_progress: null, eta_p50_seconds: null });
    expect(runProgressFraction(w, NOW)).toBeNull();
  });

  it("ignoriert run_progress außerhalb 0..1 (defensiv, Schema fängt das ab)", () => {
    const w = makeWorker({ run_progress: 1.5, eta_p50_seconds: 600, started_at: NOW - 300 });
    // 1.5 ist nicht im gültigen Bereich → Fallback auf etaFraction (0.5)
    expect(runProgressFraction(w, NOW)).toBeCloseTo(0.5);
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

  it("meldet einen zukünftigen Heartbeat als ungültig statt 0s-frisch", () => {
    expect(heartbeatAge(NOW + 5, NOW)).toBeNull();
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
    expect(kpi.kosten24hEquiv).toBe(false);
    expect(kpi.fertig24h).toBe(21);
  });

  it("nutzt cost_usd_equivalent markiert wenn actual_cost_usd fehlt oder 0 ist", () => {
    expect(deriveKpi([], 0, null, null, 1.23)).toMatchObject({
      kosten24h: 1.23,
      kosten24hEquiv: true,
    });
    expect(deriveKpi([], 0, 0, null, 2.34)).toMatchObject({
      kosten24h: 2.34,
      kosten24hEquiv: true,
    });
  });

  it("bevorzugt echte actual_cost_usd vor cost_usd_equivalent", () => {
    expect(deriveKpi([], 0, 0.42, null, 9.99)).toMatchObject({
      kosten24h: 0.42,
      kosten24hEquiv: false,
    });
  });

  it("null Kosten wenn Quelle fehlt", () => {
    const kpi = deriveKpi([], 0, null, null);
    expect(kpi.kosten24h).toBeNull();
    expect(kpi.kosten24hEquiv).toBe(false);
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

describe("planSpecHasParkedSignedChain", () => {
  it("true für echte signierte PlanSpec-Payload mit geparktem Root", () => {
    expect(planSpecHasParkedSignedChain({
      freigabe: "complete",
      kanban_state: "queued",
      kanban_root_task_id: "t_root",
      kanban_root_status: "scheduled",
      kanban_child_total: 3,
      kanban_child_done: 0,
      kanban_child_running: 0,
      kanban_child_blocked: 0,
    })).toBe(true);
  });

  it("false für complete ohne geparkten Root und ohne scheduled Kinder", () => {
    expect(planSpecHasParkedSignedChain({
      freigabe: "complete",
      kanban_state: "done",
      kanban_root_task_id: "t_root",
      kanban_root_status: "done",
      kanban_child_total: 3,
      kanban_child_done: 3,
      kanban_child_running: 0,
      kanban_child_blocked: 0,
    })).toBe(false);
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

describe("Premium-Lane avatar marker", () => {
  it("pairs the premium avatar class with the non-color title/aria marker", () => {
    expect(profileColorClass("premium")).toBe("fleet-avatar-prem");
    expect(profileColorClass("opus-builder")).toBe("fleet-avatar-prem");
    expect(premiumLaneMarker("premium")).toEqual({
      title: "Premium-Lane",
      "aria-label": "Premium-Lane",
    });
    expect(premiumLaneMarker("coder")).toEqual({});
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
    review_roles: [],
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

  it("active vor blockiert, gehalten, pending und completed (Reihenfolge)", () => {
    const tasks = [
      // Fertige Kette (completed)
      makeBoardTask({ id: "rA", title: "Fertig", status: "done", root_id: null, completed_at: NOW - 100 }),
      makeBoardTask({ id: "tA1", title: "Subtask", status: "done", root_id: "rA" }),
      // Aktive Kette (active)
      makeBoardTask({ id: "rB", title: "Aktiv", status: "done", root_id: null }),
      makeBoardTask({ id: "tB1", title: "Subtask", status: "running", root_id: "rB" }),
      // Blockierte Kette
      makeBoardTask({ id: "rC", title: "Blockiert", status: "scheduled", root_id: null }),
      makeBoardTask({ id: "tC1", title: "Subtask", status: "blocked", root_id: "rC" }),
      // Gehaltene Kette
      makeBoardTask({ id: "rD", title: "Gehalten", status: "scheduled", root_id: null }),
      makeBoardTask({ id: "tD1", title: "Subtask", status: "scheduled", root_id: "rD" }),
      // Wartende Kette (pending: todo-Kind, nichts aktiv)
      makeBoardTask({ id: "rE", title: "Wartet", status: "done", root_id: null }),
      makeBoardTask({ id: "tE1", title: "Subtask", status: "todo", root_id: "rE" }),
    ];
    const chips = buildChainChips(tasks);
    expect(chips.map((chip) => chip.rootId)).toEqual(["rB", "rC", "rD", "rE", "rA"]);
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

  it("[state=held] scheduled-Kind ohne running bleibt gehalten", () => {
    const tasks = [
      makeBoardTask({ id: "ra", title: "Root", status: "done", root_id: null }),
      makeBoardTask({ id: "ca1", status: "done", root_id: "ra" }),
      makeBoardTask({ id: "ca2", status: "scheduled", root_id: "ra" }),
    ];
    const chip = buildChainChips(tasks).find((c) => c.rootId === "ra");
    expect(chip!.state).toBe<ChainChipState>("held");
  });

  it("[state=blocked] blocked-Kind bleibt sichtbar blockiert", () => {
    const tasks = [
      makeBoardTask({ id: "rb", title: "Root", status: "done", root_id: null }),
      makeBoardTask({ id: "cb1", status: "blocked", root_id: "rb" }),
    ];
    const chip = buildChainChips(tasks).find((c) => c.rootId === "rb");
    expect(chip!.state).toBe<ChainChipState>("blocked");
  });

  it("Live-Hold: Root + Kind scheduled ergeben held; ein running-Kind ergibt active", () => {
    const heldTasks = [
      makeBoardTask({ id: "hold-root", title: "Freigabe-Hold", status: "scheduled", root_id: null }),
      makeBoardTask({ id: "hold-child", status: "scheduled", root_id: "hold-root" }),
    ];
    expect(buildChainChips(heldTasks)[0]?.state).toBe<ChainChipState>("held");

    const runningTasks = [
      ...heldTasks,
      makeBoardTask({ id: "running-child", status: "running", root_id: "hold-root" }),
    ];
    expect(buildChainChips(runningTasks)[0]?.state).toBe<ChainChipState>("active");
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

  it("summary path keeps an omitted completed chain with authoritative progress", () => {
    const chips = buildChainChips([], [{
      root_id: "r-omitted",
      root_title: "Vollständig ausgelagerte Kette",
      total: 42,
      done: 42,
      status_counts: { done: 42 },
      latest_completed_at: NOW - 5,
    }]);

    expect(chips).toEqual([{
      rootId: "r-omitted",
      label: "Vollständig ausgelagerte Kette",
      progress: 1,
      done: 42,
      total: 42,
      state: "completed",
      completedAt: NOW - 5,
    }]);
  });

  it("summary and full-list paths produce identical chips and sort order", () => {
    const tasks = [
      makeBoardTask({ id: "rA", title: "Fertig", status: "done", root_id: null, completed_at: NOW - 100 }),
      makeBoardTask({ id: "a1", status: "done", root_id: "rA", completed_at: NOW - 90 }),
      makeBoardTask({ id: "rB", title: "Aktiv", status: "done", root_id: null, completed_at: NOW - 80 }),
      makeBoardTask({ id: "b1", status: "running", root_id: "rB" }),
      makeBoardTask({ id: "rC", title: "Gehalten", status: "scheduled", root_id: null }),
      makeBoardTask({ id: "c1", status: "ready", root_id: "rC" }),
    ];
    const summaries: ChainSummary[] = [
      { root_id: "rA", root_title: "Fertig", total: 2, done: 2, status_counts: { done: 2 }, latest_completed_at: NOW - 90 },
      { root_id: "rB", root_title: "Aktiv", total: 2, done: 1, status_counts: { done: 1, running: 1 }, latest_completed_at: NOW - 80 },
      { root_id: "rC", root_title: "Gehalten", total: 2, done: 0, status_counts: { scheduled: 1, ready: 1 }, latest_completed_at: null },
    ];

    expect(buildChainChips(tasks, summaries)).toEqual(buildChainChips(tasks));
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
    expect(chainTotalCostUsdWithSource(nodes)).toMatchObject({ value: expect.closeTo(1.02), isEquivalent: false });
  });

  it("nutzt cost_usd_equivalent pro Node markiert wenn cost_usd fehlt oder 0 ist", () => {
    const { cost_usd: _omittedCostUsd, ...missingActual } = makeChainNode({ id: "n1", cost_usd_equivalent: 0.38 });
    const nodes = [missingActual, makeChainNode({ id: "n2", cost_usd: 0, cost_usd_equivalent: 0.64 })] as ChainNode[];
    expect(chainTotalCostUsd(nodes)).toBeCloseTo(1.02);
    expect(chainTotalCostUsdWithSource(nodes)).toMatchObject({ value: expect.closeTo(1.02), isEquivalent: true });
  });

  it("bevorzugt echte Node-Kosten vor cost_usd_equivalent", () => {
    const nodes = [makeChainNode({ id: "n1", cost_usd: 0.38, cost_usd_equivalent: 9.99 })];
    expect(chainTotalCostUsdWithSource(nodes)).toMatchObject({ value: expect.closeTo(0.38), isEquivalent: false });
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
  it("nur geänderte Assignee-Overrides werden gesendet", () => {
    const req = buildApproveRequest(
      "t_abc123",
      { coder: "coder", reviewer: "critic" },
      { coder: "coder", reviewer: "reviewer" }, // reviewer abweicht
      false,
    );
    expect(req.root_task_id).toBe("t_abc123");
    expect(req.assignee_overrides).toEqual({ reviewer: "critic" });
    expect(req.inject_scout).toBe(false);
  });

  it("keine Änderungen → leeres assignee_overrides-Objekt", () => {
    const req = buildApproveRequest(
      "t_def456",
      { coder: "coder", reviewer: "reviewer" },
      { coder: "coder", reviewer: "reviewer" },
      false,
    );
    expect(req.assignee_overrides).toEqual({});
  });

  it("inject_scout wird korrekt weitergegeben", () => {
    const req = buildApproveRequest("t_ghi", {}, {}, true);
    expect(req.inject_scout).toBe(true);
  });

  it("leeres Profil-Value wird nicht gesendet", () => {
    const req = buildApproveRequest(
      "t_xyz",
      { coder: "" },
      { coder: "coder" },
      false,
    );
    expect(req.assignee_overrides).toEqual({});
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

// ─── normalizeUsageWindowLabel ─────────────────────────────────────────────────
// Label/window_key-Paare sind aus agent/account_usage.py geerntet (echte
// Upstream-Provider-Fenster: Anthropic OAuth-Usage-API, OpenAI-Codex-Usage-API,
// Kimi-Kanban-Subscription-Zähler) — keine erfundenen Beispiele.

describe("normalizeUsageWindowLabel", () => {
  it("Anthropic 'Current session' (window_key session) → Sitzung", () => {
    expect(normalizeUsageWindowLabel("Current session", "session")).toBe("Sitzung");
  });

  it("Anthropic 'Current week' (window_key weekly) → Woche", () => {
    expect(normalizeUsageWindowLabel("Current week", "weekly")).toBe("Woche");
  });

  it("Codex 'Session' (window_key session) → Sitzung", () => {
    expect(normalizeUsageWindowLabel("Session", "session")).toBe("Sitzung");
  });

  it("Codex 'Weekly' (window_key weekly) → Woche", () => {
    expect(normalizeUsageWindowLabel("Weekly", "weekly")).toBe("Woche");
  });

  it("Anthropic 'Opus week' / 'Sonnet week' (Lane-spezifische Wochenfenster) → Woche", () => {
    expect(normalizeUsageWindowLabel("Opus week", "opus_week")).toBe("Woche");
    expect(normalizeUsageWindowLabel("Sonnet week", "sonnet_week")).toBe("Woche");
  });

  it("Kimi 'Kimi 5h' (window_key session, kein 'sess' im Label) → über den Key erkannt", () => {
    expect(normalizeUsageWindowLabel("Kimi 5h", "session")).toBe("Sitzung");
  });

  it("Label ohne bekanntes Muster (window_key null) bleibt unverändert", () => {
    expect(normalizeUsageWindowLabel("Subscription", null)).toBe("Subscription");
    expect(normalizeUsageWindowLabel("API key quota", null)).toBe("API key quota");
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

  it("signierte geparkte PlanSpec → Plan-Item", () => {
    const items = derivePendingItems(
      [{
        freigabe: "complete",
        kanban_state: "queued",
        kanban_root_task_id: "t_root",
        kanban_root_status: "scheduled",
        kanban_child_total: 4,
        kanban_child_done: 0,
        kanban_child_running: 0,
        kanban_child_blocked: 0,
        topic: "signed-plan",
        filename: "signed-plan.md",
      }],
      [],
    );
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "approval", topic: "signed-plan", targetSubtab: "plan" });
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

  it("backend-bestätigte Operator-Frage → Risiko-Item", () => {
    const items = derivePendingItems(
      [],
      [{ id: "t1", title: "Deploy halten", operator_question: true }],
    );
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "blocked", topic: "Deploy halten", targetSubtab: "risiko" });
  });

  it("Verifier-Prosa mit Fragezeichen und negativer Klassifikation → kein Item", () => {
    const items = derivePendingItems(
      [],
      [{ id: "t2", title: "Live-Backfill", operator_question: false }],
    );
    expect(items).toHaveLength(0);
  });

  it("blockierter Task ohne operator-Grund → kein Item", () => {
    const items = derivePendingItems(
      [],
      [{ id: "t3", title: "Hängt", operator_question: false }],
    );
    expect(items).toHaveLength(0);
  });

  it("alter Payload ohne Klassifikation → kein Item", () => {
    const items = derivePendingItems(
      [],
      [{ id: "t4", title: "Unklar" }],
    );
    expect(items).toHaveLength(0);
  });

  it("Reihenfolge: Freigaben vor blockierten Tasks", () => {
    const items = derivePendingItems(
      [{ freigabe: "operator", kanban_state: "queued", topic: "plan-a", filename: "a.md" }],
      [{ id: "t5", title: "Halt", operator_question: true }],
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
      [{ id: "t1", title: "Halt", operator_question: true }],
    );
    expect(pendingCount(items)).toBe(2);
  });
});

// ─── deriveEffectivePlanPath ──────────────────────────────────────────────────

describe("deriveEffectivePlanPath", () => {
  const paths = ["specs/a.md", "specs/b.md", "specs/c.md"];

  it("leere pendingPaths → null", () => {
    expect(deriveEffectivePlanPath(null, [])).toBeNull();
    expect(deriveEffectivePlanPath("specs/a.md", [])).toBeNull();
  });

  it("selectedPath null → erster Eintrag (verspätetes Laden)", () => {
    expect(deriveEffectivePlanPath(null, paths)).toBe("specs/a.md");
  });

  it("selectedPath noch gültig → bleibt erhalten", () => {
    expect(deriveEffectivePlanPath("specs/b.md", paths)).toBe("specs/b.md");
  });

  it("selectedPath nach Approve aus Liste rausgefallen → fällt auf ersten zurück", () => {
    // "specs/a.md" wurde approved und ist weg; b + c warten noch
    const remaining = ["specs/b.md", "specs/c.md"];
    expect(deriveEffectivePlanPath("specs/a.md", remaining)).toBe("specs/b.md");
  });

  it("genau ein Eintrag, selectedPath null → dieser Eintrag aktiv ohne Chip-Klick", () => {
    expect(deriveEffectivePlanPath(null, ["specs/only.md"])).toBe("specs/only.md");
  });

  it("genau ein Eintrag, selectedPath zeigt auf ihn → stabil", () => {
    expect(deriveEffectivePlanPath("specs/only.md", ["specs/only.md"])).toBe("specs/only.md");
  });
});

// ─── deriveSparklinePoints (Fertig-24h 7-Tage-Trend) ──────────────────────────
//
// Fixtures im echten RunsDailyResponse-Format (vgl. schemas.ts):
//   series: RunsDailyPoint[] — chronologisch aufsteigend (ältester zuerst).
//   done_tasks = erledigte Tasks des jeweiligen Tages.

describe("deriveSparklinePoints", () => {
  /** Minimaler gültiger Punkt, nur die für die Sparkline relevanten Felder. */
  function mkPoint(date: string, doneTasks: number) {
    return {
      date,
      done_roots: 0,
      done_roots_by_class: { nutzer: 0, haertung: 0, meta: 0 },
      done_tasks: doneTasks,
      cost_usd: null,
      input_tokens: null,
      output_tokens: null,
      runs_completed: doneTasks,
      runs_failed: 0,
      cycle_time_p50_seconds: null,
    };
  }

  function mkResponse(series: ReturnType<typeof mkPoint>[]) {
    return {
      days: 30,
      now: Math.floor(Date.parse("2026-07-04T12:00:00Z") / 1000),
      series,
    };
  }

  it("null-Input → null (keine Sparkline)", () => {
    expect(deriveSparklinePoints(null)).toBeNull();
    expect(deriveSparklinePoints(undefined)).toBeNull();
  });

  it("leere Serie → null", () => {
    expect(deriveSparklinePoints(mkResponse([]))).toBeNull();
  });

  it("genau 1 Punkt → null (für eine Linie braucht es ≥2 Punkte)", () => {
    expect(deriveSparklinePoints(mkResponse([mkPoint("2026-07-04", 5)]))).toBeNull();
  });

  it("genau 2 Punkte → beide Punkte, Reihenfolge erhalten", () => {
    const r = deriveSparklinePoints(
      mkResponse([mkPoint("2026-07-03", 4), mkPoint("2026-07-04", 8)]),
    );
    expect(r).toEqual([
      { date: "2026-07-03", value: 4 },
      { date: "2026-07-04", value: 8 },
    ]);
  });

  it("30-Tage-Serie → nur die letzten 7 Tage (Slicing vom Ende)", () => {
    const series = Array.from({ length: 30 }, (_, i) =>
      mkPoint(`2026-06-${String(5 + i).padStart(2, "0")}`, i + 1),
    );
    const r = deriveSparklinePoints(mkResponse(series));
    expect(r).not.toBeNull();
    expect(r!.length).toBe(7);
    // Jüngste 7 Einträge: index 23..29 → Werte 24..30
    expect(r!.map((p) => p.value)).toEqual([24, 25, 26, 27, 28, 29, 30]);
  });

  it("kurze Serie (<7) → alle Punkte zurückgeben", () => {
    const series = [mkPoint("2026-07-02", 3), mkPoint("2026-07-03", 5), mkPoint("2026-07-04", 7)];
    const r = deriveSparklinePoints(mkResponse(series));
    expect(r).toEqual([
      { date: "2026-07-02", value: 3 },
      { date: "2026-07-03", value: 5 },
      { date: "2026-07-04", value: 7 },
    ]);
  });

  it("done_tasks 0 ist ein gültiger Wert (kein Fake, aber echte 0)", () => {
    const series = [mkPoint("2026-07-03", 0), mkPoint("2026-07-04", 0)];
    const r = deriveSparklinePoints(mkResponse(series));
    expect(r).toEqual([
      { date: "2026-07-03", value: 0 },
      { date: "2026-07-04", value: 0 },
    ]);
  });

  it("Points enthalten das Datum (nicht nur den Wert) für Tooltip-Mapping", () => {
    const r = deriveSparklinePoints(
      mkResponse([mkPoint("2026-07-03", 4), mkPoint("2026-07-04", 8)]),
    );
    expect(r).not.toBeNull();
    expect(r![1].date).toBe("2026-07-04");
  });

  it("maxDays-Parameter limitiert unabhängig von der Serienlänge", () => {
    const series = [mkPoint("2026-07-01", 1), mkPoint("2026-07-02", 2), mkPoint("2026-07-03", 3), mkPoint("2026-07-04", 4)];
    const r = deriveSparklinePoints(mkResponse(series), 3);
    expect(r).not.toBeNull();
    expect(r!.length).toBe(3);
    expect(r![(r!.length - 1)].value).toBe(4);
    expect(r![(r!.length - 1)].date).toBe("2026-07-04");
  });

  it("maxDays < 2 wird auf 2 geklemmt (Linie braucht ≥2 Punkte)", () => {
    const series = [mkPoint("2026-07-03", 1), mkPoint("2026-07-04", 2)];
    const r = deriveSparklinePoints(mkResponse(series), 1);
    expect(r).not.toBeNull();
    expect(r!.length).toBe(2);
  });

  it("fehlt series-Feld → null", () => {
    expect(deriveSparklinePoints({ days: 30, now: 0 } as never)).toBeNull();
    expect(deriveSparklinePoints({ days: 30, now: 0, series: undefined } as never)).toBeNull();
  });
});

// ─── Puls-Leitstand (Variante B) — Band, Ticker, Pulse ───────────────────────

describe("clamp01", () => {
  it("klemmt in [0,1] und behandelt NaN/Infinity als 0", () => {
    expect(clamp01(-0.5)).toBe(0);
    expect(clamp01(0.42)).toBeCloseTo(0.42);
    expect(clamp01(3)).toBe(1);
    expect(clamp01(Number.NaN)).toBe(0);
    expect(clamp01(Infinity)).toBe(0);
  });
});

describe("bandWindowSeconds", () => {
  const base = { started_at: NOW - 100 };
  it("bevorzugt p90 (geerdet)", () => {
    expect(bandWindowSeconds({ ...base, eta_p90_seconds: 840, eta_p50_seconds: 300 }, NOW)).toEqual({ seconds: 840, grounded: true });
  });
  it("fällt auf max_runtime_seconds zurück (geerdet)", () => {
    expect(bandWindowSeconds({ ...base, max_runtime_seconds: 1800 }, NOW)).toEqual({ seconds: 1800, grounded: true });
  });
  it("dann p50×1.6 (geerdet)", () => {
    expect(bandWindowSeconds({ ...base, eta_p50_seconds: 100 }, NOW)).toEqual({ seconds: 160, grounded: true });
  });
  it("zuletzt elapsed×1.3 (nicht geerdet)", () => {
    expect(bandWindowSeconds(base, NOW)).toEqual({ seconds: 130, grounded: false });
  });
});

describe("computeBandGeometry", () => {
  it("füllt elapsed gegen das p90-Fenster und positioniert p50 + Ticks", () => {
    const w = {
      started_at: NOW - 400,
      eta_p50_seconds: 400,
      eta_p90_seconds: 800,
      heartbeat_ticks: [NOW - 300, NOW - 100, NOW + 200],
    };
    const geo = computeBandGeometry(w, NOW); // elapsed 400 / 800 = 0.5
    expect(geo.fillFraction).toBeCloseTo(0.5);
    expect(geo.p50Fraction).toBeCloseTo(0.5); // 400/800
    expect(geo.grounded).toBe(true);
    expect(geo.tickFractions.map((f) => Number(f.toFixed(3)))).toEqual([0.125, 0.375]);
  });

  it("deckelt die Füllung bei elapsed > Fenster auf 1", () => {
    const geo = computeBandGeometry({ started_at: NOW - 500, eta_p90_seconds: 100 }, NOW);
    expect(geo.fillFraction).toBe(1);
  });

  it("ohne Perzentile/Cap: nutzt run_progress und markiert nicht-geerdet", () => {
    const geo = computeBandGeometry({ started_at: NOW - 100, run_progress: 0.3 }, NOW);
    expect(geo.grounded).toBe(false);
    expect(geo.fillFraction).toBeCloseTo(0.3);
    expect(geo.p50Fraction).toBeNull();
  });

  it("verwirft Ticks außerhalb [0,1] (vor started_at oder jenseits des Fensters)", () => {
    const geo = computeBandGeometry(
      { started_at: NOW - 100, eta_p90_seconds: 100, heartbeat_ticks: [NOW - 200, NOW - 50, NOW + 5000] },
      NOW,
    );
    expect(geo.tickFractions).toEqual([0.5]); // nur 1050
  });
});

describe("fmtDurationClock", () => {
  it("formatiert Sekunden/Minuten/Stunden im Uhrzeit-Stil", () => {
    expect(fmtDurationClock(45)).toBe("45s");
    expect(fmtDurationClock(402)).toBe("6m42s");
    expect(fmtDurationClock(3860)).toBe("1h04m");
    expect(fmtDurationClock(7199)).toBe("2h00m");
    expect(fmtDurationClock(null)).toBe("—");
    expect(fmtDurationClock(-5)).toBe("Dauer ungültig");
  });
});

describe("laneTint", () => {
  it("mappt Rollen auf die Fleet-Farbfamilien", () => {
    expect(laneTint("coder")).toBe("coder");
    expect(laneTint("premium")).toBe("coder");
    expect(laneTint("reviewer")).toBe("reviewer");
    expect(laneTint("critic")).toBe("reviewer");
    expect(laneTint("verifier")).toBe("verifier");
    expect(laneTint("default")).toBe("neutral");
    expect(laneTint(null)).toBe("neutral");
  });
});

describe("derivePulse", () => {
  it("summiert Live-Tokens und übernimmt Slots/Queue/Done/Blocked", () => {
    const pulse = derivePulse({
      activeWorkers: [
        { input_tokens: 1000, output_tokens: 200 },
        { input_tokens: null, output_tokens: 80 },
      ],
      cap: 3,
      queue: 2,
      doneToday: 11,
      blocked: 1,
    });
    expect(pulse).toEqual({
      slotsUsed: 2,
      slotsCap: 3,
      queue: 2,
      doneToday: 11,
      blocked: 1,
      tokenSum: 1280,
    });
  });
});

describe("formatLiveEvent", () => {
  it("Heartbeat zeigt seine Note ohne Ton", () => {
    expect(formatLiveEvent({ kind: "heartbeat", note: "vitest 9/12 grün" })).toEqual({
      mark: null,
      text: "vitest 9/12 grün",
      tone: "none",
    });
  });
  it("completed → grüner Haken", () => {
    const f = formatLiveEvent({ kind: "completed", task_title: "Slice X" });
    expect(f.tone).toBe("ok");
    expect(f.mark).toBe("✓");
    expect(f.text).toContain("Slice X");
  });
  it("blocked → amber mit Note", () => {
    const f = formatLiveEvent({ kind: "blocked", task_title: "Audit", note: "operator_question" });
    expect(f.tone).toBe("warn");
    expect(f.text).toContain("operator_question");
  });
  it("crashed → alert", () => {
    expect(formatLiveEvent({ kind: "crashed", task_title: "T" }).tone).toBe("alert");
  });
});

describe("mergeLiveEvents", () => {
  it("mischt neue Events ein, dedupliziert nach id, sortiert absteigend, deckelt", () => {
    const prev = [{ id: 5 }, { id: 4 }, { id: 3 }];
    const incoming = [{ id: 7 }, { id: 6 }, { id: 5 }]; // 5 ist Duplikat
    const merged = mergeLiveEvents(prev, incoming, 4);
    expect(merged.map((e) => e.id)).toEqual([7, 6, 5, 4]);
  });
  it("leeres incoming lässt den Puffer unverändert (bis auf Sortierung/Cap)", () => {
    expect(mergeLiveEvents([{ id: 2 }, { id: 1 }], [], 10).map((e) => e.id)).toEqual([2, 1]);
  });
  it("behält kollidierende Event-IDs aus verschiedenen Boards", () => {
    const merged = mergeLiveEvents(
      [{ id: 7, board_slug: "default", at: 10 }],
      [{ id: 7, board_slug: "health-track", at: 11 }],
      10,
    );
    expect(merged.map((e) => [e.board_slug, e.id])).toEqual([
      ["health-track", 7],
      ["default", 7],
    ]);
  });
});
