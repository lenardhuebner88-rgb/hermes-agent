// @vitest-environment jsdom
/**
 * useLiveShare — beweisbare Invarianten der ECHTEN Live-Screen-Share-Session
 * (Ersatz des alten One-Shot-„Screenshare", der mobil nur den Bild-Picker war):
 *  1. Capability-Gate: getDisplayMedia-Präsenz + kein mobiler Mirage-Browser.
 *  2. start() öffnet getDisplayMedia (Video, kein Audio) + Backend-Session und
 *     streamt Frames; abgebrochene Auswahl (NotAllowedError) bleibt still.
 *  3. Backpressure: genau ein Upload gleichzeitig; ein während des Uploads
 *     erzeugter Frame ersetzt den älteren pending-Frame (latest wins, ältere
 *     verworfen).
 *  4. Cleanup: stop(), das browsereigene track.onended und Unmount stoppen den
 *     Track, melden dem Backend stop und kehren in den Ruhezustand zurück.
 *  5. attachCurrentFrame() materialisiert den Server-Frame → asset_id.
 */
import { act, cleanup, configure, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { screenShareSupported, useLiveShare } from "./useLiveShare";

// S6: Live-Share-Hooks flaky unter Merge-Gate-Last (t_1ccb0734-Churn) —
// nur asyncUtilTimeout/testTimeout scoped anheben.
configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 15_000 });

const startLiveShareMock = vi.hoisted(() => vi.fn());
const uploadLiveShareFrameMock = vi.hoisted(() => vi.fn());
const attachLiveShareFrameMock = vi.hoisted(() => vi.fn());
const stopLiveShareMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  api: {
    startLiveShare: startLiveShareMock,
    uploadLiveShareFrame: uploadLiveShareFrameMock,
    attachLiveShareFrame: attachLiveShareFrameMock,
    stopLiveShare: stopLiveShareMock,
    paAssetUrl: (id: string) => `/api/pa/asset/${id}`,
  },
}));

const DESKTOP_UA =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36";
const ANDROID_UA =
  "Mozilla/5.0 (Linux; Android 15; SM-S928B) AppleWebKit/537.36 Mobile Safari/537.36";

let stopTrack: ReturnType<typeof vi.fn>;
let track: { stop: ReturnType<typeof vi.fn>; onended: (() => void) | null };
let getDisplayMediaMock: ReturnType<typeof vi.fn>;

function setUserAgent(ua: string): void {
  Object.defineProperty(navigator, "userAgent", { configurable: true, value: ua });
}

function installStubs(): void {
  stopTrack = vi.fn();
  track = { stop: stopTrack, onended: null };
  getDisplayMediaMock = vi.fn().mockResolvedValue({
    getTracks: () => [track],
    getVideoTracks: () => [track],
  } as unknown as MediaStream);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getDisplayMedia: getDisplayMediaMock },
  });
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(function (
    this: HTMLMediaElement,
  ) {
    Object.defineProperties(this, {
      videoWidth: { configurable: true, value: 1280 },
      videoHeight: { configurable: true, value: 720 },
    });
    return Promise.resolve();
  });
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    drawImage: vi.fn(),
  } as unknown as CanvasRenderingContext2D);
}

beforeEach(() => {
  vi.clearAllMocks();
  setUserAgent(DESKTOP_UA);
  installStubs();
  startLiveShareMock.mockResolvedValue({ session_id: "live_session01" });
  uploadLiveShareFrameMock.mockResolvedValue({ ok: true });
  attachLiveShareFrameMock.mockResolvedValue({ asset_id: "asset_live.jpg" });
  stopLiveShareMock.mockResolvedValue({ ok: true });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("screenShareSupported (capability gate)", () => {
  it("true on a desktop browser exposing getDisplayMedia", () => {
    expect(screenShareSupported()).toBe(true);
  });

  it("false when getDisplayMedia is absent", () => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {},
    });
    expect(screenShareSupported()).toBe(false);
  });

  it("false on Android even if the symbol is present (known mirage)", () => {
    setUserAgent(ANDROID_UA);
    expect(screenShareSupported()).toBe(false);
  });
});

