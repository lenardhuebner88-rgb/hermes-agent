// @vitest-environment jsdom
/**
 * JarvisVoice (S3.6) — Beweisbare Invarianten von Push-to-Talk + Vorlesen:
 *  1. Mic-Button: Klick → Aufnahme (getUserMedia/MediaRecorder), zweiter
 *     Klick → transcribeAudio mit base64-data-URL + Mime → das Transkript
 *     landet im Input (Append mit Leerzeichen), KEIN Auto-Send; die
 *     Media-Tracks werden beim Stop geschlossen.
 *  2. Permission denied (getUserMedia rejectet NotAllowedError) → deutsche
 *     Meldung (role=alert), kein Absturz, kein transcribe-Call, Input leer.
 *  3. transcribe-Fehler (throw UND ok:false/leer) → Fehlermeldung, der
 *     Input bleibt unverändert.
 *  4. Vorlese-Toggle ON: eine FERTIGE Assistant-Antwort (status done) wird
 *     genau EINMAL abgespielt (speakText mit Antworttext + Audio.play()) —
 *     kein Doppel-Play bei Re-Render, keine historischen Bubbles beim Mount.
 *  5. Toggle OFF (Default) → speakText wird NIE gerufen; der Toggle-Stand
 *     persistiert in localStorage und überlebt ein Remount.
 *  6. TTS-Fehler wird best-effort geschluckt — die Bubble erscheint trotzdem.
 * Mock-Boundary: NUR @/lib/api (Repo-Muster) + Browser-APIs (jsdom kennt
 * getUserMedia/MediaRecorder/Audio nicht); die SUT läuft echt. Fixture-
 * Shapes exakt aus dem Backend (web_server.py /api/audio/transcribe|speak:
 * {ok, transcript, provider, polished} bzw. {data_url}).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen } from "@testing-library/react";

import type { PaChatMessage, PaEnginesResponse, PaTurn } from "@/lib/api";
import { _resetPollingStore } from "../hooks/pollingStore";
import { _resetEngineChoice } from "./engineSelection";
import { PTT_AUTOSEND_STORAGE_KEY } from "./useMicRecorder";
import { SPEAK_ENABLED_STORAGE_KEY } from "./useSpeechPlayback";

// Voll-Suite-Last kann waitFor über den Default (1s) hinaus bouncen
// (gleiche Vorsicht wie JarvisChat.test.tsx).
configure({ asyncUtilTimeout: 5000 });

const listPaMessagesMock = vi.hoisted(() => vi.fn());
const sendPaMessageMock = vi.hoisted(() => vi.fn());
const getPaTurnMock = vi.hoisted(() => vi.fn());
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
      getPaEngines: getPaEnginesMock,
      transcribeAudio: transcribeAudioMock,
      speakText: speakTextMock,
    },
  };
});

import { JarvisChat } from "./JarvisChat";

// ── Browser-API-Stubs (jsdom kennt diese APIs nicht) ─────────────────────

const getUserMediaMock = vi.fn();
let trackStopSpy = vi.fn();

function fakeStream(): MediaStream {
  trackStopSpy = vi.fn();
  return { getTracks: () => [{ stop: trackStopSpy }] } as unknown as MediaStream;
}

/** Minimaler MediaRecorder: stop() feuert wie der echte erst den letzten
 *  Chunk (ondataavailable), dann onstop — asynchron. */
class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = [];
  static isTypeSupported = (_type: string) => true;

  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  state: "inactive" | "recording" = "inactive";
  readonly mimeType: string;

  constructor(_stream: MediaStream, options?: { mimeType?: string }) {
    this.mimeType = options?.mimeType ?? "";
    FakeMediaRecorder.instances.push(this);
  }

  start() {
    this.state = "recording";
  }

  stop() {
    this.state = "inactive";
    setTimeout(() => {
      this.ondataavailable?.({
        data: new Blob(["fake-audio"], { type: this.mimeType || "audio/webm" }),
      });
      this.onstop?.();
    }, 0);
  }
}

