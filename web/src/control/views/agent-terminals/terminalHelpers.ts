import { useEffect, useState } from "react";
import type {
  AgentTerminalKind,
  AgentTerminalOverviewWindow,
  AgentTerminalWindow,
  AgentTerminalWorkdirOption,
  ControlOverviewDecisionQueueResponse,
  ControlOverviewHealthResponse,
  ControlOverviewKanbanBoardResponse,
  ControlOverviewKanbanTask,
  ControlOverviewVaultResponse,
  SkillInfo,
  ToolsetInfo,
} from "@/lib/api";

export const AGENTS: Array<{ kind: AgentTerminalKind; label: string; hint: string }> = [
  { kind: "hermes", label: "Hermes", hint: "hermes --tui" },
  { kind: "claude", label: "Claude", hint: "claude-cli" },
  { kind: "codex", label: "Codex", hint: "codex-cli" },
  { kind: "kimi", label: "Kimi", hint: "kimi-cli" },
  { kind: "grok", label: "Grok", hint: "grok-build / Grok 4.5" },
  { kind: "qwen", label: "Qwen", hint: "Qwen Code / qwen3.8-max-preview" },
];

export const AGENT_LABELS: Record<AgentTerminalKind, string> = Object.fromEntries(AGENTS.map((agent) => [agent.kind, agent.label])) as Record<AgentTerminalKind, string>;
export const AGENT_IDENTITY_DOT_CLASS: Record<AgentTerminalKind, string> = {
  hermes: "bg-data-2",
  claude: "bg-data-1",
  codex: "bg-data-4",
  kimi: "bg-data-5",
  grok: "bg-data-3",
  qwen: "bg-data-6",
};
const AGENT_KINDS = new Set<AgentTerminalKind>(AGENTS.map((agent) => agent.kind));

const TMUX_PREFIX = "\x02";
export const TMUX_COPY_MODE = `${TMUX_PREFIX}[`;
export const TMUX_PAGE_UP = `${TMUX_PREFIX}\x1b[5~`;
export const TMUX_LINE_STEP = 5;

const WORKDIR_STORAGE_KEY = "hermes-terminals-workdir";
/** Per-kind key; legacy global WORKDIR_STORAGE_KEY is still read once for migration. */
export function workdirStorageKeyForKind(kind: AgentTerminalKind): string {
  return `${WORKDIR_STORAGE_KEY}:${kind}`;
}
export const WORKDIR_RESET_NOTE =
  "Gespeichertes Arbeitsverzeichnis nicht verfügbar — auf Zuhause zurückgesetzt.";
export const FONT_STORAGE_KEY = "hermes-terminals-fontsize";
export const KEYS_STORAGE_KEY = "hermes-terminals-keysopen";
export const TARGET_STORAGE_KEY = "hermes-terminals-last-target";
export const LAYOUT_STORAGE_KEY = "hermes.control.agent-terminals.desktop-layout.v1";
export const PANE_TARGETS_STORAGE_KEY = "hermes.control.agent-terminals.pane-targets.v1";
export const LASTSEEN_STORAGE_KEY = "hermes-terminals-lastseen";
export const FONT_MIN = 8;
export const FONT_MAX = 20;
const PRIMARY_SESSION = "work";
export const WINDOW_INVENTORY_POLL_MS = 10000;
/** An armed close disarms itself, so a row armed and forgotten cannot be killed later by a stray click. */
export const TERMINATE_ARM_TIMEOUT_MS = 8000;
export const COPY_STATUS_TIMEOUT_MS = 2000;

export type TerminalCopyState = "idle" | "copied" | "empty" | "error";
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000];
export const OVERVIEW_POLL_MS = 5000;
// Debounce window for RESIZE escape sends (fit() stays immediate).  Mobile keyboards
// fire dozens of visualViewport events during the slide animation; 300 ms trailing
// debounce collapses the storm to one send after the keyboard settles.
export const RESIZE_SEND_DEBOUNCE_MS = 300;

const FLEET_STATE_PRIORITY: Record<AgentTerminalOverviewWindow["state"], number> = {
  frage: 0,
  laeuft: 1,
  wartet: 2,
  idle: 3,
  dead: 4,
};

// Fallback, falls capabilities noch nicht geladen sind — Wahrheit kommt vom Backend.
export const FALLBACK_WORKDIRS: AgentTerminalWorkdirOption[] = [
  { key: "home", label: "Zuhause (~)", path: "~", group: "standard" },
  { key: "hermes-agent", label: "Hermes-Agent", path: "~/.hermes/hermes-agent", group: "standard" },
  { key: "family-organizer", label: "Family Organizer", path: "~/projects/family-organizer", group: "standard" },
  { key: "orchestration", label: "Orchestrierung", path: "~/orchestration", group: "standard" },
];

export const QUICK_KEYS: Array<{ label: string; sequence: string }> = [
  { label: "Esc", sequence: "\x1b" },
  { label: "Tab", sequence: "\t" },
  { label: "⇧Tab", sequence: "\x1b[Z" },
  { label: "^C", sequence: "\x03" },
  { label: "⏎", sequence: "\r" },
];