describe("useLiveShare session lifecycle", () => {
  it("start opens getDisplayMedia (video only), a backend session and streams frames", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb) =>
      cb(new Blob(["frame"], { type: "image/jpeg" })),
    );
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    expect(result.current.supported).toBe(true);

    await act(async () => {
      await result.current.start();
    });

    expect(getDisplayMediaMock).toHaveBeenCalledWith({ video: true, audio: false });
    expect(startLiveShareMock).toHaveBeenCalledTimes(1);
    expect(result.current.active).toBe(true);
    await waitFor(() =>
      expect(uploadLiveShareFrameMock).toHaveBeenCalledWith(
        "live_session01",
        expect.any(Blob),
      ),
    );
  });

  it("backpressure: one upload in flight, newer frame replaces the pending one (latest wins)", async () => {
    vi.useFakeTimers();
    const produced: Blob[] = [];
    let n = 0;
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb) => {
      const blob = new Blob([String(n++)], { type: "image/jpeg" });
      produced.push(blob);
      cb(blob);
    });
    const uploaded: Blob[] = [];
    let releaseFirst: (() => void) | null = null;
    uploadLiveShareFrameMock.mockImplementation((_sid: string, blob: Blob) => {
      uploaded.push(blob);
      if (uploaded.length === 1) {
        return new Promise<{ ok: boolean }>((resolve) => {
          releaseFirst = () => resolve({ ok: true });
        });
      }
      return Promise.resolve({ ok: true });
    });

    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    await act(async () => {
      await result.current.start();
    });
    // frame #0 sampled immediately on start → upload #0 is in flight (hangs).
    expect(uploaded).toHaveLength(1);

    // Two more interval ticks while upload #0 hangs: #1 then #2 → only #2 kept.
    await act(async () => {
      vi.advanceTimersByTime(1000);
      vi.advanceTimersByTime(1000);
    });
    expect(uploaded).toHaveLength(1); // still only #0 uploaded

    await act(async () => {
      releaseFirst?.();
      await Promise.resolve();
    });

    // Exactly two uploads total: frame #0 and the newest pending frame #2.
    // Frame #1 was dropped (latest wins).
    expect(uploaded).toHaveLength(2);
    expect(uploaded[0]).toBe(produced[0]);
    expect(uploaded[1]).toBe(produced[2]);
  });

  it("stop() stops the track, tells the backend, and returns to idle", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb) =>
      cb(new Blob(["frame"], { type: "image/jpeg" })),
    );
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.active).toBe(true);

    act(() => result.current.stop());

    expect(stopTrack).toHaveBeenCalledTimes(1);
    expect(stopLiveShareMock).toHaveBeenCalledWith("live_session01");
    expect(result.current.active).toBe(false);
  });

  it("the browser's own stop (track.onended) tears the session down", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb) =>
      cb(new Blob(["frame"], { type: "image/jpeg" })),
    );
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    await act(async () => {
      await result.current.start();
    });
    expect(typeof track.onended).toBe("function");

    act(() => track.onended?.());

    expect(stopTrack).toHaveBeenCalledTimes(1);
    expect(result.current.active).toBe(false);
  });

  it("track.onended before startLiveShare resolves still stops the leaked backend session", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb) =>
      cb(new Blob(["frame"], { type: "image/jpeg" })),
    );
    // Hang the backend start so the share can end while it is still in flight.
    let resolveStart: (() => void) | null = null;
    startLiveShareMock.mockImplementation(
      () =>
        new Promise<{ session_id: string }>((resolve) => {
          resolveStart = () => resolve({ session_id: "live_session01" });
        }),
    );

    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    let startPromise: Promise<void> = Promise.resolve();
    await act(async () => {
      startPromise = result.current.start();
      // Let start() progress up to (and park on) the pending startLiveShare().
      for (let i = 0; i < 10 && startLiveShareMock.mock.calls.length === 0; i++) {
        await Promise.resolve();
      }
    });
    expect(startLiveShareMock).toHaveBeenCalledTimes(1);

    // The browser/user ends the share BEFORE the backend session id is known:
    // teardown runs with sessionId still null, so it cannot stop anything yet.
    expect(typeof track.onended).toBe("function");
    act(() => track.onended?.());
    expect(stopLiveShareMock).not.toHaveBeenCalled();

    // startLiveShare now resolves → the orphaned server session must be closed.
    await act(async () => {
      resolveStart?.();
      await startPromise;
    });

    expect(stopLiveShareMock).toHaveBeenCalledWith("live_session01");
    expect(result.current.active).toBe(false);
  });

  it("unmount before startLiveShare resolves still stops the leaked backend session", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb) =>
      cb(new Blob(["frame"], { type: "image/jpeg" })),
    );
    let resolveStart: (() => void) | null = null;
    startLiveShareMock.mockImplementation(
      () =>
        new Promise<{ session_id: string }>((resolve) => {
          resolveStart = () => resolve({ session_id: "live_session01" });
        }),
    );

    const { result, unmount } = renderHook(() => useLiveShare({ errorText: "boom" }));
    let startPromise: Promise<void> = Promise.resolve();
    await act(async () => {
      startPromise = result.current.start();
      for (let i = 0; i < 10 && startLiveShareMock.mock.calls.length === 0; i++) {
        await Promise.resolve();
      }
    });
    expect(startLiveShareMock).toHaveBeenCalledTimes(1);

    // Unmount while the backend session id is still unknown.
    unmount();
    expect(stopLiveShareMock).not.toHaveBeenCalled();

    await act(async () => {
      resolveStart?.();
      await startPromise;
    });

    expect(stopLiveShareMock).toHaveBeenCalledWith("live_session01");
  });

  it("unmount tears the session down (no leaked stream / interval)", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb) =>
      cb(new Blob(["frame"], { type: "image/jpeg" })),
    );
    const { result, unmount } = renderHook(() => useLiveShare({ errorText: "boom" }));
    await act(async () => {
      await result.current.start();
    });

    unmount();

    expect(stopTrack).toHaveBeenCalledTimes(1);
    expect(stopLiveShareMock).toHaveBeenCalledWith("live_session01");
  });

  it("a cancelled OS picker (NotAllowedError) stays silent, no error, no session", async () => {
    getDisplayMediaMock.mockRejectedValueOnce(
      new DOMException("cancelled", "NotAllowedError"),
    );
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.active).toBe(false);
    expect(result.current.error).toBeNull();
    expect(startLiveShareMock).not.toHaveBeenCalled();
  });

  it("a real getDisplayMedia failure surfaces the honest error text", async () => {
    getDisplayMediaMock.mockRejectedValueOnce(
      new DOMException("no device", "NotFoundError"),
    );
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.active).toBe(false);
    expect(result.current.error).toBe("boom");
  });

  it("attachCurrentFrame materialises the current frame into an asset id", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb) =>
      cb(new Blob(["frame"], { type: "image/jpeg" })),
    );
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    await act(async () => {
      await result.current.start();
    });
    let assetId: string | null = null;
    await act(async () => {
      assetId = await result.current.attachCurrentFrame();
    });
    expect(attachLiveShareFrameMock).toHaveBeenCalledWith("live_session01");
    expect(assetId).toBe("asset_live.jpg");
  });

  // ── S4-Härtung: Upload-Fehlerbudget (3 aufeinanderfolgende, Reset bei Erfolg)

  it("tolerates transient upload failures and recovers on the next good frame", async () => {
    vi.useFakeTimers();
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb) =>
      cb(new Blob(["frame"], { type: "image/jpeg" })),
    );
    uploadLiveShareFrameMock
      .mockRejectedValueOnce(new Error("flap 1"))
      .mockRejectedValueOnce(new Error("flap 2"))
      .mockResolvedValue({ ok: true });

    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    await act(async () => {
      await result.current.start();
    });
    // Frame #0 (immediate sample) failed — a single transient error must NOT
    // kill the share.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.error).toBeNull();

    // Frame #1 fails too (second consecutive) — still within the budget.
    await act(async () => {
      vi.advanceTimersByTime(1_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.error).toBeNull();
    expect(result.current.active).toBe(false);

    // Frame #2 succeeds → the share goes live, no error ever surfaced.
    await act(async () => {
      vi.advanceTimersByTime(1_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.active).toBe(true);
    expect(result.current.error).toBeNull();
    expect(stopLiveShareMock).not.toHaveBeenCalled();
  });

  it("fails closed only after three consecutive upload failures", async () => {
    vi.useFakeTimers();
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb) =>
      cb(new Blob(["frame"], { type: "image/jpeg" })),
    );
    uploadLiveShareFrameMock.mockRejectedValue(new Error("upload down"));

    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    await act(async () => {
      await result.current.start();
    });
    // Failure #1 (immediate frame) — tolerated.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.error).toBeNull();
    // Failure #2 — tolerated.
    await act(async () => {
      vi.advanceTimersByTime(1_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.error).toBeNull();
    // Failure #3 — budget exhausted: fail closed with the honest error.
    await act(async () => {
      vi.advanceTimersByTime(1_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.error).toBe("boom");
    expect(result.current.active).toBe(false);
    expect(stopLiveShareMock).toHaveBeenCalledWith("live_session01");
  });

  // ── S6.5: Frame-Age-Indikator (lastFrameAt)

  it("lastFrameAt is null before start and set after a successful frame upload", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb) =>
      cb(new Blob(["frame"], { type: "image/jpeg" })),
    );
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    expect(result.current.lastFrameAt).toBeNull();

    await act(async () => {
      await result.current.start();
    });

    await waitFor(() => expect(result.current.lastFrameAt).not.toBeNull());
    expect(typeof result.current.lastFrameAt).toBe("number");
  });

  it("lastFrameAt resets to null after stop()", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb) =>
      cb(new Blob(["frame"], { type: "image/jpeg" })),
    );
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    await act(async () => {
      await result.current.start();
    });
    await waitFor(() => expect(result.current.lastFrameAt).not.toBeNull());

    act(() => result.current.stop());

    expect(result.current.lastFrameAt).toBeNull();
  });
});
