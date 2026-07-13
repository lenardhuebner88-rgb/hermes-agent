// @vitest-environment jsdom
//
// Regression coverage for FleetTaskActions' `review`-status contract. The
// backend deliberately rejects direct PATCH review→done and review→blocked;
// no UI context may opt back into those deterministic 409 actions.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

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

  it("status=review + legacy opt-in: Ship and Rework still stay absent", () => {
    // @ts-expect-error The removed escape hatch must not re-enter the public contract.
    render(<FleetTaskActions taskId={FIXTURE_TASK_ID} status="review" allowReviewStage />);

    expect(screen.queryByRole("button", { name: "Ausliefern" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Nacharbeit" })).toBeNull();
    expect(fetchJSONMock).not.toHaveBeenCalled();
  });
});
