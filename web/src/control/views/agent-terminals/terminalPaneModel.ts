import type { TerminalTarget } from "./layout";

const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000];

export function reconnectDelayMs(attempt: number): number {
  const index = Math.min(Math.max(0, attempt), RECONNECT_DELAYS_MS.length - 1);
  return RECONNECT_DELAYS_MS[index];
}

export function canFitTerminal(host: Pick<HTMLElement, "clientWidth" | "clientHeight">): boolean {
  return host.clientWidth > 0 && host.clientHeight > 0;
}

export function formatPtyResize(cols: number, rows: number): string {
  const c = Math.max(2, Number.isFinite(cols) ? Math.floor(cols) : 0);
  const r = Math.max(2, Number.isFinite(rows) ? Math.floor(rows) : 0);
  return `\x1b[RESIZE:${c};${r}]`;
}

export function buildAttachQuery(
  target: TerminalTarget,
  paneOrder: number,
  cols: number,
  rows: number,
  isolated: boolean,
): Record<string, string> {
  return {
    session: target.session, window: target.window,
    client_id: `agent-terminals-ui-pane-${paneOrder}`,
    cols: String(cols), rows: String(rows),
    ...(isolated ? { isolated: "1" } : {}),
  };
}
