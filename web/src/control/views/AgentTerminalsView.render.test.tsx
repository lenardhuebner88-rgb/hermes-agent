// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { AgentTerminalCapabilityState, AgentTerminalOverviewResponse, AgentTerminalWindow } from "@/lib/api";
import { TERMINAL_MAIN_BACKGROUND, TERMINAL_PANE_BACKGROUND } from "@/lib/xtermSurface";

// Unter Voll-Suite-Last fällt der FakeWebSocket-onopen (setTimeout(0)) hinter den
// waitFor-Default-Timeout (1s) zurück — Timeout hochsetzen, um das Gate-Flake zu härten.
configure({ asyncUtilTimeout: 5000 });

const apiMock = {
  getAgentTerminalCapabilities: vi.fn(),
  getAgentTerminalWindows: vi.fn(),
  ensureAgentTerminalWindow: vi.fn(),
  createAgentTerminalWindow: vi.fn(),
  respawnAgentTerminalWindow: vi.fn(),
  killDeadAgentTerminalWindow: vi.fn(),
  terminateAgentTerminalWindow: vi.fn(),
  renameAgentTerminalWindow: vi.fn(),
  getAgentTerminalOverview: vi.fn(),
  sendAgentTerminalKeys: vi.fn(),
  getSkills: vi.fn(),
  getToolsets: vi.fn(),
  getControlOverviewHealth: vi.fn(),
  getControlOverviewVault: vi.fn(),
  getControlOverviewKanbanBoard: vi.fn(),
  getControlOverviewDecisionQueue: vi.fn(),
  getAccountUsage: vi.fn(),
};
const fitFitMock = vi.fn();
const terminalScrollLinesMock = vi.fn();
const terminalScrollPagesMock = vi.fn();
const terminalScrollToBottomMock = vi.fn();
const terminalFocusMock = vi.fn();
const terminalResetMock = vi.fn();
const clipboardWriteMock = vi.fn();
const copyTextToClipboardMock = vi.fn();
let triggerResize: (() => void) | null = null;
let websocketSends: string[] = [];
let documentHidden = false;
// xterm's live selection is not React state — the view reads it on demand via
// term.getSelection(), so the fake mirrors that pull model instead of a prop.
let terminalSelection = "";
// Per-pane selections, keyed by the pane's data-terminal-surface order. Each xterm
// owns its own selection; the copy chord must read the pane it was fired in, not
// whatever pane happens to be active.
let paneSelections: Record<string, string> = {};
// Realistic terminal buffer lines for the select-snapshot overlay (not lorem).
// Shape mirrors xterm buffer.active: length + getLine(i).translateToString(true).
let terminalBufferLines: string[] = [
  "piet@homeserver:~$ hermes --tui",
  "Working (5s · esc to interrupt)",
  "▌ Analysiere PlanSpec …",
  "",
];

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: apiMock,
    buildWsUrl: vi.fn().mockResolvedValue("ws://example.test/attach"),
  };
});

vi.mock("@/lib/clipboard", () => ({
  copyTextToClipboard: (...args: unknown[]) => copyTextToClipboardMock(...args),
}));

vi.mock("@xterm/xterm", () => ({ Terminal: class Terminal {} }));
vi.mock("@/lib/xtermSurface", async () => {
  const actual = await vi.importActual<typeof import("@/lib/xtermSurface")>("@/lib/xtermSurface");
  return {
    TERMINAL_THEME_STATIC: {},
    // Keep real palette exports so host-ridge assertions match production constants.
    TERMINAL_MAIN_BACKGROUND: actual.TERMINAL_MAIN_BACKGROUND,
    TERMINAL_PANE_BACKGROUND: actual.TERMINAL_PANE_BACKGROUND,
    createHermesXtermSurface: vi.fn(({ host }: { host: HTMLElement }) => ({
      term: {
        clear: vi.fn(),
        reset: terminalResetMock,
        writeln: vi.fn(),
        write: vi.fn(),
        onData: vi.fn(() => ({ dispose: vi.fn() })),
        scrollLines: terminalScrollLinesMock,
        scrollPages: terminalScrollPagesMock,
        scrollToBottom: terminalScrollToBottomMock,
        focus: terminalFocusMock,
        getSelection: () => paneSelections[host?.dataset?.terminalSurface ?? ""] ?? terminalSelection,
        dispose: vi.fn(),
        options: {},
        cols: 80,
        rows: 24,
        buffer: {
          active: {
            get length() {
              return terminalBufferLines.length;
            },
            getLine(index: number) {
              if (index < 0 || index >= terminalBufferLines.length) return undefined;
              return {
                translateToString: (trimRight?: boolean) => {
                  const raw = terminalBufferLines[index] ?? "";
                  return trimRight ? raw.replace(/\s+$/u, "") : raw;
                },
              };
            },
          },
        },
      },
      fit: { fit: fitFitMock },
    })),
  };
});

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

// Echtes Response-Shape von GET /api/agent-terminals/overview (TmuxAgentSessionService.overview()
// in hermes_cli/agent_terminals.py): now = Unix-Sekunden, tail = ANSI-bereinigte letzte Zeilen.
const overviewFixture: AgentTerminalOverviewResponse = {
  now: 1783025500,
  windows: [
    {
      session: "hermes-agents",
      window: "hermes",
      active: true,
      pane_id: "%1",
      pid: 111,
      command: "hermes",
      cwd: "/home/piet",
      dead: false,
      activity: 1783025480,
      tail: "Working (5s · esc to interrupt)\n▌ Analysiere PlanSpec …",
      state: "laeuft",
      state_source: "heuristic",
    },
    {
      session: "hermes-agents",
      window: "codex",
      active: false,
      pane_id: "%2",
      pid: 222,
      command: "node",
      cwd: "/home/piet/.hermes/hermes-agent",
      dead: false,
      activity: 1783020000,
      tail: "Allow this action? (y/n)",
      state: "frage",
      state_source: "heuristic",
    },
    {
      session: "hermes-agents",
      window: "claude",
      active: false,
      pane_id: "%3",
      pid: null,
      command: "",
      cwd: null,
      dead: true,
      activity: 1783000000,
      tail: null,
      state: "dead",
      state_source: "heuristic",
    },
  ],
};

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
  // jsdom ships no Clipboard API; the view's copy path is async-clipboard-first.
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: clipboardWriteMock },
  });
  // Hardened helper only uses writeText in secure contexts.
  Object.defineProperty(window, "isSecureContext", {
    configurable: true,
    get: () => true,
  });
}

