// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const src = readFileSync(path.resolve(import.meta.dirname, "KettenTab.tsx"), "utf8");
const kettenCss = readFileSync(path.resolve(import.meta.dirname, "ketten-v4.css"), "utf8");
const fleetCss = readFileSync(path.resolve(import.meta.dirname, "fleet.css"), "utf8");
const heuteSrc = readFileSync(path.resolve(import.meta.dirname, "HeuteTab.tsx"), "utf8");

describe("KettenTab v4 — redesign checks", () => {
  it("makes all interactive elements keyboard-focus-visible", () => {
    expect(src).toMatch(/lk-card/);
    expect(src).toMatch(/detail/);
    expect(src).toMatch(/uitem/);
    expect(src).toMatch(/done-item/);
    // CSS has focus-visible outline
    const css = readFileSync(path.resolve(import.meta.dirname, "ketten-v4.css"), "utf8");
    expect(css).toMatch(/button:focus-visible/);
  });

  it("joins worker data via task_id for run-bound model route + override", () => {
    expect(src).toContain("useHermesWorkers");
    expect(src).toContain("workerByNodeId");
    expect(src).toContain("active_model");
    expect(src).toContain("model_state");
    expect(src).toContain("model_override");
    expect(src).not.toContain("latest_run?.profile ?? null");
  });

  it("renders model-row with GGFM Override badge when override is present", () => {
    expect(src).toMatch(/model-row/);
    expect(src).toMatch(/model-override-badge/);
    expect(src).toContain("GGFM Override");
  });

  it("uses CSS design tokens (no raw hex)", () => {
    expect(kettenCss).toContain("--color-surface");
    expect(kettenCss).toContain("--color-live");
    expect(kettenCss).toContain("--color-status-ok");
    expect(kettenCss).toContain("--color-status-warn");
  });

  it("pairs premium avatar color with the double ring and title/aria marker", () => {
    const fleetPremiumRule = fleetCss.match(/\.fleet-avatar-prem\s*\{([\s\S]*?)\}/)?.[1];
    const kettenPremiumRule = kettenCss.match(/\.ketten-v4 \.avatar-premium\s*\{([\s\S]*?)\}/)?.[1];

    expect(fleetPremiumRule).toContain("var(--color-lane-prem)");
    expect(fleetPremiumRule).toMatch(/box-shadow:[^;]*var\(--color-surface-1\)[^;]*var\(--color-lane-prem\)/);
    expect(kettenPremiumRule).toContain("var(--color-lane-prem)");
    expect(kettenPremiumRule).toMatch(/box-shadow:[^;]*var\(--color-surface-1\)[^;]*var\(--color-lane-prem\)/);
    expect(src).toContain("premiumLaneMarker(focusNode.assignee)");
    expect(src).toContain("premiumLaneMarker(n.assignee)");
    expect(heuteSrc).toContain("premiumLaneMarker(w.profile)");
  });

  it("renders all 6 sections", () => {
    expect(src).toMatch(/SECTION 1.*Ketten-Liste/);
    expect(src).toMatch(/SECTION 2.*Active Chain Header/);
    expect(src).toMatch(/SECTION 3.*Step Pipeline/);
    expect(src).toMatch(/SECTION 4.*Active Step Detail/);
    expect(src).toMatch(/SECTION 5.*Upcoming Steps/);
    expect(src).toMatch(/SECTION 6.*Done.*Gate/);
  });

  it("shows heartbeat LED only for running nodes", () => {
    expect(src).toMatch(/focusNode\.status === "running" && focusHbAge/);
    expect(src).toContain("led-dot");
  });

  it("shows inline model for upcoming steps", () => {
    expect(src).toMatch(/umodel/);
    expect(src).toMatch(/umodel-override/);
    expect(src).toContain("de.worker.modelRouteNotStarted");
  });

  it("labels active-step values and gives the horizontal pipeline a fade affordance", () => {
    expect(src).toContain('<span className="metric-label">Laufzeit</span>');
    expect(src).toContain('<span className="metric-label">Tokens</span>');
    expect(src).toContain('<span className="metric-label">ETA</span>');
    expect(kettenCss).toMatch(/\.ketten-v4 \.pipe-scroll\s*\{[^}]*mask-image:\s*linear-gradient/s);
  });

  it("FIX-1: chain progress renders done/total (not the 0..1 progress ratio)", () => {
    expect(src).toContain("{chip.done}/{chip.total}");
    expect(src).toContain("laufEyebrow(state, chip.done, chip.total)");
    expect(src).not.toMatch(/chip\.progress \/ chip\.total/);
  });

  it("FIX-2: completed chips are capped at 3 with an expander", () => {
    expect(src).toContain("completedChips.slice(0, 3)");
    expect(src).toContain("chain-expander");
  });

  it("caps upcoming and expanded done lists at 20 with separate expanders", () => {
    expect(src).toContain("upcomingNodes.slice(0, showAllUpcoming ? undefined : 20)");
    expect(src).toContain("doneNodes.slice(0, showAllDone ? undefined : 20)");
    expect(src).toContain("Weitere Upcoming anzeigen");
    expect(src).toContain("Weitere fertige Schritte anzeigen");
  });

  it("FIX-3: pipeline label is the role (not stripped/sliced), model only as sub when different", () => {
    expect(src).not.toMatch(/\.replace\(\/\^\(coder/);
    expect(src).toContain('node.assignee ?? node.latest_run?.profile ?? "—"');
    expect(src).toContain("const nodeRoute = worker ?? node.latest_run");
    expect(src).toContain("<ModelRouteBadge");
  });
});

// ─── FIX-4/FIX-5: real render against the echte chain-graph payload shape ────
//
// Fixture mirrors the LIVE kanban.db chain-graph node shape (plugin_api.py
// `_chain_graph`), including the new `review_roles` rollup (ALL task_runs per
// node, not just latest_run). Slice t_2fad4004's role runs are copied verbatim
// from the live-verified example (coder/review/None, verifier/review/APPROVED,
// reviewer/review/APPROVED, critic/done/APPROVED); the running slice adds the
// mixed state (reviewer+critic done, verifier open, integrator absent) needed
// to exercise the Rollen-Track.
const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { KettenTab } from "./KettenTab";
import type { BoardResponse, BoardTask, ChainStationSummary, ChainSummary } from "../../lib/types";
import { de } from "../../i18n/de";

const ROOT_ID = "t_231b62fc";
const DONE_ID = "t_2fad4004";
const ACTIVE_ID = "t_c222ff4f";

const ROOT_TASK: BoardTask = {
  id: ROOT_ID, title: "Ketten v4 Fixes", status: "scheduled", assignee: null,
  priority: 0, created_at: 1000, started_at: 1000, completed_at: null,
  branch_name: null, latest_summary: null, link_counts: { parents: 0, children: 2 },
  comment_count: 0, progress: { done: 1, total: 2 }, age: null, tenant: "orchestrator",
  root_id: null, epic_id: null,
};
const DONE_TASK: BoardTask = {
  ...ROOT_TASK, id: DONE_ID, title: "Slice A — done", status: "done", assignee: "coder",
  completed_at: 1900, link_counts: { parents: 1, children: 0 }, root_id: ROOT_ID,
};
const ACTIVE_TASK: BoardTask = {
  ...ROOT_TASK, id: ACTIVE_ID, title: "Slice B — running", status: "running", assignee: "coder",
  completed_at: null, link_counts: { parents: 1, children: 0 }, root_id: ROOT_ID,
};

const BOARD: BoardResponse = {
  columns: [
    { name: "running", tasks: [ROOT_TASK, ACTIVE_TASK] },
    { name: "done", tasks: [DONE_TASK] },
  ],
  tenants: ["orchestrator"],
  assignees: ["coder"],
  latest_event_id: 0,
  source_errors: [],
  now: 2000,
};

const CHAIN_GRAPH_PAYLOAD = {
  schema: "kanban-chain-graph-v1",
  root_id: ROOT_ID,
  checked_at: 2000,
  nodes: [
    {
      id: ROOT_ID, title: "Ketten v4 Fixes", status: "scheduled", assignee: null,
      level: 0, parents: [], children: [DONE_ID, ACTIVE_ID],
      created_at: 1000, started_at: 1000, completed_at: null, last_heartbeat_at: null,
      runtime_seconds: null, progress: { done: 1, total: 2 }, latest_run: null,
      review_roles: [],
      cost_usd: 0, input_tokens: 0, output_tokens: 0, cost_usd_equivalent: 0, cost_effective_usd: 0,
    },
    {
      id: DONE_ID, title: "Slice A — done", status: "done", assignee: "coder",
      level: 1, parents: [ROOT_ID], children: [],
      created_at: 1000, started_at: 1800, completed_at: 1900, last_heartbeat_at: 1900,
      runtime_seconds: 100, progress: null,
      latest_run: {
        id: 1, profile: "critic", status: "done", outcome: "completed",
        started_at: 1800, ended_at: 1900, last_heartbeat_at: 1900,
        runtime_seconds: 100, heartbeat_age_seconds: 100, run_progress: 1,
      },
      // Live-verifiziertes Beispiel (Auftragsdatei): t_2fad4004.
      review_roles: [
        { profile: "coder", status: "review", verdict: null },
        { profile: "verifier", status: "review", verdict: "APPROVED" },
        { profile: "reviewer", status: "review", verdict: "APPROVED" },
        { profile: "critic", status: "done", verdict: "APPROVED" },
      ],
      cost_usd: 0.1, input_tokens: 500, output_tokens: 300, cost_usd_equivalent: 0, cost_effective_usd: 0.1,
    },
    {
      id: ACTIVE_ID, title: "Slice B — running", status: "running", assignee: "coder",
      level: 1, parents: [ROOT_ID], children: [],
      created_at: 1000, started_at: 1950, completed_at: null, last_heartbeat_at: 1990,
      runtime_seconds: 50, progress: null,
      latest_run: {
        id: 2, profile: "coder", status: "running", outcome: null,
        started_at: 1950, ended_at: null, last_heartbeat_at: 1990,
        runtime_seconds: 50, heartbeat_age_seconds: 10, run_progress: 0.4,
      },
      // Rollen-Track-Fixture: reviewer+critic APPROVED, verifier offen, integrator fehlt.
      review_roles: [
        { profile: "coder", status: "running", verdict: null },
        { profile: "reviewer", status: "done", verdict: "APPROVED" },
        { profile: "critic", status: "done", verdict: "APPROVED" },
        { profile: "verifier", status: "running", verdict: null },
      ],
      cost_usd: 0.02, input_tokens: 100, output_tokens: 20, cost_usd_equivalent: 0, cost_effective_usd: 0.02,
    },
  ],
  edges: [
    { from: ROOT_ID, to: DONE_ID },
    { from: ROOT_ID, to: ACTIVE_ID },
  ],
};

function routeFetch() {
  fetchJSONMock.mockImplementation((url: string) => {
    const u = String(url);
    if (u.includes("/chain-graph")) return Promise.resolve(CHAIN_GRAPH_PAYLOAD);
    return Promise.resolve({});
  });
}

describe("KettenTab v4 — Rollen-Track (FIX-5) + Header-Chips (FIX-4), echtes Payload-Format", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    routeFetch();
  });

  afterEach(() => {
    cleanup();
  });

  it("renders reviewer/critic done, verifier pending, integrator missing for the focused (running) slice", async () => {
    render(
      <KettenTab board={BOARD} initialRootId={ROOT_ID} now={2000} onOpenNodeDetail={() => undefined} />,
    );

    await waitFor(() => {
      expect(screen.getByText("REVIEW (aktiver Slice)")).toBeTruthy();
    });

    const wrap = screen.getByText("REVIEW (aktiver Slice)").closest(".rtrack-wrap");
    expect(wrap).not.toBeNull();
    expect(wrap!.textContent).toContain("reviewer ✓");
    expect(wrap!.textContent).toContain("critic ✓");
    expect(wrap!.textContent).toContain("verifier ⏳");
    expect(wrap!.textContent).toContain("integrator –");

    // FIX-4: Header-Chips widersprechen dem Rollen-Track/der Pipeline nicht mehr.
    expect(screen.getByText("Reviewer zugewiesen")).toBeTruthy();
    expect(screen.getByText("Critic aktiv")).toBeTruthy();
    expect(screen.getAllByText("noch nicht gestartet").length).toBeGreaterThan(0);
  });

  it("marks a partial chain-cost rollup as a lower bound", async () => {
    fetchJSONMock.mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("/chain-graph")) return Promise.resolve(CHAIN_GRAPH_PAYLOAD);
      if (u.includes("/chain-costs")) {
        return Promise.resolve({
          schema: "kanban-chain-costs-v1",
          root_id: ROOT_ID,
          totals: {
            input_tokens: 100,
            output_tokens: 20,
            cost_usd: 0,
            actual_cost_usd: 0,
            run_count: 2,
            cost_usd_equivalent: 0.1,
            api_equivalent_usd: 0.1,
            cost_effective_usd: 0.1,
            billing_neuralwatt_kwh: 0,
            billing_neuralwatt_cost_usd: 0,
            cost_usd_equivalent_candidate_runs: 2,
            cost_usd_equivalent_known_runs: 1,
            cost_usd_equivalent_state: "partial",
          },
          by_lane: [],
        });
      }
      return Promise.resolve({});
    });

    render(
      <KettenTab board={BOARD} initialRootId={ROOT_ID} now={2000} onOpenNodeDetail={() => undefined} />,
    );

    expect(await screen.findByText("≥$0.10 gesch.")).toBeTruthy();
  });

  it("marks a zero equivalent floor partial when candidate coverage is incomplete", async () => {
    fetchJSONMock.mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("/chain-graph")) return Promise.resolve(CHAIN_GRAPH_PAYLOAD);
      if (u.includes("/chain-costs")) {
        return Promise.resolve({
          schema: "kanban-chain-costs-v1",
          root_id: ROOT_ID,
          totals: {
            input_tokens: 100,
            output_tokens: 20,
            cost_usd: 0.2,
            actual_cost_usd: 0.2,
            run_count: 2,
            cost_usd_known_runs: 2,
            cost_usd_total_runs: 2,
            cost_usd_coverage: 1,
            cost_usd_state: "complete",
            cost_usd_equivalent: 0,
            api_equivalent_usd: 0,
            cost_effective_usd: 0.2,
            billing_neuralwatt_kwh: 0,
            billing_neuralwatt_cost_usd: 0,
            cost_usd_equivalent_candidate_runs: 1,
            cost_usd_equivalent_known_runs: 0,
            cost_usd_equivalent_state: "partial",
          },
          by_lane: [],
        });
      }
      return Promise.resolve({});
    });

    render(
      <KettenTab board={BOARD} initialRootId={ROOT_ID} now={2000} onOpenNodeDetail={() => undefined} />,
    );

    expect(await screen.findByText("≥$0.20")).toBeTruthy();
  });

  it("renders a scheduled approval hold as gehalten and not läuft", async () => {
    const holdChild: BoardTask = { ...ACTIVE_TASK, status: "scheduled", started_at: null };
    const holdBoard: BoardResponse = {
      ...BOARD,
      columns: [{ name: "scheduled", tasks: [ROOT_TASK, holdChild] }],
    };
    const { container } = render(
      <KettenTab board={holdBoard} initialRootId={null} now={2000} onOpenNodeDetail={() => undefined} />,
    );

    const card = container.querySelector('article.lk-card[data-state="gehalten"]');
    expect(card).not.toBeNull();
    const eyeb = card!.querySelector(".lk-eyeb");
    expect(eyeb?.textContent).toContain("Gehalten");
    expect(eyeb?.textContent).not.toContain("Läuft");
  });

  it("renders authoritative totals for a completed chain omitted from compact columns", () => {
    const compactBoard: BoardResponse = {
      ...BOARD,
      columns: [{ name: "done", tasks: [] }],
      chain_summaries: [{
        root_id: "t_compact_done",
        root_title: "Kompakte fertige Kette",
        total: 37,
        done: 37,
        status_counts: { done: 37 },
        latest_completed_at: 1999,
      }],
      done_page: {
        total_count: 100,
        loaded_count: 30,
        limit: 30,
        has_more: true,
        next_cursor: "0:1:t_cursor",
      },
    };

    render(
      <KettenTab board={compactBoard} initialRootId={null} now={2000} onOpenNodeDetail={() => undefined} />,
    );

    const item = screen.getByText("Kompakte fertige Kette").closest(".lk-done-row");
    expect(item).not.toBeNull();
    expect(within(item as HTMLElement).getByText(/37\/37/)).toBeTruthy();
    expect(item!.querySelector(".lk-done-led")).not.toBeNull();
  });

  it("clamps clipped chain titles with the full text as title fallback (graph titles keep it too)", async () => {
    render(
      <KettenTab board={BOARD} initialRootId={ROOT_ID} now={2000} onOpenNodeDetail={() => undefined} />,
    );

    const kennung = await screen.findByText("Ketten v4 Fixes", { selector: ".lk-kennung" });
    expect(kennung.getAttribute("title")).toBe("Ketten v4 Fixes");
    const focusTitle = await screen.findByText("Slice B — running", { selector: ".detail-title" });
    expect(focusTitle.getAttribute("title")).toBe("Slice B — running");
  });

  it("adds the selected board to every chain graph and chain costs request", async () => {
    render(
      <KettenTab
        board={BOARD}
        boardSlug="health-track"
        initialRootId={ROOT_ID}
        now={2000}
        onOpenNodeDetail={() => undefined}
      />,
    );

    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledWith(`/api/plugins/kanban/tasks/${ROOT_ID}/chain-graph?board=health-track`);
      expect(fetchJSONMock).toHaveBeenCalledWith(`/api/plugins/kanban/tasks/${ROOT_ID}/chain-costs?board=health-track`);
    });
  });

  it("FIX-6: Board-Switch Race — keine Requests mit altem rootId auf neuem Board", async () => {
    const { rerender } = render(
      <KettenTab
        board={BOARD}
        boardSlug="board-a"
        initialRootId={ROOT_ID}
        now={2000}
        onOpenNodeDetail={() => undefined}
      />,
    );

    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledWith(`/api/plugins/kanban/tasks/${ROOT_ID}/chain-graph?board=board-a`);
    });

    const OTHER_ROOT = "t_other_board";
    const OTHER_CHILD: BoardTask = {
      ...ROOT_TASK, id: "t_other_child", title: "Other child", status: "running", root_id: OTHER_ROOT,
    };
    const OTHER_TASK: BoardTask = {
      ...ROOT_TASK, id: OTHER_ROOT, title: "Other root", status: "scheduled", root_id: OTHER_ROOT,
    };
    const BOARD_B: BoardResponse = {
      columns: [{ name: "running", tasks: [OTHER_TASK, OTHER_CHILD] }],
      tenants: ["orchestrator"], assignees: ["coder"], latest_event_id: 1,
      source_errors: [], now: 3000,
    };

    fetchJSONMock.mockClear();
    rerender(
      <KettenTab
        board={BOARD_B}
        boardSlug="board-b"
        initialRootId={ROOT_ID}
        now={3000}
        onOpenNodeDetail={() => undefined}
      />,
    );

    // Nach dem Re-render sollten kurz keine 404-Fetches mit altem rootId passieren.
    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledWith(`/api/plugins/kanban/tasks/${OTHER_ROOT}/chain-graph?board=board-b`);
    });

    const badRequests = fetchJSONMock.mock.calls.filter(([url]) =>
      String(url).includes(ROOT_ID) && String(url).includes("board-b"),
    );
    expect(badRequests).toHaveLength(0);

    const chainGraphCalls = fetchJSONMock.mock.calls.filter(([url]) =>
      String(url).includes("/chain-graph"),
    );
    const chainCostsCalls = fetchJSONMock.mock.calls.filter(([url]) =>
      String(url).includes("/chain-costs"),
    );
    expect(chainGraphCalls).toHaveLength(1);
    expect(chainCostsCalls).toHaveLength(1);
  });
});

