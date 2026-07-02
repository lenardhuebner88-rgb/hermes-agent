import { describe, expect, it } from "vitest";
import type { AgentTerminalWindow } from "@/lib/api";
import { buildComposerPayload, classifyTerminalState, pickInitialTarget } from "./AgentTerminalsView";

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
