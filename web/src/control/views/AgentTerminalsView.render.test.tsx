// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AgentTerminalCapabilityState, AgentTerminalWindow } from "@/lib/api";

// Unter Voll-Suite-Last fällt der FakeWebSocket-onopen (setTimeout(0)) hinter den
// waitFor-Default-Timeout (1s) zurück — Timeout hochsetzen, um das Gate-Flake zu härten.
configure({ asyncUtilTimeout: 5000 });

const apiMock = {
  getAgentTerminalCapabilities: vi.fn(),
  getAgentTerminalWindows: vi.fn(),
  ensureAgentTerminalWindow: vi.fn(),
  respawnAgentTerminalWindow: vi.fn(),
  killDeadAgentTerminalWindow: vi.fn(),
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
let documentHidden = false;

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
      options: {},
      cols: 80,
      rows: 24,
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
  { session: "hermes-agents", window: "claude", active: false, pane_id: "%3", pid: null, command: "", cwd: null, dead: true },
];

async function loadView() {
  const module = await import("./AgentTerminalsView");
  return module.AgentTerminalsView;
}

function setDocumentHidden(hidden: boolean) {
  documentHidden = hidden;
  Object.defineProperty(document, "hidden", {
    configurable: true,
    get() {
      return documentHidden;
    },
  });
}

function installDom(matches = false) {
  triggerResize = null;
  setDocumentHidden(false);
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
  window.localStorage.clear();
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

  it("pauses read-only context loading while hidden and resumes on visible", async () => {
    setDocumentHidden(true);
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);

    expect(await screen.findByText("Sessions / Windows")).not.toBeNull();
    expect(apiMock.getSkills).not.toHaveBeenCalled();
    expect(apiMock.getControlOverviewHealth).not.toHaveBeenCalled();

    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));

    await waitFor(() => expect(apiMock.getSkills).toHaveBeenCalledTimes(1));
    expect(apiMock.getControlOverviewHealth).toHaveBeenCalledTimes(1);
    expect(apiMock.getControlOverviewDecisionQueue).toHaveBeenCalledTimes(1);
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

  it("sends composer input through the websocket and clears the field", async () => {
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);

    const textarea = (await screen.findByLabelText("Text an Terminal senden")) as HTMLTextAreaElement;
    await waitFor(() => expect(textarea.disabled).toBe(false));

    fireEvent.change(textarea, { target: { value: "echo hallo" } });
    fireEvent.click(screen.getByRole("button", { name: "Eingabe senden" }));
    expect(websocketSends).toContain("echo hallo\r");
    expect(textarea.value).toBe("");

    fireEvent.change(textarea, { target: { value: "zeile 1\nzeile 2" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(websocketSends).toContain("\x1b[200~zeile 1\nzeile 2\x1b[201~\r");
  });

  it("sends special keys from the mobile quick-key row", async () => {
    installDom(true);
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);

    await waitFor(() => expect((screen.getByRole("button", { name: "Send Esc" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Send Esc" }));
    expect(websocketSends).toContain("\x1b");
    fireEvent.click(screen.getByRole("button", { name: "Send ^C" }));
    expect(websocketSends).toContain("\x03");
    fireEvent.click(screen.getByRole("button", { name: "Send ⏎" }));
    expect(websocketSends).toContain("\r");
  });

  it("toggles fullscreen mode and adjusts the terminal font size", async () => {
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);

    fireEvent.click(await screen.findByRole("button", { name: "Vollbild" }));
    expect(screen.getByRole("button", { name: "Vollbild verlassen" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Vollbild verlassen" }));
    expect(screen.getByRole("button", { name: "Vollbild" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Schrift größer" }));
    await waitFor(() => expect(window.localStorage.getItem("hermes-terminals-fontsize")).toBe("13"));
  });

  it("offers respawn for dead windows and targets the recreated window", async () => {
    apiMock.respawnAgentTerminalWindow.mockResolvedValue({
      window: { session: "hermes-agents", window: "claude", active: false, pane_id: "%3", pid: 333, command: "claude", cwd: "/home/piet" },
    });
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);

    fireEvent.click(await screen.findByRole("button", { name: "Neu starten hermes-agents:claude" }));
    await waitFor(() => expect(apiMock.respawnAgentTerminalWindow).toHaveBeenCalledWith("hermes-agents", "claude"));
  });

  it("resets a stale workdir localStorage key to home after capability load", async () => {
    window.localStorage.setItem("hermes-terminals-workdir", "gibt-es-nicht");
    const AgentTerminalsView = await loadView();
    render(<AgentTerminalsView />);

    await waitFor(() => expect(window.localStorage.getItem("hermes-terminals-workdir")).toBe("home"));
    expect((screen.getByLabelText("Arbeitsverzeichnis für neue Terminals") as HTMLSelectElement).value).toBe("home");
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
