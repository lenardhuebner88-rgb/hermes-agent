// @vitest-environment jsdom
/**
 * JarvisChat — Beweisbare Invarianten des Chat-Kerns:
 *  1. Senden → {turn_id} → Poll GET /api/pa/turns/{id} bis done → Bubble aus
 *     dem neu geladenen Verlauf (messages ist Quelle der Wahrheit), inkl.
 *     Provenienz-Badge mit dem Modell.
 *  2. Error-Turn → Error-Bubble mit Fehlertext (NIE stiller Fehler). M2-FE:
 *     nach dem Verlauf-Reload markiert status==="error" die Bubble — keine
 *     Inhalts-Heuristik mehr (frischer Render ohne Hook-Gedächtnis).
 *  3. Upload-Flow: Paste → POST /api/pa/upload → attachments:[{asset_id}]
 *     im Message-POST-Body (max 1 Bild/Turn).
 *  4. Schlägt schon der Message-POST fehl, erscheint eine Composer-Fehler-
 *     zeile (role=alert).
 *  5. S2.2: Switcher-Wahl reist als engine+model im POST; bei claude-Modellen
 *     trägt die Assistant-Bubble den dezenten MAX-Marker; Nicht-Vision-
 *     Engines deaktivieren den Attach-Button (Tooltip).
 *  6. M1-FE: History-Attachments rendern über /api/pa/asset/{id}; 404 →
 *     Broken-Chip statt kaputtem Thread. Cursor-Paging: „Ältere laden" holt
 *     die nächste Seite über before_id und hängt sie vorne an.
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

import type { PaChatMessage, PaEnginesResponse, PaTurn } from "@/lib/api";
import { _resetPollingStore } from "../hooks/pollingStore";
import { _resetEngineChoice, setEngineChoice } from "./engineSelection";

// Voll-Suite-Last kann waitFor über den Default (1s) hinaus bouncen
// (gleiche Vorsicht wie projekte/FragenSection.test.tsx).
configure({ asyncUtilTimeout: 5000 });

const listPaMessagesMock = vi.hoisted(() => vi.fn());
const sendPaMessageMock = vi.hoisted(() => vi.fn());
const getPaTurnMock = vi.hoisted(() => vi.fn());
const uploadPaImageMock = vi.hoisted(() => vi.fn());
const getPaEnginesMock = vi.hoisted(() => vi.fn());

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
      getPaEngines: getPaEnginesMock,
    },
  };
});

import { JarvisChat } from "./JarvisChat";

/** Verlauf, den listPaMessagesMock gerade liefern soll (Server-Wahrheit). */
let serverMessages: PaChatMessage[] = [];
let serverNextBeforeId: number | null = null;
let msgId = 0;

const ROSTER: PaEnginesResponse = {
  default_engine: "sol",
  engines: [
    { engine: "sol", models: ["gpt-5.6-sol"], default_model: "gpt-5.6-sol", supports_images: true },
    {
      engine: "claude",
      models: ["opus-4.8", "fable-5"],
      default_model: "opus-4.8",
      supports_images: false,
    },
    { engine: "kimi", models: ["k3"], default_model: "k3", supports_images: false },
  ],
};

function makeMessage(overrides: Partial<PaChatMessage>): PaChatMessage {
  msgId += 1;
  return {
    id: msgId,
    turn_id: `turn_${msgId}`,
    role: "user",
    content: "",
    engine: "sol",
    model: "gpt-5.6-sol",
    attachments: [],
    ts: 1700000000,
    status: "done",
    error: null,
    ...overrides,
  };
}

function userMessage(content: string, overrides: Partial<PaChatMessage> = {}): PaChatMessage {
  return makeMessage({ role: "user", content, ...overrides });
}

