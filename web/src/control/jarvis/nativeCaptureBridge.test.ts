// @vitest-environment jsdom
/**
 * nativeCaptureBridge — provable invariants of the Jarvis native screen-capture
 * client (Bridge Protocol v1 over window.HermesNative):
 *  1. Handshake: init sends exactly one bridge_ready when the bridge exists, and
 *     is a silent no-op when it is absent (desktop / plain browser).
 *  2. Capability discovery: native_capabilities{screen_capture:true} flips
 *     availability and notifies subscribers; false/malformed does not.
 *  3. Capture control: start sends start_screen_capture; native events reach the
 *     registered handlers; stop detaches handlers and (optionally) notifies.
 *  4. Robustness: bad JSON, wrong version, unknown types are ignored.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  __resetNativeCaptureBridgeForTest,
  initNativeCaptureBridge,
  nativeScreenCaptureAvailable,
  startNativeScreenCapture,
  stopNativeScreenCapture,
  subscribeNativeCaptureAvailability,
  type NativeCaptureHandlers,
} from "./nativeCaptureBridge";

function noopHandlers(over: Partial<NativeCaptureHandlers> = {}): NativeCaptureHandlers {
  return {
    onStarted: vi.fn(),
    onFrame: vi.fn(),
    onStopped: vi.fn(),
    onError: vi.fn(),
    ...over,
  };
}

let posted: Record<string, unknown>[];
let emit: (message: Record<string, unknown> | string) => void;

function install(): void {
  posted = [];
  let listener: ((event: { data: string }) => void) | null = null;
  (window as unknown as { HermesNative?: unknown }).HermesNative = {
    postMessage: (message: string) => posted.push(JSON.parse(message) as Record<string, unknown>),
    addEventListener: (_type: string, fn: (event: { data: string }) => void) => {
      listener = fn;
    },
  };
  emit = (message) =>
    listener?.({ data: typeof message === "string" ? message : JSON.stringify(message) });
}

beforeEach(() => {
  __resetNativeCaptureBridgeForTest();
  delete (window as unknown as { HermesNative?: unknown }).HermesNative;
});

afterEach(() => {
  delete (window as unknown as { HermesNative?: unknown }).HermesNative;
  vi.restoreAllMocks();
});

describe("initNativeCaptureBridge handshake", () => {
  it("sends exactly one bridge_ready when the native bridge exists", () => {
    install();
    initNativeCaptureBridge();
    expect(posted).toEqual([{ v: 1, type: "bridge_ready" }]);
  });

  it("is idempotent — a second init does not re-handshake", () => {
    install();
    initNativeCaptureBridge();
    initNativeCaptureBridge();
    expect(posted).toEqual([{ v: 1, type: "bridge_ready" }]);
  });

  it("is a silent no-op when no bridge is present", () => {
    initNativeCaptureBridge();
    expect(nativeScreenCaptureAvailable()).toBe(false);
  });
});

describe("capability discovery", () => {
  it("flips availability and notifies subscribers on screen_capture:true", () => {
    install();
    const seen: boolean[] = [];
    subscribeNativeCaptureAvailability((available) => seen.push(available));
    initNativeCaptureBridge();
    expect(nativeScreenCaptureAvailable()).toBe(false);

    emit({ v: 1, type: "native_capabilities", screen_capture: true, phone_action: true });
    expect(nativeScreenCaptureAvailable()).toBe(true);
    expect(seen).toEqual([true]);
  });

  it("does not flip availability when screen_capture is false or absent", () => {
    install();
    initNativeCaptureBridge();
    emit({ v: 1, type: "native_capabilities", phone_action: true });
    expect(nativeScreenCaptureAvailable()).toBe(false);
  });

  it("unsubscribe stops further notifications", () => {
    install();
    const seen: boolean[] = [];
    const unsubscribe = subscribeNativeCaptureAvailability((a) => seen.push(a));
    initNativeCaptureBridge();
    unsubscribe();
    emit({ v: 1, type: "native_capabilities", screen_capture: true });
    expect(seen).toEqual([]);
    expect(nativeScreenCaptureAvailable()).toBe(true);
  });
});

describe("capture control + native events", () => {
  it("start sends start_screen_capture and routes native events to handlers", () => {
    install();
    initNativeCaptureBridge();
    const handlers = noopHandlers();
    startNativeScreenCapture(handlers);
    expect(posted).toContainEqual({ v: 1, type: "start_screen_capture" });

    emit({ v: 1, type: "screen_capture_started" });
    expect(handlers.onStarted).toHaveBeenCalledTimes(1);

    emit({ v: 1, type: "screen_frame", data: "QUJD" });
    expect(handlers.onFrame).toHaveBeenCalledWith("QUJD");

    emit({ v: 1, type: "screen_capture_error", code: "busy", message: "nope" });
    expect(handlers.onError).toHaveBeenCalledWith("busy", "nope");

    emit({ v: 1, type: "screen_capture_stopped", reason: "user" });
    expect(handlers.onStopped).toHaveBeenCalledWith("user");
  });

  it("stop detaches handlers and sends stop_screen_capture by default", () => {
    install();
    initNativeCaptureBridge();
    const handlers = noopHandlers();
    startNativeScreenCapture(handlers);
    stopNativeScreenCapture();
    expect(posted).toContainEqual({ v: 1, type: "stop_screen_capture" });

    // A late frame after stop must not reach the detached handlers.
    emit({ v: 1, type: "screen_frame", data: "QUJD" });
    expect(handlers.onFrame).not.toHaveBeenCalled();
  });

  it("stop with notifyNative:false does not echo stop_screen_capture", () => {
    install();
    initNativeCaptureBridge();
    startNativeScreenCapture(noopHandlers());
    posted.length = 0;
    stopNativeScreenCapture({ notifyNative: false });
    expect(posted).toEqual([]);
  });
});

describe("robustness", () => {
  it("ignores bad JSON, wrong version and unknown types", () => {
    install();
    initNativeCaptureBridge();
    const handlers = noopHandlers();
    startNativeScreenCapture(handlers);
    emit("}{ not json");
    emit({ v: 2, type: "screen_frame", data: "x" });
    emit({ v: 1, type: "totally_unknown" });
    expect(handlers.onFrame).not.toHaveBeenCalled();
    expect(handlers.onStarted).not.toHaveBeenCalled();
  });
});
