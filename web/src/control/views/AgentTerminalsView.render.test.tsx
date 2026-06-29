// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AgentTerminalCapabilityState, AgentTerminalWindow } from "@/lib/api";

const apiMock = {
  getAgentTerminalCapabilities: vi.fn(),
  getAgentTerminalWindows: vi.fn(),
  ensureAgentTerminalWindow: vi.fn(),
};
const fitFitMock = vi.fn();
let triggerResize: (() => void) | null = null;

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: apiMock,
    buildWsUrl: vi.fn().mockResolvedValue("ws://example.test/attach"),
  };
});

vi.mock("@xterm/xterm", () => ({ Terminal: class Terminal {} }));
vi.mock("@/lib/xtermSurface", () => ({
  TERMINAL_THEME_STATIC: {},
  createHermesXtermSurface: vi.fn(() => ({
    term: {
      clear: vi.fn(),
      writeln: vi.fn(),
      write: vi.fn(),
      onData: vi.fn(() => ({ dispose: vi.fn() })),
      dispose: vi.fn(),
    },
    fit: { fit: fitFitMock },
  })),
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
  send = vi.fn();
  close = vi.fn(() => this.onclose?.());
}

const capability: AgentTerminalCapabilityState = {
  tmux_available: true,
  hermes_tui_available: true,
  hermes_binary: "/usr/bin/hermes",
  reason: null,
};

const windows: AgentTerminalWindow[] = [
  { session: "hermes-agents", window: "hermes", active: true, pane_id: "%1", pid: 111, command: "hermes" },
  { session: "hermes-agents", window: "codex", active: false, pane_id: "%2", pid: 222, command: "codex" },
];

async function loadView() {
  const module = await import("./AgentTerminalsView");
  return module.AgentTerminalsView;
}

function installDom(matches = false) {
  triggerResize = null;
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
  global.ResizeObserver = class ResizeObserver {
    constructor(callback: ResizeObserverCallback) {
      triggerResize = () => callback([] as ResizeObserverEntry[], this as unknown as ResizeObserver);
    }
    observe = vi.fn();
    unobserve = vi.fn();
    disconnect = vi.fn();
  } as unknown as typeof ResizeObserver;
  global.requestAnimationFrame = (cb: FrameRequestCallback) => window.setTimeout(() => cb(0), 0);
  global.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
}

beforeEach(() => {
  vi.clearAllMocks();
  installDom(false);
  apiMock.getAgentTerminalCapabilities.mockResolvedValue(capability);
  apiMock.getAgentTerminalWindows.mockResolvedValue({ windows });
  apiMock.ensureAgentTerminalWindow.mockImplementation(async (kind: string) => ({ window: windows.find((w) => w.window === kind) ?? windows[0] }));
});

afterEach(() => {
  cleanup();
});

describe("AgentTerminalsView rendering", () => {
  it("renders the desktop three-column terminal shell and switches sessions", async () => {
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);

    expect(await screen.findByText("Sessions / Windows")).not.toBeNull();
    expect(screen.getByText("Terminal-Kontext")).not.toBeNull();
    fireEvent.click((await screen.findAllByText("codex"))[0]);
    expect(screen.getAllByText("hermes-agents:codex").length).toBeGreaterThan(0);
  });

  it("renders mobile switcher actions and the tools bottom sheet", async () => {
    installDom(true);
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);

    expect((await screen.findAllByText("Agent Terminals")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByRole("button", { name: /Tools/i })[0]);
    expect(screen.getAllByText("Terminal-Kontext").length).toBeGreaterThan(0);
  });

  it("fits the terminal on mount and when its host is resized", async () => {
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);

    expect(await screen.findByText("Sessions / Windows")).not.toBeNull();
    await waitFor(() => expect(fitFitMock).toHaveBeenCalled());
    const callsAfterMount = fitFitMock.mock.calls.length;

    triggerResize?.();

    expect(fitFitMock).toHaveBeenCalledTimes(callsAfterMount + 1);
  });

  it("renders empty and error states", async () => {
    apiMock.getAgentTerminalWindows.mockResolvedValueOnce({ windows: [] });
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);
    expect(await screen.findByText(/Kein tmux-Fenster verfügbar/)).not.toBeNull();

    cleanup();
    apiMock.getAgentTerminalCapabilities.mockRejectedValueOnce(new Error("backend offline"));
    apiMock.getAgentTerminalWindows.mockResolvedValueOnce({ windows: [] });
    render(<AgentTerminalsView />);
    await waitFor(() => expect(screen.getByText(/backend offline/)).not.toBeNull());
  });
});
