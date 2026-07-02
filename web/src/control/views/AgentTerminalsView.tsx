import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";

import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ChevronsDown,
  ChevronsUp,
  ClipboardList,
  CornerDownLeft,
  Gauge,
  Inbox,
  Maximize2,
  Minimize2,
  PanelLeft,
  PanelRight,
  RefreshCw,
  RotateCcw,
  Server,
  Share2,
  Sparkles,
  TerminalSquare,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import {
  api,
  buildWsUrl,
  type ControlOverviewDecisionQueueResponse,
  type ControlOverviewHealthResponse,
  type ControlOverviewKanbanBoardResponse,
  type ControlOverviewKanbanTask,
  type ControlOverviewVaultResponse,
  type AgentTerminalCapabilityState,
  type AgentTerminalKind,
  type AgentTerminalWindow,
  type AgentTerminalWorkdirOption,
  type SkillInfo,
  type ToolsetInfo,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { createHermesXtermSurface, TERMINAL_THEME_STATIC } from "@/lib/xtermSurface";
import { TerminalHandoffPanel } from "./TerminalHandoffPanel";

const AGENTS: Array<{ kind: AgentTerminalKind; label: string; hint: string }> = [
  { kind: "hermes", label: "Hermes", hint: "hermes --tui" },
  { kind: "claude", label: "Claude", hint: "claude-cli" },
  { kind: "codex", label: "Codex", hint: "codex-cli" },
  { kind: "kimi", label: "Kimi", hint: "kimi-cli" },
];

const AGENT_LABELS: Record<AgentTerminalKind, string> = Object.fromEntries(AGENTS.map((agent) => [agent.kind, agent.label])) as Record<AgentTerminalKind, string>;
const AGENT_KINDS = new Set<AgentTerminalKind>(AGENTS.map((agent) => agent.kind));

const TMUX_PREFIX = "\x02";
const TMUX_COPY_MODE = `${TMUX_PREFIX}[`;
const TMUX_PAGE_UP = `${TMUX_PREFIX}\x1b[5~`;
const TMUX_LINE_STEP = 5;

const WORKDIR_STORAGE_KEY = "hermes-terminals-workdir";
const FONT_STORAGE_KEY = "hermes-terminals-fontsize";
const FONT_MIN = 8;
const FONT_MAX = 20;

// Fallback, falls capabilities noch nicht geladen sind — Wahrheit kommt vom Backend.
const FALLBACK_WORKDIRS: AgentTerminalWorkdirOption[] = [
  { key: "home", label: "Zuhause (~)", path: "~" },
  { key: "hermes-agent", label: "Hermes-Agent", path: "~/.hermes/hermes-agent" },
  { key: "family-organizer", label: "Family Organizer", path: "~/projects/family-organizer" },
  { key: "orchestration", label: "Orchestrierung", path: "~/orchestration" },
];

const QUICK_KEYS: Array<{ label: string; sequence: string }> = [
  { label: "Esc", sequence: "\x1b" },
  { label: "Tab", sequence: "\t" },
  { label: "⇧Tab", sequence: "\x1b[Z" },
  { label: "^C", sequence: "\x03" },
  { label: "⏎", sequence: "\r" },
];

const CONTROL_CAPABILITIES: Array<{ label: string; patterns: string[]; command: string }> = [
  { label: "Firecrawl", patterns: ["firecrawl"], command: "/firecrawl-search" },
  { label: "Gmail", patterns: ["gmail"], command: "/gmail" },
  { label: "Calendar", patterns: ["calendar", "google-calendar"], command: "/google-calendar" },
  { label: "Kanban", patterns: ["kanban"], command: "/kanban list" },
  { label: "Browser", patterns: ["browser", "browser_"], command: "/browser status" },
];

interface ReadOnlyControlContext {
  skills: SkillInfo[];
  toolsets: ToolsetInfo[];
  health: ControlOverviewHealthResponse | null;
  vault: ControlOverviewVaultResponse | null;
  board: ControlOverviewKanbanBoardResponse | null;
  decisions: ControlOverviewDecisionQueueResponse | null;
  error: string | null;
}

const EMPTY_CONTROL_CONTEXT: ReadOnlyControlContext = {
  skills: [],
  toolsets: [],
  health: null,
  vault: null,
  board: null,
  decisions: null,
  error: null,
};

export type TerminalUiState =
  | "attached"
  | "detached"
  | "window running"
  | "dead pane"
  | "missing window"
  | "Tailscale/mobile reconnect";

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function classifyTerminalState(args: {
  window: AgentTerminalWindow | null;
  socketReady: boolean;
  socketConnecting: boolean;
  mobile: boolean;
}): TerminalUiState {
  if (!args.window) return "missing window";
  if (!args.window.pid || args.window.dead) return "dead pane";
  if (args.socketReady) return "attached";
  if (args.mobile && args.socketConnecting) return "Tailscale/mobile reconnect";
  if (args.socketConnecting) return "window running";
  return "detached";
}

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function buildComposerPayload(text: string, submit: boolean): string | null {
  if (!text) return null;
  // Mehrzeiliges als Bracketed Paste, damit CLIs (claude/codex) es als EINE
  // Eingabe nehmen statt jede Zeile einzeln zu submitten.
  // Eingebettete End-Sequenzen entfernen — sonst schließt der Text selbst den Paste-Modus
  // vorzeitig und der Rest würde als Live-Keystrokes (inkl. \r) ausgeführt.
  const body = text.includes("\n") ? `\x1b[200~${text.split("\x1b[201~").join("")}\x1b[201~` : text;
  return submit ? `${body}\r` : body;
}

function isDeadWindow(window: AgentTerminalWindow): boolean {
  return !window.pid || window.dead === true;
}

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
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

function kindFromWindow(window: AgentTerminalWindow | null, fallback: AgentTerminalKind): AgentTerminalKind {
  // Suffix-Fenster (claude-fo, hermes-agent, …) gehören zum Basis-Kind.
  const base = (window?.window ?? "").split("-")[0] as AgentTerminalKind;
  return AGENT_KINDS.has(base) ? base : fallback;
}

function terminalProcessLabel(window: AgentTerminalWindow | null, kind: AgentTerminalKind): string {
  const command = (window?.command ?? "").trim();
  if (!window?.pid || window.dead) return "dead pane";
  if (!command) return kind;
  return command.toLowerCase().includes(kind) ? command : `${command}/${kind}`;
}

function matchesCapabilityText(value: string, patterns: string[]): boolean {
  const lower = value.toLowerCase();
  return patterns.some((pattern) => lower.includes(pattern));
}

function capabilityState(
  capability: (typeof CONTROL_CAPABILITIES)[number],
  skills: SkillInfo[],
  toolsets: ToolsetInfo[],
): { label: string; tone: "ok" | "warn" | "idle"; detail: string } {
  const matchingSkills = skills.filter((skill) =>
    matchesCapabilityText(`${skill.name} ${skill.description} ${skill.category}`, capability.patterns),
  );
  const matchingToolsets = toolsets.filter((toolset) =>
    matchesCapabilityText(`${toolset.name} ${toolset.label} ${toolset.description} ${toolset.tools.join(" ")}`, capability.patterns),
  );
  const enabled = matchingSkills.some((skill) => skill.enabled) || matchingToolsets.some((toolset) => toolset.enabled);
  const known = matchingSkills.length > 0 || matchingToolsets.length > 0;
  const setupMissing = matchingToolsets.some((toolset) => toolset.enabled && !toolset.configured);
  if (enabled && setupMissing) return { label: "Setup prüfen", tone: "warn", detail: "aktiv, aber Env/Auth/Setup unvollständig" };
  if (enabled) return { label: "aktiv", tone: "ok", detail: "Skill oder Toolset ist aktiv" };
  if (known) return { label: "vorhanden", tone: "idle", detail: "vorhanden, aber nicht aktiv" };
  return { label: "nicht gefunden", tone: "idle", detail: "kein passender Skill/Toolset sichtbar" };
}

function allBoardTasks(board: ControlOverviewKanbanBoardResponse | null): ControlOverviewKanbanTask[] {
  return (board?.columns ?? []).flatMap((column) => column.tasks ?? []);
}

function activeBoardTasks(board: ControlOverviewKanbanBoardResponse | null): ControlOverviewKanbanTask[] {
  return allBoardTasks(board).filter((task) => ["running", "review", "ready", "todo", "triage"].includes(task.status));
}

function blockedBoardTasks(board: ControlOverviewKanbanBoardResponse | null): ControlOverviewKanbanTask[] {
  return allBoardTasks(board).filter((task) => task.status === "blocked");
}

function useMediaQuery(query: string, fallback = false): boolean {
  const [matches, setMatches] = useState(() => (typeof window === "undefined" ? fallback : window.matchMedia(query).matches));
  useEffect(() => {
    if (typeof window === "undefined") return;
    const media = window.matchMedia(query);
    const onChange = () => setMatches(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

function useIsMobile(): boolean {
  return useMediaQuery("(max-width: 767px)");
}

function useIsCompactTerminalLayout(): boolean {
  return useMediaQuery("(max-width: 1023px)");
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
  const agentState = capability?.agents?.[agent.kind] ?? null;
  const agentOk = agentState ? agentState.available : agent.kind === "hermes" ? (capability?.hermes_tui_available ?? false) : windowsOk;
  const ok = windowsOk && agentOk;
  const reason = agentState?.reason ?? capability?.reason ?? null;
  const label = ok ? "verfügbar" : reason?.includes("symlink") || reason?.includes("resolvable") ? "kaputter Symlink/Binary" : reason ? "CLI fehlt" : "unbekannt";
  const title = reason ?? agentState?.binary ?? agent.hint;
  return (
    <span title={title} className={cn("rounded border px-1.5 py-0.5 text-[10px]", ok ? "border-emerald-400/35 text-emerald-200" : "border-white/15 text-white/45")}>
      {label}
    </span>
  );
}

function TerminalIdentityBar({
  window,
  selectedKind,
  state,
}: {
  window: AgentTerminalWindow | null;
  selectedKind: AgentTerminalKind;
  state: TerminalUiState;
}) {
  const kind = kindFromWindow(window, selectedKind);
  const label = AGENT_LABELS[kind] ?? kind;
  const target = window ? `${window.session}:${window.window}` : "missing window";
  const cwd = window?.cwd?.trim() || "cwd unbekannt";
  const process = terminalProcessLabel(window, kind);
  return (
    <div className="sticky top-0 z-10 border-b border-cyan-300/15 bg-[#062022]/95 px-2.5 py-2 text-[11px] text-white/75 backdrop-blur sm:px-3">
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="shrink-0 font-semibold text-white">{label}</span>
        <span className="text-white/30">·</span>
        <span className="min-w-0 max-w-[9rem] truncate font-mono text-cyan-100 sm:max-w-[14rem]" title={target}>{target}</span>
        <span className="text-white/30">·</span>
        <span className="min-w-0 max-w-[13rem] truncate font-mono text-white/70 sm:max-w-[28rem]" title={cwd}>{cwd}</span>
        <span className="text-white/30">·</span>
        <span className="min-w-0 max-w-[8rem] truncate font-mono text-white/70" title={process}>{process}</span>
        <span className="text-white/30">·</span>
        <StatusPill state={state} />
      </div>
    </div>
  );
}

function MiniStat({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  tone?: "neutral" | "ok" | "warn";
}) {
  const toneClass =
    tone === "ok"
      ? "border-emerald-300/25 bg-emerald-300/10 text-emerald-100"
      : tone === "warn"
        ? "border-amber-300/25 bg-amber-300/10 text-amber-100"
        : "border-white/10 bg-white/[0.03] text-white";
  return (
    <div className={cn("min-w-0 rounded-lg border p-2", toneClass)}>
      <div className="text-[10px] uppercase tracking-normal text-white/45">{label}</div>
      <div className="mt-1 truncate text-sm font-semibold">{value}</div>
    </div>
  );
}

function TerminalControlButton({
  label,
  disabled = false,
  onClick,
  children,
}: {
  label: string;
  disabled?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onPointerDown={(event) => event.preventDefault()}
      onMouseDown={(event) => event.preventDefault()}
      onClick={onClick}
      className="grid h-10 w-full min-w-0 place-items-center rounded-md border border-white/10 bg-white/[0.04] text-white/75 transition hover:border-cyan-300/40 hover:bg-cyan-300/10 hover:text-cyan-100 active:bg-cyan-300/15 disabled:cursor-not-allowed disabled:opacity-35"
    >
      {children}
    </button>
  );
}

export function AgentTerminalsView() {
  const mobile = useIsMobile();
  const compactLayout = useIsCompactTerminalLayout();
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const tmuxCopyModeRef = useRef(false);
  const [capability, setCapability] = useState<AgentTerminalCapabilityState | null>(null);
  const [windows, setWindows] = useState<AgentTerminalWindow[]>([]);
  const [selectedKind, setSelectedKind] = useState<AgentTerminalKind>("hermes");
  const [target, setTarget] = useState<{ session: string; window: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [handoffOpen, setHandoffOpen] = useState(false);
  const [socketReady, setSocketReady] = useState(false);
  const [socketConnecting, setSocketConnecting] = useState(false);
  const [controlContext, setControlContext] = useState<ReadOnlyControlContext>(EMPTY_CONTROL_CONTEXT);
  const [composerText, setComposerText] = useState("");
  const [zen, setZen] = useState(false);
  const [zenHeight, setZenHeight] = useState<number | null>(null);
  const [workdir, setWorkdir] = useState<string>(() => {
    try {
      return window.localStorage.getItem(WORKDIR_STORAGE_KEY) ?? "home";
    } catch {
      return "home";
    }
  });
  const [fontSize, setFontSize] = useState<number | null>(() => {
    try {
      const stored = Number(window.localStorage.getItem(FONT_STORAGE_KEY));
      return Number.isFinite(stored) && stored >= FONT_MIN && stored <= FONT_MAX ? stored : null;
    } catch {
      return null;
    }
  });
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
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async tmux inventory load on mount / selectedKind change
    void refresh();
  }, [refresh]);

  const refreshReadOnlyContext = useCallback(async () => {
    const [skills, toolsets, health, vault, board, decisions] = await Promise.allSettled([
      api.getSkills(),
      api.getToolsets(),
      api.getControlOverviewHealth(),
      api.getControlOverviewVault(),
      api.getControlOverviewKanbanBoard(),
      api.getControlOverviewDecisionQueue(),
    ]);
    const errors = [skills, toolsets, health, vault, board, decisions].flatMap((result) =>
      result.status === "rejected" ? [result.reason instanceof Error ? result.reason.message : String(result.reason)] : [],
    );
    setControlContext({
      skills: skills.status === "fulfilled" ? skills.value : [],
      toolsets: toolsets.status === "fulfilled" ? toolsets.value : [],
      health: health.status === "fulfilled" ? health.value : null,
      vault: vault.status === "fulfilled" ? vault.value : null,
      board: board.status === "fulfilled" ? board.value : null,
      decisions: decisions.status === "fulfilled" ? decisions.value : null,
      error: errors.length ? errors[0] : null,
    });
  }, []);

  useEffect(() => {
    let disposed = false;
    let timer: number | null = null;

    function isHidden(): boolean {
      return typeof document !== "undefined" && document.hidden;
    }

    function clearTimer(): void {
      if (timer) {
        window.clearTimeout(timer);
        timer = null;
      }
    }

    function scheduleNext(): void {
      clearTimer();
      if (disposed || isHidden()) return;
      timer = window.setTimeout(() => void run(), 20000);
    }

    async function run(): Promise<void> {
      clearTimer();
      if (disposed || isHidden()) return;
      try {
        await refreshReadOnlyContext();
      } finally {
        scheduleNext();
      }
    }

    function onVisibilityChange(): void {
      if (isHidden()) clearTimer();
      else void run();
    }

    document.addEventListener("visibilitychange", onVisibilityChange);
    void run();
    return () => {
      disposed = true;
      clearTimer();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [refreshReadOnlyContext]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let storedFont: number | null = null;
    try {
      const stored = Number(window.localStorage.getItem(FONT_STORAGE_KEY));
      storedFont = Number.isFinite(stored) && stored >= FONT_MIN && stored <= FONT_MAX ? stored : null;
    } catch {
      storedFont = null;
    }
    const { term, fit } = createHermesXtermSurface({
      host,
      theme: { ...TERMINAL_THEME_STATIC, background: "#071b1d" },
      scrollback: 4000,
      loggerName: "agent-terminals",
      onWheelScrollBuffer: true,
      terminalOptions: storedFont ? { fontSize: storedFont } : undefined,
    });
    termRef.current = term;
    fitRef.current = fit;
    let resizeRaf = 0;
    let settleRaf1 = 0;
    let settleRaf2 = 0;
    const resize = () => {
      try {
        if (!host.isConnected || host.clientWidth <= 0 || host.clientHeight <= 0) return;
        fit.fit();
        const cols = Math.max(2, Math.floor(term.cols));
        const rows = Math.max(2, Math.floor(term.rows));
        const ws = wsRef.current;
        if (ws?.readyState === WebSocket.OPEN) {
          ws.send(`\x1b[RESIZE:${cols};${rows}]`);
        }
      } catch {
        /* best-effort fit; ignore transient resize errors */
      }
    };
    const scheduleResize = () => {
      if (resizeRaf) return;
      resizeRaf = requestAnimationFrame(() => {
        resizeRaf = 0;
        resize();
      });
    };
    scheduleResize();
    settleRaf1 = requestAnimationFrame(() => {
      settleRaf1 = 0;
      settleRaf2 = requestAnimationFrame(() => {
        settleRaf2 = 0;
        resize();
      });
    });
    const observer = new ResizeObserver(scheduleResize);
    observer.observe(host);
    window.visualViewport?.addEventListener("resize", scheduleResize);
    window.addEventListener("resize", scheduleResize);
    return () => {
      observer.disconnect();
      window.visualViewport?.removeEventListener("resize", scheduleResize);
      window.removeEventListener("resize", scheduleResize);
      if (resizeRaf) cancelAnimationFrame(resizeRaf);
      if (settleRaf1) cancelAnimationFrame(settleRaf1);
      if (settleRaf2) cancelAnimationFrame(settleRaf2);
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
    tmuxCopyModeRef.current = false;
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
          try {
            fitRef.current?.fit();
            const cols = Math.max(2, Math.floor(term.cols));
            const rows = Math.max(2, Math.floor(term.rows));
            ws.send(`\x1b[RESIZE:${cols};${rows}]`);
          } catch {
            /* best-effort initial PTY resize */
          }
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
      const response = await api.ensureAgentTerminalWindow(kind, workdir);
      setTarget(targetFromWindow(response.window));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const respawnWindow = useCallback(
    async (win: { session: string; window: string }) => {
      setError(null);
      try {
        const response = await api.respawnAgentTerminalWindow(win.session, win.window);
        setTarget(targetFromWindow(response.window));
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [refresh],
  );

  const killWindow = useCallback(
    async (win: { session: string; window: string }) => {
      setError(null);
      try {
        await api.killDeadAgentTerminalWindow(win.session, win.window);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [refresh],
  );

  const sendRaw = useCallback((sequence: string) => {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) ws.send(sequence);
  }, []);

  const syncPtySize = useCallback(() => {
    const term = termRef.current;
    const fit = fitRef.current;
    if (!term || !fit) return;
    try {
      fit.fit();
      const cols = Math.max(2, Math.floor(term.cols));
      const rows = Math.max(2, Math.floor(term.rows));
      sendRaw(`\x1b[RESIZE:${cols};${rows}]`);
    } catch {
      /* best-effort refit */
    }
  }, [sendRaw]);

  const adjustFont = useCallback(
    (delta: number) => {
      const term = termRef.current;
      if (!term) return;
      const current = fontSize ?? (typeof term.options?.fontSize === "number" ? term.options.fontSize : 12);
      const next = Math.min(FONT_MAX, Math.max(FONT_MIN, current + delta));
      term.options.fontSize = next;
      setFontSize(next);
      try {
        window.localStorage.setItem(FONT_STORAGE_KEY, String(next));
      } catch {
        /* storage optional */
      }
      requestAnimationFrame(() => syncPtySize());
    },
    [fontSize, syncPtySize],
  );

  const selectWorkdir = useCallback((key: string) => {
    setWorkdir(key);
    try {
      window.localStorage.setItem(WORKDIR_STORAGE_KEY, key);
    } catch {
      /* storage optional */
    }
  }, []);

  useEffect(() => {
    // Stale localStorage-Key (z. B. entferntes Verzeichnis) gegen die aktuell gültigen
    // Optionen validieren — sonst spawnt "Neue Fenster starten in" mit totem Key.
    // Erst nach Capability-Load: der FALLBACK-Liste könnten legitime Backend-Keys fehlen.
    if (!capability) return;
    const options = capability.workdirs?.length ? capability.workdirs : FALLBACK_WORKDIRS;
    if (!options.some((option) => option.key === workdir)) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- korrigiert Stale-Key nach Capability-Load, "home" ist immer gültig
      selectWorkdir("home");
    }
  }, [capability, workdir, selectWorkdir]);

  const scrollTerminal = useCallback(
    (action: "pageUp" | "lineUp" | "lineDown" | "bottom") => {
      const term = termRef.current;
      if (action === "pageUp") {
        term?.scrollPages(-1);
        sendRaw(tmuxCopyModeRef.current ? "\x1b[5~" : TMUX_PAGE_UP);
        tmuxCopyModeRef.current = true;
      } else if (action === "lineUp") {
        term?.scrollLines(-TMUX_LINE_STEP);
        sendRaw(`${tmuxCopyModeRef.current ? "" : TMUX_COPY_MODE}${"\x1b[A".repeat(TMUX_LINE_STEP)}`);
        tmuxCopyModeRef.current = true;
      } else if (action === "lineDown") {
        term?.scrollLines(TMUX_LINE_STEP);
        if (tmuxCopyModeRef.current) sendRaw("\x1b[B".repeat(TMUX_LINE_STEP));
      } else {
        term?.scrollToBottom();
        if (tmuxCopyModeRef.current) sendRaw("q");
        tmuxCopyModeRef.current = false;
      }
    },
    [sendRaw],
  );

  const sendKey = useCallback(
    (sequence: string) => {
      // Tasten zielen auf die App, nicht auf den Scrollback — Copy-Mode verlassen.
      if (tmuxCopyModeRef.current) {
        sendRaw("q");
        tmuxCopyModeRef.current = false;
        termRef.current?.scrollToBottom();
      }
      sendRaw(sequence);
    },
    [sendRaw],
  );

  const sendComposer = useCallback(
    (submit: boolean) => {
      const payload = buildComposerPayload(composerText, submit);
      if (!payload) return;
      // Aus dem tmux-Copy-Mode raus, sonst landet die Eingabe im Scrollback.
      if (tmuxCopyModeRef.current) {
        sendRaw("q");
        tmuxCopyModeRef.current = false;
        termRef.current?.scrollToBottom();
      }
      sendRaw(payload);
      setComposerText("");
    },
    [composerText, sendRaw],
  );

  const toggleZen = useCallback(() => {
    setZen((current) => !current);
    setZenHeight(null);
  }, []);

  // Zen/Vollbild: Höhe an den Visual Viewport koppeln, damit die Composer-Zeile
  // auf Mobile über der eingeblendeten Tastatur bleibt.
  useEffect(() => {
    if (!zen) return;
    const viewport = window.visualViewport;
    if (!viewport) return;
    const update = () => setZenHeight(Math.round(viewport.height));
    update();
    viewport.addEventListener("resize", update);
    return () => viewport.removeEventListener("resize", update);
  }, [zen]);

  const enabledSkills = useMemo(() => controlContext.skills.filter((skill) => skill.enabled), [controlContext.skills]);
  const enabledToolsets = useMemo(() => controlContext.toolsets.filter((toolset) => toolset.enabled), [controlContext.toolsets]);
  const setupNeeds = useMemo(() => enabledToolsets.filter((toolset) => !toolset.configured), [enabledToolsets]);
  const capabilityRows = useMemo(
    () =>
      CONTROL_CAPABILITIES.map((capability) => {
        const state = capabilityState(capability, controlContext.skills, controlContext.toolsets);
        if (capability.label === "Kanban" && (controlContext.board || controlContext.decisions)) {
          return {
            capability,
            state: { label: "aktiv", tone: "ok" as const, detail: "Kanban-Board/Decision-Queue erreichbar" },
          };
        }
        return { capability, state };
      }),
    [controlContext.board, controlContext.decisions, controlContext.skills, controlContext.toolsets],
  );
  const activeTasks = useMemo(() => activeBoardTasks(controlContext.board), [controlContext.board]);
  const blockedTasks = useMemo(() => blockedBoardTasks(controlContext.board), [controlContext.board]);
  const recentReceipts = controlContext.vault?.recent_receipts ?? [];
  const evidenceReceipts = recentReceipts.filter((receipt) => /deploy|test|smoke|verify|receipt|report/i.test(receipt.file)).slice(0, 4);
  const openClaims = controlContext.vault?.open_sessions ?? [];
  const healthOverall = controlContext.health?.overall ?? "unbekannt";
  const decisionCount = controlContext.decisions?.count ?? controlContext.decisions?.decisions?.length ?? 0;

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
                const dead = isDeadWindow(win);
                return (
                  <div key={`${win.session}:${win.window}`} className="flex items-stretch gap-1">
                    <button type="button" onClick={() => { setTarget(targetFromWindow(win)); setSessionsOpen(false); }} className={cn("min-w-0 flex-1 rounded-md border px-2 py-2 text-left text-xs transition", active ? "border-cyan-300/60 bg-cyan-300/10 text-cyan-100" : "border-transparent text-white/65 hover:border-white/10 hover:bg-white/5")}>
                      <span className="flex items-center justify-between gap-2"><span className="truncate">{win.window}</span><span className={cn("h-2 w-2 shrink-0 rounded-full", dead ? "bg-red-300" : "bg-emerald-300")} /></span>
                      <span className="mt-0.5 block truncate text-[10px] text-white/40">{dead ? "dead pane" : win.command || "—"}</span>
                    </button>
                    {dead && (
                      <>
                        <button type="button" aria-label={`Neu starten ${win.session}:${win.window}`} title="Fenster neu starten" onClick={() => void respawnWindow(win)} className="grid w-8 shrink-0 place-items-center rounded-md border border-white/10 text-white/55 hover:border-cyan-300/40 hover:text-cyan-100">
                          <RotateCcw className="h-3.5 w-3.5" />
                        </button>
                        <button type="button" aria-label={`Fenster schließen ${win.session}:${win.window}`} title="Totes Fenster entfernen" onClick={() => void killWindow(win)} className="grid w-8 shrink-0 place-items-center rounded-md border border-white/10 text-white/55 hover:border-red-300/40 hover:text-red-200">
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );

  const toolsVisibility = (
    <div className="grid gap-3 rounded-xl border border-white/10 bg-black/20 p-3">
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-cyan-200" />
        <div>
          <p className="hc-eyebrow">Skills / Tools</p>
          <h3 className="text-sm font-semibold text-white">Fähigkeiten sichtbar</h3>
        </div>
      </div>
      <div className="grid grid-cols-3 gap-1.5">
        <MiniStat label="Skills aktiv" value={enabledSkills.length} tone={enabledSkills.length > 0 ? "ok" : "neutral"} />
        <MiniStat label="Toolsets aktiv" value={enabledToolsets.length} tone={enabledToolsets.length > 0 ? "ok" : "neutral"} />
        <MiniStat label="Setup offen" value={setupNeeds.length} tone={setupNeeds.length > 0 ? "warn" : "ok"} />
      </div>
      <div className="grid gap-1.5 text-xs">
        {capabilityRows.map(({ capability, state }) => (
          <div key={capability.label} className="grid gap-1 rounded-lg border border-white/10 bg-white/[0.03] p-2">
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium text-white">{capability.label}</span>
              <span
                title={state.detail}
                className={cn(
                  "shrink-0 rounded-full border px-2 py-0.5 text-[10px]",
                  state.tone === "ok"
                    ? "border-emerald-300/35 text-emerald-200"
                    : state.tone === "warn"
                      ? "border-amber-300/35 text-amber-100"
                      : "border-white/15 text-white/50",
                )}
              >
                {state.label}
              </span>
            </div>
            <code className="truncate rounded bg-black/30 px-1.5 py-1 text-[10px] text-white/55" title={capability.command}>
              {capability.command}
            </code>
          </div>
        ))}
      </div>
      <div className="grid gap-1 text-[11px] text-white/60">
        <div className="flex items-start gap-2">
          <Wrench className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0">
            {setupNeeds.length
              ? `Setup/Env/Auth prüfen: ${setupNeeds.slice(0, 4).map((toolset) => toolset.name).join(", ")}`
              : "Kein aktives Toolset meldet fehlendes Setup."}
          </span>
        </div>
        <div className="truncate" title={enabledSkills.map((skill) => skill.name).join(", ")}>
          Aktive Skills: {enabledSkills.length ? enabledSkills.slice(0, 5).map((skill) => skill.name).join(", ") : "—"}
        </div>
        <div className="truncate" title={enabledToolsets.map((toolset) => toolset.name).join(", ")}>
          Aktive Toolsets: {enabledToolsets.length ? enabledToolsets.slice(0, 5).map((toolset) => toolset.name).join(", ") : "—"}
        </div>
      </div>
    </div>
  );

  const controlOverview = (
    <div className="grid gap-3 rounded-xl border border-white/10 bg-black/20 p-3">
      <div className="flex items-center gap-2">
        <Gauge className="h-4 w-4 text-emerald-200" />
        <div>
          <p className="hc-eyebrow">Tageslage</p>
          <h3 className="text-sm font-semibold text-white">Was läuft / was ist belegt</h3>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-1.5">
        <MiniStat label="Terminals" value={`${windows.filter((win) => win.pid).length}/${windows.length}`} tone={windows.some((win) => win.pid) ? "ok" : "neutral"} />
        <MiniStat label="Claims" value={openClaims.length} tone={openClaims.length > 0 ? "warn" : "ok"} />
        <MiniStat label="Kanban aktiv" value={activeTasks.length} tone={activeTasks.length > 0 ? "warn" : "neutral"} />
        <MiniStat label="Blockiert" value={blockedTasks.length + decisionCount} tone={blockedTasks.length + decisionCount > 0 ? "warn" : "ok"} />
      </div>
      <div className="grid gap-2 text-[11px] text-white/65">
        <div className="flex items-center gap-1.5">
          <CheckCircle2 className="h-3.5 w-3.5 text-emerald-200" />
          <span>Health: <span className="font-medium text-white">{healthOverall}</span></span>
        </div>
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-white/75"><Bot className="h-3.5 w-3.5" />Aktive Agent-Terminals</div>
          {windows.filter((win) => win.pid).length ? (
            <ul className="space-y-1">
              {windows.filter((win) => win.pid).slice(0, 4).map((win) => (
                <li key={`${win.session}:${win.window}`} className="truncate" title={`${win.session}:${win.window} · ${win.cwd ?? ""}`}>
                  <span className="font-mono text-cyan-100">{win.session}:{win.window}</span> · {terminalProcessLabel(win, kindFromWindow(win, selectedKind))}
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-white/45">—</div>
          )}
        </div>
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-white/75"><Inbox className="h-3.5 w-3.5" />Offene Coordination-Claims</div>
          {openClaims.length ? (
            <ul className="space-y-1">
              {openClaims.slice(0, 3).map((claim) => (
                <li key={claim.path} className="truncate" title={`${claim.agent} · ${claim.task}`}>
                  [{claim.agent}] {claim.task || claim.started}
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-white/45">keine</div>
          )}
        </div>
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-white/75"><ClipboardList className="h-3.5 w-3.5" />Letzte Belege</div>
          {(evidenceReceipts.length ? evidenceReceipts : recentReceipts).length ? (
            <ul className="space-y-1">
              {(evidenceReceipts.length ? evidenceReceipts : recentReceipts).slice(0, 4).map((receipt) => (
                <li key={receipt.path} className="truncate" title={receipt.path}>
                  <span className="font-mono text-white/45">{receipt.when}</span> [{receipt.agent}] {receipt.file}
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-white/45">—</div>
          )}
        </div>
        <div>
          <div className="mb-1 text-white/75">Kanban nächste Signale</div>
          {activeTasks.length || blockedTasks.length || decisionCount ? (
            <ul className="space-y-1">
              {activeTasks.slice(0, 2).map((task) => (
                <li key={`active-${task.id}`} className="truncate" title={task.title}>
                  <span className="font-mono text-cyan-100">{task.id}</span> · {task.status} · {task.title}
                </li>
              ))}
              {blockedTasks.slice(0, 2).map((task) => (
                <li key={`blocked-${task.id}`} className="truncate text-amber-100" title={task.title}>
                  <span className="font-mono">{task.id}</span> · blocked · {task.title}
                </li>
              ))}
              {decisionCount > 0 && <li className="text-amber-100">{decisionCount} Operator-Entscheidung(en) offen</li>}
            </ul>
          ) : (
            <div className="text-white/45">keine aktiven/blockierten Items sichtbar</div>
          )}
        </div>
        {controlContext.error && (
          <div className="rounded-lg border border-amber-300/25 bg-amber-300/10 p-2 text-amber-100">
            Kontext teilweise nicht geladen: {controlContext.error}
          </div>
        )}
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
      <button
        type="button"
        onClick={() => { setHandoffOpen(true); setToolsOpen(false); }}
        disabled={!target}
        className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-cyan-300/50 bg-cyan-300/10 px-3 py-2 text-xs text-cyan-100 hover:bg-cyan-300/20 disabled:opacity-40"
      >
        <Share2 className="h-3.5 w-3.5" />
        Handoff öffnen (Auswahl → PlanSpec/Kanban)
      </button>
      {toolsVisibility}
      {controlOverview}
    </div>
  );

  const composer = (
    <div className="shrink-0 border-t border-white/10 bg-[#06191b] px-2 py-1.5 sm:px-3">
      <div className="flex items-end gap-1.5">
        <textarea
          aria-label="Text an Terminal senden"
          value={composerText}
          onChange={(event) => setComposerText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              sendComposer(true);
            }
          }}
          placeholder={socketReady ? "Prompt oder Befehl … (Enter sendet)" : "Terminal nicht verbunden"}
          disabled={!socketReady}
          rows={Math.min(4, Math.max(1, composerText.split("\n").length))}
          enterKeyHint="send"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          className="min-h-10 min-w-0 flex-1 resize-none rounded-lg border border-white/10 bg-black/30 px-2.5 py-2 font-mono text-[13px] leading-5 text-white placeholder:text-white/30 focus:border-cyan-300/50 focus:outline-none disabled:opacity-40"
        />
        <button
          type="button"
          aria-label="Eingabe senden"
          title="Senden (mit Enter)"
          disabled={!socketReady || !composerText}
          onClick={() => sendComposer(true)}
          className="grid h-10 w-12 shrink-0 place-items-center rounded-lg border border-cyan-300/50 bg-cyan-300/15 text-cyan-100 transition hover:bg-cyan-300/25 disabled:cursor-not-allowed disabled:opacity-35"
        >
          <CornerDownLeft className="h-4 w-4" />
        </button>
      </div>
    </div>
  );

  const terminalControls = compactLayout ? (
    <div className={cn("shrink-0 border-t border-white/10 bg-[#06191b] px-2 pt-1.5", zen ? "pb-[calc(0.375rem+env(safe-area-inset-bottom,0px))]" : "pb-[calc(2.25rem+env(safe-area-inset-bottom,0px))] md:pb-2 md:pt-2")}>
      <div className="grid gap-1.5">
        <div className="grid grid-cols-2 gap-1.5">
          <div className="grid grid-cols-4 gap-1" role="group" aria-label="Terminal scroll controls">
            <TerminalControlButton label="Terminal scroll page up" onClick={() => scrollTerminal("pageUp")}>
              <ChevronsUp className="h-4 w-4" />
            </TerminalControlButton>
            <TerminalControlButton label="Terminal scroll up" onClick={() => scrollTerminal("lineUp")}>
              <ChevronUp className="h-4 w-4" />
            </TerminalControlButton>
            <TerminalControlButton label="Terminal scroll down" onClick={() => scrollTerminal("lineDown")}>
              <ChevronDown className="h-4 w-4" />
            </TerminalControlButton>
            <TerminalControlButton label="Terminal scroll to bottom" onClick={() => scrollTerminal("bottom")}>
              <ChevronsDown className="h-4 w-4" />
            </TerminalControlButton>
          </div>
          <div className="grid grid-cols-4 gap-1" role="group" aria-label="Terminal arrow key controls">
            <TerminalControlButton label="Send arrow left" disabled={!socketReady} onClick={() => sendKey("\x1b[D")}>
              <ArrowLeft className="h-4 w-4" />
            </TerminalControlButton>
            <TerminalControlButton label="Send arrow up" disabled={!socketReady} onClick={() => sendKey("\x1b[A")}>
              <ArrowUp className="h-4 w-4" />
            </TerminalControlButton>
            <TerminalControlButton label="Send arrow down" disabled={!socketReady} onClick={() => sendKey("\x1b[B")}>
              <ArrowDown className="h-4 w-4" />
            </TerminalControlButton>
            <TerminalControlButton label="Send arrow right" disabled={!socketReady} onClick={() => sendKey("\x1b[C")}>
              <ArrowRight className="h-4 w-4" />
            </TerminalControlButton>
          </div>
        </div>
        <div className="grid grid-cols-5 gap-1" role="group" aria-label="Terminal special keys">
          {QUICK_KEYS.map((key) => (
            <TerminalControlButton key={key.label} label={`Send ${key.label}`} disabled={!socketReady} onClick={() => sendKey(key.sequence)}>
              <span className="font-mono text-[11px]">{key.label}</span>
            </TerminalControlButton>
          ))}
        </div>
      </div>
    </div>
  ) : null;

  return (
    <div className="flex min-h-[calc(100vh-8rem)] flex-col gap-2 text-white sm:gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-white/10 bg-[#071b1d]/90 p-2 shadow-[0_18px_60px_rgba(0,0,0,0.22)] sm:rounded-2xl sm:p-3">
        <div className="min-w-0">
          <p className="hc-eyebrow">Agent Terminals</p>
          <div className="mt-1 flex flex-wrap items-center gap-2"><StatusPill state={state} />{loading && <span className="text-xs text-white/50">lädt…</span>}{error && <span className="inline-flex items-center gap-1 text-xs text-red-200"><AlertTriangle className="h-3 w-3" />{error}</span>}</div>
        </div>
        <div className="flex w-full flex-col gap-1.5 md:w-auto md:items-end">
          <div className="grid w-full grid-cols-4 gap-1.5 sm:grid-cols-2 md:flex md:w-auto md:flex-wrap">
            {AGENTS.map((agent) => (
              <button key={agent.kind} type="button" onClick={() => void ensureAgent(agent.kind)} className={cn("rounded-lg border px-1.5 py-2 text-center text-[11px] sm:px-2.5 sm:text-left sm:text-xs", selectedKind === agent.kind ? "border-cyan-300/60 bg-cyan-300/10 text-cyan-100" : "border-white/10 bg-white/[0.03] text-white/70 hover:bg-white/[0.06]")}>
                <span className="flex min-w-0 items-center justify-center gap-1.5 sm:justify-start sm:gap-2"><TerminalSquare className="h-3.5 w-3.5 shrink-0" /><span className="truncate">{agent.label}</span><span className="hidden sm:inline-flex"><CapabilityPill capability={capability} agent={agent} /></span></span>
              </button>
            ))}
          </div>
          <label className="flex items-center gap-1.5 text-[11px] text-white/50">
            <span className="shrink-0">Neue Fenster starten in</span>
            <select
              aria-label="Arbeitsverzeichnis für neue Terminals"
              value={workdir}
              onChange={(event) => selectWorkdir(event.target.value)}
              className="min-w-0 max-w-[12rem] rounded-lg border border-white/10 bg-[#0a2427] px-2 py-1.5 text-xs text-white/85 focus:border-cyan-300/50 focus:outline-none"
            >
              {(capability?.workdirs?.length ? capability.workdirs : FALLBACK_WORKDIRS).map((option) => (
                <option key={option.key} value={option.key}>{option.label}</option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <div className="grid flex-1 gap-2 sm:gap-3 lg:grid-cols-[260px_minmax(0,1fr)] xl:grid-cols-[260px_minmax(0,1fr)_280px]">
        <aside className="hidden min-h-[540px] rounded-2xl border border-white/10 bg-black/20 p-3 lg:block">{sessionList}</aside>
        <section
          style={zen && zenHeight ? { height: `${zenHeight}px` } : undefined}
          className={cn(
            "overflow-hidden border-white/10 bg-[#041113]",
            zen ? "fixed inset-0 z-[45] flex flex-col" : "rounded-2xl border md:min-h-[640px] lg:min-h-[540px]",
          )}
        >
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-white/10 px-3 py-2 text-xs text-white/65">
            <div className="flex min-w-0 items-center gap-2"><Activity className="h-3.5 w-3.5 shrink-0" /><span className="truncate">{target ? `${target.session}:${target.window}` : "missing window"}</span></div>
            <div className="flex shrink-0 items-center gap-1">
              <button type="button" aria-label="Schrift kleiner" title="Schrift kleiner" onClick={() => adjustFont(-1)} className="grid h-9 w-9 place-items-center rounded-md border border-white/10 font-mono text-[11px] text-white/70 hover:bg-white/10">A−</button>
              <button type="button" aria-label="Schrift größer" title="Schrift größer" onClick={() => adjustFont(1)} className="grid h-9 w-9 place-items-center rounded-md border border-white/10 font-mono text-[11px] text-white/70 hover:bg-white/10">A+</button>
              <button type="button" aria-label={zen ? "Vollbild verlassen" : "Vollbild"} title={zen ? "Vollbild verlassen" : "Vollbild"} onClick={toggleZen} className="grid h-9 w-9 place-items-center rounded-md border border-white/10 text-white/70 hover:bg-white/10">
                {zen ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
              </button>
              {compactLayout && (
                <>
                  <button type="button" onClick={() => setSessionsOpen(true)} className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-2.5 py-2 text-white/70 hover:bg-white/10"><PanelLeft className="mr-1 h-3.5 w-3.5" />Sessions</button>
                  <button type="button" onClick={() => setToolsOpen(true)} className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-2.5 py-2 text-white/70 hover:bg-white/10"><PanelRight className="mr-1 h-3.5 w-3.5" />Tools</button>
                </>
              )}
            </div>
          </div>
          {!target && !loading ? (
            <div className="grid h-[480px] place-items-center p-6 text-center text-sm text-white/55">Kein tmux-Fenster verfügbar. Agent oben wählen, um eins anzulegen.</div>
          ) : (
            <div className={cn("flex w-full flex-col", zen ? "min-h-0 flex-1" : "h-[calc(100svh-25rem)] min-h-[360px] md:h-[calc(100svh-23rem)] md:min-h-[500px] lg:h-[calc(100vh-17rem)]")}>
              <TerminalIdentityBar window={selectedWindow} selectedKind={selectedKind} state={state} />
              {state === "dead pane" && selectedWindow && (
                <div className="flex shrink-0 items-center justify-between gap-2 border-b border-amber-300/20 bg-amber-300/10 px-3 py-1.5 text-[11px] text-amber-100">
                  <span className="min-w-0 truncate">Prozess beendet — Fenster neu starten?</span>
                  <button type="button" onClick={() => void respawnWindow(selectedWindow)} className="inline-flex shrink-0 items-center gap-1 rounded-md border border-amber-300/40 px-2 py-1 hover:bg-amber-300/15">
                    <RotateCcw className="h-3 w-3" />Neu starten
                  </button>
                </div>
              )}
              <div
                ref={hostRef}
                className="min-h-0 min-w-0 flex-1 w-full overflow-hidden [&_.xterm]:box-border [&_.xterm]:px-2 [&_.xterm]:py-1 [&_.xterm-viewport]:overscroll-contain [&_.xterm-viewport]:touch-pan-y"
              />
              {composer}
              {terminalControls}
            </div>
          )}
        </section>
        <aside className="hidden min-h-[540px] rounded-2xl border border-white/10 bg-black/20 p-3 xl:block">{toolsDrawer}</aside>
      </div>

      {compactLayout && sessionsOpen && <div className="fixed inset-0 z-50 bg-black/55 p-3"><div className="h-full overflow-auto rounded-2xl border border-white/10 bg-[#071b1d] p-3"><div className="mb-2 flex justify-end"><button type="button" onClick={() => setSessionsOpen(false)} className="rounded-md border border-white/10 p-1.5 text-white/65 hover:bg-white/10"><X className="h-4 w-4" /></button></div>{sessionList}</div></div>}
      {compactLayout && toolsOpen && <div className="fixed inset-x-0 bottom-0 z-50 max-h-[85svh] overflow-auto rounded-t-3xl border border-white/10 bg-[#071b1d] p-4 shadow-2xl"><div className="mx-auto mb-3 h-1 w-12 rounded-full bg-white/20" />{toolsDrawer}</div>}

      {handoffOpen && (
        <TerminalHandoffPanel
          target={target}
          getSelection={() => termRef.current?.getSelection() ?? ""}
          onClose={() => setHandoffOpen(false)}
        />
      )}
    </div>
  );
}