/** Renders the view under a MemoryRouter — the "Zurück"-chip needs useNavigate() context. */
async function renderView() {
  const AgentTerminalsView = await loadView();
  return render(
    <MemoryRouter initialEntries={["/control/agent-terminals"]}>
      <AgentTerminalsView />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  websocketSends = [];
  terminalSelection = "";
  paneSelections = {};
  terminalBufferLines = [
    "piet@homeserver:~$ hermes --tui",
    "Working (5s · esc to interrupt)",
    "▌ Analysiere PlanSpec …",
    "",
  ];
  window.localStorage.clear();
  installDom(false);
  clipboardWriteMock.mockResolvedValue(undefined);
  // Default: helper succeeds and still exercises writeText when tests assert on it.
  copyTextToClipboardMock.mockImplementation(async (text: string) => {
    await clipboardWriteMock(text);
    return true;
  });
  apiMock.getAgentTerminalCapabilities.mockResolvedValue(capability);
  apiMock.getAgentTerminalWindows.mockResolvedValue({ windows });
  apiMock.ensureAgentTerminalWindow.mockImplementation(async (kind: string) => ({ window: windows.find((w) => w.window === kind) ?? windows[0] }));
  apiMock.createAgentTerminalWindow.mockImplementation(async (kind: string) => ({ window: windows.find((w) => w.window === kind) ?? windows[0] }));
  apiMock.getAgentTerminalOverview.mockResolvedValue(overviewFixture);
  apiMock.sendAgentTerminalKeys.mockResolvedValue({ ok: true });
  apiMock.renameAgentTerminalWindow.mockResolvedValue({ window: windows[0] });
  apiMock.terminateAgentTerminalWindow.mockResolvedValue({ ok: true });
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
  apiMock.getAccountUsage.mockResolvedValue({
    as_of: "2026-07-09T20:00:00Z",
    providers: [
      { provider: "openai-codex", available: true, source: "oauth", fetched_at: null, title: "OpenAI Codex", plan: "Plus", windows: [{ label: "5h", window_key: "five_hour", used_percent: 35, reset_at: null, detail: null }, { label: "Weekly", window_key: "weekly", used_percent: 61, reset_at: null, detail: null }], details: [], unavailable_reason: null, cached: false },
      { provider: "anthropic", available: true, source: "oauth", fetched_at: null, title: "Anthropic", plan: "Max", windows: [], details: [], unavailable_reason: null, cached: false },
      { provider: "kimi", available: true, source: "local", fetched_at: null, title: "Kimi", plan: "Coding", windows: [], details: [], unavailable_reason: null, cached: false },
    ],
  });
});

afterEach(() => {
  cleanup();
});

