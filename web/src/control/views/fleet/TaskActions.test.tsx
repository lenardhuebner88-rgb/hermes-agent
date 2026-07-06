// @vitest-environment jsdom
//
// Regression + opt-in coverage for FleetTaskActions' `review`-status contract:
// by default Ship/Rework stay hidden for `status === "review"` (the Flow
// cockpit's verifier lock — see the comment in TaskActions.tsx); the
// NodeDetailDrawer opts in via `allowReviewStage` because it shows the
// verifier verdicts (Ergebnis-Tab) right next to these buttons.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { FleetTaskActions } from "./TaskActions";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const FIXTURE_TASK_ID = "t_review123";

describe("FleetTaskActions — review-status stage buttons (default vs. allowReviewStage)", () => {
  it("status=review, default props: no Ship/Rework stage buttons", () => {
    render(<FleetTaskActions taskId={FIXTURE_TASK_ID} status="review" />);

    expect(screen.queryByRole("button", { name: "Ausliefern" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Nacharbeit" })).toBeNull();
  });

  it("status=review + allowReviewStage: Ship and Rework are present", () => {
    render(<FleetTaskActions taskId={FIXTURE_TASK_ID} status="review" allowReviewStage />);

    expect(screen.getByRole("button", { name: "Ausliefern" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Nacharbeit" })).toBeTruthy();
  });

  it("Ship arms then PATCHes the task to the stageActions('review') Ship target status", async () => {
    fetchJSONMock.mockResolvedValue({ task: { id: FIXTURE_TASK_ID, status: "done" } });

    render(<FleetTaskActions taskId={FIXTURE_TASK_ID} status="review" allowReviewStage />);

    // First click arms (two-click confirm, the house pattern) — no fetch yet.
    fireEvent.click(screen.getByRole("button", { name: "Ausliefern" }));
    expect(fetchJSONMock).not.toHaveBeenCalled();

    // Second click (the armed confirm button) fires the PATCH.
    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));

    expect(fetchJSONMock).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchJSONMock.mock.calls[0];
    expect(url).toBe(`/api/plugins/kanban/tasks/${FIXTURE_TASK_ID}`);
    expect(opts.method).toBe("PATCH");
    // ACTION.ship.target in lib/fleet.ts — the exact status Ship writes.
    expect(JSON.parse(opts.body)).toEqual({ status: "done" });
  });
});
