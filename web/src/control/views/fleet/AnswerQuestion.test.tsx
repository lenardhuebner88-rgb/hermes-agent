// @vitest-environment jsdom
//
// S6 End-to-End-Test für useAnswerQuestion / AnswerQuestion.
//
// Beweist AC-1 und AC-2: Das Absenden der Antwort führt die drei-Schritt-
// Komposition aus (POST comment → PATCH ready → POST dispatch) in der
// richtigen Reihenfolge mit der richtigen Payload aus, und der Erfolg
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

describe("AnswerQuestion — drei-Schritt-Komposition (S6 AC-1+2)", () => {
  it("POST comment (author: operator) → PATCH ready → POST dispatch in Order", async () => {
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
      expect(fetchJSONMock).toHaveBeenCalledTimes(3);
    });

    // 1. POST /tasks/{id}/comments
    const [cUrl, cOpts] = fetchJSONMock.mock.calls[0];
    expect(cUrl).toBe(`/api/plugins/kanban/tasks/${FIXTURE_TASK_ID}/comments`);
    expect(cOpts.method).toBe("POST");
    expect(JSON.parse(cOpts.body)).toEqual({
      body: "Ja, Credentials sind in ~/.env",
      author: "operator",
    });

    // 2. PATCH /tasks/{id} — status ready
    const [pUrl, pOpts] = fetchJSONMock.mock.calls[1];
    expect(pUrl).toBe(`/api/plugins/kanban/tasks/${FIXTURE_TASK_ID}`);
    expect(pOpts.method).toBe("PATCH");
    expect(JSON.parse(pOpts.body)).toEqual({ status: "ready" });

    // 3. POST /workers/0/action — dispatch tick
    const [dUrl, dOpts] = fetchJSONMock.mock.calls[2];
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

  it("Fehler im zweiten Schritt (PATCH) bricht Kette ab und zeigt Detail", async () => {
    // Comment-POST ok, PATCH schlägt fehl mit HTTP 409
    fetchJSONMock
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error("409: {\"detail\":\"Task nicht blockiert\"}"));

    render(<AnswerQuestion taskId={FIXTURE_TASK_ID} />);

    const input = screen.getByPlaceholderText("Antwort eingeben …") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Test-Antwort" } });
    fireEvent.click(screen.getByRole("button", { name: "Antworten" }));

    // Nur zwei Aufrufe (comment + fehlschlagender PATCH) — dispatch nie erreicht
    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledTimes(2);
    });

    // Fehler wird per extractDetail angezeigt (AC-2: Fehler sichtbar)
    // extractDetail parst "409: {\"detail\":\"...\"}" → liefert die Detail-Zeile
    await waitFor(() => {
      const err = screen.queryByRole("alert");
      expect(err).toBeTruthy();
      expect(err?.textContent).toContain("Task nicht blockiert");
    });

    // Keine Erfolgsmeldung
    expect(screen.queryByText("Gesendet — Task neu gestartet.")).toBeNull();
  });
});
