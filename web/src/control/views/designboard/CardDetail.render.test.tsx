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
import { statusBadge } from "./status";
import { renderToStaticMarkup } from "react-dom/server";

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

  it("renders a system task completion receipt as a timeline row", async () => {
    fetchJSONMock.mockResolvedValue({
      id: "c_1", kind: "bug", title: "Header overlaps", status: "addressed",
      target: null, linked_tasks: ["t_done"],
      entries: [{
        id: "e_receipt", author: "system", kind: "comment",
        note: "task-receipt task:t_done completed_at:2025-01-01T00:00:00Z commit:abc123",
        asset: null, html: null, pins: [], created_at: 1735689600,
      }],
      task_facets: [], derived_status: "done", kanban_ok: true,
    });
    render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>
    );

    const row = await waitFor(() => screen.getByTestId("entry-e_receipt"));
    expect(row.getAttribute("aria-label")).toBe("Verlaufszeile");
    expect(screen.getByText("2025-01-01 00:00:00Z · system · comment")).toBeTruthy();
    expect(screen.getByText(/task-receipt task:t_done/)).toBeTruthy();
    expect(screen.getByText(/commit:abc123/)).toBeTruthy();
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

  it("uploads an HTML mockup via the mockup file input", async () => {
    fetchJSONMock.mockReset();
    fetchJSONMock.mockImplementation((url: string) => {
      if (url.includes("/mockups")) return Promise.resolve({ id: "e_mock" });
      return Promise.resolve({
        id: "c_1", kind: "mockup", title: "Hero", status: "open",
        target: null, linked_tasks: [],
        entries: [], task_facets: [], derived_status: null, kanban_ok: true,
      });
    });
    render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>
    );
    const input = await waitFor(() => screen.getByTestId("mockup-upload"));
    const file = new File(["<h1>hi</h1>"], "hero.html", { type: "text/html" });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => {
      const post = fetchJSONMock.mock.calls.find(
        (c) => c[0] === "/api/design-board/cards/c_1/mockups" && c[1]?.method === "POST",
      );
      expect(post).toBeTruthy();
      expect(post![1].body instanceof FormData).toBe(true);
    });
  });

  it("shows a mapped error when a mockup upload is rejected", async () => {
    fetchJSONMock.mockReset();
    fetchJSONMock.mockImplementation((url: string) => {
      if (url.includes("/mockups"))
        return Promise.reject(new Error('413: {"detail":{"error":"file_too_large"}}'));
      return Promise.resolve({
        id: "c_1", kind: "mockup", title: "Hero", status: "open",
        target: null, linked_tasks: [],
        entries: [], task_facets: [], derived_status: null, kanban_ok: true,
      });
    });
    render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>
    );
    const input = await waitFor(() => screen.getByTestId("mockup-upload"));
    fireEvent.change(input, {
      target: { files: [new File(["x"], "big.html", { type: "text/html" })] },
    });
    await waitFor(() => expect(screen.getByText(/zu groß/)).toBeTruthy());
  });

  it("renders a mockup_html entry with a scripts-disabled live iframe", async () => {
    fetchJSONMock.mockReset();
    fetchJSONMock.mockResolvedValue({
      id: "c_1", kind: "mockup", title: "Hero", status: "open",
      target: null, linked_tasks: [],
      entries: [{
        id: "e_m", author: "claude", kind: "mockup_html", note: "hero",
        asset: "assets/hero.png", html: "assets/hero.html", pins: [], created_at: 1,
      }],
      task_facets: [], derived_status: null, kanban_ok: true,
    });
    render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("Live-HTML anzeigen")).toBeTruthy());
    fireEvent.click(screen.getByText("Live-HTML anzeigen"));
    const iframe = await waitFor(() => screen.getByTitle("mockup"));
    expect(iframe.getAttribute("sandbox")).toBe("allow-same-origin");
    expect(iframe.getAttribute("sandbox")).not.toContain("allow-scripts");
  });

  it("shows before and after screenshots side by side", async () => {
    fetchJSONMock.mockResolvedValue({
      id: "c_1", kind: "bug", title: "Header overlaps", status: "addressed",
      target: null, linked_tasks: ["t_done"],
      entries: [
        {
          id: "e_before", author: "piet", kind: "screenshot", note: "before",
          asset: "assets/before.png", html: null, pins: [], created_at: 1,
        },
        {
          id: "e_old_after", author: "system", kind: "screenshot", note: "old after",
          asset: "assets/old-after.png", html: null, pins: [], created_at: 2,
        },
        {
          id: "e_after", author: "system", kind: "screenshot", note: "after",
          asset: "assets/after.png", html: null, pins: [], created_at: 3,
        },
      ],
      task_facets: [], derived_status: "done", kanban_ok: true,
    });
    render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>
    );

    await waitFor(() => expect(screen.getByLabelText("Vorher-Nachher Screenshots")).toBeTruthy());
    expect(screen.getByText("Vorher · Operator-Screenshot")).toBeTruthy();
    expect(screen.getByText("Nachher · System-Screenshot")).toBeTruthy();
    expect(document.querySelector('img[src="/api/design-board/cards/c_1/assets/before.png"]')).toBeTruthy();
    expect(document.querySelector('img[src="/api/design-board/cards/c_1/assets/after.png"]')).toBeTruthy();
  });
});

