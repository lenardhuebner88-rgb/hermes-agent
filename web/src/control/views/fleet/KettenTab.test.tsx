// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const src = readFileSync(path.resolve(import.meta.dirname, "KettenTab.tsx"), "utf8");
const kettenCss = readFileSync(path.resolve(import.meta.dirname, "ketten-v4.css"), "utf8");
const fleetCss = readFileSync(path.resolve(import.meta.dirname, "fleet.css"), "utf8");
const heuteSrc = readFileSync(path.resolve(import.meta.dirname, "HeuteTab.tsx"), "utf8");

describe("KettenTab v4 — redesign checks", () => {
  it("makes all interactive elements keyboard-focus-visible", () => {
    expect(src).toMatch(/chain-item/);
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

  it("FIX-1: chain-list fraction uses done/total (not the 0..1 progress ratio)", () => {
    expect(src).toContain("chip.done / chip.total");
    expect(src).toContain("{chip.done}/{chip.total}");
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
import type { BoardResponse, BoardTask } from "../../lib/types";

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

  it("renders a scheduled approval hold as gehalten and not läuft", async () => {
    const holdChild: BoardTask = { ...ACTIVE_TASK, status: "scheduled", started_at: null };
    const holdBoard: BoardResponse = {
      ...BOARD,
      columns: [{ name: "scheduled", tasks: [ROOT_TASK, holdChild] }],
    };
    const { container } = render(
      <KettenTab board={holdBoard} initialRootId={null} now={2000} onOpenNodeDetail={() => undefined} />,
    );

    await waitFor(() => expect(container.querySelector(".chain-badge")?.textContent).toBe("gehalten"));
    expect(container.querySelector(".chain-badge")?.textContent).not.toContain("läuft");
    expect(container.querySelector(".glyph-waiting")).toBeTruthy();
    expect(container.querySelector(".glyph-active")).toBeNull();
  });

  it("expands clipped chain titles on tap while retaining other title fallbacks", async () => {
    render(
      <KettenTab board={BOARD} initialRootId={ROOT_ID} now={2000} onOpenNodeDetail={() => undefined} />,
    );

    const chainTitle = await screen.findByText("Ketten v4 Fixes", { selector: ".chain-title" });
    expect(chainTitle.getAttribute("title")).toBeNull();
    expect(chainTitle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(chainTitle);
    expect(chainTitle.getAttribute("aria-expanded")).toBe("true");
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
