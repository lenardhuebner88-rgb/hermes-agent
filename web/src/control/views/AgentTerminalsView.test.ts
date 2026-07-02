import { describe, expect, it } from "vitest";
import type { AgentTerminalWindow } from "@/lib/api";
import { buildComposerPayload, chipLabel, classifyTerminalState, orderWindowsForStrip, pickInitialTarget } from "./AgentTerminalsView";

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
