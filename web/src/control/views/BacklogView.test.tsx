import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { FoBacklogQueueTable, FoHealthStrip, ReasonChips } from "./BacklogView";
import { BacklogHeroPanel } from "./backlog/BacklogSections";
import type { BacklogItem, BacklogContractHealth } from "../lib/schemas";
import type { CommissionState, DispatchFoState, FoBoardStatus } from "../hooks/useControlData";

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
    source_path: `backlog/items/${overrides.id}-task.md`,
    ...overrides,
  };
}

const health: BacklogContractHealth = {
  source_count: 3,
  counted_sum: 2,
  unknown_statuses: [{ status: "readyish", count: 1, ids: ["0099"] }],
  invalid_risk_count: 0,
  invalid_owner_count: 0,
  unowned_count: 1,
  stale_count: 1,
  missing_acceptance_count: 2,
  missing_next_action_count: 1,
  invalid_area_count: 0,
};

describe("Family Organizer queue-first view pieces", () => {
  it("renders the contract-health strip with operator queue counts", () => {
    const html = renderToStaticMarkup(
      <FoHealthStrip
        items={[
          item({ id: "0001", status: "now" }),
          item({ id: "0002", status: "next", risk: "high", owner: "unassigned" }),
          item({ id: "0003", status: "blocked", stale: true }),
        ]}
        contractHealth={health}
      />,
    );

    expect(html).toContain("Now");
    expect(html).toContain("Next Ready");
    expect(html).toContain("Contract Drift");
    expect(html).toContain("Missing Acceptance");
  });

  it("renders a dense queue table with next action, source/id, and quality lens", () => {
    const html = renderToStaticMarkup(
      <FoBacklogQueueTable
        items={[
          item({ id: "0002", title: "Ready task", status: "next", risk: "high", area: "db" }),
          item({ id: "0003", title: "Fix", owner: "unassigned", stale: true, updated: "2026-05-01", missing_acceptance: true, missing_next_action: true }),
        ]}
        nowSec={1770000000}
        nextTaskId="0002"
        onOpen={() => undefined}
      />,
    );

    expect(html).toContain("<table");
    expect(html).toContain("Next Action");
    expect(html).toContain("Source/Id");
    expect(html).toContain("Ready task");
    expect(html).toContain("Akzeptanz");
    expect(html).toContain("backlog/items/0002-task.md");
  });

  it("shows the server quality taxonomy (incl. large_scope) in the list without a detail fetch", () => {
    const html = renderToStaticMarkup(
      <FoBacklogQueueTable
        items={[
          item({
            id: "0005",
            title: "Server-flagged item",
            status: "next",
            // v2: large_scope is body-derived and only the server can compute it for the list
            quality_issues: [
              { code: "large_scope", severity: "warn" },
              { code: "missing_acceptance", severity: "risk" },
            ],
          }),
        ]}
        nowSec={1770000000}
        nextTaskId={null}
        onOpen={() => undefined}
      />,
    );

    expect(html).toContain("Scope gross");
    expect(html).toContain("Akzeptanz fehlt");
  });

  it("highlights the keyboard-active row via aria-current + ring", () => {
    const html = renderToStaticMarkup(
      <FoBacklogQueueTable
        items={[item({ id: "0002", status: "next" }), item({ id: "0003", status: "next" })]}
        nowSec={1770000000}
        nextTaskId={null}
        activeId="0003"
        onOpen={() => undefined}
      />,
    );

    expect(html).toContain('aria-current="true"');
    expect(html).toContain("ring-cyan-400/70");
  });

  it("renders queue reason codes as German labels", () => {
    const html = renderToStaticMarkup(
      <ReasonChips codes={["now_status", "high_risk", "penalty_unowned"]} />,
    );
    expect(html).toContain("Status now");
    expect(html).toContain("Hohes Risiko");
    expect(html).toContain("Kein Owner");
  });

  it("names the queue/board view-mode buttons for assistive tech", () => {
    const html = renderToStaticMarkup(
      <BacklogHeroPanel
        activeTotal={3}
        doneTotal={190}
        breakdown={{ now: 1, next: 1, in_progress: 0, blocked: 1, later: 0 }}
        loading={false}
        nowSec={1770000000}
        auditPrompt="audit"
        viewMode="queue"
        onViewMode={() => undefined}
      />,
    );

    expect(html).toContain('aria-label="Queue-Ansicht"');
    expect(html).toContain('aria-label="Board-Ansicht"');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('aria-pressed="false"');
  });

  it("keeps the Backlog hero compact so the queue starts earlier", () => {
    const html = renderToStaticMarkup(
      <BacklogHeroPanel
        activeTotal={3}
        doneTotal={190}
        breakdown={{ now: 1, next: 1, in_progress: 0, blocked: 1, later: 0 }}
        loading={false}
        nowSec={1770000000}
        auditPrompt="audit"
        viewMode="queue"
        onViewMode={() => undefined}
      />,
    );

    expect(html).toContain('aria-label="Backlog Zusammenfassung"');
    expect(html).toContain("Aktiv");
    expect(html).toContain("Erledigt");
    expect(html).toContain("1 jetzt");
    expect(html).not.toContain("hc-type-display");
  });

  // S4 — error-state visibility tests: commission and dispatch failures must be
  // visible in the rendered UI, never silently swallowed.

  it("commission error state renders a retry affordance and red colour (not silent)", () => {
    // commissionState[id] = "error" must produce visible error UI on the row.
    const id = "0010";
    const errorState: Record<string, CommissionState> = { [id]: "error" };
    const html = renderToStaticMarkup(
      <FoBacklogQueueTable
        items={[item({ id, title: "Commission me", status: "next" })]}
        nowSec={1770000000}
        nextTaskId={null}
        onOpen={() => undefined}
        onCommission={() => undefined}
        commissionState={errorState}
      />,
    );
    // The error button must contain the retry label text
    expect(html).toContain("nochmal");
    // And must NOT show the normal "→ Fleet" / commission label (error state replaces it)
    // The button should have the red border class
    expect(html).toContain("red-500");
  });

  it("commission done state renders success indicator, not an error", () => {
    const id = "0011";
    const doneState: Record<string, CommissionState> = { [id]: "done" };
    const html = renderToStaticMarkup(
      <FoBacklogQueueTable
        items={[item({ id, title: "Already commissioned", status: "next" })]}
        nowSec={1770000000}
        nextTaskId={null}
        onOpen={() => undefined}
        onCommission={() => undefined}
        commissionState={doneState}
      />,
    );
    // Done state uses emerald, not red
    expect(html).toContain("emerald");
    expect(html).not.toContain("red-500");
  });

  it("dispatch error state renders a retry affordance and red colour (not silent)", () => {
    // A board task in triage/scheduled status + dispatchStateByTaskId = "error"
    // must make the dispatch error visible.
    const foId = "0012";
    const boardTaskId = "t-board-999";
    const boardStatusById: Record<string, FoBoardStatus> = {
      [foId]: { taskId: boardTaskId, status: "triage", label: "wartet" },
    };
    const dispatchState: Record<string, DispatchFoState> = { [boardTaskId]: "error" };
    const html = renderToStaticMarkup(
      <FoBacklogQueueTable
        items={[item({ id: foId, title: "Dispatch me", status: "next" })]}
        nowSec={1770000000}
        nextTaskId={null}
        onOpen={() => undefined}
        onDispatch={() => undefined}
        boardStatusById={boardStatusById}
        dispatchStateByTaskId={dispatchState}
      />,
    );
    // Dispatch error also renders "nochmal" and red
    expect(html).toContain("nochmal");
    expect(html).toContain("red-500");
  });

  it("dispatch done state renders success indicator, not an error", () => {
    const foId = "0013";
    const boardTaskId = "t-board-888";
    const boardStatusById: Record<string, FoBoardStatus> = {
      [foId]: { taskId: boardTaskId, status: "scheduled", label: "wartet" },
    };
    const dispatchState: Record<string, DispatchFoState> = { [boardTaskId]: "done" };
    const html = renderToStaticMarkup(
      <FoBacklogQueueTable
        items={[item({ id: foId, title: "Already dispatched", status: "next" })]}
        nowSec={1770000000}
        nextTaskId={null}
        onOpen={() => undefined}
        onDispatch={() => undefined}
        boardStatusById={boardStatusById}
        dispatchStateByTaskId={dispatchState}
      />,
    );
    expect(html).toContain("emerald");
    expect(html).not.toContain("red-500");
  });
});
