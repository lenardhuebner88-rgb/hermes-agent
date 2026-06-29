import { describe, expect, it } from "vitest";
import type { AgentTerminalWindow } from "@/lib/api";
import { classifyTerminalState, pickInitialTarget } from "./AgentTerminalsView";

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
  });

  it("keeps an existing window target across view switches and reconnects", () => {
    const windows = [running, dead];
    expect(pickInitialTarget(windows, "codex", { session: "hermes-agents", window: "hermes" })).toEqual({ session: "hermes-agents", window: "hermes" });
    expect(pickInitialTarget(windows, "codex", null)).toEqual({ session: "hermes-agents", window: "codex" });
    expect(pickInitialTarget([], "hermes", null)).toBeNull();
  });
});
