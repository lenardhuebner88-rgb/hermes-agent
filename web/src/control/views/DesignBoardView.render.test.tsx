// @vitest-environment jsdom
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, it, expect, vi } from "vitest";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { DesignBoardView } from "./DesignBoardView";

afterEach(cleanup);

describe("DesignBoardView (jsdom)", () => {
  it("renders cards from the API", async () => {
    fetchJSONMock.mockResolvedValueOnce([
      { id: "c_1", kind: "bug", title: "Header overlaps",
        target: { view: "FleetView" }, status: "open",
        linked_tasks: ["t_1"], updated_at: 1 },
    ]);
    render(<MemoryRouter><DesignBoardView /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("Header overlaps")).toBeTruthy());
    expect(screen.getByText("→ FleetView")).toBeTruthy();
  });
});