export const CONTROL_CAPABILITIES: Array<{ label: string; patterns: string[]; command: string }> = [
  { label: "Firecrawl", patterns: ["firecrawl"], command: "/firecrawl-search" },
  { label: "Gmail", patterns: ["gmail"], command: "/gmail" },
  { label: "Calendar", patterns: ["calendar", "google-calendar"], command: "/google-calendar" },
  { label: "Kanban", patterns: ["kanban"], command: "/kanban list" },
  { label: "Browser", patterns: ["browser", "browser_"], command: "/browser status" },
];

export interface ReadOnlyControlContext {
  skills: SkillInfo[];
  toolsets: ToolsetInfo[];
  health: ControlOverviewHealthResponse | null;
  vault: ControlOverviewVaultResponse | null;
  board: ControlOverviewKanbanBoardResponse | null;
  decisions: ControlOverviewDecisionQueueResponse | null;
  error: string | null;
}

export const EMPTY_CONTROL_CONTEXT: ReadOnlyControlContext = {
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

export function buildComposerPayload(text: string, submit: boolean): string | null {
  if (!text) return null;
  // Mehrzeiliges als Bracketed Paste, damit CLIs (claude/codex) es als EINE
  // Eingabe nehmen statt jede Zeile einzeln zu submitten.
  // Eingebettete End-Sequenzen entfernen — sonst schließt der Text selbst den Paste-Modus
  // vorzeitig und der Rest würde als Live-Keystrokes (inkl. \r) ausgeführt.
  const body = text.includes("\n") ? `\x1b[200~${text.split("\x1b[201~").join("")}\x1b[201~` : text;
  return submit ? `${body}\r` : body;
}

export function isDeadWindow(window: AgentTerminalWindow): boolean {
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

export function targetFromWindow(window: AgentTerminalWindow): { session: string; window: string } {
  return { session: window.session, window: window.window };
}

export function orderWindowsForStrip(windows: AgentTerminalWindow[]): AgentTerminalWindow[] {
  const primary = windows.filter((w) => w.session === PRIMARY_SESSION);
  const rest = windows.filter((w) => w.session !== PRIMARY_SESSION);
  return [...primary, ...rest];
}

export function chipLabel(window: AgentTerminalWindow): string {
  return window.session === PRIMARY_SESSION ? window.window : `${window.session}:${window.window}`;
}

/**
 * Short display form for a pane cwd: $HOME → ~, then at most the last two path segments.
 * Backend may omit cwd (optional on AgentTerminalWindow); callers pass null/undefined safely.
 */
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
export function formatPtyResize(cols: number, rows: number): string {
  const c = Math.max(2, Number.isFinite(cols) ? Math.floor(cols) : 0);
  const r = Math.max(2, Number.isFinite(rows) ? Math.floor(rows) : 0);
  return `\x1b[RESIZE:${c};${r}]`;
}

export function hasUnseenActivity(window: AgentTerminalWindow, lastSeen: Record<string, number>): boolean {
  if (window.activity == null) return false;
  const key = `${window.session}:${window.window}`;
  return window.activity > lastSeen[key];
}

export function formatActivityAge(now: number, activity: number | null): string {
  if (activity == null) return "—";
  const deltaSeconds = Math.max(0, Math.round(now - activity));
  if (deltaSeconds < 60) return `vor ${deltaSeconds}s`;
  const minutes = Math.floor(deltaSeconds / 60);
  if (minutes < 60) return `vor ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `vor ${hours}h`;
}

export function orderOverviewForFleet(entries: AgentTerminalOverviewWindow[]): AgentTerminalOverviewWindow[] {
  return [...entries].sort((a, b) => FLEET_STATE_PRIORITY[a.state] - FLEET_STATE_PRIORITY[b.state]);
}

export function kindFromWindow(window: AgentTerminalWindow | null, fallback: AgentTerminalKind): AgentTerminalKind {
  // Suffix-Fenster (claude-fo, hermes-agent, …) gehören zum Basis-Kind.
  const base = (window?.window ?? "").split("-")[0] as AgentTerminalKind;
  return AGENT_KINDS.has(base) ? base : fallback;
}

export function terminalProcessLabel(window: AgentTerminalWindow | null, kind: AgentTerminalKind): string {
  const command = (window?.command ?? "").trim();
  if (!window?.pid || window.dead) return "dead pane";
  if (!command) return kind;
  return command.toLowerCase().includes(kind) ? command : `${command}/${kind}`;
}

function matchesCapabilityText(value: string, patterns: string[]): boolean {
  const lower = value.toLowerCase();
  return patterns.some((pattern) => lower.includes(pattern));
}

export function capabilityState(
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

export function activeBoardTasks(board: ControlOverviewKanbanBoardResponse | null): ControlOverviewKanbanTask[] {
  return allBoardTasks(board).filter((task) => ["running", "review", "ready", "todo", "triage"].includes(task.status));
}

export function blockedBoardTasks(board: ControlOverviewKanbanBoardResponse | null): ControlOverviewKanbanTask[] {
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

export function useIsMobile(): boolean {
  return useMediaQuery("(max-width: 767px)");
}

export function useIsCompactTerminalLayout(): boolean {
  return useMediaQuery("(max-width: 1023px)");
}
