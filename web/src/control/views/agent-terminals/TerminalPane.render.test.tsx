// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";

import { formatPtyResize } from "./terminalHelpers";

const fitFitMock = vi.fn();
const attachTouchScrollBridgeMock = vi.fn(
  (_options: { host: HTMLElement; term: unknown; send: (data: string) => boolean }) => vi.fn(),
);
let websocketSends: string[] = [];

vi.mock("@/lib/api", () => ({
  buildWsUrl: vi.fn().mockResolvedValue("ws://example.test/attach"),
}));

vi.mock("@xterm/xterm", () => ({ Terminal: class Terminal {} }));

vi.mock("@/lib/xtermSurface", () => ({
  TERMINAL_THEME_STATIC: {},
  attachTouchScrollBridge: attachTouchScrollBridgeMock,
  // Non-hex placeholder — design-token ratchet counts raw hex in web/src/control.
  TERMINAL_PANE_BACKGROUND: "transparent",
  createHermesXtermSurface: vi.fn(({ host }: { host: HTMLElement }) => {
    host.dataset.terminalSurface = "1";
    return {
      term: {
        cols: 80,
        rows: 24,
        write: vi.fn(),
        writeln: vi.fn(),
        reset: vi.fn(),
        clear: vi.fn(),
        focus: vi.fn(),
        dispose: vi.fn(),
        onData: vi.fn(() => ({ dispose: vi.fn() })),
        options: {},
        scrollLines: vi.fn(),
        scrollPages: vi.fn(),
        scrollToBottom: vi.fn(),
        getSelection: () => "",
        buffer: {
          active: {
            type: "normal",
            length: 0,
            getLine: () => undefined,
          },
        },
      },
      fit: { fit: fitFitMock },
    };
  }),
}));

class FakeWebSocket {
  static OPEN = 1;
  readyState = FakeWebSocket.OPEN;
  binaryType = "";
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  constructor() {
    setTimeout(() => this.onopen?.(), 0);
  }
  send = vi.fn((data: string) => {
    websocketSends.push(data);
  });
  close = vi.fn(() => this.onclose?.());
}

describe("TerminalPane resize wire format", () => {
  beforeEach(() => {
    websocketSends = [];
    fitFitMock.mockReset();
    attachTouchScrollBridgeMock.mockClear();
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get() {
        return 360;
      },
    });
    Object.defineProperty(HTMLElement.prototype, "clientHeight", {
      configurable: true,
      get() {
        return 480;
      },
    });
    global.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    global.ResizeObserver = class ResizeObserver {
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    } as unknown as typeof ResizeObserver;
  });

  afterEach(() => {
    cleanup();
  });

  it("sends the shared CSI formatPtyResize sequence on attach open", async () => {
    const { TerminalPane } = await import("./TerminalPane");
    render(
      <TerminalPane
        target={{ session: "work", window: "claude" }}
        paneOrder={1}
        fontSize={12}
        isolated
      />,
    );

    await waitFor(() => {
      expect(websocketSends.length).toBeGreaterThan(0);
    });

    // Assert against the imported helper — not a hand-written literal — so the
    // pane stays locked to the same wire format the backend parser accepts.
    const expected = formatPtyResize(80, 24);
    expect(websocketSends).toContain(expected);
    // CSI shape: ESC [ RESIZE:cols;rows ]
    expect(expected.startsWith("\u001b[RESIZE:")).toBe(true);
    expect(expected.endsWith("]")).toBe(true);
    expect(expected).toContain("80;24");
    // Must not use the old OSC 777 format that never matched the backend parser.
    expect(websocketSends.some((s) => s.includes("777;RESIZE"))).toBe(false);
  });

  it("wires the touch-drag scroll bridge to its own host and socket", async () => {
    const { TerminalPane } = await import("./TerminalPane");
    const { unmount } = render(
      <TerminalPane
        target={{ session: "work", window: "codex" }}
        paneOrder={1}
        fontSize={12}
        isolated
      />,
    );

    // Without this the extra panes offer no way to reach tmux scrollback on a
    // phone — the primary pane scrolls and these silently do not.
    expect(attachTouchScrollBridgeMock).toHaveBeenCalledTimes(1);
    const args = attachTouchScrollBridgeMock.mock.calls[0][0];
    expect(args.host).toBeInstanceOf(HTMLElement);

    await waitFor(() => {
      expect(websocketSends.length).toBeGreaterThan(0);
    });
    const before = websocketSends.length;
    expect(args.send("[<64;1;1M")).toBe(true);
    expect(websocketSends.length).toBe(before + 1);
    expect(websocketSends[websocketSends.length - 1]).toBe("[<64;1;1M");

    const dispose = attachTouchScrollBridgeMock.mock.results[0].value;
    unmount();
    expect(dispose).toHaveBeenCalled();
  });
});