describe("CardDetail / status.tsx — leitstand token migration guard", () => {
  // `statusBadge` (status.tsx) is a self-contained export — its markup must be
  // fully free of the legacy hc- compat vocabulary and raw text-white.
  it("statusBadge renders with token classes only, no hc- compat class", () => {
    const html = renderToStaticMarkup(statusBadge("in_progress"));
    expect(html).not.toMatch(/\bhc-[a-z-]+/);
    expect(html).not.toContain("text-white");
  });

  it("keeps the loading state fully free of legacy hc- classes / text-white", () => {
    // Never resolves -> the component stays on the `!card` early-return branch,
    // which (unlike the loaded branch) does not route through SectionHeader/
    // FleetPanel, so it can be asserted 100% clean.
    fetchJSONMock.mockReset();
    fetchJSONMock.mockImplementation(() => new Promise(() => {}));
    const { container } = render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>,
    );
    expect(container.innerHTML).not.toMatch(/\bhc-[a-z-]+/);
    expect(container.innerHTML).not.toContain("text-white");
  });

  it("keeps the fully loaded card view free of CardDetail's own legacy classes", async () => {
    // The loaded view still routes meta through SectionHeader (component/
    // leitstand/SectionHeader.tsx, out of this slice's scope), which always
    // wraps a truthy `meta` in one "hc-type-label hc-dim" span, and through
    // FleetPanel (components/leitstand/atoms.tsx, likewise out of scope),
    // whose own surface still carries the "hc-surface-card" compat class.
    // Those two are the ONE known, accepted, out-of-scope exception — this
    // guard proves CardDetail itself contributes no more than that.
    fetchJSONMock.mockReset();
    fetchJSONMock.mockResolvedValue({
      id: "c_1", kind: "bug", title: "Header overlaps", status: "in_progress",
      target: { view: "FleetView" }, linked_tasks: ["t_1"],
      entries: [
        {
          id: "e_before", author: "piet", kind: "screenshot", note: "before",
          asset: "assets/before.png", html: null, pins: [], created_at: 1,
        },
        {
          id: "e_after", author: "system", kind: "screenshot", note: "sieht gut aus",
          asset: "assets/after.png", html: null, pins: [{ id: "p1", x: 0.5, y: 0.5, note: "x" }], created_at: 2,
        },
      ],
      task_facets: [{ id: "t_1", status: "running", assignee: "coder", terminal: false }],
      derived_status: "addressed",
      kanban_ok: false,
    });
    const { container } = render(
      <MemoryRouter initialEntries={["/x/c_1"]}>
        <Routes><Route path="/x/:cardId" element={<CardDetail />} /></Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("facet-t_1")).toBeTruthy());
    fireEvent.click(screen.getByTestId("status-edit"));

    const html = container.innerHTML;
    expect(html).not.toContain("text-white");
    expect(html).not.toContain("hc-soft");
    expect((html.match(/hc-type-label/g) ?? []).length).toBe(1);
    expect((html.match(/hc-dim/g) ?? []).length).toBe(1);
  });
});
