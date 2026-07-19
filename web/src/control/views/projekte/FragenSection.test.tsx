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
    // Feature A Slice 2 — Null-Variante aus dem BUILD-REPORT (kein Vorschlag).
    suggestions: null,
    suggested_by: null,
    suggest_confidence: null,
    suggested_ts: null,
    suggest_latency_ms: null,
    answer_source: null,
    ...overrides,
  };
}

/** Exaktes Payload aus feat/a-suggest-be BUILD-REPORT („Exact API shape"):
 *  Top-Rang = erstes Array-Element (nr 2), suggested_by/confidence gesetzt.
 *  Die Top-vorgeschlagene Option ist hier bewusst NICHT die `recommended`-
 *  Option, damit Marker/Highlight und via_suggestion-Logik getrennt greifen. */
function suggestedFixture(): AgentQuestionEvent {
  return fixtureEvent({
    id: 303,
    question_text: "Wie soll das Deploy laufen?",
    options: [
      { nr: 1, label: "Sofort deployen", recommended: true },
      { nr: 2, label: "Canary zuerst", recommended: false },
    ],
    suggestions: [
      { nr: 2, rationale: "Safer zero-downtime cutover." },
      { nr: 1, rationale: "Simpler rollback path." },
    ],
    suggested_by: "gpt-5.6-terra",
    suggest_confidence: "high",
    suggested_ts: "2026-07-18T20:00:00.000000+00:00",
    suggest_latency_ms: 1234.5,
    answer_source: null,
  });
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
      // Drittes Arg = via_suggestion (Slice 2): bei Fragen ohne Vorschlag
      // undefined → das Feld wird im POST-Body weggelassen (Wire-Test in
      // lib/api.test.ts).
      expect(answerAgentQuestionMock).toHaveBeenCalledWith(101, "1", undefined);
    });

    answerAgentQuestionMock.mockClear();
    fireEvent.click(screen.getByText("Yes"));
    await waitFor(() => {
      expect(answerAgentQuestionMock).toHaveBeenCalledWith(202, "y", undefined);
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
    expect(answerAgentQuestionMock).toHaveBeenCalledWith(101, "1", undefined);

    const yesButton = screen.getByText("Yes").closest("button");
    expect(yesButton?.disabled).toBe(true);
    expect(screen.getByText("Sende …")).toBeTruthy();

    resolvePost({ ok: true, verified: true, latency_s: 0.5 });
    await waitFor(() => {
      expect(yesButton?.disabled).toBe(false);
    });
  });
});

describe("FragenSection Slice 2 — KI-Antwort-Vorschläge (Report-Payload)", () => {
  it("rendert Top-Vorschlag bronze mit Rationale + Provenienz (exaktes Report-Payload)", () => {
    renderSection({ questions: [suggestedFixture()] });

    // Suggestion-Block unter den Optionen: Titel nennt die Top-Option (nr 2
    // = erstes Array-Element, NICHT die recommended Option nr 1).
    const block = screen.getByTestId("frage-suggestion-303");
    expect(block.textContent).toContain("KI-Vorschlag: Option 2");
    expect(block.textContent).toContain("Safer zero-downtime cutover.");
    expect(block.textContent).toContain("vorgeschlagen von gpt-5.6-terra");
    expect(block.textContent).toContain("Konfidenz hoch");
    // Nur der Top-Rang wird gezeigt — die zweite Suggestion bleibt unsichtbar.
    expect(screen.queryByText("Simpler rollback path.")).toBeNull();

    // Top-vorgeschlagene Option trägt den bronze Primär-Kanal + KI-Marker.
    const top = screen.getByText("Canary zuerst").closest("button");
    expect(top?.className).toContain("border-bronze/50");
    expect(top?.className).toContain("bg-bronze/10");
    expect(top?.textContent).toContain("KI-Vorschlag");

    // Die recommended-aber-nicht-vorgeschlagene Option behält „Empfohlen".
    const recommended = screen.getByText("Sofort deployen").closest("button");
    expect(recommended?.textContent).toContain("Empfohlen");
    expect(recommended?.textContent).not.toContain("KI-Vorschlag");
  });

  it("null-Suggestions: heutiges Verhalten, kein Block, kein Marker (Degradation)", () => {
    renderSection({ questions: [fixtureEvent()] });

    expect(screen.queryByTestId("frage-suggestion-101")).toBeNull();
    expect(screen.queryByText("KI-Vorschlag")).toBeNull();
    // Empfohlen-Marker der Slice-1-Welt bleibt unverändert.
    expect(screen.getByText("Empfohlen")).toBeTruthy();
  });

  it("Klick auf die Top-Option sendet via_suggestion mit der Top-nr", async () => {
    answerAgentQuestionMock.mockResolvedValue({ ok: true, verified: true, latency_s: 1 });
    renderSection({ questions: [suggestedFixture()] });

    fireEvent.click(screen.getByText("Canary zuerst"));
    await waitFor(() => {
      expect(answerAgentQuestionMock).toHaveBeenCalledWith(303, "2", 2);
    });
  });

  it("Klick auf eine ANDERE Option sendet KEIN via_suggestion", async () => {
    // Option 1 steht zwar im suggestions-Array (Rang 2), ist aber NICHT der
    // Top-Rang — der Server stempelt dann selbst suggested_edited.
    answerAgentQuestionMock.mockResolvedValue({ ok: true, verified: true, latency_s: 1 });
    renderSection({ questions: [suggestedFixture()] });

    fireEvent.click(screen.getByText("Sofort deployen"));
    await waitFor(() => {
      expect(answerAgentQuestionMock).toHaveBeenCalledWith(303, "1", undefined);
    });
    expect(answerAgentQuestionMock.mock.calls[0][2]).toBeUndefined();
  });

  it("invalid-suggestion-400 zeigt Fehler statt stillem Schlucken", async () => {
    const updateData = vi.fn();
    const props = renderSection({ questions: [suggestedFixture()], updateData });
    answerAgentQuestionMock.mockRejectedValueOnce(
      new Error('400: {"detail":{"ok":false,"reason":"invalid-suggestion"}}'),
    );

    fireEvent.click(screen.getByText("Canary zuerst"));
    await waitFor(() => {
      expect(
        screen.getByText("KI-Vorschlag nicht mehr gültig — bitte Option erneut wählen."),
      ).toBeTruthy();
    });
    // Die Frage bleibt stehen (kein optimistisches Entfernen, kein Reload) —
    // der Operator kann ohne Vorschlag erneut wählen.
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(updateData).not.toHaveBeenCalled();
    expect(props.reload).not.toHaveBeenCalled();
  });
});
