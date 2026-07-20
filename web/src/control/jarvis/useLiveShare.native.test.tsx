// @vitest-environment jsdom
/**
 * useLiveShare — NATIVE Android capture path (window.HermesNative bridge).
 * Proves that inside the native hull the hook drives the shell's MediaProjection
 * capture instead of getDisplayMedia, while reusing the SAME backend live-share
 * session + backpressure uploader:
 *  1. Capability: `supported` is false until native_capabilities{screen_capture}
 *     arrives, then true — even on an Android UA where getDisplayMedia is a mirage.
 *  2. Start: start() sends start_screen_capture and does NOT open a backend
 *     session until native confirms (screen_capture_started) — a cancelled
 *     Android dialog leaks no PA session.
 *  3. Frames: native screen_frame JPEGs are decoded and streamed to /frame.
 *  4. Stop / system-stop / error: full teardown, stop_screen_capture echoed only
 *     when WE initiate; no frame uploads after a visible stop; restart works.
 */
import { act, cleanup, configure, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { __resetNativeCaptureBridgeForTest } from "./nativeCaptureBridge";
import { useLiveShare } from "./useLiveShare";

// S6: Native-Capture-Pfad unter Voll-Suite-Last — scoped Timeouts (t_1ccb0734).
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
  },
}));

const ANDROID_UA =
  "Mozilla/5.0 (Linux; Android 15; SM-S928B) AppleWebKit/537.36 Mobile Safari/537.36";

let posted: Record<string, unknown>[];
let emit: (message: Record<string, unknown>) => void;

function installBridge(): void {
  posted = [];
  let listener: ((event: { data: string }) => void) | null = null;
  (window as unknown as { HermesNative?: unknown }).HermesNative = {
    postMessage: (message: string) => posted.push(JSON.parse(message) as Record<string, unknown>),
    addEventListener: (_type: string, fn: (event: { data: string }) => void) => {
      listener = fn;
    },
  };
  emit = (message) => listener?.({ data: JSON.stringify(message) });
}

function setUserAgent(ua: string): void {
  Object.defineProperty(navigator, "userAgent", { configurable: true, value: ua });
}

/** Render, announce native capability, start, confirm capture + first frame → "sharing". */
async function reachSharing(errorText = "boom") {
  const rendered = renderHook(() => useLiveShare({ errorText }));
  const { result } = rendered;
  act(() => emit({ v: 1, type: "native_capabilities", screen_capture: true, phone_action: true }));
  await act(async () => {
    await result.current.start();
  });
  await act(async () => {
    emit({ v: 1, type: "screen_capture_started" });
    await Promise.resolve();
    await Promise.resolve();
  });
  await act(async () => {
    emit({ v: 1, type: "screen_frame", data: "QUJDRA==" });
  });
  await waitFor(() => expect(result.current.active).toBe(true));
  return rendered;
}

beforeEach(() => {
  vi.clearAllMocks();
  __resetNativeCaptureBridgeForTest();
  setUserAgent(ANDROID_UA);
  // getDisplayMedia is a mirage on Android — the native bridge is the only path.
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: {} });
  installBridge();
  startLiveShareMock.mockResolvedValue({ session_id: "live_native01" });
  uploadLiveShareFrameMock.mockResolvedValue({ ok: true });
  attachLiveShareFrameMock.mockResolvedValue({ asset_id: "asset_native.jpg" });
  stopLiveShareMock.mockResolvedValue({ ok: true });
});

afterEach(() => {
  cleanup();
  delete (window as unknown as { HermesNative?: unknown }).HermesNative;
  vi.restoreAllMocks();
});

describe("native capability gate", () => {
  it("supported only after native_capabilities, even on Android UA", () => {
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    expect(posted).toContainEqual({ v: 1, type: "bridge_ready" });
    expect(result.current.supported).toBe(false);

    act(() => emit({ v: 1, type: "native_capabilities", screen_capture: true }));
    expect(result.current.supported).toBe(true);
  });
});

