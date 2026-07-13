// @vitest-environment jsdom
//
// S6 End-to-End-Test für useAnswerQuestion / AnswerQuestion.
//
// Beweist AC-1 und AC-2: Das Absenden der Antwort führt die atomare
// Antwort+Unblock-Transition und danach den best-effort Dispatch aus, und der Erfolg
// wird in der UI sichtbar (doneIds). Ein Fehler im zweiten Schritt bricht
// die Kette ab und zeigt die Server-Detailmeldung per extractDetail.
//
// Fixture-Payload orientiert sich am echten /tasks/{id}/comments-Contract
// (CommentBody: body + author) und am /workers/0/action-Contract
// (action + confirm + reason) aus plugins/kanban/dashboard/plugin_api.py.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { AnswerQuestion } from "./AnswerQuestion";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// Echte Fixture-Tasks mit operator_question-Blockgründen, wie sie das Board liefert.
const FIXTURE_TASK_ID = "t_abc123";

describe("AnswerQuestion — atomare Transition (S6 AC-1+2)", () => {
  it("POST answer (Kommentar + Unblock atomar) → POST dispatch in Order", async () => {
    fetchJSONMock.mockResolvedValue({ ok: true });

    render(<AnswerQuestion taskId={FIXTURE_TASK_ID} />);

    const input = screen.getByPlaceholderText("Antwort eingeben …") as HTMLInputElement;
    const submit = screen.getByRole("button", { name: "Antworten" });

    // Leer-Antwort: Button deaktiviert
    expect((submit as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(input, { target: { value: "Ja, Credentials sind in ~/.env" } });
    expect((submit as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledTimes(2);
    });

    // 1. Atomarer Backend-Übergang: Kommentar + unblock in EINER Transaktion.
    const [aUrl, aOpts] = fetchJSONMock.mock.calls[0];
    expect(aUrl).toBe(`/api/plugins/kanban/tasks/${FIXTURE_TASK_ID}/answer`);
    expect(aOpts.method).toBe("POST");
    expect(JSON.parse(aOpts.body)).toEqual({ answer: "Ja, Credentials sind in ~/.env" });

    // 2. POST /workers/0/action — best-effort dispatch tick
    const [dUrl, dOpts] = fetchJSONMock.mock.calls[1];
    expect(dUrl).toBe("/api/plugins/kanban/workers/0/action");
    expect(dOpts.method).toBe("POST");
    const dBody = JSON.parse(dOpts.body);
    expect(dBody.action).toBe("dispatch");
    expect(dBody.confirm).toBe(true);

    // Erfolg sichtbar (AC-2: die Antwort ist anschließend als done sichtbar)
    await waitFor(() => {
      expect(screen.getByText("Gesendet — Task neu gestartet.")).toBeTruthy();
    });
  });

  it("leere Antwort wird nicht gesendet (Client-Validierung)", async () => {
    render(<AnswerQuestion taskId={FIXTURE_TASK_ID} />);

    const input = screen.getByPlaceholderText("Antwort eingeben …") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "   " } });

    // Button bleibt bei nur-Leerzeichen deaktiviert
    expect((screen.getByRole("button", { name: "Antworten" }) as HTMLButtonElement).disabled).toBe(true);
    expect(fetchJSONMock).not.toHaveBeenCalled();
  });

  it("atomarer 409 bricht vor dem Dispatch ab und zeigt Detail", async () => {
    fetchJSONMock.mockRejectedValueOnce(new Error("409: {\"detail\":\"Task ist keine aktuelle Operator-Frage\"}"));

    render(<AnswerQuestion taskId={FIXTURE_TASK_ID} />);

    const input = screen.getByPlaceholderText("Antwort eingeben …") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Test-Antwort" } });
    fireEvent.click(screen.getByRole("button", { name: "Antworten" }));

    // Nur der atomare Answer-Aufruf — Dispatch nie erreicht.
    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledTimes(1);
    });

    // Fehler wird per extractDetail angezeigt (AC-2: Fehler sichtbar)
    // extractDetail parst "409: {\"detail\":\"...\"}" → liefert die Detail-Zeile
    await waitFor(() => {
      const err = screen.queryByRole("alert");
      expect(err).toBeTruthy();
      expect(err?.textContent).toContain("Task ist keine aktuelle Operator-Frage");
    });

    // Keine Erfolgsmeldung
    expect(screen.queryByText("Gesendet — Task neu gestartet.")).toBeNull();
  });
});
