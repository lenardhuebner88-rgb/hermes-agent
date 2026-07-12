import { describe, expect, it } from "vitest";
import type { AgentTerminalOverviewWindow, AgentTerminalWindow } from "@/lib/api";
import {
  buildComposerPayload,
  chipLabel,
  classifyTerminalState,
  formatActivityAge,
  formatPtyResize,
  hasUnseenActivity,
  isTerminalCopyShortcut,
  orderOverviewForFleet,
  orderWindowsForStrip,
  pickInitialTarget,
  reconnectDelayMs,
  terminalSurfaceOrder,
} from "./AgentTerminalsView";

// Echtes Datenformat: 9-Fenster-Inventar aus dem Live-System (tmux list-windows -a
// -F 'session:window active pane_id pane_pid pane_current_command pane_current_path
// pane_dead' auf dem Homeserver), Shape wie AgentTerminalWindow.to_dict() im Backend
// (hermes_cli/agent_terminals.py). Reihenfolge entspricht tmux's Ausgabe-Reihenfolge —
// "kimi-goal-test" vor "work".
const LIVE_WINDOWS: AgentTerminalWindow[] = [
  { session: "kimi-goal-test", window: "python3", active: true, pane_id: "%6", pid: 2773076, command: "python3", cwd: "/home/piet/.hermes/hermes-agent", dead: false },
  { session: "work", window: "claude", active: true, pane_id: "%14", pid: 2903185, command: "claude", cwd: "/home/piet", dead: false },
  { session: "work", window: "codex", active: false, pane_id: "%1", pid: 1296, command: "node", cwd: "/home/piet", dead: false },
  { session: "work", window: "kimi", active: false, pane_id: "%2", pid: 1303, command: "kimi-code", cwd: "/home/piet/.hermes/hermes-agent", dead: false },
  {
    session: "work",
    window: "kimi-dashboard-perf",
    active: false,
    pane_id: "%7",
    pid: 2822229,
    command: "python3",
    cwd: "/home/piet/.hermes/hermes-agent/.worktrees/kimi/dashboard-polling-perf-20260630-000253 (deleted)",
    dead: false,
  },
  { session: "work", window: "hermes-agent", active: false, pane_id: "%10", pid: 2670577, command: "python3", cwd: "/home/piet/.hermes/hermes-agent", dead: false },
  { session: "work", window: "claude-agent", active: false, pane_id: "%11", pid: 2670591, command: "2.1.197", cwd: "/home/piet/.hermes/hermes-agent", dead: false },
  { session: "work", window: "kimi-agent", active: false, pane_id: "%12", pid: 2672041, command: "kimi-code", cwd: "/home/piet/.hermes/hermes-agent", dead: false },
  { session: "work", window: "codex-agent", active: false, pane_id: "%13", pid: 2692327, command: "node", cwd: "/home/piet/.hermes/hermes-agent", dead: false },
];

const running: AgentTerminalWindow = {
  session: "hermes-agents",
  window: "hermes",
  active: true,
  pane_id: "%1",
  pid: 1234,
  command: "hermes",
};

const dead: AgentTerminalWindow = {
  ...running,
  window: "codex",
  pane_id: "%2",
  pid: null,
  command: "",
};

describe("AgentTerminalsView state helpers", () => {
  it("marks attach and reconnect states explicitly", () => {
    expect(classifyTerminalState({ window: running, socketReady: true, socketConnecting: false, mobile: false })).toBe("attached");
    expect(classifyTerminalState({ window: running, socketReady: false, socketConnecting: true, mobile: false })).toBe("window running");
    expect(classifyTerminalState({ window: running, socketReady: false, socketConnecting: true, mobile: true })).toBe("Tailscale/mobile reconnect");
    expect(classifyTerminalState({ window: running, socketReady: false, socketConnecting: false, mobile: false })).toBe("detached");
  });

  it("marks empty and dead target states", () => {
    expect(classifyTerminalState({ window: null, socketReady: false, socketConnecting: false, mobile: false })).toBe("missing window");
    expect(classifyTerminalState({ window: dead, socketReady: false, socketConnecting: false, mobile: false })).toBe("dead pane");
    // pane_dead-Flag gewinnt, auch wenn tmux noch die stale pid meldet
    expect(classifyTerminalState({ window: { ...running, dead: true }, socketReady: true, socketConnecting: false, mobile: false })).toBe("dead pane");
  });

  it("builds composer payloads with submit and bracketed paste", () => {
    expect(buildComposerPayload("", true)).toBeNull();
    expect(buildComposerPayload("ls -la", false)).toBe("ls -la");
    expect(buildComposerPayload("ls -la", true)).toBe("ls -la\r");
    expect(buildComposerPayload("zeile 1\nzeile 2", true)).toBe("\x1b[200~zeile 1\nzeile 2\x1b[201~\r");
  });

  it("strips embedded bracketed-paste end sequences to prevent a paste-mode breakout", () => {
    const payload = buildComposerPayload("zeile 1\n\x1b[201~rm -rf ~\n", true);
    expect(payload).toBe("\x1b[200~zeile 1\nrm -rf ~\n\x1b[201~\r");
    expect(payload?.split("\x1b[201~").length).toBe(2);
  });

  it("keeps an existing window target across view switches and reconnects", () => {
    const windows = [running, dead];
    expect(pickInitialTarget(windows, "codex", { session: "hermes-agents", window: "hermes" })).toEqual({ session: "hermes-agents", window: "hermes" });
    expect(pickInitialTarget(windows, "codex", null)).toEqual({ session: "hermes-agents", window: "codex" });
    expect(pickInitialTarget([], "hermes", null)).toBeNull();
  });
});

