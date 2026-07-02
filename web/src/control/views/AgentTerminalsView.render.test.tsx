// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { AgentTerminalCapabilityState, AgentTerminalOverviewResponse, AgentTerminalWindow } from "@/lib/api";

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
  renameAgentTerminalWindow: vi.fn(),
  getAgentTerminalOverview: vi.fn(),
  sendAgentTerminalKeys: vi.fn(),
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
  window.localStorage.clear();
  installDom(false);
  apiMock.getAgentTerminalCapabilities.mockResolvedValue(capability);
  apiMock.getAgentTerminalWindows.mockResolvedValue({ windows });
  apiMock.ensureAgentTerminalWindow.mockImplementation(async (kind: string) => ({ window: windows.find((w) => w.window === kind) ?? windows[0] }));
  apiMock.createAgentTerminalWindow.mockImplementation(async (kind: string) => ({ window: windows.find((w) => w.window === kind) ?? windows[0] }));
  apiMock.getAgentTerminalOverview.mockResolvedValue(overviewFixture);
  apiMock.sendAgentTerminalKeys.mockResolvedValue({ ok: true });
  apiMock.renameAgentTerminalWindow.mockResolvedValue({ window: windows[0] });
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

describe("AgentTerminalsView desktop rendering", () => {
  it("renders the desktop three-column terminal shell and switches sessions", async () => {
    await renderView();

    expect(await screen.findByText("Sessions / Windows")).not.toBeNull();
    expect(screen.getByText("Terminal-Kontext")).not.toBeNull();
    fireEvent.click((await screen.findAllByText("codex"))[0]);
    expect(screen.getAllByText("hermes-agents:codex").length).toBeGreaterThan(0);
    expect(await screen.findByText("/home/piet/.hermes/hermes-agent")).toBeTruthy();
    expect(screen.getByText("node/codex")).toBeTruthy();
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

  it("opens the create-session modal and resets a stale workdir localStorage key to home after capability load", async () => {
    window.localStorage.setItem("hermes-terminals-workdir", "gibt-es-nicht");
    await renderView();

    await waitFor(() => expect(window.localStorage.getItem("hermes-terminals-workdir")).toBe("home"));
    fireEvent.click(await screen.findByRole("button", { name: "Neue Session" }));
    expect((screen.getByLabelText("Arbeitsverzeichnis für neue Terminals") as HTMLSelectElement).value).toBe("home");
  });

  it("creates a new session via the desktop create modal", async () => {
    await renderView();

    fireEvent.click(await screen.findByRole("button", { name: "Neue Session" }));
    fireEvent.click(screen.getByRole("button", { name: /Codex/ }));
    fireEvent.click(screen.getByRole("button", { name: "Session starten" }));

    await waitFor(() => expect(apiMock.createAgentTerminalWindow).toHaveBeenCalledWith("codex", "home"));
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
