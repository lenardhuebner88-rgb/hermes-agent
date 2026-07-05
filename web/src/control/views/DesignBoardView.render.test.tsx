// @vitest-environment jsdom
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
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

  it("creates a card via the new-card form and posts to the API", async () => {
    fetchJSONMock.mockResolvedValueOnce([]); // initial list load
    fetchJSONMock.mockResolvedValueOnce({ id: "c_new" }); // POST /cards
    render(<MemoryRouter><DesignBoardView /></MemoryRouter>);
    fireEvent.click(screen.getByText("＋ Neue Karte"));
    fireEvent.change(screen.getByPlaceholderText(/Titel/), {
      target: { value: "Neuer Bug" },
    });
    fireEvent.click(screen.getByText("Karte anlegen & Screenshot hinzufügen"));
    await waitFor(() => {
      const postCall = fetchJSONMock.mock.calls.find(
        (c) => c[0] === "/api/design-board/cards" && c[1]?.method === "POST",
      );
      expect(postCall).toBeTruthy();
      expect(JSON.parse(postCall![1].body)).toMatchObject({ kind: "bug", title: "Neuer Bug" });
    });
  });
});
