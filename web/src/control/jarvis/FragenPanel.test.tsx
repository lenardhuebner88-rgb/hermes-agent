// @vitest-environment jsdom
/**
 * FragenPanel — volle Fragen-Ansicht der Jarvis-Shell (S2.6): echtes
 * AgentQuestionEvent-Format aus GET /api/agent-questions (Fixture-Stamm wie
 * projekte/FragenSection.test.tsx). Inhalte: voller Fragetext, Meta
 * (kind · session:window · Standzeit), Optionen READ-ONLY mit Empfohlen-/
 * KI-Vorschlag-Markern, Suggestion-Block mit Rationale + Provenienz.
 * Antworten nur per Link in den Klassik-Tab — keine neue Mechanik.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { AgentQuestionEvent } from "@/lib/api";

import { FragenPanel } from "./FragenPanel";

configure({ asyncUtilTimeout: 5000 });

/** Real store shape from GET /api/agent-questions (flat columns) — derselbe
 *  Fixture-Stamm wie projekte/FragenSection.test.tsx / WartetPanel.test.tsx. */
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

function renderPanel(questions: AgentQuestionEvent[], onClose = vi.fn()) {
  return {
    onClose,
    ...render(
      <MemoryRouter>
        <FragenPanel questions={questions} onClose={onClose} />
      </MemoryRouter>,
    ),
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("FragenPanel (volle Fragen-Ansicht, echtes Payload-Format)", () => {
  it("rendert Fragetext, Meta-Zeile und Optionen read-only mit Empfohlen-Marker", () => {
    renderPanel([
      fixtureEvent({
        id: 7,
        kind: "kimi",
        session: "work",
        window: "5",
        question_text: "Kettenfortschritts-Benachrichtigungsbündelung: Rate begrenzen oder aufsplitten?",
      }),
    ]);

    // Lange deutsche Komposita stehen vollständig im Text (Wrap, kein Cut).
    expect(
      screen.getByText("Kettenfortschritts-Benachrichtigungsbündelung: Rate begrenzen oder aufsplitten?"),
    ).toBeTruthy();
    expect(screen.getByText("kimi")).toBeTruthy();
    expect(screen.getByText("work:5")).toBeTruthy();
    expect(screen.getByTestId("jv-frage-age-7").textContent).toMatch(/^steht seit /);
    // Optionen sichtbar, Empfohlen-Marker an Option 1 — aber KEINE Buttons
    // (read-only; die Antwort gehört der Klassik).
    expect(screen.getByText("Ja, mergen")).toBeTruthy();
    expect(screen.getByText("Nein, warten")).toBeTruthy();
    expect(screen.getByText("Empfohlen")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /mergen/ })).toBeNull();
    const optionsGroup = screen.getByRole("group", { name: "Antwortoptionen" });
    expect(optionsGroup.querySelectorAll("button")).toHaveLength(0);
  });

  it("zeigt den KI-Top-Vorschlag (suggestions[0]) mit Marker, Rationale und Provenienz", () => {
    renderPanel([
      fixtureEvent({
        id: 9,
        suggestions: [
          { nr: 1, rationale: "Tests liefen zuletzt grün; Merge ist ungefährlich." },
          { nr: 2, rationale: "Zweitrangig — nur falls Bedenken bestehen." },
        ],
        suggested_by: "gpt-5.6-sol",
        suggest_confidence: "high",
      }),
    ]);

    expect(screen.getByText("KI-Vorschlag")).toBeTruthy();
    const sugBlock = screen.getByTestId("jv-frage-suggestion-9");
    expect(sugBlock.textContent).toContain("KI-Vorschlag: Option 1");
    expect(sugBlock.textContent).toContain("Tests liefen zuletzt grün; Merge ist ungefährlich.");
    expect(sugBlock.textContent).toContain("vorgeschlagen von gpt-5.6-sol");
    expect(sugBlock.textContent).toContain("Konfidenz hoch");
    // An der vorgeschlagenen Option steht der Marker statt „Empfohlen".
    const topOption = screen.getByText("Ja, mergen").closest(".jv-fopt");
    expect(topOption?.className).toContain("jv-sug");
    expect(topOption?.textContent).toContain("KI-Vorschlag");
    expect(topOption?.textContent).not.toContain("Empfohlen");
  });

  it("ANTWORTEN-Link pro Frage zeigt auf den Klassik-Tab (bewährter Pfad)", () => {
    renderPanel([
      fixtureEvent({ id: 1, question_text: "Erste Frage?" }),
      fixtureEvent({ id: 2, question_text: "Zweite Frage?" }),
    ]);

    const links = screen.getAllByRole("link", { name: /Frage beantworten:/ });
    expect(links).toHaveLength(2);
    for (const link of links) {
      expect(link.getAttribute("href")).toBe("/control/projekte-klassisch");
      expect(link.textContent).toContain("ANTWORTEN");
    }
  });

  it("Leer-Zustand im geöffneten Drawer (Poll hat alle Fragen aufgelöst)", () => {
    renderPanel([]);
    expect(screen.getByText("Keine offenen Fragen.")).toBeTruthy();
  });

  it("×-Button und ESC rufen onClose", () => {
    const { onClose } = renderPanel([fixtureEvent({ id: 3 })]);
    fireEvent.click(screen.getByRole("button", { name: "Fragen-Ansicht schließen" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
