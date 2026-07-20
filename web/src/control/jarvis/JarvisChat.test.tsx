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
 *  7. S3.6: Push-to-Talk transkribiert in den Composer (kein Auto-Send),
 *     Permission-/Transkriptionsfehler lassen den Input unverändert; der
 *     persistierte Vorlese-Toggle liest nur neue fertige Antworten einmal.
 *  8. S3.7: Screenshare zieht genau einen JPEG-Frame, stoppt den Stream und
 *     nutzt unverändert den bestehenden PA-Bild-Upload-Pfad.
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
const transcribeAudioMock = vi.hoisted(() => vi.fn());
const speakTextMock = vi.hoisted(() => vi.fn());

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
      transcribeAudio: transcribeAudioMock,
      speakText: speakTextMock,
    },
  };
});

import { JarvisChat } from "./JarvisChat";

/** Verlauf, den listPaMessagesMock gerade liefern soll (Server-Wahrheit). */
let serverMessages: PaChatMessage[] = [];
let serverNextBeforeId: number | null = null;
let msgId = 0;
let getUserMediaMock: ReturnType<typeof vi.fn>;
let getDisplayMediaMock: ReturnType<typeof vi.fn>;
let displayTrackStopMock: ReturnType<typeof vi.fn>;

class FakeMediaRecorder {
  static isTypeSupported = vi.fn(() => true);

  readonly mimeType: string;
  state: RecordingState = "inactive";
  ondataavailable: ((this: MediaRecorder, ev: BlobEvent) => unknown) | null = null;
  onerror: ((this: MediaRecorder, ev: Event) => unknown) | null = null;
  onstop: ((this: MediaRecorder, ev: Event) => unknown) | null = null;

  constructor(_stream: MediaStream, options?: MediaRecorderOptions) {
    this.mimeType = options?.mimeType ?? "audio/webm";
  }

  start() {
    this.state = "recording";
  }

  stop() {
    this.state = "inactive";
    this.ondataavailable?.call(
      this as unknown as MediaRecorder,
      { data: new Blob(["recording"], { type: this.mimeType }) } as BlobEvent,
    );
    this.onstop?.call(this as unknown as MediaRecorder, new Event("stop"));
  }
}

