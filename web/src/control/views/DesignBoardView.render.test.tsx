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
        derived_status: "in_progress",
        linked_tasks: ["t_1"], updated_at: 1 },
    ]);
    render(<MemoryRouter><DesignBoardView /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("Header overlaps")).toBeTruthy());
    expect(screen.getByText("→ FleetView")).toBeTruthy();
    expect(screen.getByText("in arbeit")).toBeTruthy();
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

  it("shows a warning banner when a card reports kanban_ok=false", async () => {
    fetchJSONMock.mockResolvedValueOnce([
      { id: "c_1", kind: "bug", title: "Header overlaps",
        target: null, status: "open", derived_status: null,
        linked_tasks: ["t_1"], updated_at: 1, kanban_ok: false },
    ]);
    render(<MemoryRouter><DesignBoardView /></MemoryRouter>);
    await waitFor(() =>
      expect(screen.getByText(/Kanban-Status nicht verfügbar/)).toBeTruthy(),
    );
  });

  it("clamps long target.view prose to a one-line preview", async () => {
    const longTarget =
      "Ich möchte den Kettentappen neu designen. Ich möchte tatsächlich sehen, von Anfang bis Ende, welche Steps noch sind. Zurzeit sehe ich nicht: - wann der Verifier kommt - wie der Verifier steht - ob diese Kette einen Reviewer hat oder nicht - ob da Kritik mit drin ist - was mit dem Integrator ist Ich möchte saubere States sehen, die auch tatsächlich wirklich erkennbar sind, ob diese Kette ein Problem hat, ob sie jetzt läuft oder nicht. , ein Mockup für mich zu erzeugen.";
    fetchJSONMock.mockResolvedValueOnce([
      { id: "c_a1b2c3d4", kind: "bug", title: "Redesign Ketten Tab",
        target: { view: longTarget }, status: "open", derived_status: null,
        linked_tasks: [], updated_at: 1 },
    ]);
    render(<MemoryRouter><DesignBoardView /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("Redesign Ketten Tab")).toBeTruthy());

    const preview = screen.getByText(/^→ .*…$/);
    const previewText = preview.textContent ?? "";
    expect(previewText.length).toBeLessThanOrEqual(80);
    expect(previewText).toMatch(/^→ .*…$/);
    expect(screen.queryByText(longTarget)).toBeNull();
    expect(preview.getAttribute("title")).toBe(longTarget);
  });

  it("leaves short target.view pointers unchanged", async () => {
    fetchJSONMock.mockResolvedValueOnce([
      { id: "c_1", kind: "bug", title: "Header overlaps",
        target: { view: "FleetView" }, status: "open", derived_status: null,
        linked_tasks: [], updated_at: 1 },
    ]);
    render(<MemoryRouter><DesignBoardView /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("→ FleetView")).toBeTruthy());
    expect(screen.queryByText("→ FleetView…")).toBeNull();
  });
});