describe("native capture lifecycle", () => {
  it("start requests native capture but reports sharing only after a usable uploaded frame", async () => {
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    act(() => emit({ v: 1, type: "native_capabilities", screen_capture: true }));

    await act(async () => {
      await result.current.start();
    });
    expect(posted).toContainEqual({ v: 1, type: "start_screen_capture" });
    // No backend session yet — native has not confirmed the capture.
    expect(startLiveShareMock).not.toHaveBeenCalled();
    expect(result.current.active).toBe(false);

    await act(async () => {
      emit({ v: 1, type: "screen_capture_started" });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(startLiveShareMock).toHaveBeenCalledTimes(1);
    expect(result.current.active).toBe(false);

    act(() => emit({ v: 1, type: "screen_frame", data: "QUJDRA==" }));
    await waitFor(() => expect(result.current.active).toBe(true));
    expect(uploadLiveShareFrameMock).toHaveBeenCalledWith("live_native01", expect.any(Blob));
  });

  it("buffers a frame that races ahead of the backend session and uploads it once ready", async () => {
    let resolveSession!: (value: { session_id: string }) => void;
    startLiveShareMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveSession = resolve;
      }),
    );
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    act(() => emit({ v: 1, type: "native_capabilities", screen_capture: true }));
    await act(async () => result.current.start());

    act(() => {
      emit({ v: 1, type: "screen_capture_started" });
      emit({ v: 1, type: "screen_frame", data: "QUJDRA==" });
    });
    expect(uploadLiveShareFrameMock).not.toHaveBeenCalled();
    expect(result.current.active).toBe(false);

    await act(async () => resolveSession({ session_id: "live_native01" }));
    await waitFor(() => expect(uploadLiveShareFrameMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(result.current.active).toBe(true));
  });

  it("duplicate start while native permission is pending requests capture only once", async () => {
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    act(() => emit({ v: 1, type: "native_capabilities", screen_capture: true }));
    await act(async () => {
      await Promise.all([result.current.start(), result.current.start()]);
    });
    expect(posted.filter((message) => message.type === "start_screen_capture")).toHaveLength(1);
  });

  it("native screen_frame JPEGs are decoded and streamed to the backend /frame", async () => {
    const { result } = await reachSharing();
    await act(async () => {
      emit({ v: 1, type: "screen_frame", data: "QUJDRA==" });
    });
    await waitFor(() =>
      expect(uploadLiveShareFrameMock).toHaveBeenCalledWith("live_native01", expect.any(Blob)),
    );
    // The Blob is a real JPEG-typed image, ready for the existing image-turn pipeline.
    const [, blob] = uploadLiveShareFrameMock.mock.calls[0] as [string, Blob];
    expect(blob.type).toBe("image/jpeg");
    void result;
  });

  it("stop tells native + backend and drops any later frame (no upload after stop)", async () => {
    const { result } = await reachSharing();
    posted.length = 0;

    act(() => result.current.stop());
    expect(posted).toContainEqual({ v: 1, type: "stop_screen_capture" });
    expect(stopLiveShareMock).toHaveBeenCalledWith("live_native01");
    expect(result.current.active).toBe(false);

    uploadLiveShareFrameMock.mockClear();
    act(() => emit({ v: 1, type: "screen_frame", data: "QUJDRA==" }));
    await Promise.resolve();
    expect(uploadLiveShareFrameMock).not.toHaveBeenCalled();
  });

  it("Android system stop tears down without echoing stop_screen_capture", async () => {
    const { result } = await reachSharing();
    posted.length = 0;

    await act(async () => {
      emit({ v: 1, type: "screen_capture_stopped", reason: "user" });
      await Promise.resolve();
    });
    expect(result.current.active).toBe(false);
    // Native already stopped — we must NOT echo stop back (ping-pong guard).
    expect(posted).not.toContainEqual({ v: 1, type: "stop_screen_capture" });
    expect(stopLiveShareMock).toHaveBeenCalledWith("live_native01");
  });

  it("a cancelled permission dialog leaves no active state and no leaked PA session", async () => {
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    act(() => emit({ v: 1, type: "native_capabilities", screen_capture: true }));
    await act(async () => {
      await result.current.start();
    });
    // User dismisses the Android system dialog → native reports stopped, never started.
    await act(async () => {
      emit({ v: 1, type: "screen_capture_stopped", reason: "user" });
      await Promise.resolve();
    });
    expect(startLiveShareMock).not.toHaveBeenCalled();
    expect(result.current.active).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("a native capture error surfaces the honest error text and leaks nothing", async () => {
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    act(() => emit({ v: 1, type: "native_capabilities", screen_capture: true }));
    await act(async () => {
      await result.current.start();
    });
    await act(async () => {
      emit({ v: 1, type: "screen_capture_error", code: "busy", message: "in use" });
      await Promise.resolve();
    });
    expect(result.current.active).toBe(false);
    expect(result.current.error).toBe("boom");
    expect(startLiveShareMock).not.toHaveBeenCalled();
  });

  it("a single transient upload failure no longer kills the share (S4 budget)", async () => {
    uploadLiveShareFrameMock.mockRejectedValueOnce(new Error("flap"));
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    act(() => emit({ v: 1, type: "native_capabilities", screen_capture: true }));
    await act(async () => result.current.start());
    await act(async () => {
      emit({ v: 1, type: "screen_capture_started" });
      await Promise.resolve();
      await Promise.resolve();
      emit({ v: 1, type: "screen_frame", data: "QUJDRA==" });
      await Promise.resolve();
      await Promise.resolve();
    });

    // Tolerated: no error, no teardown — the next good frame goes live.
    expect(result.current.error).toBeNull();
    expect(stopLiveShareMock).not.toHaveBeenCalled();

    act(() => emit({ v: 1, type: "screen_frame", data: "QUJDRA==" }));
    await waitFor(() => expect(result.current.active).toBe(true));
    expect(result.current.error).toBeNull();
  });

  it("fails closed only after three consecutive upload failures (S4 budget)", async () => {
    uploadLiveShareFrameMock.mockRejectedValue(new Error("upload down"));
    const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
    act(() => emit({ v: 1, type: "native_capabilities", screen_capture: true }));
    await act(async () => result.current.start());
    await act(async () => {
      emit({ v: 1, type: "screen_capture_started" });
      await Promise.resolve();
      await Promise.resolve();
    });

    // Failures #1 and #2 are tolerated (each native frame = one upload attempt).
    for (let i = 0; i < 2; i += 1) {
      await act(async () => {
        emit({ v: 1, type: "screen_frame", data: "QUJDRA==" });
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(result.current.error).toBeNull();
    }
    // Failure #3 exhausts the budget → fail closed, teardown incl. native stop.
    await act(async () => {
      emit({ v: 1, type: "screen_frame", data: "QUJDRA==" });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.error).toBe("boom");
    expect(result.current.active).toBe(false);
    expect(posted).toContainEqual({ v: 1, type: "stop_screen_capture" });
    expect(stopLiveShareMock).toHaveBeenCalledWith("live_native01");
  });

  it("times out a native start whose bridge never answers (S4 start watchdog)", async () => {
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
      act(() => emit({ v: 1, type: "native_capabilities", screen_capture: true }));
      await act(async () => {
        await result.current.start();
      });
      expect(posted).toContainEqual({ v: 1, type: "start_screen_capture" });
      expect(result.current.status).toBe("starting");

      // The bridge never emits started/stopped/error → 12 s watchdog fires:
      // back to idle with the honest error, wedged native side stopped.
      act(() => vi.advanceTimersByTime(12_001));
      expect(result.current.status).toBe("idle");
      expect(result.current.error).toBe("boom");
      expect(posted).toContainEqual({ v: 1, type: "stop_screen_capture" });
      expect(startLiveShareMock).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("times out a backend session that never receives a usable first frame", async () => {
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useLiveShare({ errorText: "boom" }));
      act(() => emit({ v: 1, type: "native_capabilities", screen_capture: true }));
      await act(async () => result.current.start());
      await act(async () => {
        emit({ v: 1, type: "screen_capture_started" });
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(result.current.active).toBe(false);

      act(() => vi.advanceTimersByTime(8_001));
      expect(result.current.error).toBe("boom");
      expect(result.current.active).toBe(false);
      expect(posted).toContainEqual({ v: 1, type: "stop_screen_capture" });
      expect(stopLiveShareMock).toHaveBeenCalledWith("live_native01");
    } finally {
      vi.useRealTimers();
    }
  });

  it("restart after stop works (fresh capture + backend session)", async () => {
    const { result } = await reachSharing();
    act(() => result.current.stop());
    expect(result.current.active).toBe(false);

    startLiveShareMock.mockResolvedValue({ session_id: "live_native02" });
    await act(async () => {
      await result.current.start();
    });
    await act(async () => {
      emit({ v: 1, type: "screen_capture_started" });
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => emit({ v: 1, type: "screen_frame", data: "QUJDRA==" }));
    await waitFor(() => expect(result.current.active).toBe(true));
    expect(stopLiveShareMock).toHaveBeenCalledWith("live_native01");

    act(() => emit({ v: 1, type: "screen_frame", data: "QUJDRA==" }));
    await waitFor(() =>
      expect(uploadLiveShareFrameMock).toHaveBeenCalledWith("live_native02", expect.any(Blob)),
    );
  });

  it("attachCurrentFrame materialises the native session's current frame", async () => {
    const { result } = await reachSharing();
    let assetId: string | null = null;
    await act(async () => {
      assetId = await result.current.attachCurrentFrame();
    });
    expect(attachLiveShareFrameMock).toHaveBeenCalledWith("live_native01");
    expect(assetId).toBe("asset_native.jpg");
  });

  it("attach failure fails closed and surfaces the bounded error", async () => {
    const { result } = await reachSharing();
    attachLiveShareFrameMock.mockRejectedValueOnce(new Error("no current frame"));
    let assetId: string | null = "not-null";
    await act(async () => {
      assetId = await result.current.attachCurrentFrame();
    });
    expect(assetId).toBeNull();
    expect(result.current.active).toBe(false);
    expect(result.current.error).toBe("boom");
    expect(stopLiveShareMock).toHaveBeenCalledWith("live_native01");
  });

  it("attach waits for an in-flight newer frame before materialising the asset", async () => {
    const { result } = await reachSharing();
    let finishUpload!: () => void;
    uploadLiveShareFrameMock.mockReturnValueOnce(
      new Promise<void>((resolve) => {
        finishUpload = resolve;
      }),
    );
    act(() => emit({ v: 1, type: "screen_frame", data: "RkVSU0g=" }));

    let attached!: Promise<string | null>;
    act(() => {
      attached = result.current.attachCurrentFrame();
    });
    expect(attachLiveShareFrameMock).not.toHaveBeenCalled();
    await act(async () => finishUpload());
    await expect(attached).resolves.toBe("asset_native.jpg");
    expect(attachLiveShareFrameMock).toHaveBeenCalledWith("live_native01");
  });
});
