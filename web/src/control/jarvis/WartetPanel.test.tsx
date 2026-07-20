// @vitest-environment jsdom
/**
 * WartetPanel — „Wartet · dezent" an der echten Entscheidungs-Inbox
 * (S2.4, GET /api/pa/inbox): dezente 3-Zeilen-Liste mit typisierten Items
 * (pa_action → PRÜFEN öffnet den Drawer, question → Klassik-Link,
 * held/freigabe → Board-Link), Teilquellen-Ausfälle (errors[]) dezent,
 * Fetch-Fehler inline (nie still). Der Expand öffnet die Inbox-Ansicht mit
 * denselben Daten (kein zweiter Poll); Karten-Inhalte und der Approval-Flow
 * sind in InboxPanel.test.tsx belegt. ?inbox=open öffnet den Drawer initial
 * (Deep-Link/Screenshot-Naht).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { PaInboxItem, PaInboxResponse, PaInboxTaskItem } from "@/lib/api";
import { _resetPollingStore } from "../hooks/pollingStore";

configure({ asyncUtilTimeout: 5000 });

const getPaInboxMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      getPaInbox: getPaInboxMock,
    },
  };
});

import { WartetPanel } from "./WartetPanel";

function paActionItem(id: number, title: string): PaInboxItem {
  return {
    type: "pa_action",
    id: `q${id}`,
    question_id: id,
    title,
    kind: "pa_action",
    category: "tmux.send_keys",
    action_payload: {
      version: 1,
      category: "tmux.send_keys",
      payload: { session: "work", window: "kimi", keys: "weiter" },
      reason: null,
    },
    options: [
      { nr: 1, label: "Ausführen", recommended: false },
      { nr: 2, label: "Ablehnen", recommended: false },
    ],
    block_radius: 1,
    ts: 1753000000,
  };
}

function questionItem(id: number, title: string): PaInboxItem {
  return {
    type: "question",
    id: `q${id}`,
    question_id: id,
    title,
    kind: "claude",
    options: [],
    block_radius: 1,
    ts: 1752990000,
  };
}

function heldItem(id: string, title: string): PaInboxTaskItem {
  return {
    type: "held_task",
    id,
    card_id: id,
    title,
    status: "blocked",
    freigabe: null,
    block_radius: 3,
    ts: 1752980000,
  };
}

function inboxResponse(items: PaInboxItem[], errors: PaInboxResponse["errors"] = []): PaInboxResponse {
  return { generated_at: 1753000001, items, errors };
}

beforeEach(() => {
  _resetPollingStore();
  getPaInboxMock.mockResolvedValue(inboxResponse([questionItem(101, "Soll ich mergen?")]));
  window.history.replaceState({}, "", "/control/projekte");
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetPollingStore();
  window.history.replaceState({}, "", "/");
});

function renderPanel() {
  return render(
    <MemoryRouter>
      <WartetPanel />
    </MemoryRouter>,
  );
}

describe("WartetPanel (echtes /api/pa/inbox-Format)", () => {
  it("rendert typisierte dezente Zeilen: question → Klassik-Link, held → Board-Link", async () => {
    getPaInboxMock.mockResolvedValue(
      inboxResponse([
        questionItem(101, "Kimi work:5 hängt — Fix senden?"),
        heldItem("t_fd072996", "Release-Kette hält auf Operator"),
      ]),
    );
    renderPanel();

    expect(await screen.findByText("Kimi work:5 hängt — Fix senden?")).toBeTruthy();
    expect(await screen.findByText("Release-Kette hält auf Operator")).toBeTruthy();

    const answerLink = await screen.findByRole("link", { name: /Frage beantworten: Kimi work:5/ });
    expect(answerLink.getAttribute("href")).toBe("/control/projekte-klassisch");
    const boardLink = await screen.findByRole("link", { name: /Zum Board: Release-Kette/ });
    expect(boardLink.getAttribute("href")).toBe("/control/fleet?task=t_fd072996");
  });

  it("pa_action-Zeile: PRÜFEN öffnet den Drawer mit der Approval-Card", async () => {
    getPaInboxMock.mockResolvedValue(
      inboxResponse([paActionItem(77, "PA-Aktion ausführen: tmux.send_keys?")]),
    );
    renderPanel();

    const review = await screen.findByRole("button", {
      name: /Aktion prüfen: PA-Aktion ausführen/,
    });
    fireEvent.click(review);

    expect(await screen.findByRole("dialog", { name: "WARTET AUF DICH" })).toBeTruthy();
    expect(await screen.findByTestId("jv-appr-q77")).toBeTruthy();
  });

  it("zeigt maximal 3 Zeilen dezent + Expand-Toggle zur Inbox-Ansicht", async () => {
    getPaInboxMock.mockResolvedValue(
      inboxResponse([1, 2, 3, 4, 5].map((n) => questionItem(n, `Entscheidung Nummer ${n}?`))),
    );
    const { container } = renderPanel();

    await screen.findByText("Entscheidung Nummer 1?");
    expect(container.querySelectorAll(".jv-qrow")).toHaveLength(3);

    const toggle = await screen.findByRole("button", { name: /\+2 WEITERE — INBOX/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);

    expect(await screen.findByRole("dialog", { name: "WARTET AUF DICH" })).toBeTruthy();
    expect(await screen.findByTestId("jv-inbox-q-q4")).toBeTruthy();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(toggle.textContent).toContain("INBOX ZUKLAPPEN");

    fireEvent.click(toggle);
    expect(screen.queryByRole("dialog", { name: "WARTET AUF DICH" })).toBeNull();
  });

  it("Teilquellen-Ausfall (errors[]) → dezente Zeile, Items bleiben", async () => {
    getPaInboxMock.mockResolvedValue(
      inboxResponse([questionItem(1, "Frage bleibt sichtbar?")], [
        { source: "kanban", "error": "disk i/o error" },
      ]),
    );
    renderPanel();

    expect(await screen.findByText("Frage bleibt sichtbar?")).toBeTruthy();
    const sourceError = await screen.findByText(/Quelle „kanban" derzeit nicht erreichbar/);
    expect(sourceError.className).toContain("jv-srcerr");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("Leer-Zustand: Nichts-wartet-Text statt falscher Zeilen, kein Expand", async () => {
    getPaInboxMock.mockResolvedValue(inboxResponse([]));
    renderPanel();

    expect(await screen.findByText(/Nichts wartet — Estate ruhig/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /INBOX ÖFFNEN/ })).toBeNull();
  });

  it("Fehler des Inbox-Polls → inline Fehlerzeile (role=alert), nie still", async () => {
    getPaInboxMock.mockRejectedValue(new Error("network timeout after 20000ms"));
    renderPanel();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Entscheidungen konnten nicht geladen werden.");
  });

  it("?inbox=open öffnet den Drawer initial (Deep-Link-Naht)", async () => {
    window.history.pushState({}, "", "/control/projekte?inbox=open");
    getPaInboxMock.mockResolvedValue(inboxResponse([paActionItem(77, "PA-Aktion?")]));
    renderPanel();

    expect(await screen.findByRole("dialog", { name: "WARTET AUF DICH" })).toBeTruthy();
  });
});

describe("S7.6: dezente Zeilen entscheidungs-first", () => {
  it("Destillation: Task-ID und Status-Suffix fliegen raus, Roh-Titel bleibt im title-Attribut", async () => {
    getPaInboxMock.mockResolvedValue(
      inboxResponse([heldItem("t_fd072996", "Task t_fd072996: Release-Kette — blocked")]),
    );
    renderPanel();

    const row = await screen.findByTestId("jv-wartet-row-t_fd072996");
    expect(screen.getByText("Release-Kette")).toBeTruthy();
    expect(row.querySelector(".jv-tx")?.getAttribute("title")).toBe(
      "Task t_fd072996: Release-Kette — blocked",
    );
    // Badges: Alter + Blockradius (heldItem hat block_radius 3).
    expect(row.textContent).toMatch(/seit \d+d/);
    expect(row.textContent).toContain("blockiert 3");
    // Board-Link und Ziel unverändert.
    expect(
      screen.getByRole("link", { name: /Zum Board: Task t_fd072996/ }).getAttribute("href"),
    ).toBe("/control/fleet?task=t_fd072996");
  });

  it("Server-summary gewinnt vor der Destillation", async () => {
    getPaInboxMock.mockResolvedValue(
      inboxResponse([
        {
          ...heldItem("t_sum9", "Task t_sum9abcdef: Kette — blocked"),
          summary: "Kette jetzt freigeben",
        },
      ]),
    );
    renderPanel();

    expect(await screen.findByText("Kette jetzt freigeben")).toBeTruthy();
    expect(screen.queryByText("Kette")).toBeNull();
  });

  it("🔑-Badge bei freigabe=operator, sonst keines", async () => {
    getPaInboxMock.mockResolvedValue(
      inboxResponse([
        {
          ...heldItem("t_key1", "Landung Sprint 3"),
          type: "freigabe_gate",
          freigabe: "operator",
        },
        heldItem("t_key2", "Kette hält auf Review"),
      ]),
    );
    renderPanel();

    const gateRow = await screen.findByTestId("jv-wartet-row-t_key1");
    expect(gateRow.textContent).toContain("🔑");
    const heldRow = await screen.findByTestId("jv-wartet-row-t_key2");
    expect(heldRow.textContent).not.toContain("🔑");
  });
});