function assistantMessage(content: string, overrides: Partial<PaChatMessage> = {}): PaChatMessage {
  return makeMessage({ role: "assistant", content, ...overrides });
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
  _resetEngineChoice();
  serverMessages = [];
  serverNextBeforeId = null;
  msgId = 0;
  listPaMessagesMock.mockImplementation(async () => ({
    messages: serverMessages,
    next_before_id: serverNextBeforeId,
  }));
  sendPaMessageMock.mockResolvedValue({ turn_id: "turn_3f9a1c" });
  getPaTurnMock.mockResolvedValue(
    turnResponse({ status: "done", reply: "Zwei Aufgaben sind offen." }),
  );
  uploadPaImageMock.mockResolvedValue({ asset_id: "asset_ab12cd.png" });
  getPaEnginesMock.mockResolvedValue(ROSTER);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetPollingStore();
  _resetEngineChoice();
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
    expect((await screen.findAllByText(/gpt-5\.6-sol/)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("status", { name: "Jarvis denkt …" })).toBeNull();

    // Kontrakt: POST-Body {text} ohne attachments-/engine-Felder bei reiner
    // Textfrage ohne Switcher-Wahl.
    expect(sendPaMessageMock).toHaveBeenCalledWith(question, undefined, undefined);
    // Turn wurde gepollt, Verlauf wurde nachgeladen.
    expect(getPaTurnMock).toHaveBeenCalledWith("turn_3f9a1c");
    expect(listPaMessagesMock.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("Error-Turn → Error-Bubble mit Fehlertext, markiert über status==='error' nach Reload", async () => {
    const question = "was ist offen?";
    const errorText = "Engine-Zeitlimit erreicht";
    getPaTurnMock.mockResolvedValue(
      turnResponse({ status: "error", reply: errorText, error: errorText }),
    );
    sendPaMessageMock.mockImplementation(async () => {
      // Backend persistiert die Fehler-Reply als Assistant-Message (fail_turn)
      // — mit status "error" auf dem Turn (M2: kein Inhalts-Abgleich mehr).
      serverMessages = [
        userMessage(question, { status: "error", error: errorText }),
        assistantMessage(errorText, { status: "error", error: errorText }),
      ];
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

  it("Error-Styling kommt aus dem Wire (status), nicht aus Hook-Gedächtnis: frischer Render", async () => {
    // Ein fehlgeschlagener Turn aus einer FRÜHEREN Session: nur der Server-
    // Verlauf spricht (kein lokaler errorContents-Ref mehr).
    serverMessages = [
      userMessage("mach was"),
      assistantMessage("Engine-Fehler: nicht erreichbar", {
        status: "error",
        error: "Engine-Fehler: nicht erreichbar",
      }),
    ];
    renderChat();

    const bubble = await screen.findByText("Engine-Fehler: nicht erreichbar");
    expect(bubble.closest(".jv-bubble-error")).toBeTruthy();
    expect(screen.getByText("FEHLER")).toBeTruthy();
  });

  it("Executor-Evidenz rendert als Assistant-Bubble mit Modell-Badge", async () => {
    serverMessages = [
      assistantMessage("PA-Aktion `tmux.send_keys`: succeeded.\nAnfrage: {…}", {
        engine: "pa-executor",
        model: "gated-actions-v1",
      }),
    ];
    renderChat();

    expect(await screen.findByText(/PA-Aktion `tmux\.send_keys`: succeeded/)).toBeTruthy();
    expect(await screen.findByText(/gated-actions-v1/)).toBeTruthy();
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
    ], undefined);
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
      userMessage("guten Morgen", { ts: 1700000000 }),
      assistantMessage("Guten Morgen, Piet. Drei Dinge liegen an.", { ts: 1700000005 }),
    ];
    renderChat();

    expect(await screen.findByText("guten Morgen")).toBeTruthy();
    expect(
      await screen.findByText("Guten Morgen, Piet. Drei Dinge liegen an."),
    ).toBeTruthy();
    const badge = await screen.findByText(/gpt-5\.6-sol/);
    expect(badge.closest(".jv-badge")).toBeTruthy();
    expect(badge.textContent).toMatch(/· \d{2}:\d{2}/);
  });

  // ── S2.2: Switcher-Wahl im POST + MAX-Marker + Bild-Disable ────────────

  it("Switcher-Wahl reist als engine+model im nächsten Turn-POST mit", async () => {
    const question = "hallo opus";
    const reply = "Opus hier.";
    getPaTurnMock.mockResolvedValue(
      turnResponse({ status: "done", reply, engine: "claude", model: "opus-4.8" }),
    );
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [
        userMessage(question),
        assistantMessage(reply, { engine: "claude", model: "opus-4.8" }),
      ];
      return { turn_id: "turn_3f9a1c" };
    });
    renderChat();
    // Roster abwarten, dann Wahl setzen (Switcher-Store, kein UI-Klick nötig).
    await screen.findByLabelText("Nachricht an Jarvis");
    setEngineChoice({ engine: "claude", model: "opus-4.8" });

    await submitQuestion(question);
    expect(await screen.findByText(reply)).toBeTruthy();
    expect(sendPaMessageMock).toHaveBeenCalledWith(question, undefined, {
      engine: "claude",
      model: "opus-4.8",
    });
  });

  it("MAX-Marker erscheint auf claude-Bubbles (Roster), nicht auf sol/kimi", async () => {
    serverMessages = [
      assistantMessage("Antwort von Opus.", { engine: "claude", model: "opus-4.8" }),
      assistantMessage("Antwort von Fable.", { engine: "claude", model: "fable-5" }),
      assistantMessage("Antwort von sol.", { engine: "sol", model: "gpt-5.6-sol" }),
      assistantMessage("Antwort von Kimi.", { engine: "kimi", model: "k3" }),
    ];
    renderChat();

    expect(await screen.findByText("Antwort von Opus.")).toBeTruthy();
    // Genau zwei MAX-Marker (opus + fable), keiner auf sol/kimi.
    const markers = await screen.findAllByText("· MAX", { exact: false });
    expect(markers).toHaveLength(2);
    for (const marker of markers) {
      expect(marker.className).toContain("jv-max");
    }
  });

  it("Nicht-Vision-Engine deaktiviert den Attach-Button (Tooltip) statt des 400", async () => {
    renderChat();
    setEngineChoice({ engine: "kimi", model: "k3" });

    const attach = (await screen.findByLabelText("Bild anhängen")) as HTMLButtonElement;
    // Disabled greift, sobald das Roster geladen ist (supports_images=false).
    await vi.waitFor(() => {
      expect(attach.disabled).toBe(true);
    });
    expect(attach.getAttribute("title")).toBe("Diese Engine unterstützt keine Bilder");

    // Paste auf dieselbe Engine → Composer-Hinweis statt Upload.
    const input = screen.getByLabelText("Nachricht an Jarvis");
    const file = new File(["\x89PNG\r\n\x1a\n"], "board.png", { type: "image/png" });
    const pasteEvent = createEvent.paste(input, {
      clipboardData: { files: [file], types: ["Files"] },
    });
    fireEvent(input, pasteEvent);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("kann keine Bilder verarbeiten");
    expect(uploadPaImageMock).not.toHaveBeenCalled();
  });

  // ── M1-FE: History-Attachments über die Asset-URL ───────────────────────

  it("History-Attachment rendert als Thumbnail über /api/pa/asset/{id}", async () => {
    serverMessages = [
      userMessage("schau mal", { attachments: [{ asset_id: "asset_img01.png" }] }),
    ];
    const { container } = renderChat();

    expect(await screen.findByText("schau mal")).toBeTruthy();
    const img = container.querySelector(".jv-attref img") as HTMLImageElement;
    expect(img).toBeTruthy();
    expect(img.getAttribute("src")).toBe("/api/pa/asset/asset_img01.png");
  });

  it("404-Asset → Broken-Attachment-Chip, Bubble und Thread bleiben", async () => {
    serverMessages = [
      userMessage("altes bild", { attachments: [{ asset_id: "asset_pruned.png" }] }),
      assistantMessage("Antwort bleibt sichtbar."),
    ];
    const { container } = renderChat();

    expect(await screen.findByText("altes bild")).toBeTruthy();
    const img = container.querySelector(".jv-attref img") as HTMLImageElement;
    fireEvent.error(img);

    expect(await screen.findByText("Bild nicht mehr verfügbar")).toBeTruthy();
    // Der Thread bleibt: die Antwort-Bubble ist weiter da.
    expect(screen.getByText("Antwort bleibt sichtbar.")).toBeTruthy();
  });

  // ── M2-FE: Cursor-Paging über before_id ─────────────────────────────────

  it("Cursor-Paging: Ältere laden holt die nächste Seite über before_id und hängt sie vorne an", async () => {
    const older = [
      userMessage("älteste frage", { id: 11, ts: 1699999900 }),
      assistantMessage("älteste antwort", { id: 12, ts: 1699999905 }),
    ];
    serverMessages = [
      userMessage("jüngste frage", { id: 21 }),
      assistantMessage("jüngste antwort", { id: 22 }),
    ];
    serverNextBeforeId = 21;
    renderChat();

    expect(await screen.findByText("jüngste antwort")).toBeTruthy();
    const button = await screen.findByRole("button", { name: "ÄLTERE LADEN" });

    // Nach dem Klick liefert der Mock die ältere Seite (Ende: next_before_id=null).
    listPaMessagesMock.mockImplementation(async (_limit: number, beforeId?: number) => {
      if (beforeId === 21) return { messages: older, next_before_id: null };
      return { messages: serverMessages, next_before_id: serverNextBeforeId };
    });
    fireEvent.click(button);

    expect(await screen.findByText("älteste antwort")).toBeTruthy();
    expect(listPaMessagesMock).toHaveBeenCalledWith(30, 21);
    // Reihenfolge: ältere Bubbles VOR den jüngeren im Thread.
    const log = screen.getByRole("log", { name: "Jarvis-Chat" });
    const text = log.textContent ?? "";
    expect(text.indexOf("älteste frage")).toBeLessThan(text.indexOf("jüngste frage"));
    // Ende erreicht (next_before_id=null) → Button weg.
    await vi.waitFor(() => {
      expect(screen.queryByRole("button", { name: "ÄLTERE LADEN" })).toBeNull();
    });
  });

  it("ohne next_before_id gibt es keinen Ältere-laden-Button", async () => {
    serverMessages = [userMessage("nur eine seite")];
    serverNextBeforeId = null;
    renderChat();

    expect(await screen.findByText("nur eine seite")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "ÄLTERE LADEN" })).toBeNull();
  });
});
