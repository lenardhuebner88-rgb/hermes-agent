// @vitest-environment jsdom
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
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
      kanban_ok: true,
    });
    render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByTestId("pin-p1")).toBeTruthy());
    expect(screen.getByTestId("facet-t_1")).toBeTruthy();
    expect(screen.getByText("→ FleetView")).toBeTruthy();
    expect(screen.getByText("in arbeit")).toBeTruthy();
  });

  it("submits a text-only comment entry", async () => {
    fetchJSONMock.mockResolvedValue({
      id: "c_1", kind: "bug", title: "Header overlaps", status: "open",
      target: null, linked_tasks: [],
      entries: [],
      task_facets: [],
      derived_status: null,
      kanban_ok: true,
    });
    render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByPlaceholderText(/Notiz zur Karte/)).toBeTruthy());
    fireEvent.change(screen.getByPlaceholderText(/Notiz zur Karte/), {
      target: { value: "Neuer Kommentar" },
    });
    fireEvent.click(screen.getByText("Kommentar speichern"));
    await waitFor(() => {
      const postCall = fetchJSONMock.mock.calls.find(
        (c) => c[0] === "/api/design-board/cards/c_1/entries" && c[1]?.method === "POST",
      );
      expect(postCall).toBeTruthy();
      expect(JSON.parse(postCall![1].body)).toMatchObject({
        author: "piet", kind: "comment", note: "Neuer Kommentar", pins: [],
      });
    });
  });

  it("allows overriding the stored card status", async () => {
    fetchJSONMock.mockResolvedValue({
      id: "c_1", kind: "bug", title: "Header overlaps", status: "open",
      target: null, linked_tasks: [],
      entries: [],
      task_facets: [],
      derived_status: null,
      kanban_ok: true,
    });
    render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByTestId("status-edit")).toBeTruthy());
    fireEvent.click(screen.getByTestId("status-edit"));
    fireEvent.change(screen.getByDisplayValue("offen"), { target: { value: "addressed" } });
    fireEvent.click(screen.getByText("Speichern"));
    await waitFor(() => {
      const patchCall = fetchJSONMock.mock.calls.find(
        (c) => c[0] === "/api/design-board/cards/c_1" && c[1]?.method === "PATCH",
      );
      expect(patchCall).toBeTruthy();
      expect(JSON.parse(patchCall![1].body)).toMatchObject({ status: "addressed" });
    });
  });

  it("captures a per-pin note and submits it with the entry", async () => {
    fetchJSONMock.mockReset();
    fetchJSONMock.mockImplementation((url: string) => {
      if (url.includes("/images")) return Promise.resolve({ name: "shot.png" });
      return Promise.resolve({
        id: "c_1", kind: "bug", title: "Header overlaps", status: "open",
        target: null, linked_tasks: [],
        entries: [], task_facets: [], derived_status: null, kanban_ok: true,
      });
    });
    render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByPlaceholderText(/Notiz zur Karte/)).toBeTruthy());
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["x"], "shot.png", { type: "image/png" });
    fireEvent.change(fileInput, { target: { files: [file] } });
    const surface = await waitFor(() => screen.getByTestId("pin-surface"));
    surface.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 200, height: 100 }) as DOMRect;
    fireEvent.click(surface, { clientX: 100, clientY: 50 });
    const noteInput = await waitFor(() => screen.getByTestId("pin-note-p1"));
    fireEvent.change(noteInput, { target: { value: "sieht komisch aus" } });
    expect((noteInput as HTMLInputElement).value).toBe("sieht komisch aus");

    fireEvent.click(screen.getByText("Eintrag speichern"));
    await waitFor(() => {
      const postCall = fetchJSONMock.mock.calls.findLast(
        (c) => c[0] === "/api/design-board/cards/c_1/entries" && c[1]?.method === "POST",
      );
      expect(postCall).toBeTruthy();
      const body = JSON.parse(postCall![1].body);
      expect(body.pins[0]).toMatchObject({ note: "sieht komisch aus" });
    });
  });

  it("shows a warning banner when kanban_ok is false", async () => {
    fetchJSONMock.mockResolvedValue({
      id: "c_1", kind: "bug", title: "Header overlaps", status: "open",
      target: null, linked_tasks: ["t_1"],
      entries: [], task_facets: [], derived_status: null, kanban_ok: false,
    });
    render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>
    );
    await waitFor(() =>
      expect(screen.getByText(/Kanban-Status nicht verfügbar/)).toBeTruthy(),
    );
  });
});