// ─── LK-1: Laufkarte mit Stanzkante und Stationen ────────────────────────────
// Verbindliche Vorlage: Design-Board c_ee80d956 / e_a4b27b43
// („B v2 — Blatt 1 von 2: KETTEN-TAB als Laufkarte“).

const themeCss = readFileSync(path.resolve(import.meta.dirname, "../../theme.css"), "utf8");

function station(over: Partial<ChainStationSummary> & { id: string }): ChainStationSummary {
  return {
    kennung: null,
    title: over.id,
    state: "offen",
    lane: null,
    runtime_seconds: null,
    cost_usd: null,
    started_at: null,
    completed_at: null,
    wait_reason: null,
    ...over,
  };
}

const LAUF_ROOT = "t_lauf_kf";
const LAUF_BOARD: BoardResponse = {
  ...BOARD,
  chain_summaries: [{
    root_id: LAUF_ROOT,
    root_title: "Kettenweite Freigabe und Ketten-Sichtbarkeit im Fleet-Board",
    total: 6,
    done: 2,
    status_counts: { done: 2, parked: 4 },
    latest_completed_at: null,
    state: "gehalten",
    kennung: "Kettenweite Freigabe",
    state_age_seconds: 6 * 3600,
    total_cost_usd: 0.34,
    stations: [
      station({ id: "s1", title: "Einzelsperre fail-closed ablehnen", state: "fertig", lane: "coder", runtime_seconds: 246, cost_usd: 0.11, started_at: 1000, completed_at: 1246 }),
      station({ id: "s2", title: "Ketten-Freigabe-Endpunkt", state: "fertig", lane: "coder", runtime_seconds: 531, cost_usd: 0.23, started_at: 1300, completed_at: 1831 }),
      station({ id: "s3", title: "Laufkarte im Ketten-Reiter", state: "gehalten", lane: "coder-frontend", wait_reason: "wartet auf Freigabe" }),
      station({ id: "s4", title: "Review-Urteil zum Hebel", state: "offen", lane: "reviewer" }),
      station({ id: "s5", title: "PlanSpec-Gate", state: "offen" }),
      station({ id: "s6", title: "Abschluss-Receipt", state: "offen" }),
    ],
    stations_total: 6,
  }],
};

