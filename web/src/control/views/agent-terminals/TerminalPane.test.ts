import { describe, expect, it } from "vitest";

import { buildAttachQuery, canFitTerminal, reconnectDelayMs } from "./terminalPaneModel";

describe("TerminalPane helpers", () => {
  it("builds one isolated attachment identity per pane", () => {
    expect(buildAttachQuery({ session: "work", window: "claude" }, 2, 101, 31, true)).toEqual({
      session: "work",
      window: "claude",
      client_id: "agent-terminals-ui-pane-2",
      cols: "101",
      rows: "31",
      isolated: "1",
    });
  });

  it("omits isolated mode for a direct single-pane attach", () => {
    expect(buildAttachQuery({ session: "work", window: "hermes" }, 0, 80, 24, false)).not.toHaveProperty("isolated");
  });

  it("guards xterm fit against zero-sized hosts", () => {
    expect(canFitTerminal({ clientWidth: 0, clientHeight: 500 })).toBe(false);
    expect(canFitTerminal({ clientWidth: 500, clientHeight: 0 })).toBe(false);
    expect(canFitTerminal({ clientWidth: 500, clientHeight: 300 })).toBe(true);
  });

  it("uses capped reconnect backoff", () => {
    expect(reconnectDelayMs(0)).toBe(1000);
    expect(reconnectDelayMs(4)).toBe(15000);
    expect(reconnectDelayMs(20)).toBe(15000);
  });
});
