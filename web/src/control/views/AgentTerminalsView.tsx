import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";

import { Activity, AlertTriangle, ChevronUp, PanelLeft, PanelRight, RefreshCw, Server, TerminalSquare, X } from "lucide-react";
import {
  api,
  buildWsUrl,
  type AgentTerminalCapabilityState,
  type AgentTerminalKind,
  type AgentTerminalWindow,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { createHermesXtermSurface, TERMINAL_THEME_STATIC } from "@/lib/xtermSurface";

const AGENTS: Array<{ kind: AgentTerminalKind; label: string; hint: string }> = [
  { kind: "hermes", label: "Hermes", hint: "hermes --tui" },
  { kind: "claude", label: "Claude", hint: "claude-cli" },
  { kind: "codex", label: "Codex", hint: "codex-cli" },
  { kind: "kimi", label: "Kimi", hint: "kimi-cli" },
];

export type TerminalUiState =
  | "attached"
  | "detached"
  | "window running"
  | "dead pane"
  | "missing window"
  | "Tailscale/mobile reconnect";

export function classifyTerminalState(args: {
  window: AgentTerminalWindow | null;
  socketReady: boolean;
  socketConnecting: boolean;
  mobile: boolean;
}): TerminalUiState {
  if (!args.window) return "missing window";
  if (!args.window.pid) return "dead pane";
  if (args.socketReady) return "attached";
  if (args.mobile && args.socketConnecting) return "Tailscale/mobile reconnect";
  if (args.socketConnecting) return "window running";
  return "detached";
}

export function pickInitialTarget(
  windows: AgentTerminalWindow[],
  preferredKind: AgentTerminalKind,
  previous: { session: string; window: string } | null,
): { session: string; window: string } | null {
  if (!windows.length) return null;
  if (previous && windows.some((w) => w.session === previous.session && w.window === previous.window)) return previous;
  const preferred = windows.find((w) => w.window === preferredKind);
  return preferred ? { session: preferred.session, window: preferred.window } : { session: windows[0].session, window: windows[0].window };
}

function targetFromWindow(window: AgentTerminalWindow): { session: string; window: string } {
  return { session: window.session, window: window.window };
}

function useIsMobile(): boolean {
  const [mobile, setMobile] = useState(() => (typeof window === "undefined" ? false : window.innerWidth < 768));
  useEffect(() => {
    if (typeof window === "undefined") return;
    const media = window.matchMedia("(max-width: 767px)");
    const onChange = () => setMobile(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);
  return mobile;
}

function StatusPill({ state }: { state: TerminalUiState }) {
  const tone =
    state === "attached"
      ? "border-emerald-400/40 bg-emerald-400/10 text-emerald-200"
      : state === "window running"
        ? "border-sky-400/40 bg-sky-400/10 text-sky-200"
        : state === "Tailscale/mobile reconnect"
          ? "border-amber-400/40 bg-amber-400/10 text-amber-100"
          : "border-red-400/35 bg-red-400/10 text-red-100";
  return <span className={cn("rounded-full border px-2 py-0.5 text-[11px] font-medium", tone)}>{state}</span>;
}

function CapabilityPill({ capability, agent }: { capability: AgentTerminalCapabilityState | null; agent: (typeof AGENTS)[number] }) {
  const windowsOk = capability?.tmux_available ?? false;
  const hermesOk = agent.kind === "hermes" ? (capability?.hermes_tui_available ?? false) : windowsOk;
  const ok = windowsOk && hermesOk;
  const label = ok ? "verfügbar" : capability?.reason?.includes("symlink") ? "kaputter Symlink/Binary" : capability?.reason ? "fehlend" : "unbekannt";
  const title = capability?.reason ?? (agent.kind === "hermes" ? capability?.hermes_binary ?? agent.hint : agent.hint);
  return (
    <span title={title} className={cn("rounded border px-1.5 py-0.5 text-[10px]", ok ? "border-emerald-400/35 text-emerald-200" : "border-amber-400/35 text-amber-100")}>
      {label}
    </span>
  );
}

export function AgentTerminalsView() {
  const mobile = useIsMobile();
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [capability, setCapability] = useState<AgentTerminalCapabilityState | null>(null);
  const [windows, setWindows] = useState<AgentTerminalWindow[]>([]);
  const [selectedKind, setSelectedKind] = useState<AgentTerminalKind>("hermes");
  const [target, setTarget] = useState<{ session: string; window: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [socketReady, setSocketReady] = useState(false);
  const [socketConnecting, setSocketConnecting] = useState(false);

  const selectedWindow = useMemo(() => {
    if (!target) return null;
    return windows.find((w) => w.session === target.session && w.window === target.window) ?? null;
  }, [target, windows]);

  const sessions = useMemo(() => Array.from(new Set(windows.map((w) => w.session))), [windows]);
  const state = classifyTerminalState({ window: selectedWindow, socketReady, socketConnecting, mobile });

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cap, win] = await Promise.all([api.getAgentTerminalCapabilities(), api.getAgentTerminalWindows()]);
      setCapability(cap);
      setWindows(win.windows);
      setTarget((previous) => pickInitialTarget(win.windows, selectedKind, previous));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [selectedKind]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const match = windows.find((w) => w.window === selectedKind);
    if (match) setTarget(targetFromWindow(match));
  }, [selectedKind, windows]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const { term, fit } = createHermesXtermSurface({
      host,
      theme: { ...TERMINAL_THEME_STATIC, background: "#071b1d" },
      scrollback: 4000,
      loggerName: "agent-terminals",
      onWheelScrollBuffer: false,
    });
    termRef.current = term;
    fitRef.current = fit;
    const resize = () => {
      try {
        fit.fit();
      } catch {}
    };
    requestAnimationFrame(resize);
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    return () => {
      observer.disconnect();
      wsRef.current?.close();
      wsRef.current = null;
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
    };
  }, []);

  useEffect(() => {
    const term = termRef.current;
    if (!term || !target) return;
    let disposed = false;
    let dataDisposable: { dispose: () => void } | null = null;
    wsRef.current?.close();
    wsRef.current = null;
    setSocketReady(false);
    setSocketConnecting(true);
    term.clear();
    term.writeln(`Attaching ${target.session}:${target.window} …`);

    void buildWsUrl("/api/agent-terminals/attach", { session: target.session, window: target.window, client_id: "agent-terminals-ui" })
      .then((url) => {
        if (disposed) return;
        const ws = new WebSocket(url);
        ws.binaryType = "arraybuffer";
        wsRef.current = ws;
        dataDisposable = term.onData((data) => {
          if (ws.readyState === WebSocket.OPEN) ws.send(data);
        });
        ws.onopen = () => {
          if (disposed) return;
          setSocketReady(true);
          setSocketConnecting(false);
          term.clear();
        };
        ws.onmessage = (event) => {
          if (typeof event.data === "string") {
            term.write(event.data);
          } else if (event.data instanceof ArrayBuffer) {
            term.write(new Uint8Array(event.data));
          }
        };
        ws.onerror = () => {
          if (!disposed) setSocketConnecting(false);
        };
        ws.onclose = () => {
          if (disposed) return;
          setSocketReady(false);
          setSocketConnecting(false);
        };
      })
      .catch((err) => {
        setSocketConnecting(false);
        setError(err instanceof Error ? err.message : String(err));
      });

    return () => {
      disposed = true;
      dataDisposable?.dispose();
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [target]);

  const ensureAgent = async (kind: AgentTerminalKind) => {
    setSelectedKind(kind);
    setError(null);
    try {
      const response = await api.ensureAgentTerminalWindow(kind);
      setTarget(targetFromWindow(response.window));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const sessionList = (
    <div className="flex h-full flex-col gap-3">
      <div className="flex items-center justify-between">
        <div>
          <p className="hc-eyebrow">tmux</p>
          <h2 className="text-sm font-semibold text-white">Sessions / Windows</h2>
        </div>
        <button type="button" onClick={() => void refresh()} aria-label="Refresh agent terminals" className="rounded-md border border-white/10 p-1.5 text-white/65 hover:bg-white/10"><RefreshCw className="h-4 w-4" /></button>
      </div>
      <div className="grid gap-2">
        {sessions.length === 0 && <div className="rounded-lg border border-white/10 p-3 text-xs text-white/60">Keine tmux-Session gefunden.</div>}
        {sessions.map((session) => (
          <div key={session} className="rounded-lg border border-white/10 bg-black/15 p-2">
            <div className="mb-2 flex items-center gap-2 text-xs font-medium text-white/75"><Server className="h-3.5 w-3.5" />{session}</div>
            <div className="grid gap-1">
              {windows.filter((w) => w.session === session).map((win) => {
                const active = target?.session === win.session && target.window === win.window;
                return (
                  <button key={`${win.session}:${win.window}`} type="button" onClick={() => { setTarget(targetFromWindow(win)); setSessionsOpen(false); }} className={cn("rounded-md border px-2 py-2 text-left text-xs transition", active ? "border-cyan-300/60 bg-cyan-300/10 text-cyan-100" : "border-transparent text-white/65 hover:border-white/10 hover:bg-white/5")}> 
                    <span className="flex items-center justify-between gap-2"><span>{win.window}</span><span className={cn("h-2 w-2 rounded-full", win.pid ? "bg-emerald-300" : "bg-red-300")} /></span>
                    <span className="mt-0.5 block truncate text-[10px] text-white/40">{win.command || "dead pane"}</span>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );

  const toolsDrawer = (
    <div className="grid gap-3 text-sm">
      <div className="flex items-center justify-between gap-3">
        <div><p className="hc-eyebrow">Tools / Handoff</p><h2 className="font-semibold text-white">Terminal-Kontext</h2></div>
        {mobile && <button type="button" onClick={() => setToolsOpen(false)} className="rounded-md border border-white/10 p-1.5 text-white/65 hover:bg-white/10"><X className="h-4 w-4" /></button>}
      </div>
      <div className="grid gap-2 rounded-xl border border-white/10 bg-black/20 p-3 text-xs text-white/70">
        <div className="flex justify-between"><span>Target</span><span className="text-white">{target ? `${target.session}:${target.window}` : "—"}</span></div>
        <div className="flex justify-between"><span>Attach</span><StatusPill state={state} /></div>
        <div className="flex justify-between"><span>Input</span><span className="text-white/80">nur User-Tasten, kein Auto-Send</span></div>
        <div className="flex justify-between"><span>Mobile</span><span className="text-white/80">reattach an dasselbe tmux-Fenster</span></div>
      </div>
      <div className="rounded-xl border border-amber-300/20 bg-amber-300/10 p-3 text-xs text-amber-50">
        Handoff bleibt optional: Diese Fläche erzwingt keinen Prompt- oder Übergabe-Flow.
      </div>
    </div>
  );

  return (
    <div className="flex min-h-[calc(100vh-8rem)] flex-col gap-3 text-white">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-white/10 bg-[#071b1d]/90 p-3 shadow-[0_18px_60px_rgba(0,0,0,0.22)]">
        <div className="min-w-0">
          <p className="hc-eyebrow">Agent Terminals</p>
          <div className="mt-1 flex flex-wrap items-center gap-2"><StatusPill state={state} />{loading && <span className="text-xs text-white/50">lädt…</span>}{error && <span className="inline-flex items-center gap-1 text-xs text-red-200"><AlertTriangle className="h-3 w-3" />{error}</span>}</div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {AGENTS.map((agent) => (
            <button key={agent.kind} type="button" onClick={() => void ensureAgent(agent.kind)} className={cn("rounded-lg border px-2.5 py-1.5 text-left text-xs", selectedKind === agent.kind ? "border-cyan-300/60 bg-cyan-300/10 text-cyan-100" : "border-white/10 bg-white/[0.03] text-white/70 hover:bg-white/[0.06]")}> 
              <span className="flex items-center gap-2"><TerminalSquare className="h-3.5 w-3.5" />{agent.label}<CapabilityPill capability={capability} agent={agent} /></span>
            </button>
          ))}
        </div>
      </div>

      <div className="grid flex-1 gap-3 md:grid-cols-[260px_minmax(0,1fr)_280px]">
        <aside className="hidden min-h-[540px] rounded-2xl border border-white/10 bg-black/20 p-3 md:block">{sessionList}</aside>
        <section className="min-h-[540px] overflow-hidden rounded-2xl border border-white/10 bg-[#041113]">
          <div className="flex items-center justify-between border-b border-white/10 px-3 py-2 text-xs text-white/65">
            <div className="flex items-center gap-2"><Activity className="h-3.5 w-3.5" />{target ? `${target.session}:${target.window}` : "missing window"}</div>
            <div className="flex items-center gap-1 md:hidden"><button type="button" onClick={() => setSessionsOpen(true)} className="inline-flex items-center rounded-md border border-white/10 px-2 py-1 text-white/70 hover:bg-white/10"><PanelLeft className="mr-1 h-3.5 w-3.5" />Sessions</button><button type="button" onClick={() => setToolsOpen(true)} className="inline-flex items-center rounded-md border border-white/10 px-2 py-1 text-white/70 hover:bg-white/10"><PanelRight className="mr-1 h-3.5 w-3.5" />Tools</button></div>
          </div>
          {!target && !loading ? (
            <div className="grid h-[480px] place-items-center p-6 text-center text-sm text-white/55">Kein tmux-Fenster verfügbar. Agent oben wählen, um eins anzulegen.</div>
          ) : (
            <div ref={hostRef} className="h-[540px] w-full overflow-hidden md:h-[calc(100vh-17rem)]" />
          )}
        </section>
        <aside className="hidden min-h-[540px] rounded-2xl border border-white/10 bg-black/20 p-3 md:block">{toolsDrawer}</aside>
      </div>

      {mobile && sessionsOpen && <div className="fixed inset-0 z-50 bg-black/55 p-3"><div className="h-full overflow-auto rounded-2xl border border-white/10 bg-[#071b1d] p-3"><div className="mb-2 flex justify-end"><button type="button" onClick={() => setSessionsOpen(false)} className="rounded-md border border-white/10 p-1.5 text-white/65 hover:bg-white/10"><X className="h-4 w-4" /></button></div>{sessionList}</div></div>}
      {mobile && toolsOpen && <div className="fixed inset-x-0 bottom-0 z-50 rounded-t-3xl border border-white/10 bg-[#071b1d] p-4 shadow-2xl"><div className="mx-auto mb-3 h-1 w-12 rounded-full bg-white/20" />{toolsDrawer}</div>}
      {mobile && !toolsOpen && <button type="button" onClick={() => setToolsOpen(true)} className="fixed bottom-4 right-4 z-40 rounded-full border border-cyan-300/40 bg-[#0b2d31] px-3 py-2 text-xs text-cyan-100 shadow-xl"><ChevronUp className="mr-1 inline h-3.5 w-3.5" />Tools</button>}
    </div>
  );
}
