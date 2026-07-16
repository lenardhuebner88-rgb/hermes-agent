import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
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
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  Columns2,
  ChevronUp,
  ChevronsDown,
  ChevronsUp,
  ClipboardList,
  Copy,
  CornerDownLeft,
  Gauge,
  Grid2X2,
  Inbox,
  Keyboard,
  LayoutGrid,
  Maximize2,
  Minimize2,
  PanelRightOpen,
  Paperclip,
  Pencil,
  PlugZap,
  Plus,
  RefreshCw,
  RotateCcw,
  Server,
  Share2,
  Sparkles,
  TerminalSquare,
  TextSelect,
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
  type AgentTerminalOverviewState,
  type AgentTerminalOverviewWindow,
  type AgentTerminalWindow,
  type AgentTerminalWorkdirOption,
  type SkillInfo,
  type ToolsetInfo,
} from "@/lib/api";
import { copyTextToClipboard } from "@/lib/clipboard";
import { cn } from "@/lib/utils";
import {
  createHermesXtermSurface,
  SGR_WHEEL_DOWN,
  SGR_WHEEL_UP,
  TERMINAL_MAIN_BACKGROUND,
  TERMINAL_THEME_STATIC,
  touchScrollSteps,
} from "@/lib/xtermSurface";
import { de } from "../i18n/de";
import { Eyebrow } from "../components/primitives";
import { Sparkline } from "../components/fleet/Sparkline";
import { KpiTile, SignalChip, type SignalTone } from "../components/leitstand";
import { TerminalHandoffPanel } from "./TerminalHandoffPanel";
import { TerminalPane, type TerminalPaneConnectionState, type TerminalPaneHandle } from "./agent-terminals/TerminalPane";
import {
  extractTerminalBufferText,
  TerminalSelectOverlay,
} from "./agent-terminals/TerminalSelectOverlay";
import { TerminalUsageDock } from "./agent-terminals/TerminalUsageDock";
import {
  normalizeDesktopLayout,
  resolvePaneTargets,
  targetKey as paneTargetKey,
  type DesktopTerminalLayout,
  type TerminalTarget as PaneTarget,
} from "./agent-terminals/layout";

const AGENTS: Array<{ kind: AgentTerminalKind; label: string; hint: string }> = [
  { kind: "hermes", label: "Hermes", hint: "hermes --tui" },
  { kind: "claude", label: "Claude", hint: "claude-cli" },
  { kind: "codex", label: "Codex", hint: "codex-cli" },
  { kind: "kimi", label: "Kimi", hint: "kimi-cli" },
  { kind: "grok", label: "Grok", hint: "grok-build / Grok 4.5" },
];

const AGENT_LABELS: Record<AgentTerminalKind, string> = Object.fromEntries(AGENTS.map((agent) => [agent.kind, agent.label])) as Record<AgentTerminalKind, string>;
const AGENT_KINDS = new Set<AgentTerminalKind>(AGENTS.map((agent) => agent.kind));

const TMUX_PREFIX = "\x02";
const TMUX_COPY_MODE = `${TMUX_PREFIX}[`;
const TMUX_PAGE_UP = `${TMUX_PREFIX}\x1b[5~`;
const TMUX_LINE_STEP = 5;

const WORKDIR_STORAGE_KEY = "hermes-terminals-workdir";
/** Per-kind key; legacy global WORKDIR_STORAGE_KEY is still read once for migration. */
function workdirStorageKeyForKind(kind: AgentTerminalKind): string {
  return `${WORKDIR_STORAGE_KEY}:${kind}`;
}
const WORKDIR_RESET_NOTE =
  "Gespeichertes Arbeitsverzeichnis nicht verfügbar — auf Zuhause zurückgesetzt.";
const FONT_STORAGE_KEY = "hermes-terminals-fontsize";
const KEYS_STORAGE_KEY = "hermes-terminals-keysopen";
const TARGET_STORAGE_KEY = "hermes-terminals-last-target";
const LAYOUT_STORAGE_KEY = "hermes.control.agent-terminals.desktop-layout.v1";
const PANE_TARGETS_STORAGE_KEY = "hermes.control.agent-terminals.pane-targets.v1";
const LASTSEEN_STORAGE_KEY = "hermes-terminals-lastseen";
const FONT_MIN = 8;
const FONT_MAX = 20;
const PRIMARY_SESSION = "work";
const WINDOW_INVENTORY_POLL_MS = 10000;
/** An armed close disarms itself, so a row armed and forgotten cannot be killed later by a stray click. */
const TERMINATE_ARM_TIMEOUT_MS = 8000;
const COPY_STATUS_TIMEOUT_MS = 2000;

type TerminalCopyState = "idle" | "copied" | "empty" | "error";
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000];
const OVERVIEW_POLL_MS = 5000;
// Debounce window for RESIZE escape sends (fit() stays immediate).  Mobile keyboards
// fire dozens of visualViewport events during the slide animation; 300 ms trailing
// debounce collapses the storm to one send after the keyboard settles.
const RESIZE_SEND_DEBOUNCE_MS = 300;

const FLEET_STATE_PRIORITY: Record<AgentTerminalOverviewWindow["state"], number> = {
  frage: 0,
  laeuft: 1,
  wartet: 2,
  idle: 3,
  dead: 4,
};

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

/**
 * Whether the UI may offer terminate (close) for this window.
 *
 * Backend additive field (`managed` on TmuxWindow.to_dict). api.ts is claimed
 * by a parallel session, so we read the optional field via a safe cast rather
 * than extending the shared type. Absent / non-false → treated as managed
 * (legacy payloads keep the close affordance).
 */