describe("orderWindowsForStrip", () => {
  it("puts the work session first (stable) and keeps other sessions after it, against the live 9-window inventory", () => {
    const ordered = orderWindowsForStrip(LIVE_WINDOWS);
    expect(ordered.map((w) => `${w.session}:${w.window}`)).toEqual([
      "work:claude",
      "work:codex",
      "work:kimi",
      "work:kimi-dashboard-perf",
      "work:hermes-agent",
      "work:claude-agent",
      "work:kimi-agent",
      "work:codex-agent",
      "kimi-goal-test:python3",
    ]);
  });

  it("is a no-op when every window already belongs to work", () => {
    const workOnly = LIVE_WINDOWS.filter((w) => w.session === "work");
    expect(orderWindowsForStrip(workOnly)).toEqual(workOnly);
  });

  it("returns an empty list unchanged", () => {
    expect(orderWindowsForStrip([])).toEqual([]);
  });
});

describe("chipLabel", () => {
  it("drops the session prefix for the work session but keeps it for other sessions", () => {
    expect(chipLabel(LIVE_WINDOWS.find((w) => w.window === "claude")!)).toBe("claude");
    expect(chipLabel(LIVE_WINDOWS.find((w) => w.window === "kimi-dashboard-perf")!)).toBe("kimi-dashboard-perf");
    expect(chipLabel(LIVE_WINDOWS.find((w) => w.session === "kimi-goal-test")!)).toBe("kimi-goal-test:python3");
  });
});

describe("reconnectDelayMs", () => {
  it("ramps 1s -> 2s -> 4s -> 8s -> 15s and caps at 15s for further attempts", () => {
    expect(reconnectDelayMs(0)).toBe(1000);
    expect(reconnectDelayMs(1)).toBe(2000);
    expect(reconnectDelayMs(2)).toBe(4000);
    expect(reconnectDelayMs(3)).toBe(8000);
    expect(reconnectDelayMs(4)).toBe(15000);
    expect(reconnectDelayMs(5)).toBe(15000);
    expect(reconnectDelayMs(99)).toBe(15000);
  });

  it("clamps negative attempts to the first delay", () => {
    expect(reconnectDelayMs(-1)).toBe(1000);
  });
});

describe("hasUnseenActivity", () => {
  // Echtes Payload-Shape: activity ist ein Unix-Sekunden-Int aus AgentTerminalWindow.to_dict()
  // (hermes_cli/agent_terminals.py, #{window_activity} aus tmux list-windows).
  const claude: AgentTerminalWindow = { ...LIVE_WINDOWS.find((w) => w.window === "claude")!, activity: 1783025490 };

  it("flags a window whose activity moved past the recorded baseline", () => {
    expect(hasUnseenActivity(claude, { "work:claude": 1783025000 })).toBe(true);
  });

  it("does not flag a window with no recorded baseline yet (never established)", () => {
    expect(hasUnseenActivity(claude, {})).toBe(false);
  });

  it("does not flag a window whose activity already matches the recorded baseline", () => {
    expect(hasUnseenActivity(claude, { "work:claude": 1783025490 })).toBe(false);
  });

  it("never flags a window without an activity timestamp", () => {
    const noActivity: AgentTerminalWindow = { ...claude, activity: null };
    expect(hasUnseenActivity(noActivity, {})).toBe(false);
    expect(hasUnseenActivity(noActivity, { "work:claude": 0 })).toBe(false);
  });
});

