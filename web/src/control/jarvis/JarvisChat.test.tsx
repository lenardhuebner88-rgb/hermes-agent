// @vitest-environment jsdom
/**
 * JarvisChat — Beweisbare Invarianten des Chat-Kerns (Brief, Sprint 1 Karte e):
 *  1. Senden → {turn_id} → Poll GET /api/pa/turns/{id} bis done → Bubble aus
 *     dem neu geladenen Verlauf (messages ist Quelle der Wahrheit), inkl.
 *     Provenienz-Badge mit dem Modell.
 *  2. Error-Turn → Error-Bubble mit Fehlertext (NIE stiller Fehler) — das
 *     Error-Styling bleibt auch nach dem Verlauf-Reload erhalten.
 *  3. Upload-Flow: Paste → POST /api/pa/upload → attachments:[{asset_id}]
 *     im Message-POST-Body (max 1 Bild/Turn).
 *  4. Schlägt schon der Message-POST fehl, erscheint eine Composer-Fehler-
 *     zeile (role=alert).
 * Payload-Shapes: realistische Formen aus tests/hermes_cli/test_pa_chat.py.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  configure,
  createEvent,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

import type { PaChatMessage, PaTurn } from "@/lib/api";
import { _resetPollingStore } from "../hooks/pollingStore";

// Voll-Suite-Last kann waitFor über den Default (1s) hinaus bouncen
// (gleiche Vorsicht wie projekte/FragenSection.test.tsx).
configure({ asyncUtilTimeout: 5000 });

const listPaMessagesMock = vi.hoisted(() => vi.fn());
const sendPaMessageMock = vi.hoisted(() => vi.fn());
const getPaTurnMock = vi.hoisted(() => vi.fn());
const uploadPaImageMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      listPaMessages: listPaMessagesMock,
      sendPaMessage: sendPaMessageMock,
      getPaTurn: getPaTurnMock,
      uploadPaImage: uploadPaImageMock,
    },
  };
});

import { JarvisChat } from "./JarvisChat";

/** Verlauf, den listPaMessagesMock gerade liefern soll (Server-Wahrheit). */
let serverMessages: PaChatMessage[] = [];

function userMessage(content: string, ts = 1700000000): PaChatMessage {
  return { role: "user", content, engine: "sol", model: "gpt-5.6-sol", ts };
}

function assistantMessage(content: string, ts = 1700000002): PaChatMessage {
  return { role: "assistant", content, engine: "sol", model: "gpt-5.6-sol", ts };
}

function turnResponse(overrides: Partial<PaTurn>): PaTurn {
  return {
    turn_id: "turn_3f9a1c",
    status: "done",
    reply: null,
    engine: "sol",
    model: "gpt-5.6-sol",
    ts: 1700000000,
    error: null,
    ...overrides,
  };
}

beforeEach(() => {
  _resetPollingStore();
  serverMessages = [];
  listPaMessagesMock.mockImplementation(async () => ({ messages: serverMessages }));
  sendPaMessageMock.mockResolvedValue({ turn_id: "turn_3f9a1c" });
  getPaTurnMock.mockResolvedValue(
    turnResponse({ status: "done", reply: "Zwei Aufgaben sind offen." }),
  );
  uploadPaImageMock.mockResolvedValue({ asset_id: "asset_ab12cd.png" });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetPollingStore();
});

/** Schnelle Turn-Poll-Kadenz statt der produktiven 1,5 s. */
function renderChat() {
  return render(<JarvisChat turnPollIntervalMs={25} />);
}

async function submitQuestion(text: string) {
  const input = await screen.findByLabelText("Nachricht an Jarvis");
  fireEvent.change(input, { target: { value: text } });
  fireEvent.click(screen.getByLabelText("Nachricht senden"));
}