describe("AgentTerminalsView desktop rendering", () => {
  it("uses Leitstand panels and 45px targets for every rendered desktop toolbar control", async () => {
    await renderView();

    const fleetPanel = await screen.findByRole("region", { name: "Terminal-Flotte" });
    expect(fleetPanel.className).toContain("rounded-panel");
    expect(fleetPanel.className).toContain("bg-surface-1");

    const toolbarLabels = [
      "1 Terminal anzeigen",
      "2 Terminals anzeigen",
      "4 Terminals anzeigen",
      "Usage Window umschalten",
      "Werkzeuge umschalten",
      "Schrift kleiner",
      "Schrift größer",
      "Vollbild",
    ];
    const toolbarButtons = toolbarLabels.map((name) => screen.getByRole("button", { name }));
    expect(toolbarButtons).toHaveLength(8);
    for (const button of toolbarButtons) {
      expect(button.className).toContain("h-12");
      expect(button.className).toContain("w-12");
    }

    const usageDock = screen.getByTestId("terminal-usage-dock");
    expect(usageDock.className).toContain("rounded-panel");
    expect(usageDock.className).toContain("bg-surface-1/95");
  });

  it("renders the desktop three-column terminal shell and switches sessions", async () => {
    await renderView();

    expect(await screen.findByText("Sessions / Windows")).not.toBeNull();
    expect(screen.getByText("Abo-Limits")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Werkzeuge umschalten" }));
    expect(screen.getByText("Terminal-Kontext")).not.toBeNull();
    fireEvent.click((await screen.findAllByText("codex"))[0]);
    expect(screen.getAllByText("hermes-agents:codex").length).toBeGreaterThan(0);
    expect(await screen.findByText("/home/piet/.hermes/hermes-agent")).toBeTruthy();
    expect(screen.getByText("node/codex")).toBeTruthy();
  });

  it("switches between stable 1x, 2x, and 4x terminal grids with unique targets", async () => {
    await renderView();
    const primaryHost = await screen.findByTestId("terminal-pane-host-0");
    expect(primaryHost).toBeTruthy();
    // Floored xterm cols/rows leave a strip of host bg — must match canvas theme constants.
    // jsdom serializes hex as rgb(); normalize the export the same way.
    const asCssColor = (hex: string) => {
      const probe = document.createElement("div");
      probe.style.backgroundColor = hex;
      return probe.style.backgroundColor;
    };
    expect(primaryHost.style.backgroundColor).toBe(asCssColor(TERMINAL_MAIN_BACKGROUND));
    const primaryCard = screen.getByTestId("terminal-pane-card-0");
    expect(primaryCard.className).toContain("w-full");
    expect(primaryCard.className).toContain("shrink-0");

    fireEvent.click(screen.getByTestId("terminal-layout-button-2"));
    expect(await screen.findByTestId("terminal-layout-2")).toBeTruthy();
    expect(screen.getByTestId("terminal-pane-host-0")).toBeTruthy();
    const splitHost = screen.getByTestId("terminal-pane-host-1");
    expect(splitHost).toBeTruthy();
    expect(splitHost.style.backgroundColor).toBe(asCssColor(TERMINAL_PANE_BACKGROUND));
    expect((screen.getByLabelText("Terminal 1") as HTMLSelectElement).value).not.toBe((screen.getByLabelText("Terminal 2") as HTMLSelectElement).value);
    const { buildWsUrl } = await import("@/lib/api");
    await waitFor(() => {
      const attachCalls = vi.mocked(buildWsUrl).mock.calls.filter(([path]) => path === "/api/agent-terminals/attach");
      expect(attachCalls.some(([, params]) => params?.client_id === "agent-terminals-ui-pane-0" && params?.isolated === "1")).toBe(true);
      expect(attachCalls.some(([, params]) => params?.client_id === "agent-terminals-ui-pane-1" && params?.isolated === "1")).toBe(true);
    });

    fireEvent.click(screen.getByTestId("terminal-layout-button-4"));
    expect(await screen.findByTestId("terminal-layout-4")).toBeTruthy();
    expect(screen.getAllByTestId(/terminal-pane-card-/)).toHaveLength(4);
    expect(window.localStorage.getItem("hermes.control.agent-terminals.desktop-layout.v1")).toBe("4");

    const primaryCallsBeforeShrink = vi.mocked(buildWsUrl).mock.calls.filter(([, params]) => params?.client_id === "agent-terminals-ui-pane-0").length;
    fireEvent.click(screen.getByTestId("terminal-layout-button-1"));
    expect(screen.queryByTestId("terminal-layout-4")).toBeNull();
    expect(screen.getByTestId("terminal-pane-host-0")).toBeTruthy();
    const primaryCallsAfterShrink = vi.mocked(buildWsUrl).mock.calls.filter(([, params]) => params?.client_id === "agent-terminals-ui-pane-0");
    expect(primaryCallsAfterShrink).toHaveLength(primaryCallsBeforeShrink);
    expect(primaryCallsAfterShrink.at(-1)?.[1]?.isolated).toBe("1");
  });

  it("restores persisted desktop 4x with four isolated panes and a collapsed Usage rail", async () => {
    window.localStorage.setItem("hermes.control.agent-terminals.desktop-layout.v1", "4");
    await renderView();
    expect(await screen.findByTestId("terminal-layout-4")).toBeTruthy();
    expect(screen.getByTestId("terminal-usage-dock").getAttribute("aria-hidden")).toBe("true");
    const { buildWsUrl } = await import("@/lib/api");
    await waitFor(() => {
      const primaryCalls = vi.mocked(buildWsUrl).mock.calls.filter(([, params]) => params?.client_id === "agent-terminals-ui-pane-0");
      expect(primaryCalls.at(-1)?.[1]?.isolated).toBe("1");
    });
  });

  it("renders a persistent fleet-strip card per fleet window and selects the terminal on click", async () => {
    await renderView();

    // One card per window in the real overview fixture shape (hermes/codex/claude).
    await waitFor(() => expect(apiMock.getAgentTerminalOverview).toHaveBeenCalled());
    expect(await screen.findByText("läuft")).toBeTruthy();
    expect(screen.getByText("frage")).toBeTruthy();
    expect(screen.getByText("tot")).toBeTruthy();
    expect(screen.getByText("Allow this action? (y/n)")).toBeTruthy();

    fireEvent.click(screen.getByText("Allow this action? (y/n)"));
    expect(screen.getAllByText("hermes-agents:codex").length).toBeGreaterThan(0);
  });

  it("renders the stat tiles from the existing skills/toolsets/kanban counts", async () => {
    await renderView();

    await screen.findByText("Abo-Limits");
    fireEvent.click(screen.getByRole("button", { name: "Werkzeuge umschalten" }));
    expect(await screen.findByText("Terminal-Kontext")).toBeTruthy();
    expect(screen.getByText("Skills aktiv")).toBeTruthy();
    expect(screen.getByText("Toolsets aktiv")).toBeTruthy();
    expect(screen.getByText("Kanban aktiv")).toBeTruthy();
    expect(screen.getByText("Blockiert")).toBeTruthy();
    expect(screen.getByText("Claims")).toBeTruthy();
  });

  it("pauses read-only context loading while hidden and resumes on visible", async () => {
    setDocumentHidden(true);
    await renderView();

    expect(await screen.findByText("Sessions / Windows")).not.toBeNull();
    expect(apiMock.getSkills).not.toHaveBeenCalled();
    expect(apiMock.getControlOverviewHealth).not.toHaveBeenCalled();

    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));

    await waitFor(() => expect(apiMock.getSkills).toHaveBeenCalledTimes(1));
    expect(apiMock.getControlOverviewHealth).toHaveBeenCalledTimes(1);
    expect(apiMock.getControlOverviewDecisionQueue).toHaveBeenCalledTimes(1);
  });

  it("refreshes the tmux inventory when the dashboard becomes visible again", async () => {
    const grokWindow: AgentTerminalWindow = {
      ...windows[0],
      session: "work",
      window: "grok",
      command: "node",
      cwd: "/home/piet",
    };
    setDocumentHidden(true);

    await renderView();
    expect(await screen.findByText("Sessions / Windows")).not.toBeNull();
    expect(screen.queryByRole("button", { name: /grok/i })).toBeNull();
    const callsBeforeResume = apiMock.getAgentTerminalWindows.mock.calls.length;
    apiMock.getAgentTerminalWindows.mockResolvedValue({ windows: [...windows, grokWindow] });

    setDocumentHidden(false);
    fireEvent(document, new Event("visibilitychange"));

    await waitFor(() => expect(apiMock.getAgentTerminalWindows.mock.calls.length).toBeGreaterThan(callsBeforeResume));
    expect(await screen.findByText("grok")).toBeTruthy();
  });

  it("fits the terminal on mount and when its host is resized", async () => {
    await renderView();

    expect(await screen.findByText("Sessions / Windows")).not.toBeNull();
    await waitFor(() => expect(fitFitMock).toHaveBeenCalled());
    const callsAfterMount = fitFitMock.mock.calls.length;

    triggerResize?.();

    await waitFor(() => expect(fitFitMock.mock.calls.length).toBeGreaterThan(callsAfterMount));
  });

  it("sends composer input through the websocket and clears the field", async () => {
    await renderView();

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

  it("toggles fullscreen mode and adjusts the terminal font size", async () => {
    await renderView();

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
    await renderView();

    fireEvent.click(await screen.findByRole("button", { name: "Neu starten hermes-agents:claude" }));
    await waitFor(() => expect(apiMock.respawnAgentTerminalWindow).toHaveBeenCalledWith("hermes-agents", "claude"));
  });

  // window.confirm() blocks the whole renderer thread; against a live tmux the
  // dialog hung ~30s and never closed the window. The close path must therefore
  // arm in-app and never reach for the native dialog.
  it("terminates a live window only after an in-app second step, never via window.confirm", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    await renderView();

    fireEvent.click(await screen.findByRole("button", { name: "Session beenden hermes-agents:codex" }));

    // Step 1 arms only — no kill call yet.
    expect(apiMock.terminateAgentTerminalWindow).not.toHaveBeenCalled();
    expect(confirmSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Beenden bestätigen hermes-agents:codex" }));

    await waitFor(() => expect(apiMock.terminateAgentTerminalWindow).toHaveBeenCalledWith("hermes-agents", "codex"));
    await waitFor(() => expect(apiMock.getAgentTerminalWindows.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it("disarms the terminate guard on cancel and kills nothing", async () => {
    await renderView();

    fireEvent.click(await screen.findByRole("button", { name: "Session beenden hermes-agents:codex" }));
    fireEvent.click(screen.getByRole("button", { name: "Beenden abbrechen hermes-agents:codex" }));

    expect(screen.getByRole("button", { name: "Session beenden hermes-agents:codex" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Beenden bestätigen hermes-agents:codex" })).toBeNull();
    expect(apiMock.terminateAgentTerminalWindow).not.toHaveBeenCalled();
  });

  // Arming one row must not arm every row — otherwise a mis-click on the confirm
  // of a neighbouring row kills a live agent session.
  it("arms the terminate guard for one window at a time", async () => {
    await renderView();

    fireEvent.click(await screen.findByRole("button", { name: "Session beenden hermes-agents:codex" }));

    expect(screen.getByRole("button", { name: "Beenden bestätigen hermes-agents:codex" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Beenden bestätigen hermes-agents:hermes" })).toBeNull();
    expect(screen.getByRole("button", { name: "Session beenden hermes-agents:hermes" })).toBeTruthy();
  });

  // Stale-poll race: an inventory response issued BEFORE a close can resolve AFTER
  // the post-close list. Without a monotonic seq guard the closed tab flashes back.
  it("drops stale windows-list responses so a newer inventory wins over an older one", async () => {
    type WindowsPayload = { windows: AgentTerminalWindow[] };
    const resolvers: Array<(value: WindowsPayload) => void> = [];
    apiMock.getAgentTerminalWindows.mockImplementation(
      () =>
        new Promise<WindowsPayload>((resolve) => {
          resolvers.push(resolve);
        }),
    );
    // Capabilities resolve immediately so refresh() is only gated on windows-list.
    apiMock.getAgentTerminalCapabilities.mockResolvedValue(capability);

    await renderView();
    await waitFor(() => expect(resolvers.length).toBeGreaterThanOrEqual(1));

    // Issue a second fetch (manual refresh) while the first is still in flight.
    fireEvent.click(screen.getByRole("button", { name: "Refresh agent terminals" }));
    await waitFor(() => expect(resolvers.length).toBeGreaterThanOrEqual(2));

    const staleList = windows; // includes codex
    const newestList = windows.filter((w) => w.window !== "codex"); // post-close

    // Newest request (seq=2) resolves first, then the older one (seq=1).
    await act(async () => {
      resolvers[1]!({ windows: newestList });
    });
    // Session-rail terminate buttons are the windows-state surface (not fleet overview).
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Session beenden hermes-agents:codex" })).toBeNull();
      expect(screen.getByRole("button", { name: "Session beenden hermes-agents:hermes" })).toBeTruthy();
    });

    await act(async () => {
      resolvers[0]!({ windows: staleList });
    });
    // Give React a tick to apply a wrongly-ordered setWindows if the guard is missing.
    await act(async () => {
      await Promise.resolve();
    });

    // Final rendered tab list must still reflect the NEWEST response, not the stale one.
    expect(screen.queryByRole("button", { name: "Session beenden hermes-agents:codex" })).toBeNull();
    expect(screen.getByRole("button", { name: "Session beenden hermes-agents:hermes" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Fenster schließen hermes-agents:claude" })).toBeTruthy();
  });

  // Close error path: row must disarm and inventory must re-fetch even when terminate rejects.
  it("disarms the terminate guard and refreshes windows when terminate fails", async () => {
    apiMock.terminateAgentTerminalWindow.mockRejectedValueOnce(new Error("terminate failed: 503"));

    await renderView();
    await screen.findByRole("button", { name: "Session beenden hermes-agents:codex" });
    const callsAfterMount = apiMock.getAgentTerminalWindows.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "Session beenden hermes-agents:codex" }));
    expect(screen.getByRole("button", { name: "Beenden bestätigen hermes-agents:codex" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Beenden bestätigen hermes-agents:codex" }));

    await waitFor(() => expect(apiMock.terminateAgentTerminalWindow).toHaveBeenCalledWith("hermes-agents", "codex"));
    // Armed state cleared — confirm gone, arm button back.
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Beenden bestätigen hermes-agents:codex" })).toBeNull();
      expect(screen.getByRole("button", { name: "Session beenden hermes-agents:codex" })).toBeTruthy();
    });
    // refresh() ran after the failure (post-mount windows-list call).
    await waitFor(() => expect(apiMock.getAgentTerminalWindows.mock.calls.length).toBeGreaterThan(callsAfterMount));
    // Error banner kept visible (survives concurrent websocket onopen clear).
    await waitFor(() => expect(screen.getByText(/terminate failed: 503/)).toBeTruthy());
  });

  // kill-dead error path: same finally-refresh contract as live terminate.
  it("refreshes windows when kill-dead fails", async () => {
    apiMock.killDeadAgentTerminalWindow.mockRejectedValueOnce(new Error("kill-dead failed"));
    await renderView();
    await screen.findByRole("button", { name: "Fenster schließen hermes-agents:claude" });
    const callsAfterMount = apiMock.getAgentTerminalWindows.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "Fenster schließen hermes-agents:claude" }));

    await waitFor(() => expect(apiMock.killDeadAgentTerminalWindow).toHaveBeenCalledWith("hermes-agents", "claude"));
    await waitFor(() => expect(apiMock.getAgentTerminalWindows.mock.calls.length).toBeGreaterThan(callsAfterMount));
    await waitFor(() => expect(screen.getByText(/kill-dead failed/)).toBeTruthy());
  });

  it("copies the xterm selection via Ctrl+Shift+C without sending ETX to tmux", async () => {
    await renderView();
    const host = await screen.findByTestId("terminal-pane-host-0");
    terminalSelection = "pane-zeile aus dem scrollback";

    fireEvent.keyDown(host, { key: "C", ctrlKey: true, shiftKey: true });

    await waitFor(() => expect(clipboardWriteMock).toHaveBeenCalledWith("pane-zeile aus dem scrollback"));
    // The copy path must never reach the socket — ETX (\x03) would SIGINT the agent.
    expect(websocketSends).not.toContain("\x03");
  });

  // The chord is bound document-wide (xterm binds its own keydown on the helper
  // textarea, so it has to be caught in the capture phase). That makes it the
  // handler's job to reject foreign targets: a selection left behind in the
  // terminal must not hijack the copy the user performs in a text field.
  it("leaves the copy chord to the browser outside the terminal surface, even with a stale selection", async () => {
    await renderView();
    await screen.findByTestId("terminal-pane-host-0");
    terminalSelection = "alter terminaltext";
    const composer = await screen.findByLabelText("Text an Terminal senden");

    const shiftCNotPrevented = fireEvent.keyDown(composer, { key: "C", ctrlKey: true, shiftKey: true });
    const insertNotPrevented = fireEvent.keyDown(composer, { key: "Insert", ctrlKey: true });

    expect(shiftCNotPrevented).toBe(true);
    expect(insertNotPrevented).toBe(true);
    expect(clipboardWriteMock).not.toHaveBeenCalled();
    expect(screen.queryByText("Kopiert")).toBeNull();
  });

  it("copies the selection of the pane the chord was fired in, not the active pane", async () => {
    await renderView();
    fireEvent.click(await screen.findByTestId("terminal-layout-button-2"));
    const extraHost = await screen.findByTestId("terminal-pane-host-1");
    paneSelections["0"] = "auswahl aus pane 0";
    paneSelections["1"] = "auswahl aus pane 1";

    fireEvent.keyDown(extraHost, { key: "C", ctrlKey: true, shiftKey: true });

    await waitFor(() => expect(clipboardWriteMock).toHaveBeenCalledWith("auswahl aus pane 1"));
    expect(clipboardWriteMock).not.toHaveBeenCalledWith("auswahl aus pane 0");
    expect(websocketSends).not.toContain("\x03");
  });

  it("copies the selection via the visible toolbar control without sending ETX", async () => {
    await renderView();
    terminalSelection = "kopierbare ausgabe";

    fireEvent.click(await screen.findByRole("button", { name: "Auswahl kopieren" }));

    await waitFor(() => expect(clipboardWriteMock).toHaveBeenCalledWith("kopierbare ausgabe"));
    expect(websocketSends).not.toContain("\x03");
  });

  it("copies nothing when no selection exists", async () => {
    await renderView();
    const host = await screen.findByTestId("terminal-pane-host-0");
    terminalSelection = "";

    fireEvent.click(await screen.findByRole("button", { name: "Auswahl kopieren" }));
    fireEvent.keyDown(host, { key: "C", ctrlKey: true, shiftKey: true });

    expect(clipboardWriteMock).not.toHaveBeenCalled();
    expect(copyTextToClipboardMock).not.toHaveBeenCalled();
    expect(await screen.findByText("Keine Auswahl")).toBeTruthy();
  });

  // Mobile has no xterm touch selection — "Text auswählen" freezes the active
  // pane buffer into a native selectable <pre> overlay (S5).
  it("opens the select overlay with a frozen snapshot of the fake terminal buffer", async () => {
    await renderView();
    await screen.findByTestId("terminal-pane-host-0");

    fireEvent.click(await screen.findByRole("button", { name: "Text auswählen" }));

    const dialog = await screen.findByRole("dialog", { name: "Terminal-Text auswählen" });
    expect(dialog).toBeTruthy();
    // Distinctive real-looking shell/agent line from the fake buffer (scoped to
    // the overlay — the fleet strip also shows overview tails with similar text).
    const snapshot = dialog.querySelector("pre");
    expect(snapshot?.textContent).toContain("piet@homeserver:~$ hermes --tui");
    expect(snapshot?.textContent).toContain("Analysiere PlanSpec");
  });

  it("copies the full buffer snapshot via Alles kopieren through the clipboard helper", async () => {
    await renderView();
    await screen.findByTestId("terminal-pane-host-0");

    fireEvent.click(await screen.findByRole("button", { name: "Text auswählen" }));
    await screen.findByRole("dialog", { name: "Terminal-Text auswählen" });

    fireEvent.click(screen.getByRole("button", { name: "Alles kopieren" }));

    const expectedSnapshot = [
      "piet@homeserver:~$ hermes --tui",
      "Working (5s · esc to interrupt)",
      "▌ Analysiere PlanSpec …",
    ].join("\n");

    await waitFor(() => expect(copyTextToClipboardMock).toHaveBeenCalledWith(expectedSnapshot));
    expect(websocketSends).not.toContain("\x03");
  });

  // Plain Ctrl+C stays the agent interrupt — hijacking it whenever a stale
  // selection exists would silently break the only way to stop a runaway agent.
  it("leaves plain Ctrl+C to the terminal even with an active selection", async () => {
    await renderView();
    const host = await screen.findByTestId("terminal-pane-host-0");
    terminalSelection = "markierter text";

    fireEvent.keyDown(host, { key: "c", ctrlKey: true });

    expect(clipboardWriteMock).not.toHaveBeenCalled();
  });

  // The desktop single view attached directly (no isolated=1), so tmux forced
  // every other client to the browser's window size. Isolation is the desktop
  // contract for every layout; compact/mobile keeps its direct attach.
  it("attaches the desktop single view in isolated mode and keeps it isolated across target switches", async () => {
    await renderView();
    const { buildWsUrl } = await import("@/lib/api");

    await waitFor(() => {
      const primaryCalls = vi.mocked(buildWsUrl).mock.calls.filter(([, params]) => params?.client_id === "agent-terminals-ui-pane-0");
      expect(primaryCalls.at(-1)?.[1]?.isolated).toBe("1");
    });

    fireEvent.click((await screen.findAllByText("codex"))[0]);

    await waitFor(() => {
      const primaryCalls = vi.mocked(buildWsUrl).mock.calls.filter(([, params]) => params?.client_id === "agent-terminals-ui-pane-0");
      expect(primaryCalls.at(-1)?.[1]?.session).toBe("hermes-agents");
      expect(primaryCalls.at(-1)?.[1]?.window).toBe("codex");
      expect(primaryCalls.at(-1)?.[1]?.isolated).toBe("1");
    });
  });

  // term.clear() keeps the prompt line plus every mode the old session left set
  // (alt buffer, scroll region, SGR), so the previous agent's frame bled into the
  // next one. Only a full reset() gives the new target a clean surface.
  it("resets the xterm buffer when the target switches", async () => {
    await renderView();
    await waitFor(() => expect(terminalResetMock).toHaveBeenCalled());
    const resetsAfterMount = terminalResetMock.mock.calls.length;

    fireEvent.click((await screen.findAllByText("codex"))[0]);

    await waitFor(() => expect(terminalResetMock.mock.calls.length).toBeGreaterThan(resetsAfterMount));
  });

  it("opens the create-session modal and resets a stale workdir localStorage key to home after capability load", async () => {
    window.localStorage.setItem("hermes-terminals-workdir", "gibt-es-nicht");
    await renderView();

    await waitFor(() => expect(window.localStorage.getItem("hermes-terminals-workdir")).toBe("home"));
    fireEvent.click(await screen.findByRole("button", { name: "Neue Session" }));
    expect((screen.getByLabelText("Arbeitsverzeichnis für neue Terminals") as HTMLSelectElement).value).toBe("home");
  });

  it("creates a Grok Build session via the desktop create modal", async () => {
    await renderView();

    fireEvent.click(await screen.findByRole("button", { name: "Neue Session" }));
    fireEvent.click(screen.getByRole("button", { name: /Grok/ }));
    fireEvent.click(screen.getByRole("button", { name: "Session starten" }));

    await waitFor(() => expect(apiMock.createAgentTerminalWindow).toHaveBeenCalledWith("grok", "home"));
  });

  it("renders empty and error states", async () => {
    apiMock.getAgentTerminalWindows.mockResolvedValueOnce({ windows: [] });
    await renderView();
    expect(await screen.findByText(/Kein tmux-Fenster verfügbar/)).not.toBeNull();

    cleanup();
    apiMock.getAgentTerminalCapabilities.mockRejectedValueOnce(new Error("backend offline"));
    apiMock.getAgentTerminalWindows.mockResolvedValueOnce({ windows: [] });
    await renderView();
    await waitFor(() => expect(screen.getByText(/backend offline/)).not.toBeNull());
  });
});

describe("AgentTerminalsView mobile rendering (compactLayout)", () => {
  it("forces a persisted 4x layout back to one mounted terminal on compact screens", async () => {
    installDom(true);
    window.localStorage.setItem("hermes.control.agent-terminals.desktop-layout.v1", "4");
    await renderView();

    expect(await screen.findByTestId("terminal-pane-host-0")).toBeTruthy();
    expect(screen.queryByTestId("terminal-layout-4")).toBeNull();
    expect(screen.queryByTestId("terminal-pane-host-1")).toBeNull();
    expect(screen.getByTestId("terminal-usage-dock").getAttribute("aria-hidden")).toBe("true");
    const { buildWsUrl } = await import("@/lib/api");
    await waitFor(() => {
      const primaryCalls = vi.mocked(buildWsUrl).mock.calls.filter(([, params]) => params?.client_id === "agent-terminals-ui-pane-0");
      expect(primaryCalls.at(-1)?.[1]?.isolated).toBeUndefined();
    });
  });

  it("renders an immersive chip strip with the fixture windows and a sticky + chip", async () => {
    installDom(true);
    await renderView();

    // Chips hängen an geladenen Fenstern — als Erstes darauf warten, sonst sind
    // die folgenden Sync-Assertions ein Race gegen den async getAgentTerminalWindows-Resolve.
    expect(await screen.findByRole("button", { name: "hermes-agents:hermes" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "hermes-agents:codex" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "hermes-agents:claude" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Zurück zum Dashboard" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Neue Session starten" })).toBeTruthy();
    expect(screen.getByLabelText("Text an Terminal senden")).toBeTruthy();
    // Kein Page-Header, keine Header-Karte auf compactLayout.
    expect(screen.queryByText("Agent Terminals")).toBeNull();
  });

  it("navigates back to /control via the chip strip back button", async () => {
    installDom(true);
    const AgentTerminalsView = await loadView();
    render(
      <MemoryRouter initialEntries={["/control/agent-terminals"]}>
        <Routes>
          <Route path="/control/agent-terminals" element={<AgentTerminalsView />} />
          <Route path="/control" element={<div>CONTROL_HOME</div>} />
        </Routes>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Zurück zum Dashboard" }));
    expect(await screen.findByText("CONTROL_HOME")).toBeTruthy();
  });

  it("switches windows by tapping an inactive chip, then opens its session sheet on a second tap", async () => {
    installDom(true);
    await renderView();

    const codexChip = await screen.findByRole("button", { name: "hermes-agents:codex" });
    fireEvent.click(codexChip);
    fireEvent.click(codexChip);

    // "Sitzung schließen" existiert nur im geöffneten Session-Sheet — eindeutiger
    // Beleg dafür, dass der zweite Tap auf den (jetzt aktiven) Chip das Sheet öffnet.
    expect(await screen.findByRole("button", { name: "Sitzung schließen" })).toBeTruthy();
  });

  it("opens the tools sheet from the session sheet", async () => {
    installDom(true);
    await renderView();

    const activeChip = await screen.findByRole("button", { name: "hermes-agents:hermes" });
    fireEvent.click(activeChip);
    fireEvent.click(screen.getByRole("button", { name: "Tools / Tageslage" }));
    expect((await screen.findAllByText("Terminal-Kontext")).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("Fähigkeiten sichtbar")).length).toBeGreaterThan(0);
  });

  it("refreshes the window list from the session sheet action grid", async () => {
    installDom(true);
    await renderView();

    const activeChip = await screen.findByRole("button", { name: "hermes-agents:hermes" });
    fireEvent.click(activeChip);
    await waitFor(() => expect(apiMock.getAgentTerminalWindows).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Liste aktualisieren" }));
    await waitFor(() => expect(apiMock.getAgentTerminalWindows).toHaveBeenCalledTimes(2));
  });

  it("keeps the key row hidden by default and reveals it via the composer toggle", async () => {
    installDom(true);
    await renderView();

    await screen.findByLabelText("Text an Terminal senden");
    expect(screen.queryByRole("button", { name: "Terminal scroll page up" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Send Esc" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Tastenleiste einblenden" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Send Esc" }) as HTMLButtonElement).disabled).toBe(false));
    expect(window.localStorage.getItem("hermes-terminals-keysopen")).toBe("1");

    fireEvent.click(screen.getByRole("button", { name: "Terminal scroll page up" }));
    expect(terminalScrollPagesMock).toHaveBeenCalledWith(-1);
    expect(websocketSends).toContain("\x02\x1b[5~");

    fireEvent.click(screen.getByRole("button", { name: "Send ^C" }));
    expect(websocketSends).toContain("\x03");

    fireEvent.click(screen.getByRole("button", { name: "Tastenleiste ausblenden" }));
    expect(screen.queryByRole("button", { name: "Send Esc" })).toBeNull();
  });

  it("creates a new session via the mobile create sheet", async () => {
    installDom(true);
    await renderView();

    fireEvent.click(await screen.findByRole("button", { name: "Neue Session starten" }));
    fireEvent.click(screen.getByRole("button", { name: /Codex/ }));
    fireEvent.click(screen.getByRole("button", { name: "Session starten" }));

    await waitFor(() => expect(apiMock.createAgentTerminalWindow).toHaveBeenCalledWith("codex", "home"));
  });

  it("renames the active window from the session sheet and refreshes the window list", async () => {
    installDom(true);
    apiMock.renameAgentTerminalWindow.mockResolvedValue({
      window: { session: "hermes-agents", window: "hermes-2", active: true, pane_id: "%1", pid: 111, command: "hermes", cwd: "/home/piet" },
    });
    await renderView();

    const activeChip = await screen.findByRole("button", { name: "hermes-agents:hermes" });
    fireEvent.click(activeChip);

    const input = (await screen.findByLabelText("Neuer Fenstername")) as HTMLInputElement;
    expect(input.value).toBe("hermes");
    fireEvent.change(input, { target: { value: "hermes-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Umbenennen" }));

    await waitFor(() => expect(apiMock.renameAgentTerminalWindow).toHaveBeenCalledWith("hermes-agents", "hermes", "hermes-2"));
    await waitFor(() => expect(apiMock.getAgentTerminalWindows).toHaveBeenCalledTimes(2));
  });

  it("shows the fleet overview toggle in the chip strip and renders cards from the fetched overview", async () => {
    installDom(true);
    await renderView();

    fireEvent.click(await screen.findByRole("button", { name: "Flotten-Übersicht" }));
    await waitFor(() => expect(apiMock.getAgentTerminalOverview).toHaveBeenCalled());

    expect(await screen.findByText("Braucht dich")).toBeTruthy();
    expect(screen.getByText("Läuft")).toBeTruthy();
    expect(screen.getByText("Tot")).toBeTruthy();
    expect(screen.getByText(/Allow this action\?/)).toBeTruthy();
    expect(screen.getByText("Zustände: Heuristik aus Terminal-Ausgabe")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Terminal-Ansicht" }));
    expect(screen.queryByText("Zustände: Heuristik aus Terminal-Ausgabe")).toBeNull();
  });

  it("jumps back into the terminal view when a fleet card is tapped outside broadcast mode", async () => {
    installDom(true);
    await renderView();

    fireEvent.click(await screen.findByRole("button", { name: "Flotten-Übersicht" }));
    const codexCard = await screen.findByText(/Allow this action\?/);
    fireEvent.click(codexCard);

    expect(screen.queryByText("Zustände: Heuristik aus Terminal-Ausgabe")).toBeNull();
    expect(screen.getAllByText("hermes-agents:codex").length).toBeGreaterThan(0);
  });

  it("requires a confirmation step before broadcasting to selected sessions", async () => {
    installDom(true);
    await renderView();

    fireEvent.click(await screen.findByRole("button", { name: "Flotten-Übersicht" }));
    await screen.findByText("Braucht dich");
    fireEvent.click(screen.getByRole("button", { name: "Senden an mehrere" }));

    // Nur lebende Karten sind auswählbar — Klick auf die "laeuft"-Karte selektiert sie.
    fireEvent.click(screen.getByText("Läuft"));

    const textarea = screen.getByLabelText("Text an mehrere Terminals senden");
    fireEvent.change(textarea, { target: { value: "status" } });
    fireEvent.click(screen.getByRole("button", { name: "An 1 senden" }));

    expect(await screen.findByText("Wirklich an 1 Sessions senden?")).toBeTruthy();
    expect(apiMock.sendAgentTerminalKeys).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Ja" }));
    await waitFor(() => expect(apiMock.sendAgentTerminalKeys).toHaveBeenCalledWith("hermes-agents", "hermes", "status\r"));
  });
});

describe("AgentTerminalsView attach reconnect on initial connect failure", () => {
  it("schedules a 1s backoff retry with an honest status line, reconnects, and resets the attempt counter on success", async () => {
    // Higher than the default vitest test timeout — several StatusPill
    // instances render on desktop (header + identity bar + tools drawer),
    // so this waits out real setTimeout(0) WS-open ticks across multiple
    // waitFor polls rather than racing the default 5s budget.
    // Tracks only WebSockets that actually get constructed — the failed initial
    // attempt below rejects inside buildWsUrl(), BEFORE `new WebSocket(...)` runs.
    const wsInstances: FakeWebSocket[] = [];
    class TrackingWebSocket extends FakeWebSocket {
      constructor() {
        super();
        wsInstances.push(this);
      }
    }
    global.WebSocket = TrackingWebSocket as unknown as typeof WebSocket;

    // Dynamic import (not a static top-level import): a static value import of
    // "@/lib/api" here would run before the vi.mock factory's `apiMock` const is
    // initialized (TDZ crash) — the module is already loaded via loadView() by
    // the time a test body runs, so this just resolves the mocked binding.
    const { buildWsUrl } = await import("@/lib/api");
    const mockedBuildWsUrl = vi.mocked(buildWsUrl);
    // Simulates a backend that's down/restarting when the attach flow starts —
    // the promise that builds the WS URL (e.g. the ws-ticket fetch) rejects
    // BEFORE any socket ever opens.
    mockedBuildWsUrl.mockRejectedValueOnce(new Error("backend offline"));
    const setTimeoutSpy = vi.spyOn(window, "setTimeout");

    await renderView();
    await screen.findByText("Sessions / Windows");

    await waitFor(() => expect(mockedBuildWsUrl).toHaveBeenCalledTimes(1));
    // RED without the fix: this error is set, but nothing ever retries — the
    // terminal is stuck on "Attaching …" forever.
    await screen.findByText(/backend offline/);

    const firstSchedule = setTimeoutSpy.mock.calls.find(([, delay]) => delay === 1000);
    if (!firstSchedule) throw new Error("expected a 1000ms backoff timer to be scheduled after the initial connect failure");
    const [firstRetryCallback] = firstSchedule;

    // Simulate the 1s backoff elapsing (same effect as real time passing).
    act(() => {
      (firstRetryCallback as () => void)();
    });

    await waitFor(() => expect(mockedBuildWsUrl).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(wsInstances.length).toBe(1));
    // Desktop renders several StatusPill instances at once (header + identity
    // bar + tools drawer) — getAllByText, not findByText (which requires a
    // single match).
    await waitFor(() => expect(screen.getAllByText("attached").length).toBeGreaterThan(0));
    // Stale failure banner must clear once the retry lands.
    expect(screen.queryByText(/backend offline/)).toBeNull();

    // Attempt counter must reset on success: a later drop re-arms at 1s again,
    // not 2s (which it would if the counter had kept climbing across retries).
    const callsBeforeSecondDrop = setTimeoutSpy.mock.calls.length;
    act(() => {
      wsInstances[0].close();
    });
    const secondSchedule = setTimeoutSpy.mock.calls.slice(callsBeforeSecondDrop).find(([, delay]) => delay === 1000);
    expect(secondSchedule).toBeTruthy();
  }, 15000);
});
