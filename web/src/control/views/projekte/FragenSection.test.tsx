// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { AgentQuestionEvent } from "@/lib/api";

// Voll-Suite-Last kann waitFor über den Default (1s) hinaus bouncen
// (gleiche Vorsicht wie agent-terminals/AnswerSheet.test.tsx).
configure({ asyncUtilTimeout: 5000 });

const answerAgentQuestionMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      answerAgentQuestion: answerAgentQuestionMock,
    },
  };
});

import { FragenSection } from "./FragenSection";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/** Real store shape from GET /api/agent-questions (flat columns) — derselbe
 *  Fixture-Stamm wie agent-terminals/AnswerSheet.test.tsx, kein erfundener
 *  Shape. */
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
    ...overrides,
  };
}

function ynFixture(): AgentQuestionEvent {
  return fixtureEvent({
    id: 202,
    kind: null,
    question_text: "Continue? (y/n)",
    options: [
      { nr: "y", label: "Yes", recommended: true },
      { nr: "n", label: "No", recommended: false },
    ],
  });
}

function renderSection(overrides: Partial<Parameters<typeof FragenSection>[0]> = {}) {
  const props = {
    questions: [fixtureEvent()] as ReadonlyArray<AgentQuestionEvent>,
    reload: vi.fn(),
    updateData: vi.fn(),
    ...overrides,
  };
  render(<FragenSection {...props} />);
  return props;
}

