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
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { __resetNativeCaptureBridgeForTest } from "./nativeCaptureBridge";
import { useLiveShare } from "./useLiveShare";

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

/** Render, announce native capability, start, confirm capture → "sharing". */
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
  it("start requests native capture and opens the backend session only after confirmation", async () => {
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
    await waitFor(() => expect(result.current.active).toBe(true));
    expect(startLiveShareMock).toHaveBeenCalledTimes(1);
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
});