describe("KettenTab LK-1 — Laufkarte mit Stanzkante und Stationen", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    routeFetch();
  });

  afterEach(() => {
    cleanup();
  });

  it("AC-1+AC-5: rendert die Kette als Block mit Stationen, Lane und zustandsgemäßen Meta-Angaben", () => {
    const { container } = render(
      <KettenTab board={LAUF_BOARD} initialRootId={LAUF_ROOT} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    const card = container.querySelector('article.lk-card[data-state="gehalten"]');
    expect(card).not.toBeNull();

    const doneRows = card!.querySelectorAll("li.lk-st.is-done");
    expect(doneRows).toHaveLength(2);
    expect(doneRows[0].textContent).toContain("Einzelsperre fail-closed ablehnen");
    expect(doneRows[0].textContent).toContain("coder");
    expect(doneRows[0].textContent).toContain("4 min"); // Laufzeit
    expect(doneRows[0].textContent).toContain("$0,11"); // Kosten
    // Stempel-Haken an erledigten Stationen
    expect(doneRows[0].querySelector(".lk-stamp")?.textContent).toBe("✓");

    const held = card!.querySelector("li.lk-st.is-held");
    expect(held?.textContent).toContain("coder-frontend");
    expect(held?.textContent).toContain("wartet auf Freigabe"); // Grund des Wartens

    const waiting = card!.querySelector("li.lk-st.is-wait");
    expect(waiting?.textContent).toContain("reviewer");
    expect(waiting?.textContent).toContain("bereit");
  });

  it("AC-6: kürzt lange Ketten sichtbar und nennt die Gesamtzahl", () => {
    const { container } = render(
      <KettenTab board={LAUF_BOARD} initialRootId={LAUF_ROOT} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    const card = container.querySelector("article.lk-card")!;
    const stationRows = card.querySelectorAll("li.lk-st");
    expect(stationRows).toHaveLength(4); // Kappung auf die ersten Stationen
    const more = card.querySelector("li.lk-more");
    expect(more?.textContent).toBe("… +2 weitere von 6 Stationen");
  });

  it("AC-3: die Kennung ist das lauteste Element — größer und stärker als der Prosatitel", () => {
    const { container } = render(
      <KettenTab board={LAUF_BOARD} initialRootId={LAUF_ROOT} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    const card = container.querySelector("article.lk-card")!;
    const kennung = card.querySelector(".lk-kennung");
    expect(kennung?.textContent).toBe("Kettenweite Freigabe");
    const titel = card.querySelector(".lk-titel");
    expect(titel?.textContent).toBe("Kettenweite Freigabe und Ketten-Sichtbarkeit im Fleet-Board");
    // Kennung steht vor dem Titel im Kopf
    expect(kennung!.compareDocumentPosition(titel!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    // CSS-Vertrag: Kennung 700/1.05rem Display, Titel nur Sekundär-Skalenstufe
    const kRule = fleetCss.match(/\.lk-kennung\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(kRule).toMatch(/font:\s*700\s+1\.05rem/);
    expect(kRule).toContain("var(--font-display)");
    const tRule = fleetCss.match(/\.lk-titel\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(tRule).toContain("var(--text-sec)");
  });

  it("AC-4: die Zustandszeile nennt Zustand und Fortschritt in Worten", () => {
    const { container } = render(
      <KettenTab board={LAUF_BOARD} initialRootId={LAUF_ROOT} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    const eyeb = container.querySelector('article.lk-card[data-state="gehalten"] .lk-eyeb');
    expect(eyeb?.textContent).toContain("Gehalten · Station 2 von 6 durch");
    expect(eyb_led(eyeb)).not.toBeNull();

    const runBoard: BoardResponse = {
      ...BOARD,
      chain_summaries: [{
        root_id: "t_lauf_run",
        root_title: "Laufende Kette",
        total: 5,
        done: 1,
        status_counts: { done: 1, running: 1, todo: 3 },
        latest_completed_at: null,
        state: "laeuft",
      }],
    };
    const { container: runContainer } = render(
      <KettenTab board={runBoard} initialRootId={null} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    const runEyeb = runContainer.querySelector('article.lk-card[data-state="laeuft"] .lk-eyeb');
    expect(runEyeb?.textContent).toContain("Läuft · Station 2 von 5");
  });

  it("AC-2: Segment-Form pro Stationszustand — Stahl mit Querkerbe, massiv, Kontur", () => {
    // erledigt = massiver Stahl mit Querkerbe (nie das Ampel-Grün des Status-Kanals)
    const doneRule = fleetCss.match(/\.lk-st\.is-done \.lk-notch b\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(doneRule).toContain("var(--color-stamp)");
    expect(doneRule).not.toContain("--color-status-ok");
    expect(fleetCss).toMatch(/\.lk-st\.is-done \.lk-notch::after/); // Querkerbe
    // laufend = massiv in Bronze
    expect(fleetCss).toMatch(/\.lk-st\.is-now \.lk-notch b\s*\{[^}]*var\(--color-live\)/);
    // offen + gehalten = ausgestanzte Kontur (inset-Rahmen, transparente Füllung)
    const waitRule = fleetCss.match(/\.lk-st\.is-wait \.lk-notch b\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(waitRule).toContain("background: transparent");
    expect(waitRule).toMatch(/box-shadow:\s*inset/);
    const heldRule = fleetCss.match(/\.lk-st\.is-held \.lk-notch b\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(heldRule).toContain("background: transparent");
    expect(heldRule).toMatch(/box-shadow:\s*inset/);
    // Stahl-Tinte ist ein Token in theme.css, keine Rohfarbe in der Komponenten-CSS
    expect(themeCss).toMatch(/--color-stamp:\s*#[0-9a-f]{6}/i);
    expect(themeCss).toMatch(/--color-stamp-2:\s*#[0-9a-f]{6}/i);
    const doneRuleHex = fleetCss.match(/\.lk-st\.is-done \.lk-notch b\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(doneRuleHex).not.toMatch(/#[0-9a-f]{3,8}/i);
  });

  it("AC-7: einziger Startknopf ist „Kette freigeben“ und wirkt auf die ganze Kette", async () => {
    const { container } = render(
      <KettenTab board={LAUF_BOARD} initialRootId={LAUF_ROOT} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    const card = container.querySelector("article.lk-card")!;
    // Keine Möglichkeit, eine einzelne Station zu starten
    const list = card.querySelector("ol.lk-stationen")!;
    expect(within(list as HTMLElement).queryByRole("button")).toBeNull();

    const releaseBtn = within(card as HTMLElement).getByRole("button", { name: de.fleet.laufFreigeben });
    fireEvent.click(releaseBtn);
    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledWith(
        `/api/plugins/kanban/tasks/${LAUF_ROOT}/flow-release`,
        expect.objectContaining({ method: "POST", body: JSON.stringify({ release_level: "live" }) }),
      );
    });
  });

  it("AC-7: readOnly blendet den Freigabe-Knopf aus", () => {
    render(
      <KettenTab board={LAUF_BOARD} initialRootId={LAUF_ROOT} now={2000} readOnly onOpenNodeDetail={() => undefined} />,
    );
    expect(screen.queryByRole("button", { name: de.fleet.laufFreigeben })).toBeNull();
  });

  it("meldet einen Freigabe-Fehler als sichtbaren Alert", async () => {
    fetchJSONMock.mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("flow-release")) return Promise.reject(new Error("HTTP 409: Konflikt"));
      if (u.includes("/chain-graph")) return Promise.resolve(CHAIN_GRAPH_PAYLOAD);
      return Promise.resolve({});
    });
    render(
      <KettenTab board={LAUF_BOARD} initialRootId={LAUF_ROOT} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    fireEvent.click(screen.getByRole("button", { name: de.fleet.laufFreigeben }));
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("Freigabe fehlgeschlagen");
    });
  });

  it("degradiert ohne Stationsliste auf Kennung, Zustand und Fortschritt — keine erfundenen Stationen", () => {
    const { container } = render(
      <KettenTab board={BOARD} initialRootId={ROOT_ID} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    expect(container.querySelector("ol.lk-stationen")).toBeNull();
    const kennung = container.querySelector(".lk-kennung");
    expect(kennung?.textContent).toBe("Ketten v4 Fixes");
    const eyeb = container.querySelector(".lk-eyeb");
    expect(eyeb?.textContent).toContain("Läuft · Station 2 von 3");
  });

  it("AC-8: lange Titel klemmen mit Ellipsis und tragen den Volltext als title-Fallback", () => {
    const kRule = fleetCss.match(/\.lk-kennung\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(kRule).toContain("white-space: nowrap");
    expect(kRule).toContain("overflow: hidden");
    expect(kRule).toContain("text-overflow: ellipsis");
    const tRule = fleetCss.match(/\.lk-titel\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(tRule).toContain("-webkit-line-clamp: 2");

    // 215 Zeichen — die im Live-Board vorkommende Titellänge
    const longTitle = "L".repeat(215);
    const longBoard: BoardResponse = {
      ...BOARD,
      chain_summaries: [{
        root_id: "t_lauf_long",
        root_title: longTitle,
        total: 2,
        done: 0,
        status_counts: { parked: 2 },
        latest_completed_at: null,
      }],
    };
    const { container } = render(
      <KettenTab board={longBoard} initialRootId={null} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    const kennung = container.querySelector(".lk-kennung");
    expect(kennung?.getAttribute("title")).toBe(longTitle);
    // Kein doppelter Prosatitel, wenn Kennung-Fallback dem Titel entspricht
    expect(container.querySelector(".lk-titel")).toBeNull();
  });
});

function eyb_led(eyeb: Element | null): Element | null {
  return eyeb?.querySelector(".lk-led") ?? null;
}

// ─── LK-2: Aufmerksamkeits-Ordnung, Kopf-Zähler, Leer/Laden/Fehler ───────────
// Verbindliche Vorlage wie LK-1: Design-Board c_ee80d956 / e_a4b27b43.

function lk2Summary(over: Partial<ChainSummary> & { root_id: string }): ChainSummary {
  return {
    root_title: over.root_id,
    total: 3,
    done: 0,
    status_counts: { todo: 3 },
    latest_completed_at: null,
    kennung: over.root_id,
    ...over,
  };
}

const LK2_SUMMARIES: ChainSummary[] = [
  // Bewusst „falsche“ Eingabereihenfolge — die Komponente muss selbst ordnen.
  lk2Summary({ root_id: "t_lk2_done1", kennung: "Fertig alt", done: 3, status_counts: { done: 3 }, latest_completed_at: 900 }),
  lk2Summary({ root_id: "t_lk2_hold", kennung: "Gehalten", done: 2, status_counts: { done: 2, parked: 1 }, state: "gehalten", state_age_seconds: 3600 }),
  lk2Summary({ root_id: "t_lk2_run", kennung: "Laeuft", done: 1, status_counts: { done: 1, running: 1, todo: 1 }, state: "laeuft", state_age_seconds: 120 }),
  lk2Summary({ root_id: "t_lk2_brk1", kennung: "Angebrochen eins", done: 2, status_counts: { done: 2, todo: 1 }, state: "angebrochen", state_age_seconds: 7200 }),
  lk2Summary({ root_id: "t_lk2_wait", kennung: "Wartet", done: 0, status_counts: { todo: 3 } }),
  lk2Summary({ root_id: "t_lk2_brk2", kennung: "Angebrochen zwei", done: 1, status_counts: { done: 1, todo: 2 }, state: "angebrochen", state_age_seconds: 600 }),
  lk2Summary({ root_id: "t_lk2_done2", kennung: "Fertig neu", done: 3, status_counts: { done: 3 }, latest_completed_at: 1900 }),
  lk2Summary({ root_id: "t_lk2_done3", kennung: "Fertig mitte", done: 3, status_counts: { done: 3 }, latest_completed_at: 1400 }),
];

function lk2Board(summaries: ChainSummary[]): BoardResponse {
  return {
    ...BOARD,
    columns: [{
      name: "running",
      tasks: [{ ...ROOT_TASK, id: "t_lk2_run_child", title: "Laufendes Kind", status: "running", root_id: "t_lk2_run" }],
    }],
    chain_summaries: summaries,
  };
}

const LK2_KENNUNG_ORDER = [
  "Angebrochen eins", "Angebrochen zwei", "Laeuft", "Gehalten", "Wartet",
  "Fertig neu", "Fertig mitte", "Fertig alt",
];

function lk2ListKennungen(container: HTMLElement): string[] {
  return [...container.querySelectorAll(".lk-list .lk-kennung, .lk-list .lk-done-kennung")]
    .map((el) => el.textContent ?? "");
}

describe("KettenTab LK-2 — Aufmerksamkeits-Ordnung, Kopf-Zähler, Zustände", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    routeFetch();
  });

  afterEach(() => {
    cleanup();
  });

  it("AC-1: ordnet angebrochen → laufend → gehalten → wartend → fertig, nicht nach Alter", () => {
    const { container } = render(
      <KettenTab board={lk2Board(LK2_SUMMARIES)} initialRootId={null} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    const states = [...container.querySelectorAll(".lk-list > *")].map((el) =>
      el.classList.contains("lk-done-row") ? "fertig" : (el as HTMLElement).dataset.state);
    expect(states).toEqual([
      "angebrochen", "angebrochen", "laeuft", "gehalten", "wartet", "fertig", "fertig", "fertig",
    ]);
    expect(lk2ListKennungen(container)).toEqual(LK2_KENNUNG_ORDER);
  });

  it("AC-2: fertige Kette belegt genau eine Zeile mit Kennung, Fortschritt und Zeitangabe", () => {
    const { container } = render(
      <KettenTab board={lk2Board(LK2_SUMMARIES)} initialRootId={null} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    const rows = container.querySelectorAll(".lk-done-row");
    expect(rows).toHaveLength(3);
    const first = rows[0];
    expect(first.querySelector(".lk-done-kennung")?.textContent).toBe("Fertig neu");
    const meta = first.querySelector(".lk-done-meta")?.textContent ?? "";
    expect(meta).toContain("3/3"); // Fortschritt
    expect(meta).toMatch(/·/); // kompakte Zeitangabe dahinter
  });

  it("AC-3: Kopf nennt Gesamtzahl und angebrochene getrennt", () => {
    render(
      <KettenTab board={lk2Board(LK2_SUMMARIES)} initialRootId={null} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    expect(screen.getByText("8 · 2 angebrochen")).toBeTruthy();
  });

  it("AC-4: Reihenfolge ist bei gleichem Zustand stabil — permutierte Eingabe, gleiche Ausgabe", () => {
    const { container: a } = render(
      <KettenTab board={lk2Board(LK2_SUMMARIES)} initialRootId={null} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    const forward = lk2ListKennungen(a);
    cleanup();
    const { container: b } = render(
      <KettenTab board={lk2Board([...LK2_SUMMARIES].reverse())} initialRootId={null} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    expect(lk2ListKennungen(b)).toEqual(forward);
    expect(lk2ListKennungen(b)).toEqual(LK2_KENNUNG_ORDER);
  });

  it("AC-5: Leerzustand folgt der Doktrin — Situation, Bewertung, nächste Aktion, kein ok-Grün", () => {
    const emptyBoard: BoardResponse = { ...BOARD, columns: [], chain_summaries: [] };
    const { container } = render(
      <KettenTab board={emptyBoard} initialRootId={null} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    const empty = container.querySelector(".kt-empty");
    expect(empty).not.toBeNull();
    const lines = empty!.querySelectorAll("p");
    expect(lines).toHaveLength(3);
    expect(lines[0].textContent).toBe(de.fleet.kettenLeer); // Situation
    expect(lines[1].textContent).toBe(de.fleet.kettenLeerDesc); // Bewertung
    expect(lines[2].textContent).toContain(de.fleet.kettenLeerAktion); // nächste Aktion
    // CSS-Vertrag: kein ok-Grün im Leerzustand
    const emptyRules = kettenCss.match(/\.kt-empty[^{]*\{[^}]*\}/g) ?? [];
    expect(emptyRules.length).toBeGreaterThan(0);
    for (const rule of emptyRules) {
      expect(rule).not.toMatch(/status-ok|--kt-live\b/);
    }
  });

  it("AC-6: Ladezustand behält die Kettengeometrie und ist von leer unterscheidbar", () => {
    const { container } = render(
      <KettenTab board={null} initialRootId={null} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    const loading = container.querySelector('.lk-list-loading[role="status"]');
    expect(loading).not.toBeNull();
    const skeletons = loading!.querySelectorAll(".lk-skeleton");
    expect(skeletons).toHaveLength(3);
    // Kettengeometrie: Stationen mit Notch bleiben erhalten, damit nichts springt
    expect(skeletons[0].querySelectorAll(".lk-stationen .lk-notch").length).toBeGreaterThan(0);
    expect(container.querySelector(".kt-empty")).toBeNull();
  });

  it("AC-6: Fehlerzustand ist von leer und ladend unterscheidbar", async () => {
    fetchJSONMock.mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("/chain-graph")) return Promise.reject(new Error("boom"));
      return Promise.resolve({});
    });
    const { container } = render(
      <KettenTab board={LAUF_BOARD} initialRootId={LAUF_ROOT} now={2000} onOpenNodeDetail={() => undefined} />,
    );
    await waitFor(() => {
      expect(container.querySelector('.kt-error[role="alert"]')).not.toBeNull();
    });
    expect(container.querySelector(".kt-error")!.textContent).toContain(de.fleet.kettenGraphFehler);
    expect(container.querySelector(".kt-empty")).toBeNull();
    expect(container.querySelector(".lk-list-loading")).toBeNull();
  });
});
