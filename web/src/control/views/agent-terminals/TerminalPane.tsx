import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { Terminal } from "@xterm/xterm";

import { buildWsUrl } from "../../../lib/api";
import {
  createHermesXtermSurface,
  TERMINAL_PANE_BACKGROUND,
  TERMINAL_THEME_STATIC,
} from "../../../lib/xtermSurface";
import type { TerminalTarget } from "./layout";
import { buildAttachQuery, canFitTerminal, formatPtyResize, reconnectDelayMs } from "./terminalPaneModel";

const RESIZE_SEND_DEBOUNCE_MS = 300;

export interface TerminalPaneConnectionState {
  ready: boolean;
  connecting: boolean;
  error: string | null;
}

export interface TerminalPaneHandle {
  sendRaw(raw: string): boolean;
  fitAndResize(): void;
  focus(): void;
  reconnect(): void;
  scrollLines(lines: number): void;
  scrollPages(pages: number): void;
  scrollToBottom(): void;
  getSelection(): string;
}

export interface TerminalPaneProps {
  target: TerminalTarget;
  paneOrder: number;
  fontSize: number;
  isolated?: boolean;
  active?: boolean;
  className?: string;
  onActivate?: () => void;
  onConnectionChange?: (state: TerminalPaneConnectionState) => void;
}