describe("JarvisChat (LIVE-Kontrakt /api/pa/*, Payload-Shapes aus test_pa_chat.py)", () => {
  it("Senden → turn_id → Poll → Bubble erscheint aus dem neu geladenen Verlauf", async () => {
    const question = "was ist offen?";
    const reply = "Zwei Aufgaben sind offen.";
    getPaTurnMock
      .mockResolvedValueOnce(turnResponse({ status: "running" }))
      .mockResolvedValue(turnResponse({ status: "done", reply }));
    sendPaMessageMock.mockImplementation(async () => {
      // Server-Wahrheit nach dem Turn: beide Bubbles im Verlauf.
      serverMessages = [userMessage(question), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    renderChat();

    await submitQuestion(question);

    // Pending: User-Bubble sofort + Denk-Zustand sichtbar.
    expect(await screen.findByText(question)).toBeTruthy();
    expect(screen.getByRole("status", { name: "Jarvis denkt …" })).toBeTruthy();

    // Done: Verlauf neu geladen — Antwort-Bubble + Provenienz-Badge (Modell).
    expect(await screen.findByText(reply)).toBeTruthy();
    expect((await screen.findAllByText(/gpt-5\.6-sol ·/)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("status", { name: "Jarvis denkt …" })).toBeNull();

    // Kontrakt: POST-Body {text} ohne attachments-Feld bei reiner Textfrage.
    expect(sendPaMessageMock).toHaveBeenCalledWith(question, undefined);
    // Turn wurde gepollt, Verlauf wurde nachgeladen.
    expect(getPaTurnMock).toHaveBeenCalledWith("turn_3f9a1c");
    expect(listPaMessagesMock.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("Error-Turn → Error-Bubble mit Fehlertext, Styling überlebt den Verlauf-Reload", async () => {
    const question = "was ist offen?";
    const errorText = "Engine-Zeitlimit erreicht";
    getPaTurnMock.mockResolvedValue(
      turnResponse({ status: "error", reply: errorText, error: errorText }),
    );
    sendPaMessageMock.mockImplementation(async () => {
      // Backend persistiert die Fehler-Reply als Assistant-Message (fail_turn).
      serverMessages = [userMessage(question), assistantMessage(errorText)];
      return { turn_id: "turn_3f9a1c" };
    });
    renderChat();

    await submitQuestion(question);

    const errorBubble = await screen.findByText(errorText);
    expect(errorBubble).toBeTruthy();
    // Error-Bubble ist als solche markiert (Label + Klasse), kein stiller Fehler.
    expect(screen.getByText("FEHLER")).toBeTruthy();
    expect(errorBubble.closest(".jv-bubble-error")).toBeTruthy();
  });

  it("Upload-Flow: Paste → upload → attachments im POST-Body (max 1 Bild)", async () => {
    const question = "was ist auf dem Board?";
    const reply = "Ich sehe drei Spalten.";
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [userMessage(question), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    const { container } = renderChat();

    const input = await screen.findByLabelText("Nachricht an Jarvis");
    const file = new File(["\x89PNG\r\n\x1a\n"], "board.png", { type: "image/png" });
    const pasteEvent = createEvent.paste(input, {
      clipboardData: { files: [file], types: ["Files"] },
    });
    fireEvent(input, pasteEvent);

    // Upload läuft sofort; Vorschau-Thumbnail in der Composer-Zeile.
    expect(await screen.findByText("board.png")).toBeTruthy();
    expect(uploadPaImageMock).toHaveBeenCalledWith(file);

    fireEvent.change(input, { target: { value: question } });
    fireEvent.click(screen.getByLabelText("Nachricht senden"));

    expect(await screen.findByText(reply)).toBeTruthy();
    // Kontrakt: attachments:[{asset_id}] im Message-POST.
    expect(sendPaMessageMock).toHaveBeenCalledWith(question, [
      { asset_id: "asset_ab12cd.png" },
    ]);
    // Thumbnail ist nach dem Senden aus der Leiste gewandert (in die Bubble).
    expect(container.querySelector(".jv-attachchip")).toBeNull();
  });

  it("Attach-Button (Datei-Picker) führt zum selben Upload-Kontrakt", async () => {
    const { container } = renderChat();
    const fileInput = container.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const file = new File(["jpeg"], "foto.jpg", { type: "image/jpeg" });
    fireEvent.change(fileInput, { target: { files: [file] } });

    expect(await screen.findByText("foto.jpg")).toBeTruthy();
    expect(uploadPaImageMock).toHaveBeenCalledWith(file);
  });

  it("Message-POST-Fehler → Composer-Fehlerzeile (role=alert), kein stiller Fehler", async () => {
    sendPaMessageMock.mockRejectedValue(new Error('400: {"detail":"Unbekannte asset_id"}'));
    renderChat();

    await submitQuestion("was ist offen?");

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Nachricht konnte nicht gesendet werden.");
    expect(alert.textContent).toContain("Unbekannte asset_id");
  });

  it("Upload-Fehler → Composer-Fehlerzeile statt leerem Attach-Chip", async () => {
    uploadPaImageMock.mockRejectedValue(new Error('413: {"detail":"Bild ist zu groß"}'));
    const { container } = renderChat();

    const fileInput = container.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const file = new File(["x"], "riesig.png", { type: "image/png" });
    fireEvent.change(fileInput, { target: { files: [file] } });

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Bild-Upload fehlgeschlagen.");
    expect(alert.textContent).toContain("Bild ist zu groß");
    expect(container.querySelector(".jv-attachchip")).toBeNull();
  });

  it("Bestehender Verlauf rendert User/Assistant-Bubbles mit Provenienz-Badge", async () => {
    serverMessages = [
      userMessage("guten Morgen", 1700000000),
      assistantMessage("Guten Morgen, Piet. Drei Dinge liegen an.", 1700000005),
    ];
    renderChat();

    expect(await screen.findByText("guten Morgen")).toBeTruthy();
    expect(
      await screen.findByText("Guten Morgen, Piet. Drei Dinge liegen an."),
    ).toBeTruthy();
    const badge = await screen.findByText(/gpt-5\.6-sol · \d{2}:\d{2}/);
    expect(badge.className).toContain("jv-badge");
  });
});
