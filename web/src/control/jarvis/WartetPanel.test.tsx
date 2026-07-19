// @vitest-environment jsdom
/**
 * WartetPanel — „Wartet · dezent" an den ECHTEN offenen Agentenfragen:
 * Payload-Format exakt wie in projekte/FragenSection.test.tsx (flacher
 * AgentQuestionEvent-Stamm aus GET /api/agent-questions, kein erfundener
 * Shape). Tap führt zur bestehenden Beantwortung im klassischen Tab
 * (/control/projekte-klassisch) — keine neue Antwort-Mechanik. S2.6: der
 * Expand-Toggle öffnet die volle Fragen-Ansicht (FragenPanel) mit denselben
 * Daten; Detail-Inhalte des Drawers deckt FragenPanel.test.tsx.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { AgentQuestionEvent } from "@/lib/api";
import { _resetPollingStore } from "../hooks/pollingStore";

configure({ asyncUtilTimeout: 5000 });

const listAgentQuestionsMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      listAgentQuestions: listAgentQuestionsMock,
    },
  };
});

import { WartetPanel } from "./WartetPanel";

/** Real store shape from GET /api/agent-questions (flat columns) — derselbe
 *  Fixture-Stamm wie projekte/FragenSection.test.tsx. */
function fixtureEvent(overrides: Partial<AgentQuestionEvent> = {}): AgentQuestionEvent {
  return {
    id: 101,
    ts: "2026-07-17T10:00:00Z",
    updated_ts: null,
    source: "scrape",
    session: "hermes-main",
    window: "1",
    pane_id: "%12",
    fingerprint: "fp-101",
    kind: "claude",
    cwd: "/home/piet/.hermes/hermes-agent",
    question_text: "Soll ich den Branch mergen?",
    options: [
      { nr: 1, label: "Ja, mergen", recommended: true },
      { nr: 2, label: "Nein, warten", recommended: false },
    ],
    class: null,
    status: "open",
    answered_by: null,
    answer: null,
    latency_s: null,
    answer_verified: null,
    override: 0,
    suggestions: null,
    suggested_by: null,
    suggest_confidence: null,
    suggested_ts: null,
    suggest_latency_ms: null,
    answer_source: null,
    ...overrides,
  };
}

beforeEach(() => {
  _resetPollingStore();
  listAgentQuestionsMock.mockResolvedValue({ questions: [fixtureEvent()] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetPollingStore();
});

function renderPanel() {
  return render(
    <MemoryRouter>
      <WartetPanel />
    </MemoryRouter>,
  );
}

describe("WartetPanel (echtes GET /api/agent-questions-Format)", () => {
  it("rendert offene Fragen mit Text und Antworten-Link auf den klassischen Tab", async () => {
    listAgentQuestionsMock.mockResolvedValue({
      questions: [
        fixtureEvent({ id: 101, question_text: "Kimi work:5 hängt — Fix-Anweisung senden?" }),
        fixtureEvent({ id: 202, question_text: "t_fd072996: Tests splitten oder mappen?" }),
      ],
    });
    renderPanel();

    expect(await screen.findByText("Kimi work:5 hängt — Fix-Anweisung senden?")).toBeTruthy();
    expect(await screen.findByText("t_fd072996: Tests splitten oder mappen?")).toBeTruthy();

    const links = await screen.findAllByRole("link", { name: /Frage beantworten:/ });
    expect(links).toHaveLength(2);
    for (const link of links) {
      expect(link.getAttribute("href")).toBe("/control/projekte-klassisch");
    }
  });

  it("zeigt maximal 3 Zeilen dezent + Expand-Toggle zur vollen Fragen-Ansicht", async () => {
    listAgentQuestionsMock.mockResolvedValue({
      questions: [1, 2, 3, 4, 5].map((n) =>
        fixtureEvent({ id: n, question_text: `Frage Nummer ${n}?` }),
      ),
    });
    const { container } = renderPanel();

    await screen.findByText("Frage Nummer 1?");
    const rows = container.querySelectorAll(".jv-qrow");
    expect(rows).toHaveLength(3);

    // Toggle öffnet den Drawer mit ALLEN Fragen (gleiche Daten, kein zweiter Poll).
    const toggle = await screen.findByRole("button", { name: /\+2 WEITERE — ALLE FRAGEN/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);

    const drawer = await screen.findByRole("region", { name: "OFFENE FRAGEN" });
    expect(drawer).toBeTruthy();
    expect(await screen.findByTestId("jv-frage-4")).toBeTruthy();
    expect(await screen.findByTestId("jv-frage-5")).toBeTruthy();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(toggle.textContent).toContain("FRAGEN ZUKLAPPEN");

    // Antworten läuft weiter über den Klassik-Pfad: Drawer- und dezente
    // ANTWORTEN-Links zeigen beide auf /control/projekte-klassisch.
    const links = await screen.findAllByRole("link", { name: /Frage beantworten:/ });
    expect(links.length).toBeGreaterThanOrEqual(5);
    for (const link of links) {
      expect(link.getAttribute("href")).toBe("/control/projekte-klassisch");
    }

    // Zuklappen über denselben Toggle.
    fireEvent.click(toggle);
    expect(screen.queryByRole("region", { name: "OFFENE FRAGEN" })).toBeNull();
  });

  it("Expand bei ≤3 Fragen ohne Weitere-Zähler; × und ESC schließen den Drawer", async () => {
    listAgentQuestionsMock.mockResolvedValue({
      questions: [fixtureEvent({ id: 1, question_text: "Einzige Frage?" })],
    });
    renderPanel();

    const toggle = await screen.findByRole("button", { name: /ALLE FRAGEN/ });
    expect(toggle.textContent).not.toContain("WEITERE");
    fireEvent.click(toggle);
    await screen.findByRole("region", { name: "OFFENE FRAGEN" });

    fireEvent.click(screen.getByRole("button", { name: "Fragen-Ansicht schließen" }));
    expect(screen.queryByRole("region", { name: "OFFENE FRAGEN" })).toBeNull();

    fireEvent.click(toggle);
    await screen.findByRole("region", { name: "OFFENE FRAGEN" });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("region", { name: "OFFENE FRAGEN" })).toBeNull();
  });

  it("kein Expand-Toggle im Leer-/Fehlerzustand", async () => {
    listAgentQuestionsMock.mockResolvedValue({ questions: [] });
    renderPanel();
    expect(await screen.findByText(/Nichts wartet — Estate ruhig/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /ALLE FRAGEN/ })).toBeNull();
  });

  it("Leer-Zustand: Nichts-wartet-Text statt falscher Zeilen", async () => {
    listAgentQuestionsMock.mockResolvedValue({ questions: [] });
    renderPanel();

    expect(await screen.findByText(/Nichts wartet — Estate ruhig/)).toBeTruthy();
  });

  it("Fehler des Fragen-Polls → inline Fehlerzeile (role=alert), nie still", async () => {
    listAgentQuestionsMock.mockRejectedValue(new Error("network timeout after 20000ms"));
    renderPanel();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Fragen konnten nicht geladen werden.");
  });
});