describe("formatActivityAge", () => {
  it("formats seconds, minutes and hours against a real Unix-second baseline", () => {
    const now = 1783025500;
    expect(formatActivityAge(now, now - 10)).toBe("vor 10s");
    expect(formatActivityAge(now, now - 185)).toBe("vor 3m");
    expect(formatActivityAge(now, now - 7300)).toBe("vor 2h");
  });

  it("clamps a slightly-in-the-future activity (clock skew) to 0s instead of a negative age", () => {
    expect(formatActivityAge(1783025500, 1783025501)).toBe("vor 0s");
  });

  it("renders a dash when there is no activity timestamp", () => {
    expect(formatActivityAge(1783025500, null)).toBe("—");
  });
});

describe("orderOverviewForFleet", () => {
  // Realistische Zustände aus TmuxAgentSessionService.overview() (hermes_cli/agent_terminals.py):
  // state ist "dead" | "frage" | "laeuft" | "wartet" | "idle", tail ist ANSI-bereinigt und mehrzeilig.
  const OVERVIEW_FIXTURE: AgentTerminalOverviewWindow[] = [
    {
      session: "work",
      window: "kimi",
      active: false,
      pane_id: "%2",
      pid: 1303,
      command: "kimi-code",
      cwd: "/home/piet/.hermes/hermes-agent",
      dead: false,
      activity: 1783025100,
      tail: "> Analysiere Testabdeckung …\n─ ready │",
      state: "wartet",
      state_source: "heuristic",
    },
    {
      session: "work",
      window: "codex",
      active: false,
      pane_id: "%1",
      pid: 1296,
      command: "node",
      cwd: "/home/piet",
      dead: false,
      activity: 1783025480,
      tail: "Working (12s · esc to interrupt)\n▌ Building the fix …",
      state: "laeuft",
      state_source: "heuristic",
    },
    {
      session: "work",
      window: "claude",
      active: true,
      pane_id: "%14",
      pid: 2903185,
      command: "claude",
      cwd: "/home/piet",
      dead: false,
      activity: 1783024000,
      tail: "Allow this action? (y/n)",
      state: "frage",
      state_source: "heuristic",
    },
    {
      session: "work",
      window: "hermes-agent",
      active: false,
      pane_id: "%10",
      pid: null,
      command: "",
      cwd: "/home/piet/.hermes/hermes-agent",
      dead: true,
      activity: 1783020000,
      tail: null,
      state: "dead",
      state_source: "heuristic",
    },
    {
      session: "kimi-goal-test",
      window: "python3",
      active: true,
      pane_id: "%6",
      pid: 2773076,
      command: "python3",
      cwd: "/home/piet/.hermes/hermes-agent",
      dead: false,
      activity: 1783010000,
      tail: "$ ",
      state: "idle",
      state_source: "heuristic",
    },
  ];

  it("orders frage > laeuft > wartet > idle > dead", () => {
    const ordered = orderOverviewForFleet(OVERVIEW_FIXTURE);
    expect(ordered.map((w) => w.state)).toEqual(["frage", "laeuft", "wartet", "idle", "dead"]);
    expect(ordered.map((w) => `${w.session}:${w.window}`)).toEqual([
      "work:claude",
      "work:codex",
      "work:kimi",
      "kimi-goal-test:python3",
      "work:hermes-agent",
    ]);
  });

  it("keeps the relative order of entries that share the same state (stable sort)", () => {
    const twoRunning: AgentTerminalOverviewWindow[] = [
      { ...OVERVIEW_FIXTURE[1], window: "codex-agent" },
      { ...OVERVIEW_FIXTURE[1], window: "codex" },
    ];
    expect(orderOverviewForFleet(twoRunning).map((w) => w.window)).toEqual(["codex-agent", "codex"]);
  });

  it("returns an empty list unchanged", () => {
    expect(orderOverviewForFleet([])).toEqual([]);
  });
});

