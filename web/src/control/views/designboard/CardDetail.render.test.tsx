// @vitest-environment jsdom
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { afterEach, describe, it, expect, vi } from "vitest";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { CardDetail } from "./CardDetail";

afterEach(cleanup);

describe("CardDetail (jsdom)", () => {
  it("renders an entry pin and a linked-task facet", async () => {
    fetchJSONMock.mockResolvedValue({
      id: "c_1", kind: "bug", title: "Header overlaps", status: "open",
      target: { view: "FleetView" }, linked_tasks: ["t_1"],
      entries: [{
        id: "e_1", author: "piet", kind: "screenshot", note: "gap",
        asset: "assets/e1.png", html: null,
        pins: [{ id: "p1", x: 0.5, y: 0.5, note: "here" }], created_at: 1,
      }],
      task_facets: [{ id: "t_1", status: "running", assignee: "coder", terminal: false }],
      derived_status: "in_progress",
    });
    render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByTestId("pin-p1")).toBeTruthy());
    expect(screen.getByTestId("facet-t_1")).toBeTruthy();
    expect(screen.getByText("→ FleetView")).toBeTruthy();
  });
});