function installVoiceBrowserStubs() {
  getUserMediaMock = vi.fn().mockResolvedValue({
    getTracks: () => [{ stop: vi.fn() }],
  } as unknown as MediaStream);
  displayTrackStopMock = vi.fn();
  getDisplayMediaMock = vi.fn().mockResolvedValue({
    getTracks: () => [{ stop: displayTrackStopMock }],
  } as unknown as MediaStream);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: getUserMediaMock, getDisplayMedia: getDisplayMediaMock },
  });
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);

  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(function (
    this: HTMLMediaElement,
  ): Promise<void> {
    queueMicrotask(() => this.dispatchEvent(new Event("ended")));
    return Promise.resolve();
  });
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
}

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
  transcribeAudioMock.mockResolvedValue({
    ok: true,
    transcript: "hallo welt",
    provider: "local",
    polished: false,
  });
  speakTextMock.mockResolvedValue({ data_url: "data:audio/mpeg;base64,SUQz" });
  window.localStorage.clear();
  installVoiceBrowserStubs();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
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

  it("Screenshare-Klick erfasst einen JPEG-Frame, lädt ihn einmal hoch und stoppt den Stream", async () => {
    const drawImage = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage,
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback) => {
      callback(new Blob(["jpeg-frame"], { type: "image/jpeg" }));
    });
    vi.mocked(HTMLMediaElement.prototype.play).mockImplementationOnce(function (
      this: HTMLMediaElement,
    ) {
      Object.defineProperties(this, {
        videoWidth: { configurable: true, value: 1280 },
        videoHeight: { configurable: true, value: 720 },
      });
      this.dispatchEvent(new Event("loadeddata"));
      return Promise.resolve();
    });
    renderChat();

    fireEvent.click(await screen.findByRole("button", { name: "Bildschirm teilen" }));

    await vi.waitFor(() => expect(uploadPaImageMock).toHaveBeenCalledTimes(1));
    const file = uploadPaImageMock.mock.calls[0]?.[0] as File;
    expect(file).toBeInstanceOf(File);
    expect(file.name).toBe("screenshot.jpg");
    expect(file.type).toBe("image/jpeg");
    expect(getDisplayMediaMock).toHaveBeenCalledWith({ video: true });
    expect(drawImage).toHaveBeenCalledTimes(1);
    expect(displayTrackStopMock).toHaveBeenCalledTimes(1);
  });

  it("abgebrochener Screenshare-Picker bleibt still und hängt kein Bild an", async () => {
    getDisplayMediaMock.mockRejectedValueOnce(new DOMException("cancelled", "NotAllowedError"));
    renderChat();

    fireEvent.click(await screen.findByRole("button", { name: "Bildschirm teilen" }));

    await vi.waitFor(() => expect(getDisplayMediaMock).toHaveBeenCalledTimes(1));
    expect(uploadPaImageMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("toBlob(null) zeigt die Screenshare-Fehlermeldung und stoppt den Stream", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage: vi.fn(),
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback) => {
      callback(null);
    });
    vi.mocked(HTMLMediaElement.prototype.play).mockImplementationOnce(function (
      this: HTMLMediaElement,
    ) {
      Object.defineProperties(this, {
        videoWidth: { configurable: true, value: 800 },
        videoHeight: { configurable: true, value: 600 },
      });
      this.dispatchEvent(new Event("loadeddata"));
      return Promise.resolve();
    });
    renderChat();

    fireEvent.click(await screen.findByRole("button", { name: "Bildschirm teilen" }));

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Bildschirmaufnahme fehlgeschlagen",
    );
    expect(uploadPaImageMock).not.toHaveBeenCalled();
    expect(displayTrackStopMock).toHaveBeenCalledTimes(1);
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

  // ── S3.6: Push-to-Talk + persistiertes Vorlesen ─────────────────────────

  it("Mic-Klick → Aufnahme → Stop transkribiert in den Input, ohne Auto-Send", async () => {
    renderChat();

    const mic = await screen.findByRole("button", { name: "Diktieren" });
    fireEvent.click(mic);
    const stopMic = await screen.findByRole("button", {
      name: "Aufnahme läuft — zum Stoppen tippen",
    });
    fireEvent.click(stopMic);

    const input = (await screen.findByLabelText("Nachricht an Jarvis")) as HTMLInputElement;
    await vi.waitFor(() => {
      expect(transcribeAudioMock).toHaveBeenCalledWith(
        expect.stringMatching(/^data:audio\/webm(?:;codecs=opus)?;base64,/),
        "audio/webm;codecs=opus",
      );
      expect(input.value).toBe("hallo welt");
    });
    expect(sendPaMessageMock).not.toHaveBeenCalled();
  });

  it("Mic-Permission verweigert → deutsche Meldung, kein Absturz", async () => {
    getUserMediaMock.mockRejectedValueOnce(new DOMException("denied", "NotAllowedError"));
    renderChat();

    fireEvent.click(await screen.findByRole("button", { name: "Diktieren" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Mikrofon-Zugriff verweigert — im Browser erlauben");
    expect(screen.getByRole("button", { name: "Diktieren" })).toBeTruthy();
  });

  it("Transkriptionsfehler zeigt Meldung und lässt den Input unverändert", async () => {
    transcribeAudioMock.mockRejectedValueOnce(new Error("STT offline"));
    renderChat();

    const input = (await screen.findByLabelText("Nachricht an Jarvis")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "bestehender Text" } });
    fireEvent.click(screen.getByRole("button", { name: "Diktieren" }));
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Aufnahme läuft — zum Stoppen tippen",
      }),
    );

    expect((await screen.findByRole("alert")).textContent).toContain("Aufnahme fehlgeschlagen");
    expect(input.value).toBe("bestehender Text");
    expect(sendPaMessageMock).not.toHaveBeenCalled();
  });

  it("Vorlesen ON spielt eine neue fertige Antwort einmal; Re-Render nicht doppelt", async () => {
    const reply = "Das ist die neue Antwort.";
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [userMessage("lies vor"), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    const view = renderChat();
    const speakToggle = await screen.findByRole("button", { name: "Antworten vorlesen" });
    fireEvent.click(speakToggle);

    await submitQuestion("lies vor");
    expect(await screen.findByText(reply)).toBeTruthy();
    await vi.waitFor(() => {
      expect(speakTextMock).toHaveBeenCalledTimes(1);
      expect(speakTextMock).toHaveBeenCalledWith(reply);
      expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
    });

    view.rerender(<JarvisChat turnPollIntervalMs={25} />);
    await vi.waitFor(() => expect(speakTextMock).toHaveBeenCalledTimes(1));
  });

  it("Vorlesen OFF liest weder Historie noch eine neue fertige Antwort", async () => {
    serverMessages = [assistantMessage("Historische Antwort")];
    renderChat();
    expect(await screen.findByText("Historische Antwort")).toBeTruthy();
    expect(speakTextMock).not.toHaveBeenCalled();

    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [
        ...serverMessages,
        userMessage("bleib still"),
        assistantMessage("Neue stille Antwort"),
      ];
      return { turn_id: "turn_3f9a1c" };
    });
    await submitQuestion("bleib still");
    expect(await screen.findByText("Neue stille Antwort")).toBeTruthy();
    expect(speakTextMock).not.toHaveBeenCalled();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("Vorlese-Toggle persistiert in localStorage und wird beim Remount geladen", async () => {
    const first = renderChat();
    const toggle = await screen.findByRole("button", { name: "Antworten vorlesen" });
    fireEvent.click(toggle);
    expect(window.localStorage.getItem("jarvis.speak.enabled")).toBe("1");
    first.unmount();

    renderChat();
    const restored = await screen.findByRole("button", { name: "Antworten vorlesen" });
    expect(restored.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(restored);
    expect(window.localStorage.getItem("jarvis.speak.enabled")).toBe("0");
  });
});
