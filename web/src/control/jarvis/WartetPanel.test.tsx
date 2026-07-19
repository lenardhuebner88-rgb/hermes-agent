// @vitest-environment jsdom
/**
 * WartetPanel — „Wartet · dezent" an den ECHTEN offenen Agentenfragen:
 * Payload-Format exakt wie in projekte/FragenSection.test.tsx (flacher
 * AgentQuestionEvent-Stamm aus GET /api/agent-questions, kein erfundener
 * Shape). Tap führt zur bestehenden Beantwortung im klassischen Tab
 * (/control/projekte-klassisch) — keine neue Antwort-Mechanik.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, render, screen } from "@testing-library/react";
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

  it("zeigt maximal 3 Zeilen dezent + Weitere-Link bei mehr offenen Fragen", async () => {
    listAgentQuestionsMock.mockResolvedValue({
      questions: [1, 2, 3, 4, 5].map((n) =>
        fixtureEvent({ id: n, question_text: `Frage Nummer ${n}?` }),
      ),
    });
    const { container } = renderPanel();

    await screen.findByText("Frage Nummer 1?");
    const rows = container.querySelectorAll(".jv-qrow");
    expect(rows).toHaveLength(3);
    const more = await screen.findByRole("link", { name: /\+2 WEITERE/ });
    expect(more.getAttribute("href")).toBe("/control/projekte-klassisch");
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
