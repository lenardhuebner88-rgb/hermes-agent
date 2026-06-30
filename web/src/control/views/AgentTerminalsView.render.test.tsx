// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AgentTerminalCapabilityState, AgentTerminalWindow } from "@/lib/api";

const apiMock = {
  getAgentTerminalCapabilities: vi.fn(),
  getAgentTerminalWindows: vi.fn(),
  ensureAgentTerminalWindow: vi.fn(),
  getSkills: vi.fn(),
  getToolsets: vi.fn(),
  getControlOverviewHealth: vi.fn(),
  getControlOverviewVault: vi.fn(),
  getControlOverviewKanbanBoard: vi.fn(),
  getControlOverviewDecisionQueue: vi.fn(),
};
const fitFitMock = vi.fn();
const terminalScrollLinesMock = vi.fn();
const terminalScrollPagesMock = vi.fn();
const terminalScrollToBottomMock = vi.fn();
const terminalFocusMock = vi.fn();
let triggerResize: (() => void) | null = null;
let websocketSends: string[] = [];

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
      scrollLines: terminalScrollLinesMock,
      scrollPages: terminalScrollPagesMock,
      scrollToBottom: terminalScrollToBottomMock,
      focus: terminalFocusMock,
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
  send = vi.fn((data: string) => {
    websocketSends.push(data);
  });
  close = vi.fn(() => this.onclose?.());
}

const capability: AgentTerminalCapabilityState = {
  tmux_available: true,
  hermes_tui_available: true,
  hermes_binary: "/usr/bin/hermes",
  reason: null,
};

const windows: AgentTerminalWindow[] = [
  { session: "hermes-agents", window: "hermes", active: true, pane_id: "%1", pid: 111, command: "hermes", cwd: "/home/piet" },
  { session: "hermes-agents", window: "codex", active: false, pane_id: "%2", pid: 222, command: "node", cwd: "/home/piet/.hermes/hermes-agent" },
];

async function loadView() {
  const module = await import("./AgentTerminalsView");
  return module.AgentTerminalsView;
}

function installDom(matches = false) {
  triggerResize = null;
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
  websocketSends = [];
  installDom(false);
  apiMock.getAgentTerminalCapabilities.mockResolvedValue(capability);
  apiMock.getAgentTerminalWindows.mockResolvedValue({ windows });
  apiMock.ensureAgentTerminalWindow.mockImplementation(async (kind: string) => ({ window: windows.find((w) => w.window === kind) ?? windows[0] }));
  apiMock.getSkills.mockResolvedValue([
    { name: "firecrawl-search", description: "Search with Firecrawl", category: "web", enabled: true },
    { name: "gmail", description: "Gmail inbox triage", category: "productivity", enabled: false },
  ]);
  apiMock.getToolsets.mockResolvedValue([
    { name: "browser", label: "Browser", description: "Browser automation", enabled: true, configured: true, tools: ["browser_navigate"] },
    { name: "kanban", label: "Kanban", description: "Kanban board", enabled: true, configured: true, tools: ["kanban_list"] },
  ]);
  apiMock.getControlOverviewHealth.mockResolvedValue({ overall: "healthy", subsystems: {} });
  apiMock.getControlOverviewVault.mockResolvedValue({
    open_sessions: [{ agent: "codex", started: "2026-06-29T22:51Z", task: "Implement Hermes slices", path: "/vault/session.md" }],
    recent_receipts: [{ when: "22:40", agent: "Hermes", file: "terminal-smoke-receipt.md", path: "/vault/receipt.md" }],
  });
  apiMock.getControlOverviewKanbanBoard.mockResolvedValue({
    columns: [
      { name: "running", tasks: [{ id: "t_run", title: "Live smoke", status: "running", assignee: "coder" }] },
      { name: "blocked", tasks: [{ id: "t_block", title: "Needs operator", status: "blocked", assignee: "operator" }] },
    ],
  });
  apiMock.getControlOverviewDecisionQueue.mockResolvedValue({ count: 1, decisions: [{ task_id: "t_block", task_title: "Needs operator" }] });
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
    expect(await screen.findByText("/home/piet/.hermes/hermes-agent")).toBeTruthy();
    expect(screen.getByText("node/codex")).toBeTruthy();
  });

  it("renders mobile switcher actions and the tools bottom sheet", async () => {
    installDom(true);
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);

    expect((await screen.findAllByText("Agent Terminals")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByRole("button", { name: /Tools/i })[0]);
    expect(screen.getAllByText("Terminal-Kontext").length).toBeGreaterThan(0);
    expect((await screen.findAllByText("Fähigkeiten sichtbar")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Tageslage").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Firecrawl").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Kanban").length).toBeGreaterThan(0);
  });

  it("renders mobile terminal scroll and arrow controls", async () => {
    installDom(true);
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);

    await waitFor(() => expect((screen.getByRole("button", { name: "Send arrow up" }) as HTMLButtonElement).disabled).toBe(false));

    const pageUp = await screen.findByRole("button", { name: "Terminal scroll page up" });
    fireEvent.click(pageUp);
    expect(terminalScrollPagesMock).toHaveBeenCalledWith(-1);
    expect(websocketSends).toContain("\x02\x1b[5~");

    fireEvent.click(screen.getByRole("button", { name: "Terminal scroll up" }));
    expect(terminalScrollLinesMock).toHaveBeenCalledWith(-5);
    expect(websocketSends).toContain("\x1b[A".repeat(5));

    fireEvent.click(screen.getByRole("button", { name: "Terminal scroll down" }));
    expect(terminalScrollLinesMock).toHaveBeenCalledWith(5);
    expect(websocketSends).toContain("\x1b[B".repeat(5));

    fireEvent.click(screen.getByRole("button", { name: "Terminal scroll to bottom" }));
    expect(terminalScrollToBottomMock).toHaveBeenCalled();
    expect(websocketSends).toContain("q");

    const arrowButtons = [
      ["Send arrow left", "\x1b[D"],
      ["Send arrow up", "\x1b[A"],
      ["Send arrow down", "\x1b[B"],
      ["Send arrow right", "\x1b[C"],
    ] as const;
    arrowButtons.forEach(([label, sequence]) => {
      fireEvent.click(screen.getByRole("button", { name: label }));
      expect(websocketSends).toContain(sequence);
    });
    expect(terminalFocusMock).not.toHaveBeenCalled();
  });

  it("fits the terminal on mount and when its host is resized", async () => {
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);

    expect(await screen.findByText("Sessions / Windows")).not.toBeNull();
    await waitFor(() => expect(fitFitMock).toHaveBeenCalled());
    const callsAfterMount = fitFitMock.mock.calls.length;

    triggerResize?.();

    await waitFor(() => expect(fitFitMock.mock.calls.length).toBeGreaterThan(callsAfterMount));
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