/** Minimales Audio-Element: play() resolved sofort, Events steuerbar. */
class FakeAudio {
  static instances: FakeAudio[] = [];
  src: string;
  play = vi.fn(() => Promise.resolve());
  pause = vi.fn();
  private listeners = new Map<string, Array<{ cb: EventListener; once: boolean }>>();

  constructor(src = "") {
    this.src = src;
    FakeAudio.instances.push(this);
  }

  addEventListener(type: string, cb: EventListener, options?: { once?: boolean }) {
    const list = this.listeners.get(type) ?? [];
    list.push({ cb, once: options?.once ?? false });
    this.listeners.set(type, list);
  }

  removeEventListener(type: string, cb: EventListener) {
    this.listeners.set(
      type,
      (this.listeners.get(type) ?? []).filter((entry) => entry.cb !== cb),
    );
  }

  emit(type: string) {
    const event = new Event(type);
    for (const entry of [...(this.listeners.get(type) ?? [])]) {
      if (entry.once) this.removeEventListener(type, entry.cb);
      entry.cb(event);
    }
  }
}

// ── Server-Wahrheit (gleiches Harness wie JarvisChat.test.tsx) ───────────

let serverMessages: PaChatMessage[] = [];
let msgId = 0;

const ROSTER: PaEnginesResponse = {
  default_engine: "sol",
  engines: [
    { engine: "sol", models: ["gpt-5.6-sol"], default_model: "gpt-5.6-sol", supports_images: true },
    { engine: "claude", models: ["opus-4.8"], default_model: "opus-4.8", supports_images: false },
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
  window.localStorage.clear();
  serverMessages = [];
  msgId = 0;
  FakeMediaRecorder.instances = [];
  FakeAudio.instances = [];
  getUserMediaMock.mockReset();
  getUserMediaMock.mockImplementation(async () => fakeStream());
  Object.defineProperty(window.navigator, "mediaDevices", {
    value: { getUserMedia: getUserMediaMock },
    configurable: true,
  });
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
  vi.stubGlobal("Audio", FakeAudio);
  listPaMessagesMock.mockImplementation(async () => ({
    messages: serverMessages,
    next_before_id: null,
  }));
  sendPaMessageMock.mockResolvedValue({ turn_id: "turn_3f9a1c" });
  getPaTurnMock.mockResolvedValue(
    turnResponse({ status: "done", reply: "Zwei Aufgaben sind offen." }),
  );
  getPaEnginesMock.mockResolvedValue(ROSTER);
  // Fixture-Shape exakt aus dem Backend (/api/audio/transcribe|speak).
  transcribeAudioMock.mockResolvedValue({
    ok: true,
    transcript: "hallo welt",
    provider: "local",
    polished: false,
  });
  speakTextMock.mockResolvedValue({ data_url: "data:audio/mpeg;base64,AAAA" });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  window.localStorage.clear();
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

/** Eine volle Diktat-Runde: Klick → Aufnahme läuft → zweiter Klick → Stop. */
async function dictateCycle() {
  fireEvent.click(await screen.findByLabelText("Diktieren"));
  fireEvent.click(await screen.findByLabelText("Aufnahme läuft — zum Stoppen tippen"));
}

describe("JarvisChat S3.6 — Push-to-Talk (Mic → /api/audio/transcribe)", () => {
  it("Klick → Aufnahme → zweiter Klick transkribiert in den Input (KEIN Auto-Send)", async () => {
    renderChat();
    const input = (await screen.findByLabelText("Nachricht an Jarvis")) as HTMLInputElement;

    await dictateCycle();

    await vi.waitFor(() => {
      expect(transcribeAudioMock).toHaveBeenCalledTimes(1);
    });
    const [dataUrl, mimeType] = transcribeAudioMock.mock.calls[0] as [string, string];
    expect(dataUrl).toMatch(/^data:audio\/webm/);
    expect(dataUrl).toContain(";base64,");
    expect(mimeType).toBe("audio/webm;codecs=opus");

    await vi.waitFor(() => {
      expect(input.value).toBe("hallo welt");
    });
    // KEIN Auto-Send: das Transkript wird NICHT selbstständig abgeschickt.
    expect(sendPaMessageMock).not.toHaveBeenCalled();
    // Lebenszyklus: Browser-Permission direkt angefragt, Tracks beim Stop zu.
    expect(getUserMediaMock).toHaveBeenCalledWith({ audio: true });
    expect(trackStopSpy).toHaveBeenCalled();
  });

  it("Transkript wird mit Leerzeichen an bestehenden Input-Text angehängt", async () => {
    renderChat();
    const input = (await screen.findByLabelText("Nachricht an Jarvis")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "notiz" } });

    await dictateCycle();

    await vi.waitFor(() => {
      expect(input.value).toBe("notiz hallo welt");
    });
    expect(sendPaMessageMock).not.toHaveBeenCalled();
  });

  it("S7: Auto-Send AN sendet das erfolgreiche Transkript direkt als Turn", async () => {
    renderChat();
    const toggle = await screen.findByLabelText("Diktat direkt senden");
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(toggle);

    await dictateCycle();

    await vi.waitFor(() => {
      expect(sendPaMessageMock).toHaveBeenCalledWith("hallo welt", undefined, undefined);
    });
    expect((screen.getByLabelText("Nachricht an Jarvis") as HTMLInputElement).value).toBe("");
  });

  it("S7: Auto-Send-Toggle persistiert und überlebt ein Remount", async () => {
    const first = renderChat();
    const toggle = await screen.findByLabelText("Diktat direkt senden");

    fireEvent.click(toggle);
    expect(window.localStorage.getItem(PTT_AUTOSEND_STORAGE_KEY)).toBe("1");

    first.unmount();
    renderChat();
    const remounted = await screen.findByLabelText("Diktat direkt senden");
    expect(remounted.getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(remounted);
    expect(window.localStorage.getItem(PTT_AUTOSEND_STORAGE_KEY)).toBe("0");
  });

  it("Permission denied → deutsche Meldung (role=alert), kein Absturz, Input leer", async () => {
    getUserMediaMock.mockReset();
    getUserMediaMock.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    renderChat();
    const input = (await screen.findByLabelText("Nachricht an Jarvis")) as HTMLInputElement;

    fireEvent.click(await screen.findByLabelText("Diktieren"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Mikrofon-Zugriff verweigert — im Browser erlauben");
    expect(transcribeAudioMock).not.toHaveBeenCalled();
    expect(input.value).toBe("");
  });

  it("transcribe-Fehler (throw UND ok:false) → Fehlermeldung, Input unverändert", async () => {
    transcribeAudioMock.mockRejectedValueOnce(new Error('500: {"detail":"groq down"}'));
    renderChat();
    const input = (await screen.findByLabelText("Nachricht an Jarvis")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "bleibt" } });

    await dictateCycle();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Aufnahme fehlgeschlagen");
    expect(input.value).toBe("bleibt");

    // Zweite Runde: Backend meldet ok:false mit leerem Transkript — gleiche
    // Behandlung (Fehler, kein Input-Eingriff).
    transcribeAudioMock.mockResolvedValueOnce({
      ok: false,
      transcript: "",
      provider: "local",
      polished: false,
    });
    await dictateCycle();
    await vi.waitFor(() => {
      expect(transcribeAudioMock).toHaveBeenCalledTimes(2);
    });
    expect(input.value).toBe("bleibt");
  });
});

describe("JarvisChat S3.6 — Vorlese-Toggle (/api/audio/speak)", () => {
  it("Toggle ON: fertige Assistant-Antwort wird genau EINMAL abgespielt (kein Doppel-Play)", async () => {
    const reply = "Zwei Aufgaben sind offen.";
    getPaTurnMock.mockResolvedValue(turnResponse({ status: "done", reply }));
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [userMessage("was ist offen?"), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    const { rerender } = renderChat();

    const toggle = await screen.findByLabelText("Antworten vorlesen");
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-pressed")).toBe("true");

    await submitQuestion("was ist offen?");
    expect(await screen.findByText(reply)).toBeTruthy();

    await vi.waitFor(() => {
      expect(speakTextMock).toHaveBeenCalledWith(reply);
    });
    expect(FakeAudio.instances).toHaveLength(1);
    expect(FakeAudio.instances[0].src).toBe("data:audio/mpeg;base64,AAAA");
    expect(FakeAudio.instances[0].play).toHaveBeenCalled();
    // Wiedergabe sauber beenden lassen (ended löst das Play-Promise auf).
    FakeAudio.instances[0].emit("ended");

    // Kein Doppel-Play bei Re-Render / erneutem Effekt-Lauf.
    rerender(<JarvisChat turnPollIntervalMs={25} />);
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(speakTextMock).toHaveBeenCalledTimes(1);
    expect(FakeAudio.instances).toHaveLength(1);
  });

  it("S7: Mic-Start während des Vorlesens stoppt die Wiedergabe sofort", async () => {
    const reply = "Ich lese noch vor.";
    getPaTurnMock.mockResolvedValue(turnResponse({ status: "done", reply }));
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [userMessage("lies vor"), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    renderChat();
    fireEvent.click(await screen.findByLabelText("Antworten vorlesen"));
    await submitQuestion("lies vor");

    await vi.waitFor(() => expect(FakeAudio.instances).toHaveLength(1));
    const audio = FakeAudio.instances[0];
    fireEvent.click(await screen.findByLabelText("Diktieren"));

    expect(audio.pause).toHaveBeenCalledTimes(1);
    expect(audio.src).toBe("");
    expect(await screen.findByLabelText("Aufnahme läuft — zum Stoppen tippen")).toBeTruthy();
  });

  it("Toggle OFF (Default): speakText wird NIE gerufen", async () => {
    const reply = "Zwei Aufgaben sind offen.";
    getPaTurnMock.mockResolvedValue(turnResponse({ status: "done", reply }));
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [userMessage("was ist offen?"), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    renderChat();

    await submitQuestion("was ist offen?");
    expect(await screen.findByText(reply)).toBeTruthy();

    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(speakTextMock).not.toHaveBeenCalled();
    expect(FakeAudio.instances).toHaveLength(0);
  });

  it("Historie beim Mount wird NIE vorgelesen (Baseline, auch bei Toggle ON)", async () => {
    window.localStorage.setItem(SPEAK_ENABLED_STORAGE_KEY, "1");
    serverMessages = [userMessage("guten Morgen"), assistantMessage("Guten Morgen, Piet.")];
    renderChat();

    expect(await screen.findByText("Guten Morgen, Piet.")).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(speakTextMock).not.toHaveBeenCalled();
    expect(FakeAudio.instances).toHaveLength(0);
  });

  it("Toggle-Stand persistiert in localStorage und überlebt ein Remount", async () => {
    const first = renderChat();
    const toggle = await screen.findByLabelText("Antworten vorlesen");
    expect(toggle.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(toggle);
    expect(window.localStorage.getItem(SPEAK_ENABLED_STORAGE_KEY)).toBe("1");

    first.unmount();
    renderChat();
    const remounted = await screen.findByLabelText("Antworten vorlesen");
    expect(remounted.getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(remounted);
    expect(window.localStorage.getItem(SPEAK_ENABLED_STORAGE_KEY)).toBe("0");
  });

  it("TTS-Fehler wird best-effort geschluckt — die Antwort-Bubble erscheint trotzdem", async () => {
    const reply = "Antwort trotz TTS-Ausfall.";
    getPaTurnMock.mockResolvedValue(turnResponse({ status: "done", reply }));
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [userMessage("frage"), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    speakTextMock.mockRejectedValue(new Error("500: TTS down"));
    renderChat();
    fireEvent.click(await screen.findByLabelText("Antworten vorlesen"));

    await submitQuestion("frage");
    expect(await screen.findByText(reply)).toBeTruthy();
    await vi.waitFor(() => {
      expect(speakTextMock).toHaveBeenCalledWith(reply);
    });
    // Kein Absturz, kein Audio — der Chat läuft normal weiter.
    expect(FakeAudio.instances).toHaveLength(0);
  });
});