export const TerminalPane = forwardRef<TerminalPaneHandle, TerminalPaneProps>(function TerminalPane(
  {
    target,
    paneOrder,
    fontSize,
    isolated = true,
    active = false,
    className = "",
    onActivate,
    onConnectionChange,
  },
  forwardedRef,
) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<ReturnType<typeof createHermesXtermSurface>["fit"] | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectAttemptRef = useRef(0);
  const resizeTimerRef = useRef<number | null>(null);
  const manualCloseRef = useRef(false);
  const [nonce, setNonce] = useState(0);
  const [connection, setConnection] = useState<TerminalPaneConnectionState>({
    ready: false,
    connecting: true,
    error: null,
  });

  const updateConnection = useCallback((patch: Partial<TerminalPaneConnectionState>) => {
    setConnection((current) => ({ ...current, ...patch }));
  }, []);

  const sendResize = useCallback(() => {
    const term = termRef.current;
    const ws = wsRef.current;
    if (!term || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(formatPtyResize(term.cols, term.rows));
  }, []);

  const fitAndResize = useCallback(() => {
    const host = hostRef.current;
    if (!host || !canFitTerminal(host)) return;
    try {
      fitRef.current?.fit();
    } catch {
      return;
    }
    if (resizeTimerRef.current !== null) window.clearTimeout(resizeTimerRef.current);
    resizeTimerRef.current = window.setTimeout(() => {
      resizeTimerRef.current = null;
      sendResize();
    }, RESIZE_SEND_DEBOUNCE_MS);
  }, [sendResize]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const surface = createHermesXtermSurface({
      host,
      theme: { ...TERMINAL_THEME_STATIC, background: TERMINAL_PANE_BACKGROUND },
      loggerName: "AgentTerminalPane",
      appAwareWheel: true,
      terminalOptions: { fontSize: 13 },
    });
    const { term, fit } = surface;
    termRef.current = term;
    fitRef.current = fit;
    const dataSubscription = term.onData((raw) => {
      const ws = wsRef.current;
      if (ws?.readyState === WebSocket.OPEN) ws.send(raw);
    });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fitAndResize);
    observer?.observe(host);
    window.setTimeout(fitAndResize, 0);
    return () => {
      observer?.disconnect();
      dataSubscription.dispose();
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
    };
  }, [fitAndResize]); // xterm owns a stable host for the lifetime of this pane.

  useEffect(() => {
    const term = termRef.current;
    if (!term) return;
    term.options.fontSize = fontSize;
    window.setTimeout(fitAndResize, 0);
  }, [fitAndResize, fontSize]);

  useEffect(() => {
    onConnectionChange?.(connection);
  }, [connection, onConnectionChange]);

  useEffect(() => {
    let disposed = false;
    manualCloseRef.current = false;
    reconnectAttemptRef.current = 0;
    termRef.current?.clear();
    termRef.current?.writeln(`Attaching ${target.session}:${target.window} …`);

    const clearReconnect = () => {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    const scheduleReconnect = () => {
      if (disposed || manualCloseRef.current || reconnectTimerRef.current !== null) return;
      const attempt = reconnectAttemptRef.current;
      reconnectAttemptRef.current = attempt + 1;
      const delay = reconnectDelayMs(attempt) + paneOrder * 70;
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null;
        void connect();
      }, delay);
    };

    const connect = async () => {
      if (disposed) return;
      updateConnection({ ready: false, connecting: true, error: null });
      try {
        const term = termRef.current;
        const query = buildAttachQuery(
          { session: target.session, window: target.window }, paneOrder,
          term?.cols || 80, term?.rows || 24, isolated,
        );
        const url = await buildWsUrl("/api/agent-terminals/attach", query);
        if (disposed) return;
        const ws = new WebSocket(url);
        ws.binaryType = "arraybuffer";
        wsRef.current = ws;
        ws.onopen = () => {
          if (disposed || wsRef.current !== ws) return;
          reconnectAttemptRef.current = 0;
          updateConnection({ ready: true, connecting: false, error: null });
          const current = termRef.current;
          if (current) ws.send(formatPtyResize(current.cols, current.rows));
          window.setTimeout(fitAndResize, 0);
        };
        ws.onmessage = (event) => {
          if (disposed || wsRef.current !== ws) return;
          const payload = event.data;
          if (payload instanceof ArrayBuffer) termRef.current?.write(new Uint8Array(payload));
          else termRef.current?.write(String(payload));
        };
        ws.onerror = () => {
          if (disposed || wsRef.current !== ws) return;
          updateConnection({ error: "Terminal-Verbindung fehlgeschlagen." });
        };
        ws.onclose = (event) => {
          if (wsRef.current === ws) wsRef.current = null;
          if (disposed) return;
          updateConnection({
            ready: false,
            connecting: true,
            error: event.code === 1000 ? null : "Terminal getrennt – erneuter Verbindungsversuch …",
          });
          scheduleReconnect();
        };
      } catch (error) {
        if (disposed) return;
        updateConnection({
          ready: false,
          connecting: true,
          error: error instanceof Error ? error.message : "Terminal-Verbindung fehlgeschlagen.",
        });
        scheduleReconnect();
      }
    };

    void connect();
    return () => {
      disposed = true;
      manualCloseRef.current = true;
      clearReconnect();
      const ws = wsRef.current;
      wsRef.current = null;
      if (ws && ws.readyState < WebSocket.CLOSING) ws.close(1000, "pane-unmount");
    };
  }, [fitAndResize, isolated, nonce, paneOrder, target.session, target.window, updateConnection]);

  useEffect(() => {
    if (active) termRef.current?.focus();
  }, [active]);

  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState !== "visible") return;
      const ws = wsRef.current;
      if (!ws || ws.readyState === WebSocket.CLOSED) setNonce((value) => value + 1);
      else window.setTimeout(fitAndResize, 0);
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [fitAndResize]);

  useImperativeHandle(
    forwardedRef,
    () => ({
      sendRaw(raw) {
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) return false;
        ws.send(raw);
        return true;
      },
      fitAndResize,
      focus() {
        termRef.current?.focus();
      },
      reconnect() {
        const ws = wsRef.current;
        wsRef.current = null;
        if (ws && ws.readyState < WebSocket.CLOSING) ws.close(1000, "manual-reconnect");
        setNonce((value) => value + 1);
      },
      scrollLines(lines) {
        termRef.current?.scrollLines(lines);
      },
      scrollPages(pages) {
        termRef.current?.scrollPages(pages);
      },
      scrollToBottom() {
        termRef.current?.scrollToBottom();
      },
      getSelection() {
        return termRef.current?.getSelection() ?? "";
      },
    }),
    [fitAndResize],
  );

  return (
    <div
      ref={hostRef}
      data-testid={`terminal-pane-host-${paneOrder}`}
      data-ready={connection.ready ? "true" : "false"}
      className={`h-full min-h-0 min-w-0 overflow-hidden bg-surface-0 ${className}`}
      onMouseDown={onActivate}
      onFocus={onActivate}
      tabIndex={0}
    />
  );
});