describe("formatPtyResize", () => {
  // Live-Incident-Fixture (2026-07-03): Handy = 69 Spalten, Keyboard-offen = 35 Zeilen,
  // Keyboard-zu = 49 Zeilen. Echte Sequenz aus dem Resize-Storm: "\x1b[RESIZE:69;35]".
  it("formats the live incident fixture correctly (69×35, keyboard open)", () => {
    expect(formatPtyResize(69, 35)).toBe("\x1b[RESIZE:69;35]");
  });

  it("formats the live incident fixture correctly (69×49, keyboard closed)", () => {
    expect(formatPtyResize(69, 49)).toBe("\x1b[RESIZE:69;49]");
  });

  it("floors fractional dimensions", () => {
    expect(formatPtyResize(69.9, 48.7)).toBe("\x1b[RESIZE:69;48]");
  });

  it("clamps 0 to minimum 2", () => {
    expect(formatPtyResize(0, 0)).toBe("\x1b[RESIZE:2;2]");
  });

  it("clamps 1 to minimum 2", () => {
    expect(formatPtyResize(1, 1)).toBe("\x1b[RESIZE:2;2]");
  });

  it("clamps NaN to minimum 2", () => {
    expect(formatPtyResize(NaN, NaN)).toBe("\x1b[RESIZE:2;2]");
  });

  it("clamps negative values to minimum 2", () => {
    expect(formatPtyResize(-5, -10)).toBe("\x1b[RESIZE:2;2]");
  });

  it("formats the default 80×24 size", () => {
    expect(formatPtyResize(80, 24)).toBe("\x1b[RESIZE:80;24]");
  });
});

describe("isTerminalCopyShortcut", () => {
  const event = (patch: Partial<KeyboardEvent>): KeyboardEvent =>
    ({ ctrlKey: false, metaKey: false, shiftKey: false, key: "", ...patch }) as KeyboardEvent;

  it("accepts Ctrl+Shift+C in both key casings", () => {
    expect(isTerminalCopyShortcut(event({ ctrlKey: true, shiftKey: true, key: "C" }))).toBe(true);
    expect(isTerminalCopyShortcut(event({ ctrlKey: true, shiftKey: true, key: "c" }))).toBe(true);
  });

  it("accepts Ctrl+Insert", () => {
    expect(isTerminalCopyShortcut(event({ ctrlKey: true, key: "Insert" }))).toBe(true);
  });

  // Plain Ctrl+C must keep reaching tmux as ETX — it is the agent interrupt.
  it("rejects plain Ctrl+C so the interrupt still reaches the agent", () => {
    expect(isTerminalCopyShortcut(event({ ctrlKey: true, key: "c" }))).toBe(false);
    expect(isTerminalCopyShortcut(event({ ctrlKey: true, key: "C" }))).toBe(false);
  });

  it("rejects unrelated keys and bare modifiers", () => {
    expect(isTerminalCopyShortcut(event({ ctrlKey: true, shiftKey: true, key: "V" }))).toBe(false);
    expect(isTerminalCopyShortcut(event({ shiftKey: true, key: "C" }))).toBe(false);
    expect(isTerminalCopyShortcut(event({ key: "Insert" }))).toBe(false);
  });

  // Cmd+C on macOS is the OS-level copy — the browser already handles it.
  it("rejects meta-key combinations", () => {
    expect(isTerminalCopyShortcut(event({ metaKey: true, ctrlKey: true, shiftKey: true, key: "C" }))).toBe(false);
  });
});

describe("terminalSurfaceOrder", () => {
  // Mirrors what Element.closest() gives the handler: the nearest ancestor carrying
  // data-terminal-surface, or null when the event came from outside every xterm.
  const target = (surface: string | null): EventTarget =>
    ({
      closest: (selector: string) =>
        selector === "[data-terminal-surface]" && surface !== null
          ? { getAttribute: () => surface }
          : null,
    }) as unknown as EventTarget;

  it("returns the pane order of the xterm surface the event came from", () => {
    expect(terminalSurfaceOrder(target("0"))).toBe(0);
    expect(terminalSurfaceOrder(target("3"))).toBe(3);
  });

  // The blocking case: a stale terminal selection must not let Ctrl+Shift+C in the
  // composer or the rename field copy old terminal output instead of the field's text.
  it("returns null for events outside any terminal surface", () => {
    expect(terminalSurfaceOrder(target(null))).toBeNull();
    expect(terminalSurfaceOrder(null)).toBeNull();
    expect(terminalSurfaceOrder({} as EventTarget)).toBeNull();
  });

  it("returns null for a malformed pane order", () => {
    expect(terminalSurfaceOrder(target(""))).toBeNull();
    expect(terminalSurfaceOrder(target("pane"))).toBeNull();
    expect(terminalSurfaceOrder(target("-1"))).toBeNull();
  });
});
