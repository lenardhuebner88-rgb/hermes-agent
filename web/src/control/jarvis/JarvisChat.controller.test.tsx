// @vitest-environment jsdom
/**
 * G2b — JarvisChat-Controller-Naht (NEUE Datei; Bestandstests unberührt):
 *  - onController: Mount → Controller, Unmount → null
 *  - submitQuestion extern = Composer-Pfad
 *  - resetState / sessionEpoch leeren Alt-Seiten + Cursor; pending Turn invalid
 *  - voiceMode unterdrückt Auto-TTS; Default-Pfad und Barge-in bleiben
 *  - PlanSpec-Cards, PTT-Auto-Send, Live-Share mit Naht-Props unverändert
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen } from "@testing-library/react";

import type { PaChatMessage, PaEnginesResponse, PaPlanspecDraft, PaTurn } from "@/lib/api";
import { _resetPollingStore } from "../hooks/pollingStore";
import { _resetEngineChoice } from "./engineSelection";
import { PTT_AUTOSEND_STORAGE_KEY } from "./useMicRecorder";
import { SPEAK_ENABLED_STORAGE_KEY } from "./useSpeechPlayback";

configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 15_000 });

const listPaMessagesMock = vi.hoisted(() => vi.fn());
const sendPaMessageMock = vi.hoisted(() => vi.fn());
const getPaTurnMock = vi.hoisted(() => vi.fn());
const uploadPaImageMock = vi.hoisted(() => vi.fn());
const getPaEnginesMock = vi.hoisted(() => vi.fn());
const getPaInboxMock = vi.hoisted(() => vi.fn());
const transcribeAudioMock = vi.hoisted(() => vi.fn());
const speakTextMock = vi.hoisted(() => vi.fn());
const draftPlanspecMock = vi.hoisted(() => vi.fn());
const proposePlanspecMock = vi.hoisted(() => vi.fn());
const startLiveShareMock = vi.hoisted(() => vi.fn());
const uploadLiveShareFrameMock = vi.hoisted(() => vi.fn());
const attachLiveShareFrameMock = vi.hoisted(() => vi.fn());
const stopLiveShareMock = vi.hoisted(() => vi.fn());

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
      getPaInbox: getPaInboxMock,
      transcribeAudio: transcribeAudioMock,
      speakText: speakTextMock,
      draftPlanspec: draftPlanspecMock,
      proposePlanspec: proposePlanspecMock,
      startLiveShare: startLiveShareMock,
      uploadLiveShareFrame: uploadLiveShareFrameMock,
      attachLiveShareFrame: attachLiveShareFrameMock,
      stopLiveShare: stopLiveShareMock,
    },
  };
});

import { JarvisChat, type JarvisChatController } from "./JarvisChat";

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

function installVoiceBrowserStubs() {
  getUserMediaMock = vi.fn().mockResolvedValue({
    getTracks: () => [{ stop: vi.fn() }],
  } as unknown as MediaStream);
  displayTrackStopMock = vi.fn();
  getDisplayMediaMock = vi.fn().mockImplementation(async () => {
    const track = { stop: displayTrackStopMock, onended: null };
    return {
      getTracks: () => [track],
      getVideoTracks: () => [track],
    } as unknown as MediaStream;
  });
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: getUserMediaMock, getDisplayMedia: getDisplayMediaMock },
  });
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
  vi.stubGlobal("Audio", FakeAudio);
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

function makeDraft(overrides: Partial<PaPlanspecDraft> = {}): PaPlanspecDraft {
  return {
    draft_id: "draft_0123456789abcdef01234567",
    planspec_text:
      "---\nfreigabe: operator\nlive_test_depth: contract\n---\n\nZiel und Grenzen.\n",
    validation: { status: "CLEAN", findings: [] },
    slices: [
      { id: "S1", title: "Endpoint und Tests implementieren", lane: "coder", deps: [] },
      { id: "S2", title: "Verhalten unabhängig verifizieren", lane: "verifier", deps: ["S1"] },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  _resetPollingStore();
  _resetEngineChoice();
  serverMessages = [];
  serverNextBeforeId = null;
  msgId = 0;
  FakeAudio.instances = [];
  listPaMessagesMock.mockImplementation(async (_limit?: number, beforeId?: number) => {
    if (beforeId != null) {
      return { messages: [], next_before_id: null };
    }
    return { messages: serverMessages, next_before_id: serverNextBeforeId };
  });
  sendPaMessageMock.mockResolvedValue({ turn_id: "turn_3f9a1c" });
  getPaTurnMock.mockResolvedValue(
    turnResponse({ status: "done", reply: "Zwei Aufgaben sind offen." }),
  );
  uploadPaImageMock.mockResolvedValue({ asset_id: "asset_ab12cd.png" });
  getPaEnginesMock.mockResolvedValue(ROSTER);
  getPaInboxMock.mockResolvedValue({ items: [], errors: [] });
  transcribeAudioMock.mockResolvedValue({
    ok: true,
    transcript: "hallo welt",
    provider: "local",
    polished: false,
  });
  speakTextMock.mockResolvedValue({ data_url: "data:audio/mpeg;base64,SUQz" });
  draftPlanspecMock.mockResolvedValue(makeDraft());
  proposePlanspecMock.mockResolvedValue({ question_id: 123 });
  startLiveShareMock.mockResolvedValue({ session_id: "live_abcdef012345" });
  uploadLiveShareFrameMock.mockResolvedValue({ ok: true });
  attachLiveShareFrameMock.mockResolvedValue({ asset_id: "asset_live99.jpg" });
  stopLiveShareMock.mockResolvedValue({ ok: true });
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

function renderChat(
  props: {
    onController?: (c: JarvisChatController | null) => void;
    voiceMode?: boolean;
    sessionEpoch?: number;
  } = {},
) {
  return render(<JarvisChat turnPollIntervalMs={25} {...props} />);
}

async function loadOlderPage() {
  const older = [
    userMessage("älteste frage", { id: 11, ts: 1699999900 }),
    assistantMessage("älteste antwort", { id: 12, ts: 1699999905 }),
  ];
  serverMessages = [
    userMessage("jüngste frage", { id: 21 }),
    assistantMessage("jüngste antwort", { id: 22 }),
  ];
  serverNextBeforeId = 21;
  listPaMessagesMock.mockImplementation(async (_limit: number, beforeId?: number) => {
    if (beforeId === 21) return { messages: older, next_before_id: null };
    return { messages: serverMessages, next_before_id: serverNextBeforeId };
  });
}

describe("JarvisChat G2b Controller-Naht", () => {
  it("onController: Mount liefert Controller, Unmount null", async () => {
    const onController = vi.fn();
    const view = renderChat({ onController });
    await screen.findByLabelText("Nachricht an Jarvis");

    expect(onController).toHaveBeenCalled();
    const controller = onController.mock.calls.find(
      (call) => call[0] != null,
    )?.[0] as JarvisChatController;
    expect(controller).toBeTruthy();
    expect(typeof controller.submitQuestion).toBe("function");
    expect(typeof controller.resetState).toBe("function");

    view.unmount();
    expect(onController).toHaveBeenLastCalledWith(null);
  });

  it("submitQuestion extern sendet wie der Composer (Message + Turn)", async () => {
    const question = "was ist offen?";
    const reply = "Zwei Aufgaben sind offen.";
    let controller: JarvisChatController | null = null;
    getPaTurnMock
      .mockResolvedValueOnce(turnResponse({ status: "running" }))
      .mockResolvedValue(turnResponse({ status: "done", reply }));
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [userMessage(question), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    renderChat({
      onController: (c) => {
        controller = c;
      },
    });
    await screen.findByLabelText("Nachricht an Jarvis");
    expect(controller).toBeTruthy();

    controller!.submitQuestion(question);

    expect(await screen.findByText(question)).toBeTruthy();
    expect(screen.getByRole("status", { name: "Jarvis denkt …" })).toBeTruthy();
    expect(await screen.findByText(reply)).toBeTruthy();
    expect(sendPaMessageMock).toHaveBeenCalledWith(question, undefined, undefined);
    expect(getPaTurnMock).toHaveBeenCalledWith("turn_3f9a1c");
  });

  it("resetState leert nachgeladene Alt-Seiten und den Cursor, lädt frisch", async () => {
    let controller: JarvisChatController | null = null;
    await loadOlderPage();
    renderChat({
      onController: (c) => {
        controller = c;
      },
    });

    expect(await screen.findByText("jüngste antwort")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "ÄLTERE LADEN" }));
    expect(await screen.findByText("älteste antwort")).toBeTruthy();

    // Nach Reset: nur noch Server-Stand (ohne Alt-Seite); Cursor wieder da.
    serverMessages = [userMessage("frisch", { id: 31 }), assistantMessage("neu", { id: 32 })];
    serverNextBeforeId = 31;
    controller!.resetState();

    expect(await screen.findByText("neu")).toBeTruthy();
    await vi.waitFor(() => {
      expect(screen.queryByText("älteste antwort")).toBeNull();
      expect(screen.queryByText("älteste frage")).toBeNull();
    });
    // Cursor vom frischen Server-Stand → Button wieder sichtbar.
    expect(await screen.findByRole("button", { name: "ÄLTERE LADEN" })).toBeTruthy();
  });

  it("sessionEpoch-Wechsel leert Alt-Seiten; Erst-Render mit Epoch ohne Reset", async () => {
    await loadOlderPage();
    const view = renderChat({ sessionEpoch: 1 });

    expect(await screen.findByText("jüngste antwort")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "ÄLTERE LADEN" }));
    expect(await screen.findByText("älteste antwort")).toBeTruthy();

    // Epoch gleich → kein Reset (älteste bleibt).
    view.rerender(<JarvisChat turnPollIntervalMs={25} sessionEpoch={1} />);
    expect(screen.getByText("älteste antwort")).toBeTruthy();

    // Epoch-Wechsel → Reset.
    serverMessages = [userMessage("nach epoch", { id: 41 })];
    serverNextBeforeId = null;
    view.rerender(<JarvisChat turnPollIntervalMs={25} sessionEpoch={2} />);

    expect(await screen.findByText("nach epoch")).toBeTruthy();
    await vi.waitFor(() => {
      expect(screen.queryByText("älteste antwort")).toBeNull();
    });
  });

  it("Reset während Pending-Turn: keine späte Bubble, kein Crash", async () => {
    let controller: JarvisChatController | null = null;
    let resolveTurn: ((value: PaTurn) => void) | null = null;
    getPaTurnMock.mockImplementation(
      () =>
        new Promise<PaTurn>((resolve) => {
          resolveTurn = resolve;
        }),
    );
    sendPaMessageMock.mockResolvedValue({ turn_id: "turn_late" });
    renderChat({
      onController: (c) => {
        controller = c;
      },
    });
    await screen.findByLabelText("Nachricht an Jarvis");

    controller!.submitQuestion("läuft noch");
    expect(await screen.findByText("läuft noch")).toBeTruthy();
    expect(screen.getByRole("status", { name: "Jarvis denkt …" })).toBeTruthy();
    // Ersten Turn-Poll abwarten, damit die späte Antwort echt „in flight“ ist.
    await vi.waitFor(() => expect(getPaTurnMock).toHaveBeenCalled());
    expect(resolveTurn).toBeTypeOf("function");

    serverMessages = [];
    serverNextBeforeId = null;
    controller!.resetState();

    await vi.waitFor(() => {
      expect(screen.queryByText("läuft noch")).toBeNull();
      expect(screen.queryByRole("status", { name: "Jarvis denkt …" })).toBeNull();
    });

    // Späte Turn-Antwort darf keine Pending-/Error-Bubble erzeugen.
    resolveTurn!(
      turnResponse({
        turn_id: "turn_late",
        status: "done",
        reply: "späte Antwort die nicht erscheinen darf",
      }),
    );
    await new Promise((r) => setTimeout(r, 120));
    expect(screen.queryByText("späte Antwort die nicht erscheinen darf")).toBeNull();
    expect(screen.queryByRole("status", { name: "Jarvis denkt …" })).toBeNull();
  });

  it("voiceMode=true: Auto-TTS feuert nicht; Toggle bleibt bedienbar", async () => {
    const reply = "Keine Auto-Stimme.";
    window.localStorage.setItem(SPEAK_ENABLED_STORAGE_KEY, "1");
    getPaTurnMock.mockResolvedValue(turnResponse({ status: "done", reply }));
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [userMessage("frage"), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    renderChat({ voiceMode: true });

    const toggle = await screen.findByRole("button", { name: "Antworten vorlesen" });
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
    // Manueller Toggle bleibt funktional.
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-pressed")).toBe("true");

    const input = screen.getByLabelText("Nachricht an Jarvis");
    fireEvent.change(input, { target: { value: "frage" } });
    fireEvent.click(screen.getByLabelText("Nachricht senden"));
    expect(await screen.findByText(reply)).toBeTruthy();

    await new Promise((r) => setTimeout(r, 100));
    expect(speakTextMock).not.toHaveBeenCalled();
  });

  it("ohne voiceMode (Default): Auto-TTS bei Toggle ON wie bisher", async () => {
    const reply = "Bitte vorlesen.";
    window.localStorage.setItem(SPEAK_ENABLED_STORAGE_KEY, "1");
    getPaTurnMock.mockResolvedValue(turnResponse({ status: "done", reply }));
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [userMessage("lies vor"), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    renderChat();

    const input = await screen.findByLabelText("Nachricht an Jarvis");
    fireEvent.change(input, { target: { value: "lies vor" } });
    fireEvent.click(screen.getByLabelText("Nachricht senden"));
    expect(await screen.findByText(reply)).toBeTruthy();
    await vi.waitFor(() => {
      expect(speakTextMock).toHaveBeenCalledWith(reply);
    });
  });

  it("TTS-Barge-in (Mic-Start stoppt Wiedergabe) mit voiceMode unverändert", async () => {
    const reply = "Ich lese noch vor.";
    // voiceMode unterdrückt Auto-TTS — manuelles play via Toggle-Pfad:
    // hier: ohne voiceMode starten, dann barge-in beweisen (Bestehendes).
    window.localStorage.setItem(SPEAK_ENABLED_STORAGE_KEY, "1");
    getPaTurnMock.mockResolvedValue(turnResponse({ status: "done", reply }));
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [userMessage("lies vor"), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    renderChat({ voiceMode: false });

    const input = await screen.findByLabelText("Nachricht an Jarvis");
    fireEvent.change(input, { target: { value: "lies vor" } });
    fireEvent.click(screen.getByLabelText("Nachricht senden"));
    expect(await screen.findByText(reply)).toBeTruthy();
    await vi.waitFor(() => expect(FakeAudio.instances.length).toBeGreaterThanOrEqual(1));
    const audio = FakeAudio.instances[0];

    fireEvent.click(await screen.findByLabelText("Diktieren"));
    expect(audio.pause).toHaveBeenCalled();
    expect(audio.src).toBe("");
    expect(await screen.findByLabelText("Aufnahme läuft — zum Stoppen tippen")).toBeTruthy();
  });

  it("PlanSpec-Cards rendern unverändert mit gesetzten Naht-Props", async () => {
    renderChat({
      voiceMode: true,
      sessionEpoch: 7,
      onController: () => {},
    });

    const input = await screen.findByLabelText("Nachricht an Jarvis");
    fireEvent.change(input, { target: { value: "/plan mache einen Health-Report" } });
    fireEvent.click(screen.getByLabelText("Nachricht senden"));

    expect(draftPlanspecMock).toHaveBeenCalledWith("mache einen Health-Report", undefined);
    expect(sendPaMessageMock).not.toHaveBeenCalled();
    const card = await screen.findByTestId("jv-plan-draft_0123456789abcdef01234567");
    expect(card.textContent).toContain("PLANSPEC-ENTWURF");
    expect(card.textContent).toContain("VALIDATE: CLEAN");
  });

  it("PTT-Auto-Send funktioniert weiter mit voiceMode", async () => {
    window.localStorage.setItem(PTT_AUTOSEND_STORAGE_KEY, "1");
    renderChat({ voiceMode: true });

    fireEvent.click(await screen.findByLabelText("Diktieren"));
    fireEvent.click(await screen.findByLabelText("Aufnahme läuft — zum Stoppen tippen"));

    await vi.waitFor(() => {
      expect(sendPaMessageMock).toHaveBeenCalledWith("hallo welt", undefined, undefined);
    });
  });

  it("Live-Share-Status/Frame-Attach mit Naht-Props unverändert", async () => {
    // Frame-Sampling scharf machen (jsdom-Videos haben sonst 0×0).
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage: vi.fn(),
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback) => {
      callback(new Blob(["jpeg-frame"], { type: "image/jpeg" }));
    });
    vi.mocked(HTMLMediaElement.prototype.play).mockImplementation(function (
      this: HTMLMediaElement,
    ) {
      Object.defineProperties(this, {
        videoWidth: { configurable: true, value: 1280 },
        videoHeight: { configurable: true, value: 720 },
      });
      return Promise.resolve();
    });

    const question = "was siehst du?";
    const reply = "Einen Board-Screenshot.";
    getPaTurnMock.mockResolvedValue(turnResponse({ status: "done", reply }));
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [userMessage(question), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    renderChat({ voiceMode: true, sessionEpoch: 3, onController: () => {} });

    fireEvent.click(await screen.findByLabelText("Bildschirm live teilen"));
    await vi.waitFor(() => {
      expect(startLiveShareMock).toHaveBeenCalled();
    });
    expect(await screen.findByText("Teilt Bildschirm")).toBeTruthy();

    const input = screen.getByLabelText("Nachricht an Jarvis");
    fireEvent.change(input, { target: { value: question } });
    fireEvent.click(screen.getByLabelText("Nachricht senden"));

    await vi.waitFor(() => {
      expect(attachLiveShareFrameMock).toHaveBeenCalled();
      expect(sendPaMessageMock).toHaveBeenCalledWith(
        question,
        [{ asset_id: "asset_live99.jpg" }],
        undefined,
      );
    });
    expect(await screen.findByText(reply)).toBeTruthy();
  });

  it("ohne Naht-Props: Senden und Bubble wie bisher (Default-Pfad)", async () => {
    const question = "default pfad";
    const reply = "alles gut";
    getPaTurnMock.mockResolvedValue(turnResponse({ status: "done", reply }));
    sendPaMessageMock.mockImplementation(async () => {
      serverMessages = [userMessage(question), assistantMessage(reply)];
      return { turn_id: "turn_3f9a1c" };
    });
    renderChat();

    const input = await screen.findByLabelText("Nachricht an Jarvis");
    fireEvent.change(input, { target: { value: question } });
    fireEvent.click(screen.getByLabelText("Nachricht senden"));
    expect(await screen.findByText(reply)).toBeTruthy();
    expect(sendPaMessageMock).toHaveBeenCalledWith(question, undefined, undefined);
    expect(speakTextMock).not.toHaveBeenCalled();
  });
});
