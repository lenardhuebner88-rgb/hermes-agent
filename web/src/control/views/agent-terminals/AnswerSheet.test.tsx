// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { AgentQuestionEvent } from "@/lib/api";

// Voll-Suite-Last kann waitFor über den Default (1s) hinaus bouncen.
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

import { AnswerSheet } from "./AnswerSheet";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/** Real store shape from GET /api/agent-questions (flat columns). */
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

describe("AnswerSheet (real agent-question event format)", () => {
  it("renders options including Empfohlen badge (int nr + y/n string nr, kind:null)", () => {
    const q = fixtureEvent();
    render(
      <AnswerSheet
        questions={[q, ynFixture()]}
        onClose={() => {}}
        reload={vi.fn()}
      />,
    );

    expect(screen.getByTestId("answer-sheet")).toBeTruthy();
    expect(screen.getByText("Soll ich den Branch mergen?")).toBeTruthy();
    expect(screen.getByText("Ja, mergen")).toBeTruthy();
    expect(screen.getByText("Nein, warten")).toBeTruthy();
    expect(screen.getByText("Empfohlen")).toBeTruthy();
    // Head question only for I1 — y/n fixture is second in list, not rendered yet.
    expect(screen.queryByText("Continue? (y/n)")).toBeNull();
  });

  it("renders y/n string nr options when that event is head", () => {
    render(
      <AnswerSheet questions={[ynFixture()]} onClose={() => {}} reload={vi.fn()} />,
    );
    expect(screen.getByText("Continue? (y/n)")).toBeTruthy();
    expect(screen.getByText("Yes")).toBeTruthy();
    expect(screen.getByText("No")).toBeTruthy();
    expect(screen.getByText("Empfohlen")).toBeTruthy();
    // kind:null → fallback label "agent"
    expect(screen.getByText("agent")).toBeTruthy();
  });

  it("click on option 1 calls api.answerAgentQuestion(id, \"1\")", async () => {
    answerAgentQuestionMock.mockResolvedValueOnce({
      ok: true,
      verified: true,
      latency_s: 1.2,
    });
    const reload = vi.fn();
    render(
      <AnswerSheet
        questions={[fixtureEvent()]}
        onClose={() => {}}
        reload={reload}
      />,
    );

    fireEvent.click(screen.getByText("Ja, mergen"));
    await waitFor(() => {
      expect(answerAgentQuestionMock).toHaveBeenCalledWith(101, "1");
    });
  });

  it("key \"2\" selects option 2; unmapped \"7\" sends nothing", async () => {
    answerAgentQuestionMock.mockResolvedValue({
      ok: true,
      verified: true,
      latency_s: 0.5,
    });
    render(
      <AnswerSheet
        questions={[fixtureEvent()]}
        onClose={() => {}}
        reload={vi.fn()}
      />,
    );

    fireEvent.keyDown(window, { key: "7" });
    expect(answerAgentQuestionMock).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { key: "2" });
    await waitFor(() => {
      expect(answerAgentQuestionMock).toHaveBeenCalledWith(101, "2");
    });
  });

  it("mid-flight head swap keeps buttons disabled and does not leak the error to the next question", async () => {
    // Poll can swap the head question WHILE a POST is in flight (backend
    // claims at POST start; verify-sleep makes the request take seconds).
    let rejectPost: (err: Error) => void = () => {};
    answerAgentQuestionMock.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { rejectPost = reject; }),
    );
    const qA = fixtureEvent();
    const qB = ynFixture();
    const { rerender } = render(
      <AnswerSheet questions={[qA, qB]} onClose={() => {}} reload={vi.fn()} />,
    );

    fireEvent.click(screen.getByText("Ja, mergen"));
    expect(answerAgentQuestionMock).toHaveBeenCalledWith(101, "1");

    // Poll tick drops qA (already claimed server-side) → qB becomes head
    // while the POST is still pending.
    rerender(<AnswerSheet questions={[qB]} onClose={() => {}} reload={vi.fn()} />);

    // Buttons of the NEW head stay disabled while our POST is in flight …
    const yesButton = screen.getByText("Yes").closest("button");
    expect(yesButton?.disabled).toBe(true);

    // … and qA's late failure must not render under qB.
    rejectPost(new Error('409: {"detail":{"ok":false,"reason":"superseded"}}'));
    await waitFor(() => {
      expect(yesButton?.disabled).toBe(false);
    });
    expect(screen.queryByText("Frage hat sich geändert")).toBeNull();
  });

  it("409 superseded surfaces \"Frage hat sich geändert\"", async () => {
    answerAgentQuestionMock.mockRejectedValueOnce(
      new Error('409: {"detail":{"ok":false,"reason":"superseded"}}'),
    );
    render(
      <AnswerSheet
        questions={[fixtureEvent()]}
        onClose={() => {}}
        reload={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByText("Ja, mergen"));
    await waitFor(() => {
      expect(screen.getByText("Frage hat sich geändert")).toBeTruthy();
    });
    expect(screen.getByRole("button", { name: "Aktualisieren" })).toBeTruthy();
  });
});

  it("renders live standing age from event.ts", () => {
    const q = fixtureEvent({ ts: new Date(Date.now() - 5 * 60_000).toISOString() });
    render(
      <AnswerSheet questions={[q]} onClose={() => {}} reload={vi.fn()} />,
    );
    const age = screen.getByTestId("answer-sheet-age");
    expect(age.textContent).toMatch(/steht seit/);
  });

  it("focusId promotes that question to head", () => {
    const q1 = fixtureEvent({ id: 1, question_text: "First open?" });
    const q2 = fixtureEvent({
      id: 2,
      question_text: "Deep-link target?",
      options: [
        { nr: 1, label: "A", recommended: false },
        { nr: 2, label: "B", recommended: false },
      ],
    });
    render(
      <AnswerSheet
        questions={[q1, q2]}
        focusId={2}
        onClose={() => {}}
        reload={vi.fn()}
      />,
    );
    expect(screen.getByText("Deep-link target?")).toBeTruthy();
    expect(screen.queryByText("First open?")).toBeNull();
  });

  it("closedHint shows when no open questions (deep-link already answered)", () => {
    render(
      <AnswerSheet
        questions={[]}
        focusId={999}
        closedHint="bereits beantwortet/abgelaufen"
        onClose={() => {}}
        reload={vi.fn()}
      />,
    );
    expect(screen.getByTestId("answer-sheet-closed-hint").textContent).toMatch(
      /bereits beantwortet/,
    );
  });

  it("deep-link target closed but another question open shows warn banner", () => {
    const other = fixtureEvent({ id: 7, question_text: "Other open?" });
    render(
      <AnswerSheet
        questions={[other]}
        focusId={999}
        closedHint="bereits beantwortet/abgelaufen"
        onClose={() => {}}
        reload={vi.fn()}
      />,
    );
    // The other question renders…
    expect(screen.getByText("Other open?")).toBeTruthy();
    // …but never silently: the banner names the deep-link outcome (Codex I3).
    expect(screen.getByTestId("answer-sheet-deeplink-hint").textContent).toMatch(
      /bereits beantwortet/,
    );
  });