export function isManagedWindow(window: AgentTerminalWindow): boolean {
  const managed = (window as { managed?: unknown }).managed;
  return managed !== false;
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

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function orderWindowsForStrip(windows: AgentTerminalWindow[]): AgentTerminalWindow[] {
  const primary = windows.filter((w) => w.session === PRIMARY_SESSION);
  const rest = windows.filter((w) => w.session !== PRIMARY_SESSION);
  return [...primary, ...rest];
}

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function chipLabel(window: AgentTerminalWindow): string {
  return window.session === PRIMARY_SESSION ? window.window : `${window.session}:${window.window}`;
}

/**
 * Short display form for a pane cwd: $HOME → ~, then at most the last two path segments.
 * Backend may omit cwd (optional on AgentTerminalWindow); callers pass null/undefined safely.
 */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function formatCwdShort(cwd: string | null | undefined): string {
  const raw = cwd?.trim();
  if (!raw) return "unbekannt";
  let display = raw;
  // Linux operator host: tmux pane_current_path is absolute under /home/<user>.
  const homeMatch = display.match(/^\/home\/[^/]+/);
  if (homeMatch) {
    const rest = display.slice(homeMatch[0].length);
    display = rest ? `~${rest}` : "~";
  }
  if (display === "~") return "~";
  if (display.startsWith("~/")) {
    const rest = display.slice(2).split("/").filter(Boolean);
    if (rest.length <= 2) return `~/${rest.join("/")}`;
    return `~/${rest.slice(-2).join("/")}`;
  }
  const segs = display.split("/").filter(Boolean);
  if (segs.length <= 2) return display.startsWith("/") ? `/${segs.join("/")}` : segs.join("/");
  return segs.slice(-2).join("/");
}

/** Read last workdir for a kind: per-kind key → legacy global (migration) → home. */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function readStoredWorkdir(kind: AgentTerminalKind): string {
  try {
    const perKind = window.localStorage.getItem(workdirStorageKeyForKind(kind));
    if (perKind) return perKind;
    // One-shot migration read of the pre-S4 global key (do not delete it).
    const legacy = window.localStorage.getItem(WORKDIR_STORAGE_KEY);
    if (legacy) return legacy;
  } catch {
    /* storage optional */
  }
  return "home";
}

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function reconnectDelayMs(attempt: number): number {
  const index = Math.min(Math.max(Math.trunc(attempt), 0), RECONNECT_DELAYS_MS.length - 1);
  return RECONNECT_DELAYS_MS[index];
}

/**
 * True for the terminal's copy chords: Ctrl+Shift+C and Ctrl+Insert.
 *
 * Plain Ctrl+C is deliberately NOT a copy chord. It is the only way to interrupt
 * a running agent, and the common "copy when something is selected" shortcut would
 * silently swallow that interrupt whenever a stale selection is left in the buffer.
 * Both chords here are copy-only — neither ever writes ETX (or anything else) to
 * the socket. Cmd/Meta is left to the OS.
 */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function isTerminalCopyShortcut(event: Pick<KeyboardEvent, "ctrlKey" | "metaKey" | "shiftKey" | "key">): boolean {
  if (event.metaKey || !event.ctrlKey) return false;
  if (event.shiftKey) return event.key === "C" || event.key === "c";
  return event.key === "Insert";
}

/**
 * Pane order of the xterm surface a keyboard event was fired in, or null when it
 * came from anywhere else (composer, rename field, session rail, Fleet card …).
 *
 * The copy chord has to be caught document-wide in the capture phase — xterm binds
 * its own keydown on the helper textarea, so a listener on the pane would be too
 * late. That makes rejecting foreign targets this function's job: without it, a
 * selection left behind in a terminal would hijack the user's Ctrl+Shift+C inside a
 * text field and silently put stale terminal output on the clipboard.
 */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function terminalSurfaceOrder(target: EventTarget | null): number | null {
  const element = target as Element | null;
  if (typeof element?.closest !== "function") return null;
  const surface = element.closest("[data-terminal-surface]");
  const order = surface?.getAttribute("data-terminal-surface");
  if (!order || !/^\d+$/.test(order)) return null;
  return Number(order);
}

/** Build the PTY resize escape sequence, clamping to valid dimensions (≥ 2, floored).
 *  Handles NaN/Infinity by falling back to 2 (Math.max propagates NaN, so we guard). */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function formatPtyResize(cols: number, rows: number): string {
  const c = Math.max(2, Number.isFinite(cols) ? Math.floor(cols) : 0);
  const r = Math.max(2, Number.isFinite(rows) ? Math.floor(rows) : 0);
  return `\x1b[RESIZE:${c};${r}]`;
}

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function hasUnseenActivity(window: AgentTerminalWindow, lastSeen: Record<string, number>): boolean {
  if (window.activity == null) return false;
  const key = `${window.session}:${window.window}`;
  return window.activity > lastSeen[key];
}

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function formatActivityAge(now: number, activity: number | null): string {
  if (activity == null) return "—";
  const deltaSeconds = Math.max(0, Math.round(now - activity));
  if (deltaSeconds < 60) return `vor ${deltaSeconds}s`;
  const minutes = Math.floor(deltaSeconds / 60);
  if (minutes < 60) return `vor ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `vor ${hours}h`;
}

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function orderOverviewForFleet(entries: AgentTerminalOverviewWindow[]): AgentTerminalOverviewWindow[] {
  return [...entries].sort((a, b) => FLEET_STATE_PRIORITY[a.state] - FLEET_STATE_PRIORITY[b.state]);
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

function TerminalStatusChip({ state }: { state: TerminalUiState }) {
  const tone =
    state === "attached"
      ? "ok"
      : state === "window running"
        ? "ok"
        : state === "Tailscale/mobile reconnect"
          ? "warn"
          : "alert";
  return <SignalChip tone={tone} label={state} className="shrink-0" />;
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
    <span title={title} className={cn("rounded-card border px-1.5 py-0.5 text-[10px]", ok ? "border-status-ok/35 text-status-ok" : "border-line text-ink-3")}>
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
  // cwd is optional on the windows payload (AgentTerminalWindow.cwd); shorten for chrome.
  const cwdRaw = window?.cwd?.trim() || "";
  const cwdShort = formatCwdShort(window?.cwd);
  const process = terminalProcessLabel(window, kind);
  return (
    <div className="sticky top-0 z-10 border-b border-line-soft bg-surface-2/95 px-2.5 py-2 text-[11px] text-ink-2 backdrop-blur sm:px-3">
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="shrink-0 font-semibold text-ink">{label}</span>
        <span className="text-ink-3">·</span>
        <span className="min-w-0 max-w-[9rem] truncate font-mono text-live sm:max-w-[14rem]" title={target}>{target}</span>
        <span className="text-ink-3">·</span>
        <span
          data-testid="terminal-cwd-chip"
          className="min-w-0 max-w-[13rem] truncate font-mono text-ink-2 sm:max-w-[28rem]"
          title={cwdRaw || cwdShort}
        >
          {cwdShort}
        </span>
        <span className="text-ink-3">·</span>
        <span className="min-w-0 max-w-[8rem] truncate font-mono text-ink-2" title={process}>{process}</span>
        <span className="text-ink-3">·</span>
        <TerminalStatusChip state={state} />
      </div>
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
      className="grid h-9 w-full min-w-0 place-items-center rounded-card border border-line bg-surface-2 text-ink-2 transition hover:border-live/40 hover:bg-live/10 hover:text-live active:bg-live/15 disabled:cursor-not-allowed disabled:opacity-35"
    >
      {children}
    </button>
  );
}

function fleetStateMeta(state: AgentTerminalOverviewWindow["state"]): { label: string; tone: SignalTone } {
  switch (state) {
    case "frage":
      return { label: "Braucht dich", tone: "alert" };
    case "laeuft":
      return { label: "Läuft", tone: "ok" };
    case "wartet":
      return { label: "Fertig/wartet", tone: "ok" };
    case "dead":
      return { label: "Tot", tone: "alert" };
    default:
      return { label: "Idle", tone: "neutral" };
  }
}

// Persistent fleet strip's status-chip vocabulary — deliberately distinct
// from fleetStateMeta() above (used by the full-screen "Flotte" overlay).
// Follows DESIGN.md rule 2 literally: läuft/ok = green,
// frage/degraded = warn, tot/failed = alert, idle = neutral ink-3. "wartet"
// (fertig, wartet auf Weiteres) reads as ok — same bucket as läuft's calm end.
const STRIP_STATE_META: Record<AgentTerminalOverviewState, { label: string; tone: SignalTone }> = {
  laeuft: { label: "läuft", tone: "ok" },
  frage: { label: "frage", tone: "warn" },
  wartet: { label: "wartet", tone: "ok" },
  idle: { label: "idle", tone: "neutral" },
  dead: { label: "tot", tone: "alert" },
};

function StatTile({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  tone?: "neutral" | "ok" | "warn";
}) {
  return (
    <KpiTile
      label={label}
      value={value}
      dot={tone === "ok" ? "ready" : tone === "warn" ? "warn" : "idle"}
      className="border-line-soft"
    />
  );
}

/** Persistent fleet-strip card — the always-on summary above the terminal
 *  pane (desktop only). Distinct from FleetCard: compact, one line of tail,
 *  no respawn/kill actions (those stay in the full "Flotte" overlay).
 *  Clicking selects that terminal. */
function FleetStripCard({
  win,
  now,
  isCurrent,
  onSelect,
}: {
  win: AgentTerminalOverviewWindow;
  now: number;
  isCurrent: boolean;
  onSelect: () => void;
}) {
  const meta = STRIP_STATE_META[win.state] ?? STRIP_STATE_META.idle;
  const tailLine = (win.tail ?? "").split("\n").filter((line) => line.trim()).slice(-1)[0]?.trim();
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "min-w-0 rounded-card border bg-surface-2 px-3 py-2.5 text-left transition hover:bg-surface-3",
        win.state === "frage" ? "border-status-alert/50" : "border-line-soft",
        isCurrent ? "shadow-[inset_3px_0_0_var(--color-bronze)] bg-surface-3" : "hover:border-line",
      )}
    >
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className={cn("min-w-0 truncate font-mono text-xs font-semibold", isCurrent ? "text-live" : "text-ink")}>{chipLabel(win)}</span>
        <SignalChip tone={meta.tone} label={meta.label} className="px-2 py-0.5 text-[9px]" />
      </div>
      <div className="mt-1.5 truncate text-[10px] text-ink-3">{tailLine || formatActivityAge(now, win.activity ?? null)}</div>
    </button>
  );
}

function FleetCard({
  win,
  now,
  selected,
  broadcastMode,
  onToggleSelect,
  onOpen,
  onRespawn,
  onKill,
  onTerminate,
  terminateArmed,
  terminateBusy,
  onConfirmTerminate,
  onCancelTerminate,
}: {
  win: AgentTerminalOverviewWindow;
  now: number;
  selected: boolean;
  broadcastMode: boolean;
  onToggleSelect: () => void;
  onOpen: () => void;
  onRespawn: () => void;
  onKill: () => void;
  /** Arms the close guard (step 1) — it never kills on its own. */
  onTerminate: () => void;
  terminateArmed: boolean;
  terminateBusy: boolean;
  onConfirmTerminate: () => void;
  onCancelTerminate: () => void;
}) {
  const meta = fleetStateMeta(win.state);
  const dead = win.state === "dead";
  const managed = isManagedWindow(win);
  const selectable = broadcastMode && !dead;
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => (selectable ? onToggleSelect() : onOpen())}
      onKeyDown={(event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        if (selectable) onToggleSelect();
        else onOpen();
      }}
      className={cn(
        "relative grid cursor-pointer gap-2 rounded-card border bg-surface-2 p-3 text-left transition hover:bg-surface-3",
        selected ? "shadow-[inset_3px_0_0_var(--color-bronze)] bg-surface-3" : "border-line hover:border-line",
      )}
    >
      {selectable && (
        <span
          aria-hidden="true"
          className={cn(
            "absolute right-2 top-2 grid h-4 w-4 place-items-center rounded-card border",
            selected ? "border-live bg-live/80 text-surface-0" : "border-line",
          )}
        >
          {selected && <CheckCircle2 className="h-3 w-3" />}
        </span>
      )}
      <div className="flex min-w-0 items-center gap-1.5 pr-5">
        <SignalChip tone={meta.tone} label={meta.label} className="px-2 py-0.5 text-[10px]" />
        <span className="min-w-0 truncate font-mono text-xs text-ink-2">{chipLabel(win)}</span>
        {!managed && (
          <span
            data-testid={`extern-badge-${win.session}:${win.window}`}
            className="shrink-0 rounded-full border border-line px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-ink-3"
            title="Externes Fenster — nur anzeigen/anhängen, Schließen deaktiviert"
          >
            extern
          </span>
        )}
      </div>
      <div className="text-[10px] text-ink-3">{formatActivityAge(now, win.activity ?? null)}</div>
      <pre className="max-h-24 overflow-hidden whitespace-pre-wrap break-words rounded-card bg-surface-2 p-1.5 font-mono text-[10px] leading-tight text-ink-2">
        {(win.tail ?? "").split("\n").slice(-5).join("\n") || "—"}
      </pre>
      {dead && (
        <div className="flex gap-1.5">
          {/* Respawn only for dashboard-managed dead windows — foreign dead panes
              keep Entfernen (kill-dead) but must not recreate under work. */}
          {managed && (
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                onRespawn();
              }}
              className="inline-flex flex-1 items-center justify-center gap-1 rounded-card border border-line px-2 py-1.5 text-[11px] text-ink-2 hover:border-live/40 hover:text-live"
            >
              <RotateCcw className="h-3 w-3" />Respawn
            </button>
          )}
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onKill();
            }}
            className="inline-flex flex-1 items-center justify-center gap-1 rounded-card border border-line px-2 py-1.5 text-[11px] text-ink-2 hover:border-status-alert/40 hover:text-status-alert"
          >
            <Trash2 className="h-3 w-3" />Entfernen
          </button>
        </div>
      )}
      {/* Two-step close, same guard as the session rail — a single click on a Fleet
          card must never be able to kill a live agent session.
          Foreign (managed===false) windows stay visible but read-only for close. */}
      {!dead && managed && (
        <div className="flex gap-1.5">
          {terminateArmed ? (
            <>
              <button
                type="button"
                disabled={terminateBusy}
                onClick={(event) => {
                  event.stopPropagation();
                  onConfirmTerminate();
                }}
                className="inline-flex flex-1 items-center justify-center gap-1 rounded-card border border-status-alert/60 bg-status-alert/15 px-2 py-1.5 text-[11px] text-status-alert hover:bg-status-alert/25 disabled:cursor-not-allowed disabled:opacity-40"
                aria-label={`Beenden bestätigen ${win.session}:${win.window}`}
              >
                <Check className="h-3 w-3" />Wirklich beenden
              </button>
              <button
                type="button"
                disabled={terminateBusy}
                onClick={(event) => {
                  event.stopPropagation();
                  onCancelTerminate();
                }}
                className="inline-flex items-center justify-center gap-1 rounded-card border border-line px-2 py-1.5 text-[11px] text-ink-3 hover:bg-surface-3 hover:text-ink-2 disabled:cursor-not-allowed disabled:opacity-40"
                aria-label={`Beenden abbrechen ${win.session}:${win.window}`}
              >
                <X className="h-3 w-3" />Abbrechen
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                onTerminate();
              }}
              className="inline-flex flex-1 items-center justify-center gap-1 rounded-card border border-status-alert/30 px-2 py-1.5 text-[11px] text-status-alert hover:border-status-alert/60 hover:bg-status-alert/10"
              aria-label={`Session beenden ${win.session}:${win.window}`}
            >
              <Trash2 className="h-3 w-3" />Session beenden
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function AgentTerminalsView() {
  const navigate = useNavigate();
  const mobile = useIsMobile();
  const compactLayout = useIsCompactTerminalLayout();
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const extraPaneOneRef = useRef<TerminalPaneHandle | null>(null);
  const extraPaneTwoRef = useRef<TerminalPaneHandle | null>(null);
  const extraPaneThreeRef = useRef<TerminalPaneHandle | null>(null);
  const extraPaneRefs = useMemo(
    () => [extraPaneOneRef, extraPaneTwoRef, extraPaneThreeRef],
    [],
  );
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const tmuxCopyModeRef = useRef(false);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const resizeSendTimerRef = useRef<number | null>(null);
  const chipRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  // Monotonic seq for windows-list fetches: an in-flight poll issued before a
  // close must never overwrite a newer post-close list (stale tab flash).
  const windowsSeqRef = useRef(0);
  const windowsAppliedSeqRef = useRef(0);
  const [capability, setCapability] = useState<AgentTerminalCapabilityState | null>(null);
  const [windows, setWindows] = useState<AgentTerminalWindow[]>([]);
  const [selectedKind, setSelectedKind] = useState<AgentTerminalKind>("hermes");
  const [target, setTarget] = useState<{ session: string; window: string } | null>(() => {
    try {
      const raw = window.localStorage.getItem(TARGET_STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as { session?: unknown; window?: unknown };
      if (typeof parsed.session === "string" && typeof parsed.window === "string") {
        return { session: parsed.session, window: parsed.window };
      }
      return null;
    } catch {
      return null;
    }
  });
  const [desktopLayout, setDesktopLayout] = useState<DesktopTerminalLayout>(() => {
    try {
      return normalizeDesktopLayout(window.localStorage.getItem(LAYOUT_STORAGE_KEY));
    } catch {
      return 1;
    }
  });
  const [extraTargets, setExtraTargets] = useState<Array<PaneTarget | null>>(() => {
    try {
      const parsed = JSON.parse(window.localStorage.getItem(PANE_TARGETS_STORAGE_KEY) ?? "[]") as unknown;
      if (!Array.isArray(parsed)) return [null, null, null];
      return [0, 1, 2].map((index) => {
        const item = parsed[index] as { session?: unknown; window?: unknown } | undefined;
        return item && typeof item.session === "string" && typeof item.window === "string"
          ? { session: item.session, window: item.window }
          : null;
      });
    } catch {
      return [null, null, null];
    }
  });
  const [activePane, setActivePane] = useState(0);
  // Isolation is a property of the layout, not of history: every desktop attach
  // gets its own tmux client so the browser never forces its window size onto the
  // other clients of that session. Compact/mobile keeps the single direct attach
  // (its viewport IS the intended size) — that contract is unchanged.
  const primaryIsolated = !compactLayout;
  const [paneConnections, setPaneConnections] = useState<Record<number, TerminalPaneConnectionState>>({});
  const [rightRail, setRightRail] = useState<"usage" | "tools" | null>(() => !compactLayout && desktopLayout === 4 ? null : "usage");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Close/kill failures must survive a concurrent websocket onopen (which clears
  // attach errors). Track them in a ref so onopen only clears connection noise.
  const actionErrorRef = useRef<string | null>(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [sessionSheetOpen, setSessionSheetOpen] = useState(false);
  const [createSheetOpen, setCreateSheetOpen] = useState(false);
  const [createKind, setCreateKind] = useState<AgentTerminalKind>("hermes");
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [attachNonce, setAttachNonce] = useState(0);
  const [handoffOpen, setHandoffOpen] = useState(false);
  const [socketReady, setSocketReady] = useState(false);
  const [socketConnecting, setSocketConnecting] = useState(false);
  const [controlContext, setControlContext] = useState<ReadOnlyControlContext>(EMPTY_CONTROL_CONTEXT);
  const [composerText, setComposerText] = useState("");
  const [zen, setZen] = useState(false);
  const [immersiveHeight, setImmersiveHeight] = useState<number | null>(null);
  const [keysOpen, setKeysOpen] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(KEYS_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [workdir, setWorkdir] = useState<string>(() => readStoredWorkdir("hermes"));
  /** Visible note when a stored workdir key was invalid and we fell back to home. */
  const [workdirResetNote, setWorkdirResetNote] = useState<string | null>(null);
  const [fontSize, setFontSize] = useState<number | null>(() => {
    try {
      const stored = Number(window.localStorage.getItem(FONT_STORAGE_KEY));
      return Number.isFinite(stored) && stored >= FONT_MIN && stored <= FONT_MAX ? stored : null;
    } catch {
      return null;
    }
  });
  const [lastSeen, setLastSeen] = useState<Record<string, number>>(() => {
    try {
      const raw = window.localStorage.getItem(LASTSEEN_STORAGE_KEY);
      const parsed = raw ? (JSON.parse(raw) as Record<string, number>) : {};
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
      return {};
    }
  });
  const [renameValue, setRenameValue] = useState("");
  const [renameBusy, setRenameBusy] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  /** `session:window` of the window whose close is armed (step 1 of the guard), or null. */
  const [pendingTerminate, setPendingTerminate] = useState<string | null>(null);
  const [terminateBusy, setTerminateBusy] = useState(false);
  const [copyState, setCopyState] = useState<TerminalCopyState>("idle");
  /** Frozen buffer snapshot for the "Text auswählen" overlay; null = closed. */
  const [selectSnapshot, setSelectSnapshot] = useState<string | null>(null);
  /** Invalidates in-flight capture fetches when the overlay closes/reopens. */
  const selectSnapshotSeqRef = useRef(0);
  const [view, setView] = useState<"terminal" | "flotte">("terminal");
  const [overview, setOverview] = useState<AgentTerminalOverviewWindow[]>([]);
  const [overviewNow, setOverviewNow] = useState<number>(() => Date.now() / 1000);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [broadcastOpen, setBroadcastOpen] = useState(false);
  const [broadcastSelection, setBroadcastSelection] = useState<Set<string>>(new Set());
  const [broadcastText, setBroadcastText] = useState("");
  const [broadcastConfirming, setBroadcastConfirming] = useState(false);
  const [broadcastBusy, setBroadcastBusy] = useState(false);
  const [broadcastError, setBroadcastError] = useState<string | null>(null);
  const visiblePaneCount: DesktopTerminalLayout = compactLayout ? 1 : desktopLayout;
  const paneTargets = useMemo<Array<PaneTarget | null>>(() => [target, ...extraTargets], [extraTargets, target]);
  const activeTarget = paneTargets[Math.min(activePane, visiblePaneCount - 1)] ?? target;
  const selectedWindow = useMemo(() => {
    if (!activeTarget) return null;
    return windows.find((w) => w.session === activeTarget.session && w.window === activeTarget.window) ?? null;
  }, [activeTarget, windows]);
  const activeConnection = activePane === 0
    ? { ready: socketReady, connecting: socketConnecting, error: null }
    : paneConnections[activePane] ?? { ready: false, connecting: true, error: null };
  const activeSocketReady = activeConnection.ready;

  const sessions = useMemo(() => Array.from(new Set(windows.map((w) => w.session))), [windows]);
  const orderedWindows = useMemo(() => orderWindowsForStrip(windows), [windows]);
  const orderedOverview = useMemo(() => orderOverviewForFleet(overview), [overview]);
  const state = classifyTerminalState({ window: selectedWindow, socketReady: activeConnection.ready, socketConnecting: activeConnection.connecting, mobile });

  const selectPaneTarget = useCallback((paneIndex: number, next: PaneTarget) => {
    const duplicateIndex = paneTargets.findIndex((candidate, index) => index !== paneIndex && candidate && paneTargetKey(candidate) === paneTargetKey(next));
    if (duplicateIndex >= 0 && duplicateIndex < visiblePaneCount) {
      setActivePane(duplicateIndex);
      return;
    }
    if (paneIndex === 0) {
      setTarget(next);
    } else {
      setExtraTargets((current) => current.map((candidate, index) => (index === paneIndex - 1 ? next : candidate)));
    }
    setActivePane(paneIndex);
  }, [paneTargets, visiblePaneCount]);

  const chooseDesktopLayout = useCallback((layout: DesktopTerminalLayout) => {
    setDesktopLayout(layout);
    if (layout === 4) setRightRail(null);
    setActivePane((current) => Math.min(current, layout - 1));
  }, []);

  useEffect(() => {
    if (!compactLayout) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- compact mode must keep one direct mobile attach.
    setActivePane(0);
  }, [compactLayout]);

  useEffect(() => {
    if (!target || windows.length === 0) return;
    const available = windows.map(targetFromWindow);
    const resolved = resolvePaneTargets(available, [target, ...extraTargets], desktopLayout);
    const next = resolved.slice(1);
    if (JSON.stringify(next) === JSON.stringify(extraTargets)) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reconcile persisted pane targets with live tmux inventory.
    setExtraTargets(next);
  }, [desktopLayout, extraTargets, target, windows]);

  useEffect(() => {
    try {
      window.localStorage.setItem(LAYOUT_STORAGE_KEY, String(desktopLayout));
      window.localStorage.setItem(PANE_TARGETS_STORAGE_KEY, JSON.stringify(extraTargets));
    } catch {
      // Persistence is best-effort (private browsing / storage policy).
    }
  }, [desktopLayout, extraTargets]);

  const refresh = useCallback(async () => {
    setLoading(true);
    actionErrorRef.current = null;
    setError(null);
    const seq = ++windowsSeqRef.current;
    try {
      const [cap, win] = await Promise.all([api.getAgentTerminalCapabilities(), api.getAgentTerminalWindows()]);
      // Sequence guard covers capability too — a stale full refresh must not
      // overwrite a newer capability payload (same race as setWindows).
      if (seq > windowsAppliedSeqRef.current) {
        windowsAppliedSeqRef.current = seq;
        setCapability(cap);
        setWindows(win.windows);
        setTarget((previous) => pickInitialTarget(win.windows, selectedKind, previous));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [selectedKind]);

  const refreshWindowInventory = useCallback(async () => {
    const seq = ++windowsSeqRef.current;
    const win = await api.getAgentTerminalWindows();
    if (seq > windowsAppliedSeqRef.current) {
      windowsAppliedSeqRef.current = seq;
      setWindows(win.windows);
      setTarget((previous) => pickInitialTarget(win.windows, selectedKind, previous));
    }
  }, [selectedKind]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async tmux inventory load on mount / selectedKind change
    void refresh();
  }, [refresh]);

  // Keep the tmux inventory live independently of the overview cards. The
  // healer can create a missing managed window while this page is already
  // open; without this poll it stayed invisible until a manual reload.
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
      timer = window.setTimeout(() => void run(), WINDOW_INVENTORY_POLL_MS);
    }

    async function run(): Promise<void> {
      clearTimer();
      if (disposed || isHidden()) return;
      try {
        await refreshWindowInventory();
      } catch {
        // The initial/manual refresh owns the visible error state. A transient
        // background inventory failure must not cover a still-attached pane.
      } finally {
        scheduleNext();
      }
    }

    function onVisibilityChange(): void {
      if (isHidden()) clearTimer();
      else void run();
    }

    document.addEventListener("visibilitychange", onVisibilityChange);
    scheduleNext();
    return () => {
      disposed = true;
      clearTimer();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [refreshWindowInventory]);

  useEffect(() => {
    if (!target) return;
    try {
      window.localStorage.setItem(TARGET_STORAGE_KEY, JSON.stringify(target));
    } catch {
      /* storage optional */
    }
  }, [target]);

  // Merkt sich die zuletzt gesehene Aktivität des aktiven Fensters — läuft mit,
  // sobald refresh() neue window.activity-Werte liefert (Zielwechsel, manuelles
  // Neuladen), damit der Chip nach dem Betrachten nicht als "ungesehen" markiert bleibt.
  useEffect(() => {
    if (!target || !selectedWindow || selectedWindow.activity == null) return;
    const key = `${target.session}:${target.window}`;
    const activity = selectedWindow.activity;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- syncs the lastSeen baseline from window.activity whenever refresh() delivers a new value for the active target
    setLastSeen((previous) => {
      if (previous[key] === activity) return previous;
      const next = { ...previous, [key]: activity };
      try {
        window.localStorage.setItem(LASTSEEN_STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* storage optional */
      }
      return next;
    });
  }, [target, selectedWindow]);

  useEffect(() => {
    if (!target) return;
    const key = `${target.session}:${target.window}`;
    const chip = chipRefs.current[key];
    if (!chip) return;
    try {
      chip.scrollIntoView({ inline: "nearest", behavior: "smooth" });
    } catch {
      /* jsdom / older browsers: best-effort scroll only */
    }
  }, [target]);

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
      theme: { ...TERMINAL_THEME_STATIC, background: TERMINAL_MAIN_BACKGROUND },
      scrollback: 4000,
      loggerName: "agent-terminals",
      onWheelScrollBuffer: true,
      appAwareWheel: true,
      terminalOptions: storedFont ? { fontSize: storedFont } : undefined,
    });
    termRef.current = term;
    fitRef.current = fit;
    let resizeRaf = 0;
    let settleRaf1 = 0;
    let settleRaf2 = 0;
    const scheduleDebouncedResizeSend = () => {
      // fit() has already run — just (re-)arm the debounce timer for the RESIZE send.
      if (resizeSendTimerRef.current != null) window.clearTimeout(resizeSendTimerRef.current);
      resizeSendTimerRef.current = window.setTimeout(() => {
        resizeSendTimerRef.current = null;
        try {
          const ws = wsRef.current;
          if (ws?.readyState === WebSocket.OPEN) {
            ws.send(formatPtyResize(term.cols, term.rows));
          }
        } catch {
          /* WS closed/term disposed between arm and fire — best-effort send */
        }
      }, RESIZE_SEND_DEBOUNCE_MS);
    };
    const resize = () => {
      try {
        if (!host.isConnected || host.clientWidth <= 0 || host.clientHeight <= 0) return;
        fit.fit();
        scheduleDebouncedResizeSend();
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

    // Touch-drag scroll bridge: xterm v6 has no touch→application path at all
    // (touch-drag only scrolls the local viewport, a no-op on the alternate
    // buffer tmux runs fullscreen in). Translate vertical drag into tmux's
    // SGR wheel reports instead, one report per row-sized step. `sendRaw`
    // isn't defined yet at this point in the component body — this mirrors
    // its exact WS-send logic via `wsRef` directly rather than depending on
    // declaration order.
    let touchLastY: number | null = null;
    let touchAccumPx = 0;
    let touchStepPx = 0;
    const onTouchStart = (event: TouchEvent) => {
      if (term.buffer.active.type !== "alternate") return;
      const touch = event.touches[0];
      if (!touch) return;
      touchLastY = touch.clientY;
      touchAccumPx = 0;
      touchStepPx = Math.max(8, Math.round(host.clientHeight / Math.max(1, term.rows)));
    };
    const onTouchMove = (event: TouchEvent) => {
      if (term.buffer.active.type !== "alternate") return;
      if (touchLastY == null) return;
      // Without mouse tracking there is nothing we could send (raw SGR would
      // leak into the app) — bail BEFORE preventDefault so the gesture isn't
      // consumed for nothing; the scroll buttons remain the fallback.
      if (term.modes.mouseTrackingMode === "none") return;
      const touch = event.touches[0];
      if (!touch) return;
      // Stop the native (no-op on the alt buffer) viewport scroll and
      // pull-to-refresh from fighting the gesture.
      event.preventDefault();
      const deltaY = touch.clientY - touchLastY;
      touchLastY = touch.clientY;
      const { steps, remainder } = touchScrollSteps(touchAccumPx + deltaY, touchStepPx);
      touchAccumPx = remainder;
      if (steps === 0) return;
      // Finger moves DOWN (steps > 0) -> wheel UP -> back in history.
      const sequence = steps > 0 ? SGR_WHEEL_UP : SGR_WHEEL_DOWN;
      const ws = wsRef.current;
      if (ws?.readyState !== WebSocket.OPEN) return;
      for (let i = 0; i < Math.abs(steps); i += 1) ws.send(sequence);
    };
    const onTouchEnd = () => {
      touchLastY = null;
      touchAccumPx = 0;
    };
    host.addEventListener("touchstart", onTouchStart, { passive: false });
    host.addEventListener("touchmove", onTouchMove, { passive: false });
    host.addEventListener("touchend", onTouchEnd, { passive: false });
    host.addEventListener("touchcancel", onTouchEnd, { passive: false });

    return () => {
      observer.disconnect();
      window.visualViewport?.removeEventListener("resize", scheduleResize);
      window.removeEventListener("resize", scheduleResize);
      host.removeEventListener("touchstart", onTouchStart);
      host.removeEventListener("touchmove", onTouchMove);
      host.removeEventListener("touchend", onTouchEnd);
      host.removeEventListener("touchcancel", onTouchEnd);
      if (resizeRaf) cancelAnimationFrame(resizeRaf);
      if (settleRaf1) cancelAnimationFrame(settleRaf1);
      if (settleRaf2) cancelAnimationFrame(settleRaf2);
      if (resizeSendTimerRef.current != null) {
        window.clearTimeout(resizeSendTimerRef.current);
        resizeSendTimerRef.current = null;
      }
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
    // attachNonce is intentionally unused here beyond forcing a re-attach —
    // "Neu verbinden" bumps it to reopen the socket for the same target.
    void attachNonce;
    let disposed = false;
    let dataDisposable: { dispose: () => void } | null = null;
    wsRef.current?.close();
    wsRef.current = null;
    tmuxCopyModeRef.current = false;
    setSocketReady(false);
    setSocketConnecting(true);
    // reset(), not clear(): clear() keeps the last line plus every mode the previous
    // session left behind (alternate buffer, scroll region, SGR/charset state), so the
    // old agent's frame bleeds into the new target. reset() hands over a clean surface.
    term.reset();
    term.writeln(`Attaching ${target.session}:${target.window} …`);

    // Backoff-Reconnect, geteilt zwischen "Verbindung ging auf, dann weg" (onclose)
    // und "Verbindung kam nie zustande" (initialer buildWsUrl/Connect-Fehler) — sonst
    // bleibt der zweite Fall ein toter "Attaching …"-Screen ohne geplanten Retry.
    const scheduleReconnect = (): number => {
      const attempt = reconnectAttemptRef.current;
      reconnectAttemptRef.current = attempt + 1;
      const delayMs = reconnectDelayMs(attempt);
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null;
        if (!disposed) setAttachNonce((n) => n + 1);
      }, delayMs);
      return delayMs;
    };

    // Pre-fit before opening the socket so cols/rows are passed as query params —
    // the backend spawns the PTY at this size, avoiding the initial 80×24 blank-screen.
    try {
      fitRef.current?.fit();
    } catch {
      /* best-effort pre-attach fit */
    }
    const attachCols = String(Math.max(2, Math.floor(term.cols)));
    const attachRows = String(Math.max(2, Math.floor(term.rows)));
    void buildWsUrl("/api/agent-terminals/attach", {
      session: target.session,
      window: target.window,
      client_id: "agent-terminals-ui-pane-0",
      cols: attachCols,
      rows: attachRows,
      ...(primaryIsolated ? { isolated: "1" } : {}),
    })
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
          reconnectAttemptRef.current = 0;
          setSocketReady(true);
          setSocketConnecting(false);
          // Clear attach/connection errors only — never wipe a close/kill banner.
          if (actionErrorRef.current == null) setError(null);
          term.clear();
          // Ungedebounct: dieser Send folgt direkt auf den Handshake — kein Storm-Risiko.
          try {
            fitRef.current?.fit();
            ws.send(formatPtyResize(term.cols, term.rows));
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
          // Unerwarteter Abbruch (nicht Ziel-Wechsel/Unmount, sonst wäre disposed=true) —
          // mit Backoff automatisch neu verbinden, statt den User zum manuellen "Neu
          // verbinden" zu zwingen (mobile Tailscale-Reconnects sind häufig).
          scheduleReconnect();
        };
      })
      .catch((err) => {
        if (disposed) return;
        setSocketReady(false);
        setSocketConnecting(false);
        const message = err instanceof Error ? err.message : String(err);
        setError(message);
        // Fehler VOR dem Socket-Open (z.B. getWsTicket()/Backend während Deploy-Restart
        // weg) war bislang ein toter Endpunkt ohne Retry — derselbe Backoff wie onclose,
        // sonst hängt das Terminal nach einem Backend-Neustart für immer auf "Attaching …".
        const delayMs = scheduleReconnect();
        term.writeln(`Verbindung fehlgeschlagen (${message}) — neuer Versuch in ${Math.round(delayMs / 1000)}s …`);
      });

    return () => {
      disposed = true;
      dataDisposable?.dispose();
      if (reconnectTimerRef.current != null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [attachNonce, primaryIsolated, target]);

  // Tab wird sichtbar, Socket ist (noch) nicht offen → statt auf den nächsten
  // Backoff-Tick zu warten, sofort neu verbinden (Reset des Backoffs).
  useEffect(() => {
    function onVisibilityChange(): void {
      if (typeof document === "undefined" || document.hidden) return;
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) return;
      if (reconnectTimerRef.current != null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      reconnectAttemptRef.current = 0;
      setAttachNonce((n) => n + 1);
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, []);

  const submitCreateSession = useCallback(async () => {
    setCreateBusy(true);
    setCreateError(null);
    try {
      const response = await api.createAgentTerminalWindow(createKind, workdir);
      setSelectedKind(createKind);
      selectPaneTarget(activePane, targetFromWindow(response.window));
      await refresh();
      setCreateSheetOpen(false);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreateBusy(false);
    }
  }, [activePane, createKind, refresh, selectPaneTarget, workdir]);

  const respawnWindow = useCallback(
    async (win: { session: string; window: string }) => {
      setError(null);
      try {
        const response = await api.respawnAgentTerminalWindow(win.session, win.window);
        selectPaneTarget(activePane, targetFromWindow(response.window));
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [activePane, refresh, selectPaneTarget],
  );

  const killWindow = useCallback(
    async (win: { session: string; window: string }) => {
      actionErrorRef.current = null;
      setError(null);
      try {
        await api.killDeadAgentTerminalWindow(win.session, win.window);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        actionErrorRef.current = msg;
        setError(msg);
      } finally {
        // Always resync inventory — even on failure the server may have
        // partially applied / the local dead-flag may be stale. Prefer the
        // windows-only path so refresh()'s setError(null) does not race the banner.
        try {
          await refreshWindowInventory();
        } catch {
          // Inventory failures stay silent here; the kill error (if any) remains.
        }
      }
    },
    [refreshWindowInventory],
  );

  // Close guard, step 1 of 2. window.confirm() blocks the renderer thread: against a
  // live tmux the native dialog hung for ~30s and still did not close the window. The
  // guard is therefore in-app — arming a row, never blocking, and auto-disarming so a
  // forgotten armed row cannot be killed by a later stray click.
  const requestTerminate = useCallback((win: { session: string; window: string }) => {
    actionErrorRef.current = null;
    setError(null);
    setPendingTerminate(paneTargetKey(win));
  }, []);

  const cancelTerminate = useCallback(() => setPendingTerminate(null), []);

  // Close guard, step 2 of 2 — the only path that actually kills a tmux window.
  const confirmTerminate = useCallback(
    async (win: { session: string; window: string }) => {
      actionErrorRef.current = null;
      setError(null);
      setTerminateBusy(true);
      try {
        await api.terminateAgentTerminalWindow(win.session, win.window);
        // Success: full refresh (capabilities + windows).
        await refresh();
      } catch (err) {
        // Keep the error banner across inventory resync + any concurrent socket onopen.
        const msg = err instanceof Error ? err.message : String(err);
        actionErrorRef.current = msg;
        setError(msg);
        try {
          await refreshWindowInventory();
        } catch {
          // Keep the terminate error; background inventory owns no error UI.
        }
      } finally {
        // Always disarm: an error must not leave the row armed.
        setPendingTerminate(null);
        setTerminateBusy(false);
      }
    },
    [refresh, refreshWindowInventory],
  );

  useEffect(() => {
    if (!pendingTerminate) return;
    const timer = window.setTimeout(() => setPendingTerminate(null), TERMINATE_ARM_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }, [pendingTerminate]);

  /** xterm's selection is not React state — pull it from the pane that owns it. */
  const readPaneSelection = useCallback(
    (paneOrder: number): string => {
      if (paneOrder > 0) return extraPaneRefs[paneOrder - 1]?.current?.getSelection() ?? "";
      return termRef.current?.getSelection() ?? "";
    },
    [extraPaneRefs],
  );

  // Copy path. Never touches wsRef: a copy must not put ETX (or any byte) on the
  // socket, otherwise "copy" would SIGINT the very agent whose output is being copied.
  // Routes through the hardened clipboard helper (secure-context + execCommand fallback)
  // — navigator.clipboard.writeText alone silently fails on plain HTTP / non-secure.
  const copyText = useCallback(async (selection: string): Promise<void> => {
    if (!selection) {
      setCopyState("empty");
      return;
    }
    try {
      const ok = await copyTextToClipboard(selection);
      setCopyState(ok ? "copied" : "error");
    } catch {
      setCopyState("error");
    }
  }, []);

  /** Restore focus after terminal-originated copy (clipboard fallback can steal it). */
  const focusPane = useCallback(
    (paneOrder: number) => {
      if (paneOrder > 0) {
        extraPaneRefs[paneOrder - 1]?.current?.focus();
        return;
      }
      termRef.current?.focus();
    },
    [extraPaneRefs],
  );

  /** Selection of the focused pane — for callers without a pane of their own
   *  (toolbar button, handoff panel), unlike the keyboard chord which knows its pane. */
  const readActiveSelection = useCallback(
    (): string => readPaneSelection(activePane),
    [activePane, readPaneSelection],
  );

  const copySelection = useCallback(async (): Promise<void> => {
    await copyText(readActiveSelection());
    // Terminal-originated only — overlay "Alles kopieren" must not steal focus back.
    // Compact/touch layout: focusing xterm's hidden textarea would summon the
    // soft keyboard; the fallback-steals-focus problem is a desktop-chord case.
    if (!compactLayout) focusPane(activePane);
  }, [activePane, compactLayout, copyText, focusPane, readActiveSelection]);

  /** Active pane buffer as plain text — used for the mobile select-snapshot overlay. */
  const readActiveBufferText = useCallback((): string => {
    if (activePane > 0) {
      return extraPaneRefs[activePane - 1]?.current?.getBufferText() ?? "";
    }
    return extractTerminalBufferText(termRef.current);
  }, [activePane, extraPaneRefs]);

  const readActiveBufferType = useCallback((): string | undefined => {
    if (activePane > 0) {
      return extraPaneRefs[activePane - 1]?.current?.getActiveBufferType();
    }
    return termRef.current?.buffer?.active?.type;
  }, [activePane, extraPaneRefs]);

  const openSelectOverlay = useCallback(() => {
    // Capture once at open so polling / WS traffic cannot destroy a native selection.
    // Alternate buffer (tmux attach / TUI) has no client scrollback — fetch ~2000
    // lines from the server capture API (same path as TerminalHandoffPanel).
    const clientText = readActiveBufferText();
    const bufferType = readActiveBufferType();
    const captureTarget = activeTarget;
    const requestSeq = ++selectSnapshotSeqRef.current;
    if (bufferType === "alternate" && captureTarget) {
      setSelectSnapshot("Lade Verlauf …");
      void api
        .captureAgentTerminalWindow(captureTarget.session, captureTarget.window, -2000)
        .then((resp) => {
          // A close (or newer open) during the fetch wins — a late resolve must
          // not reopen the overlay.
          if (requestSeq !== selectSnapshotSeqRef.current) return;
          const content = typeof resp?.content === "string" ? resp.content : "";
          setSelectSnapshot(content || clientText);
        })
        .catch(() => {
          if (requestSeq !== selectSnapshotSeqRef.current) return;
          setSelectSnapshot(clientText);
        });
      return;
    }
    setSelectSnapshot(clientText);
  }, [activeTarget, readActiveBufferText, readActiveBufferType]);

  const closeSelectOverlay = useCallback(() => {
    selectSnapshotSeqRef.current += 1;
    setSelectSnapshot(null);
  }, []);

  useEffect(() => {
    if (copyState === "idle") return;
    const timer = window.setTimeout(() => setCopyState("idle"), COPY_STATUS_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!isTerminalCopyShortcut(event)) return;
      // Only chords fired inside an xterm surface are ours, and they copy THAT pane —
      // outside (composer, rename field, …) the focused control's own copy must win,
      // even while a stale selection still sits in a terminal.
      const paneOrder = terminalSurfaceOrder(event.target);
      if (paneOrder === null) return;
      const selection = readPaneSelection(paneOrder);
      // No selection → stay out of the way entirely and let the key reach the app.
      if (!selection) return;
      event.preventDefault();
      event.stopPropagation();
      void copyText(selection).then(() => {
        focusPane(paneOrder);
      });
    };
    // Capture phase: xterm binds its own keydown on the helper textarea, so the chord
    // has to be intercepted before it can be turned into terminal input.
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [copyText, focusPane, readPaneSelection]);

  const renameWindow = useCallback(async () => {
    if (!selectedWindow) return;
    const name = renameValue.trim();
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(name)) {
      setRenameError("Nur Buchstaben, Ziffern, _ und - (1–64 Zeichen).");
      return;
    }
    setRenameBusy(true);
    setRenameError(null);
    try {
      const response = await api.renameAgentTerminalWindow(selectedWindow.session, selectedWindow.window, name);
      const oldKey = `${selectedWindow.session}:${selectedWindow.window}`;
      const newKey = `${selectedWindow.session}:${name}`;
      if (oldKey !== newKey) {
        setLastSeen((previous) => {
          if (!(oldKey in previous)) return previous;
          const next = { ...previous };
          next[newKey] = next[oldKey];
          delete next[oldKey];
          try {
            window.localStorage.setItem(LASTSEEN_STORAGE_KEY, JSON.stringify(next));
          } catch {
            /* storage optional */
          }
          return next;
        });
      }
      selectPaneTarget(activePane, targetFromWindow(response.window));
      await refresh();
    } catch (err) {
      setRenameError(err instanceof Error ? err.message : String(err));
    } finally {
      setRenameBusy(false);
    }
  }, [activePane, refresh, renameValue, selectPaneTarget, selectedWindow]);

  const sendRaw = useCallback((sequence: string) => {
    if (activePane > 0) {
      extraPaneRefs[activePane - 1]?.current?.sendRaw(sequence);
      return;
    }
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) ws.send(sequence);
  }, [activePane, extraPaneRefs]);

  const syncPtySize = useCallback(() => {
    const term = termRef.current;
    const fit = fitRef.current;
    if (!term || !fit) return;
    try {
      fit.fit();
      // Debounce the RESIZE send to avoid a storm when called from adjustFont
      // (requestAnimationFrame → syncPtySize path) or repeated font-size taps.
      if (resizeSendTimerRef.current != null) window.clearTimeout(resizeSendTimerRef.current);
      resizeSendTimerRef.current = window.setTimeout(() => {
        resizeSendTimerRef.current = null;
        try {
          const ws = wsRef.current;
          if (ws?.readyState === WebSocket.OPEN) {
            ws.send(formatPtyResize(term.cols, term.rows));
          }
        } catch {
          /* WS closed/term disposed between arm and fire — best-effort send */
        }
      }, RESIZE_SEND_DEBOUNCE_MS);
    } catch {
      /* best-effort refit */
    }
  }, []);

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
      // Persist for the current create-sheet kind only (legacy global key left intact).
      window.localStorage.setItem(workdirStorageKeyForKind(createKind), key);
    } catch {
      /* storage optional */
    }
  }, [createKind]);

  // When the create-sheet kind changes, restore that kind's remembered workdir.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- kind switch must rehydrate per-kind memory
    setWorkdir(readStoredWorkdir(createKind));
    setWorkdirResetNote(null);
  }, [createKind]);

  useEffect(() => {
    // Stale localStorage-Key (z. B. entferntes Verzeichnis) gegen die aktuell gültigen
    // Optionen validieren — sonst spawnt "Neue Fenster starten in" mit totem Key.
    // Erst nach Capability-Load: der FALLBACK-Liste könnten legitime Backend-Keys fehlen.
    // Unlike pre-S4, surface a visible note instead of a silent home reset.
    if (!capability) return;
    const options = capability.workdirs?.length ? capability.workdirs : FALLBACK_WORKDIRS;
    if (!options.some((option) => option.key === workdir)) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- korrigiert Stale-Key nach Capability-Load, "home" ist immer gültig
      selectWorkdir("home");
      setWorkdirResetNote(WORKDIR_RESET_NOTE);
    }
  }, [capability, workdir, selectWorkdir]);

  const scrollTerminal = useCallback(
    (action: "pageUp" | "lineUp" | "lineDown" | "bottom") => {
      const term = activePane > 0 ? null : termRef.current;
      const handle = activePane > 0 ? extraPaneRefs[activePane - 1]?.current : null;
      if (action === "pageUp") {
        if (handle) handle.scrollPages(-1);
        else term?.scrollPages(-1);
        sendRaw(tmuxCopyModeRef.current ? "\x1b[5~" : TMUX_PAGE_UP);
        tmuxCopyModeRef.current = true;
      } else if (action === "lineUp") {
        if (handle) handle.scrollLines(-TMUX_LINE_STEP);
        else term?.scrollLines(-TMUX_LINE_STEP);
        sendRaw(`${tmuxCopyModeRef.current ? "" : TMUX_COPY_MODE}${"\x1b[A".repeat(TMUX_LINE_STEP)}`);
        tmuxCopyModeRef.current = true;
      } else if (action === "lineDown") {
        if (handle) handle.scrollLines(TMUX_LINE_STEP);
        else term?.scrollLines(TMUX_LINE_STEP);
        if (tmuxCopyModeRef.current) sendRaw("\x1b[B".repeat(TMUX_LINE_STEP));
      } else {
        if (handle) handle.scrollToBottom();
        else term?.scrollToBottom();
        if (tmuxCopyModeRef.current) sendRaw("q");
        tmuxCopyModeRef.current = false;
      }
    },
    [activePane, extraPaneRefs, sendRaw],
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

  // Phone → terminal upload: photo-picker button, paste, and drag&drop all
  // funnel through here. Sequential (not Promise.all) so the composer text
  // gets each path appended in drop order and one slow/huge upload doesn't
  // block the others from ever finishing.
  const uploadFiles = useCallback(async (files: FileList | File[]) => {
    const list = Array.from(files);
    if (!list.length) return;
    setUploadBusy(true);
    setUploadError(null);
    try {
      for (const file of list) {
        const result = await api.uploadAgentTerminalFile(file);
        setComposerText((current) => `${current}${result.path} `);
      }
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : String(err));
    } finally {
      setUploadBusy(false);
    }
  }, []);

  const toggleZen = useCallback(() => {
    setZen((current) => !current);
    setImmersiveHeight(null);
  }, []);

  const toggleKeysOpen = useCallback(() => {
    setKeysOpen((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(KEYS_STORAGE_KEY, next ? "1" : "0");
      } catch {
        /* storage optional */
      }
      return next;
    });
  }, []);

  useEffect(() => {
    if (sessionSheetOpen && selectedWindow) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- seeds the rename input with the current window name whenever the sheet opens for a (new) window
      setRenameValue(selectedWindow.window);
      setRenameError(null);
    }
  }, [sessionSheetOpen, selectedWindow]);

  const fetchOverview = useCallback(async () => {
    setOverviewLoading(true);
    try {
      const response = await api.getAgentTerminalOverview();
      setOverview(response.windows);
      setOverviewNow(response.now);
      setOverviewError(null);
    } catch (err) {
      setOverviewError(err instanceof Error ? err.message : String(err));
    } finally {
      setOverviewLoading(false);
    }
  }, []);

  // Fleet-Polling: auf Desktop IMMER an (speist den persistenten Fleet-Strip
  // über der Terminal-Fläche), auf compactLayout nur solange die Flotte-
  // Übersicht offen ist — gleiches visibility-aware-Timer-Muster wie
  // refreshReadOnlyContext oben.
  useEffect(() => {
    if (compactLayout && view !== "flotte") return;
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
      timer = window.setTimeout(() => void run(), OVERVIEW_POLL_MS);
    }

    async function run(): Promise<void> {
      clearTimer();
      if (disposed || isHidden()) return;
      try {
        await fetchOverview();
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
  }, [view, compactLayout, fetchOverview]);

  const toggleBroadcastMode = useCallback(() => {
    setBroadcastOpen((current) => {
      const next = !current;
      if (!next) {
        setBroadcastSelection(new Set());
        setBroadcastConfirming(false);
        setBroadcastError(null);
      }
      return next;
    });
  }, []);

  const toggleBroadcastSelection = useCallback((key: string) => {
    setBroadcastSelection((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const openFromFleet = useCallback((win: AgentTerminalOverviewWindow) => {
    selectPaneTarget(activePane, targetFromWindow(win));
    setView("terminal");
  }, [activePane, selectPaneTarget]);

  const sendBroadcast = useCallback(async () => {
    const payload = buildComposerPayload(broadcastText, true);
    const targets = orderedOverview.filter((win) => broadcastSelection.has(`${win.session}:${win.window}`));
    if (!payload || targets.length === 0) return;
    setBroadcastBusy(true);
    setBroadcastError(null);
    try {
      await Promise.all(targets.map((win) => api.sendAgentTerminalKeys(win.session, win.window, payload)));
      setBroadcastText("");
      setBroadcastSelection(new Set());
      setBroadcastOpen(false);
      setBroadcastConfirming(false);
    } catch (err) {
      setBroadcastError(err instanceof Error ? err.message : String(err));
    } finally {
      setBroadcastBusy(false);
    }
  }, [broadcastText, broadcastSelection, orderedOverview]);

  // compactLayout ist IMMER immersiv (fixed inset-0), Zen bleibt der Desktop-Vollbild-Toggle.
  const immersive = compactLayout || zen;

  // Immersiv: Höhe an den Visual Viewport koppeln, damit die Composer-Zeile
  // auf Mobile über der eingeblendeten Tastatur bleibt.
  useEffect(() => {
    if (!immersive) return;
    const viewport = window.visualViewport;
    if (!viewport) return;
    const update = () => setImmersiveHeight(Math.round(viewport.height));
    update();
    viewport.addEventListener("resize", update);
    return () => viewport.removeEventListener("resize", update);
  }, [immersive]);

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

  // Fleet-Overview keyed by "session:window" — lets the session lanes and the
  // ticker line look up a window's heuristic state without another fetch.
  const overviewByKey = useMemo(() => {
    const map = new Map<string, AgentTerminalOverviewWindow>();
    for (const win of overview) map.set(`${win.session}:${win.window}`, win);
    return map;
  }, [overview]);
  const selectedOverview = activeTarget ? overviewByKey.get(`${activeTarget.session}:${activeTarget.window}`) ?? null : null;

  const sessionList = (
    <div className="flex h-full flex-col gap-3">
      <div className="flex items-center justify-between border-b border-line-soft pb-3">
        <div>
          <Eyebrow>tmux</Eyebrow>
          <h2 className="text-sm font-semibold text-ink">Sessions / Windows</h2>
        </div>
        <button type="button" onClick={() => void refresh()} aria-label="Refresh agent terminals" className="grid h-12 w-12 place-items-center rounded-card border border-line bg-surface-2 text-ink-2 hover:border-live/40 hover:bg-surface-3 hover:text-live"><RefreshCw className="h-4 w-4" /></button>
      </div>
      <div className="grid gap-2">
        {sessions.length === 0 && <div className="rounded-card border border-line p-3 text-xs text-ink-3">Keine tmux-Session gefunden.</div>}
        {sessions.map((session) => (
          <div key={session} className="overflow-hidden rounded-card border border-line bg-surface-2">
            <div className="flex min-h-10 items-center gap-2 border-b border-line-soft px-3 text-xs font-medium text-ink-2"><Server className="h-3.5 w-3.5" />{session}</div>
            <div className="grid divide-y divide-line-soft">
              {windows.filter((w) => w.session === session).map((win) => {
                const active = activeTarget?.session === win.session && activeTarget.window === win.window;
                const dead = isDeadWindow(win);
                const managed = isManagedWindow(win);
                const laneOverview = overviewByKey.get(`${win.session}:${win.window}`);
                const laneState: AgentTerminalOverviewState = laneOverview?.state ?? (dead ? "dead" : "idle");
                return (
                  <div key={`${win.session}:${win.window}`} className={cn("flex min-h-12 items-stretch border-l-2", active ? "border-l-live bg-surface-3" : "border-l-transparent")}>
                    <button type="button" onClick={() => selectPaneTarget(activePane, targetFromWindow(win))} className={cn("min-w-0 flex-1 px-2.5 py-2 text-left text-xs transition", active ? "text-live" : "text-ink-2 hover:bg-surface-3")}>
                      <span className="flex items-center justify-between gap-2">
                        <span className="flex min-w-0 items-center gap-1.5">
                          <span className="truncate">{win.window}</span>
                          {!managed && (
                            <span
                              data-testid={`extern-badge-${win.session}:${win.window}`}
                              className="shrink-0 rounded-full border border-line px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-ink-3"
                              title="Externes Fenster — nur anzeigen/anhängen, Schließen deaktiviert"
                            >
                              extern
                            </span>
                          )}
                        </span>
                        <span className={cn("h-2 w-2 shrink-0 rounded-full", dead ? "bg-status-alert" : "bg-status-ok")} />
                      </span>
                      <span className="mt-0.5 block truncate text-[10px] text-ink-3">{dead ? "dead pane" : win.command || "—"}</span>
                      <span className="mt-0.5 block truncate font-mono text-[10px] text-ink-3" title={win.cwd?.trim() || undefined}>
                        {formatCwdShort(win.cwd)}
                      </span>
                      <Sparkline state={laneState} className="mt-1" />
                    </button>
                    {dead && (
                      <>
                        {/* WCAG 2.5.8 Desktop-Pointer-Ausnahme: 32×45 px bleibt hier nötig,
                            damit zwei redundante Row-Aktionen im 260-px-Rail nicht die
                            Zielzeile verdrängen; 24×24 px Mindestfläche bleibt erfüllt.
                            Respawn only for managed dead windows; kill-dead stays for foreign. */}
                        {managed && (
                          <button type="button" aria-label={`Neu starten ${win.session}:${win.window}`} title="Fenster neu starten" onClick={() => void respawnWindow(win)} className="grid w-8 shrink-0 place-items-center border-l border-line-soft text-ink-3 hover:bg-surface-3 hover:text-live">
                            <RotateCcw className="h-3.5 w-3.5" />
                          </button>
                        )}
                        <button type="button" aria-label={`Fenster schließen ${win.session}:${win.window}`} title="Totes Fenster entfernen" onClick={() => void killWindow(win)} className="grid w-8 shrink-0 place-items-center border-l border-line-soft text-ink-3 hover:bg-surface-3 hover:text-status-alert">
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </>
                    )}
                    {/* Foreign live windows: visible + attachable, no terminate (backend would 503). */}
                    {!dead && managed && (pendingTerminate === `${win.session}:${win.window}` ? (
                      <>
                        <button type="button" aria-label={`Beenden bestätigen ${win.session}:${win.window}`} title="Wirklich beenden — die laufende Agent-Arbeit geht verloren" disabled={terminateBusy} onClick={() => void confirmTerminate(win)} className="grid w-8 shrink-0 place-items-center border-l border-line-soft bg-status-alert/20 text-status-alert hover:bg-status-alert/30 disabled:cursor-not-allowed disabled:opacity-40">
                          <Check className="h-3.5 w-3.5" />
                        </button>
                        <button type="button" aria-label={`Beenden abbrechen ${win.session}:${win.window}`} title="Abbrechen" disabled={terminateBusy} onClick={cancelTerminate} className="grid w-8 shrink-0 place-items-center border-l border-line-soft text-ink-3 hover:bg-surface-3 hover:text-ink-2 disabled:cursor-not-allowed disabled:opacity-40">
                          <X className="h-3.5 w-3.5" />
                        </button>
                      </>
                    ) : (
                      <button type="button" aria-label={`Session beenden ${win.session}:${win.window}`} title="Laufende Session beenden" onClick={() => requestTerminate(win)} className="grid w-8 shrink-0 place-items-center border-l border-line-soft text-status-alert/70 hover:bg-status-alert/10 hover:text-status-alert">
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    ))}
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
    <div className="grid min-w-0 gap-3 rounded-card border border-line bg-surface-2 p-3">
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-brand" />
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-ink-3">Skills / Tools</p>
          <h3 className="text-sm font-semibold text-ink">Fähigkeiten sichtbar</h3>
        </div>
      </div>
      <div className="grid min-w-0 grid-cols-2 gap-1.5">
        <StatTile label="Skills aktiv" value={enabledSkills.length} tone={enabledSkills.length > 0 ? "ok" : "neutral"} />
        <StatTile label="Toolsets aktiv" value={enabledToolsets.length} tone={enabledToolsets.length > 0 ? "ok" : "neutral"} />
      </div>
      <div className="grid gap-1 text-xs">
        {capabilityRows.map(({ capability, state }) => (
          <div
            key={capability.label}
            title={capability.command}
            className="flex items-center justify-between gap-2 rounded-card border border-line bg-surface-2 px-2 py-1.5"
          >
            <span className="min-w-0 truncate font-medium text-ink">{capability.label}</span>
            <span
              title={state.detail}
              className={cn(
                "shrink-0 rounded-full border px-2 py-0.5 text-[10px]",
                state.tone === "ok"
                  ? "border-status-ok/35 text-status-ok"
                  : state.tone === "warn"
                    ? "border-status-warn/35 text-status-warn"
                    : "border-line text-ink-3",
              )}
            >
              {state.label}
            </span>
          </div>
        ))}
      </div>
      <div className="grid gap-1 text-[11px] text-ink-3">
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
    <div className="grid min-w-0 gap-3 rounded-card border border-line bg-surface-2 p-3">
      <div className="flex items-center gap-2">
        <Gauge className="h-4 w-4 text-status-ok" />
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-ink-3">Tageslage</p>
          <h3 className="text-sm font-semibold text-ink">Was läuft / was ist belegt</h3>
        </div>
      </div>
      <div className="grid min-w-0 grid-cols-2 gap-1.5">
        <StatTile label="Terminals" value={`${windows.filter((win) => win.pid).length}/${windows.length}`} tone={windows.some((win) => win.pid) ? "ok" : "neutral"} />
        <StatTile label="Kanban aktiv" value={activeTasks.length} tone={activeTasks.length > 0 ? "warn" : "neutral"} />
        <StatTile label="Blockiert" value={blockedTasks.length + decisionCount} tone={blockedTasks.length + decisionCount > 0 ? "warn" : "ok"} />
        <StatTile label="Claims" value={openClaims.length} tone={openClaims.length > 0 ? "warn" : "ok"} />
      </div>
      <div className="grid min-w-0 gap-2 text-[11px] text-ink-2 [&>div]:min-w-0">
        <div className="flex items-center gap-1.5">
          <CheckCircle2 className="h-3.5 w-3.5 text-status-ok" />
          <span>Health: <span className="font-medium text-ink">{healthOverall}</span></span>
        </div>
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-ink-2"><Bot className="h-3.5 w-3.5" />Aktive Agent-Terminals</div>
          {windows.filter((win) => win.pid).length ? (
            <ul className="space-y-1">
              {windows.filter((win) => win.pid).slice(0, 4).map((win) => (
                <li key={`${win.session}:${win.window}`} className="truncate" title={`${win.session}:${win.window} · ${win.cwd ?? ""}`}>
                  <span className="font-mono text-live">{win.session}:{win.window}</span> · {terminalProcessLabel(win, kindFromWindow(win, selectedKind))}
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-ink-3">—</div>
          )}
        </div>
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-ink-2"><Inbox className="h-3.5 w-3.5" />Offene Coordination-Claims</div>
          {openClaims.length ? (
            <ul className="space-y-1">
              {openClaims.slice(0, 3).map((claim) => (
                <li key={claim.path} className="truncate" title={`${claim.agent} · ${claim.task}`}>
                  [{claim.agent}] {claim.task || claim.started}
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-ink-3">keine</div>
          )}
        </div>
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-ink-2"><ClipboardList className="h-3.5 w-3.5" />Letzte Belege</div>
          {(evidenceReceipts.length ? evidenceReceipts : recentReceipts).length ? (
            <ul className="space-y-1">
              {(evidenceReceipts.length ? evidenceReceipts : recentReceipts).slice(0, 4).map((receipt) => (
                <li key={receipt.path} className="truncate" title={receipt.path}>
                  <span className="font-mono text-ink-3">{receipt.when}</span> [{receipt.agent}] {receipt.file}
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-ink-3">—</div>
          )}
        </div>
        <div>
          <div className="mb-1 text-ink-2">Kanban nächste Signale</div>
          {activeTasks.length || blockedTasks.length || decisionCount ? (
            <ul className="space-y-1">
              {activeTasks.slice(0, 2).map((task) => (
                <li key={`active-${task.id}`} className="truncate" title={task.title}>
                  <span className="font-mono text-live">{task.id}</span> · {task.status} · {task.title}
                </li>
              ))}
              {blockedTasks.slice(0, 2).map((task) => (
                <li key={`blocked-${task.id}`} className="truncate text-status-warn" title={task.title}>
                  <span className="font-mono">{task.id}</span> · blocked · {task.title}
                </li>
              ))}
              {decisionCount > 0 && <li className="text-status-warn">{decisionCount} Operator-Entscheidung(en) offen</li>}
            </ul>
          ) : (
            <div className="text-ink-3">keine aktiven/blockierten Items sichtbar</div>
          )}
        </div>
        {controlContext.error && (
          <div className="rounded-card border border-status-warn/25 bg-status-warn/10 p-2 text-status-warn">
            Kontext teilweise nicht geladen: {controlContext.error}
          </div>
        )}
      </div>
    </div>
  );

  const toolsDrawer = (
    <div className="grid min-w-0 gap-3 text-sm">
      <div className="flex items-center justify-between gap-3">
        <div><Eyebrow>Tools / Handoff</Eyebrow><h2 className="font-semibold text-ink">Terminal-Kontext</h2></div>
        {compactLayout && <button type="button" onClick={() => setToolsOpen(false)} className="rounded-card border border-line p-1.5 text-ink-2 hover:bg-surface-3"><X className="h-4 w-4" /></button>}
      </div>
      <div className="grid gap-2 rounded-card border border-line bg-surface-2 p-3 text-xs text-ink-2">
        <div className="flex justify-between"><span>Target</span><span className="text-ink">{activeTarget ? `${activeTarget.session}:${activeTarget.window}` : "—"}</span></div>
        <div className="flex justify-between"><span>Attach</span><TerminalStatusChip state={state} /></div>
        <div className="flex justify-between"><span>Input</span><span className="text-ink-2">nur User-Tasten, kein Auto-Send</span></div>
        <div className="flex justify-between"><span>Mobile</span><span className="text-ink-2">reattach an dasselbe tmux-Fenster</span></div>
      </div>
      <div className="rounded-card border border-status-warn/20 bg-status-warn/10 p-3 text-xs text-status-warn">
        Handoff bleibt optional: Diese Fläche erzwingt keinen Prompt- oder Übergabe-Flow.
      </div>
      <button
        type="button"
        onClick={() => { setHandoffOpen(true); setToolsOpen(false); }}
        disabled={!target}
        className="flex w-full items-center justify-center gap-1.5 whitespace-normal text-center rounded-card border border-live/50 bg-live/10 px-3 py-2 text-xs text-live hover:bg-live/20 disabled:opacity-40"
      >
        <Share2 className="h-3.5 w-3.5" />
        Handoff öffnen (Auswahl → PlanSpec/Kanban)
      </button>
      {toolsVisibility}
      {controlOverview}
    </div>
  );

  const composer = (
    <div
      className={cn(
        "shrink-0 border-t border-line-soft bg-surface-1 px-2 py-1.5 sm:px-3",
        compactLayout && !keysOpen && "pb-[calc(0.375rem+env(safe-area-inset-bottom,0px))]",
      )}
    >
      {uploadError && (
        <div className="mb-1.5 rounded-card border border-status-alert/30 bg-status-alert/10 p-2 text-xs text-status-alert">
          {de.agentTerminals.uploadError} {uploadError}
        </div>
      )}
      <div className="flex items-end gap-1.5">
        {compactLayout && (
          <button
            type="button"
            aria-label={keysOpen ? "Tastenleiste ausblenden" : "Tastenleiste einblenden"}
            aria-pressed={keysOpen}
            title={keysOpen ? "Tastenleiste ausblenden" : "Tastenleiste einblenden"}
            onPointerDown={(event) => event.preventDefault()}
            onClick={toggleKeysOpen}
            className={cn(
              "grid h-10 w-10 shrink-0 place-items-center rounded-card border transition",
              keysOpen ? "border-live/50 bg-live/15 text-live" : "border-line bg-surface-2 text-ink-2 hover:bg-surface-3",
            )}
          >
            <Keyboard className="h-4 w-4" />
          </button>
        )}
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          className="hidden"
          onChange={(event) => {
            const { files } = event.target;
            if (files && files.length) void uploadFiles(files);
            // Reset so re-picking the same photo still fires onChange.
            event.target.value = "";
          }}
        />
        <button
          type="button"
          aria-label={de.agentTerminals.attachFile}
          title={uploadBusy ? de.agentTerminals.uploading : de.agentTerminals.attachFile}
          disabled={uploadBusy}
          onClick={() => fileInputRef.current?.click()}
          className="grid h-10 w-10 shrink-0 place-items-center rounded-card border border-line bg-surface-2 text-ink-2 transition hover:bg-surface-3 disabled:opacity-40"
        >
          {uploadBusy ? (
            <span aria-hidden="true" className="h-4 w-4 animate-spin rounded-full border-2 border-ink-3 border-t-transparent" />
          ) : (
            <Paperclip className="h-4 w-4" />
          )}
        </button>
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
          onPaste={(event) => {
            const files = event.clipboardData?.files;
            if (files && files.length) {
              event.preventDefault();
              void uploadFiles(files);
            }
          }}
          placeholder={activeSocketReady ? "Prompt oder Befehl … (Enter sendet)" : "Terminal nicht verbunden"}
          disabled={!activeSocketReady}
          rows={Math.min(4, Math.max(1, composerText.split("\n").length))}
          enterKeyHint="send"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          className="agent-terminal-composer-input min-h-10 min-w-0 flex-1 resize-none rounded-card border border-line bg-surface-2 px-2.5 py-2 font-mono text-[13px] leading-5 text-ink placeholder:text-ink-3 focus:border-live/50 focus:outline-none disabled:opacity-40"
        />
        <button
          type="button"
          aria-label="Eingabe senden"
          title="Senden (mit Enter)"
          disabled={!activeSocketReady || !composerText}
          onClick={() => sendComposer(true)}
          className="grid h-10 w-12 shrink-0 place-items-center rounded-card border border-live/50 bg-live/15 text-live transition hover:bg-live/25 disabled:cursor-not-allowed disabled:opacity-35"
        >
          <CornerDownLeft className="h-4 w-4" />
        </button>
      </div>
    </div>
  );

  const keysBar = compactLayout && keysOpen ? (
    <div className="shrink-0 border-t border-line-soft bg-surface-1 px-2 pt-1.5 pb-[calc(0.375rem+env(safe-area-inset-bottom,0px))]">
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
            <TerminalControlButton label="Send arrow left" disabled={!activeSocketReady} onClick={() => sendKey("\x1b[D")}>
              <ArrowLeft className="h-4 w-4" />
            </TerminalControlButton>
            <TerminalControlButton label="Send arrow up" disabled={!activeSocketReady} onClick={() => sendKey("\x1b[A")}>
              <ArrowUp className="h-4 w-4" />
            </TerminalControlButton>
            <TerminalControlButton label="Send arrow down" disabled={!activeSocketReady} onClick={() => sendKey("\x1b[B")}>
              <ArrowDown className="h-4 w-4" />
            </TerminalControlButton>
            <TerminalControlButton label="Send arrow right" disabled={!activeSocketReady} onClick={() => sendKey("\x1b[C")}>
              <ArrowRight className="h-4 w-4" />
            </TerminalControlButton>
          </div>
        </div>
        <div className="grid grid-cols-5 gap-1" role="group" aria-label="Terminal special keys">
          {QUICK_KEYS.map((key) => (
            <TerminalControlButton key={key.label} label={`Send ${key.label}`} disabled={!activeSocketReady} onClick={() => sendKey(key.sequence)}>
              <span className="font-mono text-[11px]">{key.label}</span>
            </TerminalControlButton>
          ))}
        </div>
      </div>
    </div>
  ) : null;

  const sessionSheetKind = kindFromWindow(selectedWindow, selectedKind);
  const sessionSheetDead = selectedWindow ? isDeadWindow(selectedWindow) : false;
  const sessionSheetManaged = selectedWindow ? isManagedWindow(selectedWindow) : true;

  const chipStrip = (
    <div className="flex h-11 shrink-0 items-stretch border-b border-line-soft bg-surface-1">
      <button
        type="button"
        aria-label={view === "flotte" ? "Terminal-Ansicht" : "Flotten-Übersicht"}
        onClick={() => setView((current) => (current === "flotte" ? "terminal" : "flotte"))}
        className="grid shrink-0 place-items-center border-r border-line-soft px-3 text-ink-2 hover:bg-surface-3"
      >
        {view === "flotte" ? <TerminalSquare className="h-4 w-4" /> : <LayoutGrid className="h-4 w-4" />}
      </button>
      <button
        type="button"
        aria-label="Zurück zum Dashboard"
        onClick={() => navigate("/control")}
        className="grid shrink-0 place-items-center border-r border-line-soft px-3 text-ink-2 hover:bg-surface-3"
      >
        <ChevronLeft className="h-4 w-4" />
      </button>
      <div className="flex min-w-0 flex-1 items-stretch gap-1.5 overflow-x-auto px-1.5 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        {orderedWindows.map((win) => {
          const active = activeTarget?.session === win.session && activeTarget.window === win.window;
          const dead = isDeadWindow(win);
          const key = `${win.session}:${win.window}`;
          const unseen = !active && hasUnseenActivity(win, lastSeen);
          return (
            <button
              key={key}
              ref={(el) => {
                chipRefs.current[key] = el;
              }}
              type="button"
              onClick={() => (active ? setSessionSheetOpen(true) : selectPaneTarget(activePane, targetFromWindow(win)))}
              className={cn(
                "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-3 text-xs font-medium transition",
                active ? "border-live/60 bg-live/10 text-live" : "border-line bg-surface-2 text-ink-2",
              )}
            >
              <span className="relative grid h-2 w-2 shrink-0 place-items-center">
                <span className={cn("h-2 w-2 rounded-full", dead ? "bg-status-alert" : "bg-status-ok")} />
                {unseen && <span className="absolute -right-1 -top-1 h-1.5 w-1.5 rounded-full bg-live" />}
              </span>
              <span className="max-w-[8rem] truncate">{chipLabel(win)}</span>
            </button>
          );
        })}
      </div>
      <button
        type="button"
        aria-label="Neue Session starten"
        onClick={() => setCreateSheetOpen(true)}
        className="grid shrink-0 place-items-center border-l border-line-soft px-3 text-live hover:bg-live/10"
      >
        <Plus className="h-4 w-4" />
      </button>
    </div>
  );

  const sessionSheet = compactLayout && sessionSheetOpen && selectedWindow && (
    <div className="fixed inset-x-0 bottom-0 z-50 max-h-[85svh] overflow-auto rounded-t-panel border border-line bg-surface-1 p-4 pb-[calc(1rem+env(safe-area-inset-bottom,0px))] shadow-2xl">
      <div className="mx-auto mb-3 h-1 w-12 rounded-full bg-ink-3/20" />
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <Eyebrow>{AGENT_LABELS[sessionSheetKind] ?? sessionSheetKind}</Eyebrow>
          <div className="flex min-w-0 items-center gap-1.5">
            <h2 className="truncate font-mono text-sm font-semibold text-ink">{`${selectedWindow.session}:${selectedWindow.window}`}</h2>
            {!sessionSheetManaged && (
              <span
                data-testid={`extern-badge-${selectedWindow.session}:${selectedWindow.window}`}
                className="shrink-0 rounded-full border border-line px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-ink-3"
                title="Externes Fenster — nur anzeigen/anhängen, Schließen deaktiviert"
              >
                extern
              </span>
            )}
          </div>
        </div>
        <button type="button" onClick={() => setSessionSheetOpen(false)} aria-label="Sitzung schließen" className="shrink-0 rounded-card border border-line p-1.5 text-ink-2 hover:bg-surface-3"><X className="h-4 w-4" /></button>
      </div>
      <div className="mt-2 flex items-center gap-1.5">
        <Pencil className="h-3.5 w-3.5 shrink-0 text-ink-3" />
        <input
          aria-label="Neuer Fenstername"
          value={renameValue}
          disabled={renameBusy}
          onChange={(event) => setRenameValue(event.target.value)}
          className="min-w-0 flex-1 rounded-card border border-line bg-surface-2 px-2.5 py-1.5 font-mono text-xs text-ink focus:border-live/50 focus:outline-none disabled:opacity-40"
        />
        <button
          type="button"
          onClick={() => void renameWindow()}
          disabled={renameBusy || !renameValue.trim() || renameValue.trim() === selectedWindow.window}
          className="inline-flex shrink-0 items-center gap-1 rounded-card border border-line bg-surface-2 px-2.5 py-1.5 text-xs text-ink-2 hover:border-live/40 hover:text-live disabled:cursor-not-allowed disabled:opacity-40"
        >
          {renameBusy ? "…" : "Umbenennen"}
        </button>
      </div>
      {renameError && <div className="mt-1.5 rounded-card border border-status-alert/30 bg-status-alert/10 p-2 text-[11px] text-status-alert">{renameError}</div>}
      <div className="mt-3 grid gap-1.5 rounded-card border border-line bg-surface-2 p-3 text-xs text-ink-2">
        <div className="flex items-center justify-between gap-2">
          <span>cwd</span>
          <span
            data-testid="session-sheet-cwd"
            className="min-w-0 truncate font-mono text-ink-2"
            title={selectedWindow.cwd?.trim() || undefined}
          >
            {formatCwdShort(selectedWindow.cwd)}
          </span>
        </div>
        <div className="flex items-center justify-between gap-2"><span>Prozess</span><span className="min-w-0 truncate font-mono text-ink-2">{terminalProcessLabel(selectedWindow, sessionSheetKind)}</span></div>
        <div className="flex items-center justify-between gap-2"><span>Status</span><TerminalStatusChip state={state} /></div>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
        <button type="button" onClick={() => { if (activePane > 0) extraPaneRefs[activePane - 1]?.current?.reconnect(); else setAttachNonce((n) => n + 1); }} className="flex flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
          <PlugZap className="h-4 w-4" /><span>Neu verbinden</span>
        </button>
        <button type="button" disabled={!activeSocketReady} onClick={() => sendKey("\x03")} className="flex flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3 disabled:cursor-not-allowed disabled:opacity-35">
          <span className="font-mono text-sm">^C</span><span>^C senden</span>
        </button>
        {sessionSheetDead && sessionSheetManaged && (
          <button type="button" onClick={() => { void respawnWindow(selectedWindow); setSessionSheetOpen(false); }} className="flex flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
            <RotateCcw className="h-4 w-4" /><span>Neu starten</span>
          </button>
        )}
        {sessionSheetDead && (
          <button type="button" onClick={() => { void killWindow(selectedWindow); setSessionSheetOpen(false); }} className="flex flex-col items-center gap-1 rounded-card border border-status-alert/25 px-2 py-2.5 text-center leading-tight text-status-alert hover:bg-status-alert/10">
            <Trash2 className="h-4 w-4" /><span>Fenster entfernen</span>
          </button>
        )}
        {/* Same two-step guard as the desktop rail: the first tap arms, the second kills.
            The sheet stays open in between so the armed state is visible where it was armed.
            Foreign (managed===false) windows stay attachable but offer no terminate. */}
        {!sessionSheetDead && sessionSheetManaged && (pendingTerminate === `${selectedWindow.session}:${selectedWindow.window}` ? (
          <button type="button" aria-label={`Beenden bestätigen ${selectedWindow.session}:${selectedWindow.window}`} disabled={terminateBusy} onClick={() => { void confirmTerminate(selectedWindow).then(() => setSessionSheetOpen(false)); }} className="flex flex-col items-center gap-1 rounded-card border border-status-alert/50 bg-status-alert/15 px-2 py-2.5 text-center leading-tight text-status-alert hover:bg-status-alert/25 disabled:cursor-not-allowed disabled:opacity-40">
            <Check className="h-4 w-4" /><span>Wirklich beenden</span>
          </button>
        ) : (
          <button type="button" aria-label={`Session beenden ${selectedWindow.session}:${selectedWindow.window}`} onClick={() => requestTerminate(selectedWindow)} className="flex flex-col items-center gap-1 rounded-card border border-status-alert/25 px-2 py-2.5 text-center leading-tight text-status-alert hover:bg-status-alert/10">
            <Trash2 className="h-4 w-4" /><span>Session beenden</span>
          </button>
        ))}
        <button type="button" onClick={() => { setHandoffOpen(true); setSessionSheetOpen(false); }} className="flex flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
          <Share2 className="h-4 w-4" /><span>Handoff öffnen</span>
        </button>
        <button type="button" onClick={() => adjustFont(-1)} className="flex flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
          <span className="font-mono text-sm">A−</span><span>Schrift kleiner</span>
        </button>
        <button type="button" onClick={() => adjustFont(1)} className="flex flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
          <span className="font-mono text-sm">A+</span><span>Schrift größer</span>
        </button>
        <button type="button" onClick={() => { setToolsOpen(true); setSessionSheetOpen(false); }} className="flex flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
          <Wrench className="h-4 w-4" /><span>Tools / Tageslage</span>
        </button>
        <button type="button" onClick={() => void refresh()} className="flex flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
          <RefreshCw className="h-4 w-4" /><span>Liste aktualisieren</span>
        </button>
      </div>
    </div>
  );

  const createSessionForm = (
    <div className="grid gap-3">
      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-5">
        {AGENTS.map((agent) => (
          <button
            key={agent.kind}
            type="button"
            onClick={() => setCreateKind(agent.kind)}
            className={cn(
              "rounded-card border px-2 py-2 text-left text-xs transition",
              createKind === agent.kind ? "border-live/60 bg-live/10 text-live" : "border-line bg-surface-2 text-ink-2 hover:bg-surface-3",
            )}
          >
            <span className="flex min-w-0 items-center justify-between gap-1.5">
              <span className="flex min-w-0 items-center gap-1.5"><TerminalSquare className="h-3.5 w-3.5 shrink-0" /><span className="truncate">{agent.label}</span></span>
              <CapabilityPill capability={capability} agent={agent} />
            </span>
          </button>
        ))}
      </div>
      <label className="grid gap-1 text-xs text-ink-3">
        <span>Arbeitsverzeichnis</span>
        <select
          aria-label="Arbeitsverzeichnis für neue Terminals"
          value={workdir}
          onChange={(event) => {
            setWorkdirResetNote(null);
            selectWorkdir(event.target.value);
          }}
          className="rounded-card border border-line bg-surface-2 px-2 py-2 text-xs text-ink-2 focus:border-live/50 focus:outline-none"
        >
          {(capability?.workdirs?.length ? capability.workdirs : FALLBACK_WORKDIRS).map((option) => (
            <option key={option.key} value={option.key}>{option.label}</option>
          ))}
        </select>
      </label>
      {workdirResetNote && (
        <div className="rounded-card border border-line bg-surface-2 p-2 text-[11px] text-ink-3" role="status">
          {workdirResetNote}
        </div>
      )}
      {createError && <div className="rounded-card border border-status-alert/30 bg-status-alert/10 p-2 text-xs text-status-alert">{createError}</div>}
      <button
        type="button"
        onClick={() => void submitCreateSession()}
        disabled={createBusy}
        className="inline-flex items-center justify-center gap-1.5 rounded-card border border-live/50 bg-live/15 px-3 py-2.5 text-sm font-medium text-live hover:bg-live/25 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {createBusy ? "Startet…" : "Session starten"}
      </button>
    </div>
  );

  const createSheetHeader = (
    <div className="mb-3 flex items-center justify-between gap-2">
      <div><Eyebrow>Neue Session</Eyebrow><h2 className="text-sm font-semibold text-ink">Agent wählen</h2></div>
      <button type="button" onClick={() => setCreateSheetOpen(false)} aria-label="Schließen" className="rounded-card border border-line p-1.5 text-ink-2 hover:bg-surface-3"><X className="h-4 w-4" /></button>
    </div>
  );

  const createSheet = createSheetOpen && (
    compactLayout ? (
      <div className="fixed inset-x-0 bottom-0 z-50 max-h-[85svh] overflow-auto rounded-t-panel border border-line bg-surface-1 p-4 pb-[calc(1rem+env(safe-area-inset-bottom,0px))] shadow-2xl">
        <div className="mx-auto mb-3 h-1 w-12 rounded-full bg-ink-3/20" />
        {createSheetHeader}
        {createSessionForm}
      </div>
    ) : (
      <div className="fixed inset-0 z-50 grid place-items-center bg-surface-0/60 p-4">
        <div className="w-full max-w-sm rounded-panel border border-line bg-surface-1 p-4 shadow-2xl">
          {createSheetHeader}
          {createSessionForm}
        </div>
      </div>
    )
  );

  const fleetPanel = (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <Eyebrow>Flotte</Eyebrow>
          <h2 className="text-sm font-semibold text-ink">
            {orderedOverview.length} Fenster · {orderedOverview.filter((win) => win.state !== "dead").length} aktiv
          </h2>
        </div>
        <div className="flex items-center gap-2">
          {overviewLoading && <span className="text-xs text-ink-3">lädt…</span>}
          <button
            type="button"
            onClick={toggleBroadcastMode}
            aria-pressed={broadcastOpen}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-card border px-3 py-1.5 text-xs font-medium transition",
              broadcastOpen ? "border-live/60 bg-live/15 text-live" : "border-line bg-surface-2 text-ink-2 hover:bg-surface-3",
            )}
          >
            Senden an mehrere
          </button>
        </div>
      </div>
      {overviewError && (
        <div className="flex items-center gap-1.5 rounded-card border border-status-alert/25 bg-status-alert/10 px-2.5 py-1.5 text-xs text-status-alert">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />{overviewError}
        </div>
      )}
      {orderedOverview.length === 0 ? (
        <div className="rounded-card border border-line p-4 text-center text-xs text-ink-3">
          {overviewLoading ? "Lädt Flotten-Übersicht…" : "Keine tmux-Fenster gefunden."}
        </div>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {orderedOverview.map((win) => {
            const key = `${win.session}:${win.window}`;
            return (
              <FleetCard
                key={key}
                win={win}
                now={overviewNow}
                selected={broadcastSelection.has(key)}
                broadcastMode={broadcastOpen}
                onToggleSelect={() => toggleBroadcastSelection(key)}
                onOpen={() => openFromFleet(win)}
                onRespawn={() => void respawnWindow(win)}
                onKill={() => void killWindow(win)}
                onTerminate={() => requestTerminate(win)}
                terminateArmed={pendingTerminate === key}
                terminateBusy={terminateBusy}
                onConfirmTerminate={() => void confirmTerminate(win)}
                onCancelTerminate={cancelTerminate}
              />
            );
          })}
        </div>
      )}
      {broadcastOpen && (
        <div className="grid gap-1.5 rounded-card border border-live/30 bg-surface-1 p-2.5">
          {broadcastConfirming ? (
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-status-warn">
              <span>Wirklich an {broadcastSelection.size} Sessions senden?</span>
              <div className="flex gap-1.5">
                <button
                  type="button"
                  onClick={() => void sendBroadcast()}
                  disabled={broadcastBusy}
                  className="rounded-card border border-live/50 bg-live/15 px-2.5 py-1 text-live hover:bg-live/25 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {broadcastBusy ? "Sendet…" : "Ja"}
                </button>
                <button
                  type="button"
                  onClick={() => setBroadcastConfirming(false)}
                  className="rounded-card border border-line px-2.5 py-1 text-ink-2 hover:bg-surface-3"
                >
                  Abbrechen
                </button>
              </div>
            </div>
          ) : (
            <div className="flex items-end gap-1.5">
              <textarea
                aria-label="Text an mehrere Terminals senden"
                value={broadcastText}
                onChange={(event) => setBroadcastText(event.target.value)}
                rows={Math.min(2, Math.max(1, broadcastText.split("\n").length))}
                placeholder="Prompt oder Befehl für die Auswahl …"
                className="min-h-9 min-w-0 flex-1 resize-none rounded-card border border-line bg-surface-2 px-2.5 py-1.5 font-mono text-[13px] leading-5 text-ink placeholder:text-ink-3 focus:border-live/50 focus:outline-none"
              />
              <button
                type="button"
                onClick={() => setBroadcastConfirming(true)}
                disabled={!broadcastText || broadcastSelection.size === 0}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-card border border-live/50 bg-live/15 px-3 py-2 text-xs font-medium text-live hover:bg-live/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                An {broadcastSelection.size} senden
              </button>
            </div>
          )}
          {broadcastError && <div className="rounded-card border border-status-alert/30 bg-status-alert/10 p-2 text-[11px] text-status-alert">{broadcastError}</div>}
        </div>
      )}
      <p className="text-center text-[10px] text-ink-3">Zustände: Heuristik aus Terminal-Ausgabe</p>
    </div>
  );

  const handlePaneConnection = useCallback((paneIndex: number, connection: TerminalPaneConnectionState) => {
    setPaneConnections((current) => {
      const previous = current[paneIndex];
      if (previous?.ready === connection.ready && previous.connecting === connection.connecting && previous.error === connection.error) return current;
      return { ...current, [paneIndex]: connection };
    });
  }, []);

  const paneHeader = (paneIndex: number, paneTarget: PaneTarget | null) => {
    const connection = paneIndex === 0
      ? { ready: socketReady, connecting: socketConnecting }
      : paneConnections[paneIndex] ?? { ready: false, connecting: true };
    const usedByOtherPane = new Set(
      paneTargets
        .slice(0, visiblePaneCount)
        .filter((candidate, index): candidate is PaneTarget => index !== paneIndex && Boolean(candidate))
        .map(paneTargetKey),
    );
    return (
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-line-soft bg-surface-1 px-2">
        <button
          type="button"
          className="inline-flex min-h-6 shrink-0 items-center gap-1.5 rounded-card px-1 text-[10px] text-ink-2 hover:bg-surface-3"
          aria-label={`Pane ${paneIndex + 1} aktivieren (${connection.ready ? "verbunden" : connection.connecting ? "verbindet…" : "getrennt"})`}
          onClick={() => setActivePane(paneIndex)}
        >
          <span aria-hidden className={cn("hc-led size-2 rounded-full", connection.ready ? "hc-led-live" : connection.connecting ? "hc-led-warn" : "hc-led-error")} />
          {connection.ready ? "verbunden" : connection.connecting ? "verbindet…" : "getrennt"}
        </button>
        <select
          aria-label={`Terminal ${paneIndex + 1}`}
          value={paneTarget ? paneTargetKey(paneTarget) : ""}
          onChange={(event) => {
            const next = windows.find((item) => `${item.session}:${item.window}` === event.target.value);
            if (next) selectPaneTarget(paneIndex, targetFromWindow(next));
          }}
          className="min-w-0 flex-1 truncate bg-transparent font-mono text-[11px] text-ink outline-none"
        >
          <option value="" disabled>Terminal wählen</option>
          {windows.map((item) => {
            const optionTarget = targetFromWindow(item);
            return (
              <option key={`${item.session}:${item.window}`} value={paneTargetKey(optionTarget)} disabled={usedByOtherPane.has(paneTargetKey(optionTarget))}>
                {item.session}:{item.window}
              </option>
            );
          })}
        </select>
        <span className="font-mono text-[10px] text-ink-3">{paneIndex + 1}/{visiblePaneCount}</span>
      </div>
    );
  };

  const primaryHost = (
    <div
      ref={hostRef}
      data-testid="terminal-pane-host-0"
      data-terminal-surface="0"
      // Match xterm canvas theme so floored cols/rows leave no ridge of surface-0.
      className="xterm-surface min-h-0 min-w-0 flex-1 overflow-hidden"
      style={{ backgroundColor: TERMINAL_MAIN_BACKGROUND }}
      onMouseDown={() => setActivePane(0)}
      onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }}
      onDrop={(event) => {
        event.preventDefault();
        const files = event.dataTransfer?.files;
        if (files && files.length) void uploadFiles(files);
      }}
      aria-label="Live terminal output"
    />
  );

  const terminalPaneSurface = (
    <div
      data-testid={`terminal-layout-${visiblePaneCount}`}
      className={cn(
        "min-h-0 min-w-0 flex-1 bg-surface-0",
        visiblePaneCount === 1
          ? "flex"
          : "grid gap-2 p-2",
        visiblePaneCount === 2 && "grid-cols-2 grid-rows-1",
        visiblePaneCount === 4 && "grid-cols-2 grid-rows-2",
      )}
    >
      {paneTargets.slice(0, visiblePaneCount).map((paneTarget, paneIndex) => (
        <section
          key={paneIndex}
          data-testid={`terminal-pane-card-${paneIndex}`}
          className={cn(
            "flex h-full min-h-0 min-w-0 w-full shrink-0 flex-col overflow-hidden",
            visiblePaneCount > 1 && "rounded-panel border bg-surface-0 shadow-[0_12px_30px_rgba(0,0,0,.22)]",
            visiblePaneCount > 1 && (activePane === paneIndex ? "border-live/55 ring-1 ring-live/15" : "border-line"),
          )}
          onMouseDown={() => setActivePane(paneIndex)}
        >
          {visiblePaneCount > 1 && paneHeader(paneIndex, paneTarget)}
          {paneIndex === 0 ? primaryHost : paneTarget ? (
            <TerminalPane
              ref={extraPaneRefs[paneIndex - 1]}
              target={paneTarget}
              paneOrder={paneIndex}
              fontSize={fontSize ?? 13}
              isolated
              active={activePane === paneIndex}
              onActivate={() => setActivePane(paneIndex)}
              onConnectionChange={(connection) => handlePaneConnection(paneIndex, connection)}
            />
          ) : (
            <button type="button" className="grid h-full place-items-center text-xs text-ink-3" onClick={() => setActivePane(paneIndex)}>
              Weiteres Terminal wählen
            </button>
          )}
        </section>
      ))}
    </div>
  );

  return (
    <div className="flex min-h-[calc(100vh-8rem)] flex-col gap-2 text-ink sm:gap-3">
      {!compactLayout && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-panel border border-line bg-surface-1 p-3">
          <div className="min-w-0">
            <Eyebrow>Agent Terminals</Eyebrow>
            <div className="mt-1 flex flex-wrap items-center gap-2"><TerminalStatusChip state={state} />{loading && <span className="text-xs text-ink-3">lädt…</span>}{error && <span className="inline-flex items-center gap-1 text-xs text-status-alert"><AlertTriangle className="h-3 w-3" />{error}</span>}</div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={() => setView((current) => (current === "flotte" ? "terminal" : "flotte"))}
              aria-pressed={view === "flotte"}
              className={cn(
                "inline-flex min-h-12 items-center gap-1.5 rounded-card border px-3 text-xs font-medium transition",
                view === "flotte" ? "border-live/60 bg-live/15 text-live" : "border-line bg-surface-2 text-ink-2 hover:bg-surface-3",
              )}
            >
              <LayoutGrid className="h-3.5 w-3.5" />
              Flotte
            </button>
            <button
              type="button"
              onClick={() => setCreateSheetOpen(true)}
              className="inline-flex min-h-12 items-center gap-1.5 rounded-card border border-live/50 bg-live/10 px-3 text-xs font-medium text-live hover:bg-live/20"
            >
              <Plus className="h-3.5 w-3.5" />
              Neue Session
            </button>
          </div>
        </div>
      )}

      {!compactLayout && orderedOverview.length > 0 && (
        <section className="hidden rounded-panel border border-line bg-surface-1 p-3 lg:block" aria-label="Terminal-Flotte">
          <div className="mb-2 flex items-baseline justify-between gap-3 border-b border-line-soft pb-2">
            <Eyebrow>Fleet</Eyebrow>
            <span className="font-data text-micro tabular-nums text-ink-3">{orderedOverview.length} Fenster</span>
          </div>
          <div className="grid grid-cols-2 gap-2 xl:grid-cols-4">
            {orderedOverview.map((win) => (
              <FleetStripCard
                key={`${win.session}:${win.window}`}
                win={win}
                now={overviewNow}
                isCurrent={activeTarget?.session === win.session && activeTarget.window === win.window}
                onSelect={() => selectPaneTarget(activePane, targetFromWindow(win))}
              />
            ))}
          </div>
        </section>
      )}

      <div className={cn(
        "relative grid flex-1 gap-2 sm:gap-3 lg:grid-cols-[260px_minmax(0,1fr)]",
        rightRail ? "xl:grid-cols-[260px_minmax(0,1fr)_300px]" : "xl:grid-cols-[260px_minmax(0,1fr)]",
      )}>
        <aside className="hidden min-h-[540px] overflow-y-auto rounded-panel border border-line bg-surface-1 p-3 lg:block">{sessionList}</aside>
        <section
          style={immersive && immersiveHeight ? { height: `${immersiveHeight}px` } : undefined}
          className={cn(
            "relative overflow-hidden border-line bg-surface-1",
            immersive ? "fixed inset-0 z-[45] flex flex-col" : "rounded-panel border md:min-h-[640px] lg:min-h-[540px]",
          )}
        >
          {compactLayout && chipStrip}
          {compactLayout && view === "terminal" && (
            // Mobile toolbar: xterm has no touch selection, so "Auswählen" opens a
            // frozen native-text snapshot; copy stays reachable next to it.
            <div className="flex shrink-0 items-center justify-between gap-2 border-b border-line-soft bg-surface-1 px-2 py-1 text-xs text-ink-2">
              <div className="min-w-0">
                {copyState !== "idle" && (
                  <span role="status" className={cn("shrink-0 text-[10px]", copyState === "copied" ? "text-live" : copyState === "error" ? "text-status-alert" : "text-ink-3")}>
                    {copyState === "copied" ? "Kopiert" : copyState === "empty" ? "Keine Auswahl" : "Kopieren fehlgeschlagen"}
                  </span>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <button
                  type="button"
                  aria-label="Auswahl kopieren"
                  title="Auswahl kopieren"
                  onClick={() => void copySelection()}
                  className="grid h-11 w-11 place-items-center rounded-card border border-line bg-surface-2 text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live"
                >
                  <Copy className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  aria-label="Text auswählen"
                  title="Text auswählen"
                  onClick={openSelectOverlay}
                  className="inline-flex h-11 items-center gap-1 rounded-card border border-line bg-surface-2 px-2.5 text-[11px] font-medium text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live"
                >
                  <TextSelect className="h-3.5 w-3.5" />
                  Auswählen
                </button>
              </div>
            </div>
          )}
          {!compactLayout && view === "terminal" && (
            <div className="flex shrink-0 items-center justify-between gap-3 border-b border-line-soft bg-surface-1 px-3 py-2 text-xs text-ink-2">
              <div className="flex min-w-0 items-center gap-2">
                <Activity className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">{activeTarget ? `${activeTarget.session}:${activeTarget.window}` : "missing window"}</span>
                {copyState !== "idle" && (
                  <span role="status" className={cn("shrink-0 text-[10px]", copyState === "copied" ? "text-live" : copyState === "error" ? "text-status-alert" : "text-ink-3")}>
                    {copyState === "copied" ? "Kopiert" : copyState === "empty" ? "Keine Auswahl" : "Kopieren fehlgeschlagen"}
                  </span>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-1">
                {/* Copy is a UI-only action — it reads the xterm selection and never writes
                    to the socket, so it cannot interrupt the attached agent. */}
                <button
                  type="button"
                  aria-label="Auswahl kopieren"
                  title="Auswahl kopieren (Strg+Umschalt+C)"
                  onClick={() => void copySelection()}
                  className="grid h-12 w-12 place-items-center rounded-card border border-line bg-surface-2 text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live"
                >
                  <Copy className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  aria-label="Text auswählen"
                  title="Text auswählen (Snapshot zum Markieren)"
                  onClick={openSelectOverlay}
                  className="grid h-12 w-12 place-items-center rounded-card border border-line bg-surface-2 text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live"
                >
                  <TextSelect className="h-3.5 w-3.5" />
                </button>
                {([1, 2, 4] as DesktopTerminalLayout[]).map((layout) => (
                  <button
                    key={layout}
                    type="button"
                    data-testid={`terminal-layout-button-${layout}`}
                    aria-label={`${layout} Terminal${layout > 1 ? "s" : ""} anzeigen`}
                    title={`${layout}× Terminal`}
                    onClick={() => chooseDesktopLayout(layout)}
                    className={cn("grid h-12 w-12 place-items-center rounded-card border text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live", desktopLayout === layout ? "border-live/50 bg-live/10 text-live" : "border-line bg-surface-2")}
                  >
                    {layout === 1 ? <TerminalSquare className="h-3.5 w-3.5" /> : layout === 2 ? <Columns2 className="h-3.5 w-3.5" /> : <Grid2X2 className="h-3.5 w-3.5" />}
                  </button>
                ))}
                <button
                  type="button"
                  aria-label="Usage Window umschalten"
                  title="ChatGPT / Claude / Kimi Usage"
                  onClick={() => setRightRail((current) => current === "usage" ? null : "usage")}
                  className={cn("grid h-12 w-12 place-items-center rounded-card border text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live", rightRail === "usage" ? "border-live/50 bg-live/10 text-live" : "border-line bg-surface-2")}
                >
                  <Gauge className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  aria-label="Werkzeuge umschalten"
                  title="Werkzeuge"
                  onClick={() => setRightRail((current) => current === "tools" ? null : "tools")}
                  className={cn("grid h-12 w-12 place-items-center rounded-card border text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live", rightRail === "tools" ? "border-live/50 bg-live/10 text-live" : "border-line bg-surface-2")}
                >
                  <PanelRightOpen className="h-3.5 w-3.5" />
                </button>
                <button type="button" aria-label="Schrift kleiner" title="Schrift kleiner" onClick={() => adjustFont(-1)} className="grid h-12 w-12 place-items-center rounded-card border border-line bg-surface-2 font-mono text-[11px] text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live">A−</button>
                <button type="button" aria-label="Schrift größer" title="Schrift größer" onClick={() => adjustFont(1)} className="grid h-12 w-12 place-items-center rounded-card border border-line bg-surface-2 font-mono text-[11px] text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live">A+</button>
                <button type="button" aria-label={zen ? "Vollbild verlassen" : "Vollbild"} title={zen ? "Vollbild verlassen" : "Vollbild"} onClick={toggleZen} className="grid h-12 w-12 place-items-center rounded-card border border-line bg-surface-2 text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live">
                  {zen ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
                </button>
              </div>
            </div>
          )}
          {compactLayout && error && (
            <div className="flex shrink-0 items-center gap-1.5 border-b border-status-alert/25 bg-status-alert/10 px-3 py-1.5 text-[11px] text-status-alert">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              <span className="min-w-0 truncate">{error}</span>
            </div>
          )}
          {!activeTarget && !loading ? (
            <div className="grid h-[480px] place-items-center p-6 text-center text-sm text-ink-3">Kein tmux-Fenster verfügbar. Neue Session über „+" anlegen.</div>
          ) : (
            <div className={cn("flex w-full flex-col", immersive ? "min-h-0 flex-1" : "h-[calc(100svh-25rem)] min-h-[360px] md:h-[calc(100svh-23rem)] md:min-h-[500px] lg:h-[calc(100vh-17rem)]")}>
              {!compactLayout && <TerminalIdentityBar window={selectedWindow} selectedKind={selectedKind} state={state} />}
              {!compactLayout && selectedOverview && (
                // Ticker line: only the fields we actually have data for. The
                // mockup also shows model/effort, session-token-budget and
                // bypass-permission-mode — none of those are exposed by this
                // view's data (no model/effort/token-budget/permission-mode
                // field on AgentTerminalWindow/-OverviewWindow), so they are
                // deliberately omitted rather than fabricated.
                <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 border-b border-line-soft bg-live/[0.03] px-3 py-1.5 text-[10px] text-ink-3">
                  <span>letztes Event <b className="font-semibold text-ink-2">{formatActivityAge(overviewNow, selectedOverview.activity ?? null)}</b></span>
                </div>
              )}
              {state === "dead pane" && selectedWindow && isManagedWindow(selectedWindow) && (
                <div className="flex shrink-0 items-center justify-between gap-2 border-b border-status-warn/20 bg-status-warn/10 px-3 py-1.5 text-[11px] text-status-warn">
                  <span className="min-w-0 truncate">Prozess beendet — Fenster neu starten?</span>
                  <button type="button" onClick={() => void respawnWindow(selectedWindow)} className="inline-flex shrink-0 items-center gap-1 rounded-card border border-status-warn/40 px-2 py-1 hover:bg-status-warn/15">
                    <RotateCcw className="h-3 w-3" />Neu starten
                  </button>
                </div>
              )}
              {terminalPaneSurface}
              {composer}
              {keysBar}
            </div>
          )}
          {view === "flotte" && (
            // Overlay statt Unmount: das xterm-Host-Div darunter bleibt gemountet
            // (ResizeObserver/WS-Refs bleiben gültig), Flotte deckt es nur visuell/interaktiv ab.
            // Auf compact beginnt das Overlay UNTER dem Chip-Strip (h-11) — sonst deckt es
            // den Flotte/Terminal-Toggle und den Zurück-Button ab (jsdom-Tests sehen das nicht).
            <div className={cn("absolute inset-x-0 bottom-0 z-10 overflow-y-auto bg-surface-0 p-3", compactLayout ? "top-11" : "top-0")}>{fleetPanel}</div>
          )}
        </section>
        <TerminalUsageDock open={!compactLayout && rightRail === "usage"} onClose={() => setRightRail(null)} />
        {!compactLayout && rightRail === "tools" ? (
          <aside className="absolute inset-y-0 right-0 z-30 min-h-[540px] w-[min(300px,calc(100%-1rem))] min-w-0 overflow-hidden rounded-panel border border-line bg-surface-1 p-3 shadow-[-24px_0_55px_rgba(0,0,0,.38)] xl:relative xl:w-auto xl:shadow-none">
            {toolsDrawer}
          </aside>
        ) : null}
      </div>

      {sessionSheet}
      {createSheet}
      {compactLayout && toolsOpen && <div className="fixed inset-x-0 bottom-0 z-50 max-h-[85svh] overflow-auto rounded-t-panel border border-line bg-surface-1 p-4 shadow-2xl"><div className="mx-auto mb-3 h-1 w-12 rounded-full bg-ink-3/20" />{toolsDrawer}</div>}

      {handoffOpen && (
        <TerminalHandoffPanel
          target={target}
          getSelection={readActiveSelection}
          onClose={() => setHandoffOpen(false)}
        />
      )}

      {selectSnapshot !== null && (
        <TerminalSelectOverlay text={selectSnapshot} onClose={closeSelectOverlay} />
      )}
    </div>
  );
}