describe("FragenSection (echtes GET /api/agent-questions-Format)", () => {
  it("rendert N offene Fragen aus dem realistischen Payload (int-nr + y/n)", () => {
    renderSection({ questions: [fixtureEvent(), ynFixture()] });

    expect(screen.getByRole("region", { name: "Offene Fragen" })).toBeTruthy();
    expect(screen.getByText("2 offene Fragen")).toBeTruthy();
    // Beide Fragen mit ALLEN Options-Labels sichtbar (nicht nur die Head-Frage).
    expect(screen.getByText("Soll ich den Branch mergen?")).toBeTruthy();
    expect(screen.getByText("Ja, mergen")).toBeTruthy();
    expect(screen.getByText("Nein, warten")).toBeTruthy();
    expect(screen.getByText("Continue? (y/n)")).toBeTruthy();
    expect(screen.getByText("Yes")).toBeTruthy();
    expect(screen.getByText("No")).toBeTruthy();
    // kind:null → Fallback-Label "agent" (AnswerSheet-Idiom).
    expect(screen.getByText("agent")).toBeTruthy();
  });

  it("Klick auf eine Option ruft den answer-Endpunkt mit (event_id, answer)", async () => {
    // Der Endpunkt-Kontrakt: api.answerAgentQuestion POSTet
    // /api/agent-questions/{event_id}/answer mit {answer, answered_by} —
    // die Komponente übergibt die Options-nr als String (AnswerSheet-Idiom).
    answerAgentQuestionMock.mockResolvedValue({ ok: true, verified: true, latency_s: 1 });
    renderSection({ questions: [fixtureEvent(), ynFixture()] });

    fireEvent.click(screen.getByText("Ja, mergen"));
    await waitFor(() => {
      expect(answerAgentQuestionMock).toHaveBeenCalledWith(101, "1");
    });

    answerAgentQuestionMock.mockClear();
    fireEvent.click(screen.getByText("Yes"));
    await waitFor(() => {
      expect(answerAgentQuestionMock).toHaveBeenCalledWith(202, "y");
    });
  });

  it("hebt die recommended-Option visuell in bronze hervor (Primär-Kanal)", () => {
    renderSection();

    const recommended = screen.getByText("Ja, mergen").closest("button");
    expect(recommended?.className).toContain("border-bronze/50");
    expect(recommended?.className).toContain("bg-bronze/10");
    expect(screen.getByText("Empfohlen")).toBeTruthy();

    const normal = screen.getByText("Nein, warten").closest("button");
    expect(normal?.className).not.toContain("bg-bronze/10");
    expect(normal?.className).toContain("border-line");
  });

  it("leerer Zustand rendert ruhig, nicht als Fehler", () => {
    renderSection({ questions: [] });

    expect(screen.getByText("Keine offenen Fragen.")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    // Kein Count-Chip bei 0 (ruhige Leere statt Null-Statistik).
    expect(screen.queryByText("0 offene Fragen")).toBeNull();
  });

  it("409/superseded zeigt „Frage hat sich geändert“ statt stillem Schlucken", async () => {
    const props = renderSection();
    answerAgentQuestionMock.mockRejectedValueOnce(
      new Error('409: {"detail":{"ok":false,"reason":"superseded"}}'),
    );

    fireEvent.click(screen.getByText("Ja, mergen"));
    await waitFor(() => {
      expect(screen.getByText("Frage hat sich geändert")).toBeTruthy();
    });

    fireEvent.click(screen.getByRole("button", { name: "Aktualisieren" }));
    expect(props.reload).toHaveBeenCalled();
  });

  it("entfernt eine beantwortete Frage optimistisch aus dem Poll-Snapshot", async () => {
    const updateData = vi.fn();
    const props = renderSection({ updateData });
    answerAgentQuestionMock.mockResolvedValueOnce({ ok: true, verified: true, latency_s: 0.5 });

    fireEvent.click(screen.getByText("Ja, mergen"));
    await waitFor(() => {
      expect(updateData).toHaveBeenCalled();
    });
    // Der Updater filtert genau die beantwortete event_id aus dem Snapshot.
    const updater = updateData.mock.calls[0][0] as (
      prev: { questions: AgentQuestionEvent[] } | null,
    ) => { questions: AgentQuestionEvent[] } | null;
    const next = updater({ questions: [fixtureEvent(), ynFixture()] });
    expect(next?.questions.map((q) => q.id)).toEqual([202]);
    expect(updater(null)).toBeNull();
    expect(props.reload).toHaveBeenCalled();
  });

  it("Fetch-Fehler rendert inline-Warnung, Sektion bleibt montiert", () => {
    renderSection({ questions: [], error: true });

    expect(screen.getByRole("region", { name: "Offene Fragen" })).toBeTruthy();
    expect(screen.getByText("Offene Fragen konnten nicht geladen werden.")).toBeTruthy();
    // Leer ≠ Fehler: neben der Warnung keine falsche "keine Fragen"-Behauptung.
    expect(screen.queryByText("Keine offenen Fragen.")).toBeNull();
  });

  it("hält den 44px-Touch-Kontrakt mobil, Desktop-Dichte ab tab", () => {
    // Regression analog ProjectCard.test.tsx „≥44px touch target": jsdom kann
    // keine px rechnen — der Klassen-Pin sichert den Kontrakt (min-h-11 = 44px).
    renderSection();

    for (const label of ["Ja, mergen", "Nein, warten"]) {
      const button = screen.getByText(label).closest("button");
      expect(button?.className).toContain("min-h-11");
      expect(button?.className).toContain("tab:min-h-9");
    }
  });

  it("sperrt alle Options-Buttons während eine Antwort läuft", async () => {
    let resolvePost: (value: { ok: boolean; verified: boolean; latency_s: number }) => void = () => {};
    answerAgentQuestionMock.mockImplementationOnce(
      () => new Promise((resolve) => { resolvePost = resolve; }),
    );
    renderSection({ questions: [fixtureEvent(), ynFixture()] });

    fireEvent.click(screen.getByText("Ja, mergen"));
    expect(answerAgentQuestionMock).toHaveBeenCalledWith(101, "1");

    const yesButton = screen.getByText("Yes").closest("button");
    expect(yesButton?.disabled).toBe(true);
    expect(screen.getByText("Sende …")).toBeTruthy();

    resolvePost({ ok: true, verified: true, latency_s: 0.5 });
    await waitFor(() => {
      expect(yesButton?.disabled).toBe(false);
    });
  });
});
