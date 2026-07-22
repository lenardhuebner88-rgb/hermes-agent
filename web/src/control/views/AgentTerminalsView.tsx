import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
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
  Link2,
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
  type AgentTerminalCapabilityState,
  type AgentTerminalContextProfile,
  type AgentTerminalExecutionProfile,
  type AgentTerminalKind,
  type AgentTerminalOverviewState,
  type AgentTerminalOverviewWindow,
  type AgentTerminalRespawnAction,
  type AgentTerminalStartMode,
  type AgentTerminalWindow,
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
import { TerminalHandoffPanel } from "./TerminalHandoffPanel";
import { TerminalPane, type TerminalPaneConnectionState, type TerminalPaneHandle } from "./agent-terminals/TerminalPane";
import {
  extractTerminalBufferText,
  TerminalSelectOverlay,
} from "./agent-terminals/TerminalSelectOverlay";
import { TerminalUsageDock } from "./agent-terminals/TerminalUsageDock";
import { AnswerSheet } from "./agent-terminals/AnswerSheet";
import { QuestionPill } from "./agent-terminals/QuestionPill";
import { useAgentQuestions } from "../hooks/agentQuestions";
import {
  normalizeDesktopLayout,
  resolvePaneTargets,
  targetKey as paneTargetKey,
  type DesktopTerminalLayout,
  type TerminalTarget as PaneTarget,
} from "./agent-terminals/layout";
import {
  AGENTS,
  AGENT_IDENTITY_DOT_CLASS,
  AGENT_LABELS,
  CONTROL_CAPABILITIES,
  COPY_STATUS_TIMEOUT_MS,
  EMPTY_CONTROL_CONTEXT,
  FALLBACK_WORKDIRS,
  FONT_MAX,
  FONT_MIN,
  FONT_STORAGE_KEY,
  KEYS_STORAGE_KEY,
  LASTSEEN_STORAGE_KEY,
  LAYOUT_STORAGE_KEY,
  OVERVIEW_POLL_MS,
  PANE_TARGETS_STORAGE_KEY,
  QUICK_KEYS,
  RESIZE_SEND_DEBOUNCE_MS,
  TARGET_STORAGE_KEY,
  TERMINATE_ARM_TIMEOUT_MS,
  TMUX_COPY_MODE,
  TMUX_LINE_STEP,
  TMUX_PAGE_UP,
  WINDOW_INVENTORY_POLL_MS,
  WORKDIR_RESET_NOTE,
  activeBoardTasks,
  blockedBoardTasks,
  buildComposerPayload,
  capabilityState,
  chipLabel,
  classifyTerminalState,
  formatActivityAge,
  formatCwdShort,
  formatPtyResize,
  hasUnseenActivity,
  isDeadWindow,
  isManagedWindow,
  isTerminalCopyShortcut,
  kindFromWindow,
  orderOverviewForFleet,
  orderWindowsForStrip,
  pickInitialTarget,
  readStoredWorkdir,
  reconnectDelayMs,
  targetFromWindow,
  terminalProcessLabel,
  terminalSurfaceOrder,
  useIsCompactTerminalLayout,
  useIsMobile,
  workdirStorageKeyForKind,
  type ReadOnlyControlContext,
  type TerminalCopyState,
} from "./agent-terminals/terminalHelpers";
import {
  CapabilityPill,
  FleetCard,
  FleetStripCard,
  StatTile,
  STRIP_STATE_META,
  TerminalControlButton,
  TerminalIdentityBar,
  TerminalStatusChip,
} from "./agent-terminals/FleetCard";

// Re-export pure helpers so AgentTerminalsView.test.ts keeps importing from this module.
export {
  buildComposerPayload,
  chipLabel,
  classifyTerminalState,
  formatActivityAge,
  formatCwdShort,
  formatPtyResize,
  hasUnseenActivity,
  isManagedWindow,
  isTerminalCopyShortcut,
  orderOverviewForFleet,
  orderWindowsForStrip,
  pickInitialTarget,
  readStoredWorkdir,
  reconnectDelayMs,
  terminalSurfaceOrder,
} from "./agent-terminals/terminalHelpers";
export type { TerminalUiState } from "./agent-terminals/terminalHelpers";

export function pickDeepLinkedTarget(
  windows: AgentTerminalWindow[],
  session: string,
  window?: string | null,
): { session: string; window: string } | null {
  // Exaktes Fenster zuerst; kennt die Inventur das Fenster nicht (stale Link,
  // Index-statt-Name aus älteren Backends), fällt der Link auf die Session
  // zurück statt auf die globale Default-Auswahl (ui-verify 2026-07-18).
  const match =
    (window
      ? windows.find((candidate) => candidate.session === session && candidate.window === window)
      : undefined) ?? windows.find((candidate) => candidate.session === session);
  return match ? targetFromWindow(match) : null;
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
  /** Skip setWindows when inventory payload is deep-equal (pollingStore payloadJson pattern). */
  const windowsJsonRef = useRef<string>("");
  /** Skip setOverview when fleet overview payload is deep-equal. */
  const overviewJsonRef = useRef<string>("");
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
  const [createStartMode, setCreateStartMode] = useState<AgentTerminalStartMode>("free");
  const [createContextProfile, setCreateContextProfile] = useState<AgentTerminalContextProfile>("full");
  // Default respawn action stays server-closed ("fresh"); UI selection is not
  // wired yet, so keep a constant instead of an unused React setter.
  const respawnAction: AgentTerminalRespawnAction = "fresh";
  const [createError, setCreateError] = useState<string | null>(null);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [attachNonce, setAttachNonce] = useState(0);
  const [handoffOpen, setHandoffOpen] = useState(false);
  const [executionCapsuleOpen, setExecutionCapsuleOpen] = useState(false);
  const [executionCapsuleTaskId, setExecutionCapsuleTaskId] = useState("");
  const [executionCapsuleRunId, setExecutionCapsuleRunId] = useState("");
  const [executionCapsuleProfile, setExecutionCapsuleProfile] =
    useState<AgentTerminalExecutionProfile>("implementation");
  const [executionCapsuleSummary, setExecutionCapsuleSummary] = useState("");
  const [executionCapsuleDecisions, setExecutionCapsuleDecisions] = useState("");
  const [executionCapsuleNextSteps, setExecutionCapsuleNextSteps] = useState("");
  const [executionCapsuleRisks, setExecutionCapsuleRisks] = useState("");
  const [executionCapsuleBusy, setExecutionCapsuleBusy] = useState(false);
  const [executionCapsuleError, setExecutionCapsuleError] = useState<string | null>(null);
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
  const agentQuestions = useAgentQuestions();
  const [answerSheetOpen, setAnswerSheetOpen] = useState(false);
  const [answerFocusId, setAnswerFocusId] = useState<number | null>(null);
  const [answerClosedHint, setAnswerClosedHint] = useState<string | null>(null);
  const openQuestions = agentQuestions.data?.questions ?? [];
  /** Oldest open question ts for store-pill live age (I3). */
  const oldestOpenTs = useMemo(() => {
    if (openQuestions.length === 0) return null;
    let best: string | null = null;
    let bestMs = Infinity;
    for (const q of openQuestions) {
      const ms = Date.parse(q.ts);
      if (!Number.isFinite(ms)) continue;
      if (ms < bestMs) {
        bestMs = ms;
        best = q.ts;
      }
    }
    return best ?? openQuestions[openQuestions.length - 1]?.ts ?? null;
  }, [openQuestions]);
  const [searchParams, setSearchParams] = useSearchParams();
  const deepLinkConsumedRef = useRef(false);
  // Deep-link ?question=<id> → open AnswerSheet focused on that event (I3).
  useEffect(() => {
    if (deepLinkConsumedRef.current) return;
    const raw = searchParams.get("question");
    if (raw == null || raw === "") return;
    // Wait for first poll result so we know open vs closed.
    if (agentQuestions.loading && agentQuestions.data == null) return;
    deepLinkConsumedRef.current = true;
    const id = Number(raw);
    const next = new URLSearchParams(searchParams);
    next.delete("question");
    setSearchParams(next, { replace: true });
    if (!Number.isFinite(id) || id <= 0) return;
    const found = openQuestions.some((q) => q.id === id);
    setAnswerFocusId(id);
    setAnswerSheetOpen(true);
    // Only claim "closed" when the questions fetch actually succeeded — after
    // a failed first poll (data == null) the question may well still be open
    // (Kimi review m7).
    setAnswerClosedHint(
      found || agentQuestions.data == null ? null : "bereits beantwortet/abgelaufen",
    );
  }, [
    searchParams,
    setSearchParams,
    agentQuestions.loading,
    agentQuestions.data,
    openQuestions,
  ]);
  // A question deep-link wins when both contracts are present; the effect above consumes it first.
  useEffect(() => {
    if (deepLinkConsumedRef.current) return;
    const session = searchParams.get("session");
    if (session == null || session === "") return;
    if (searchParams.get("question")) return;
    if (loading) return;
    deepLinkConsumedRef.current = true;
    const window = searchParams.get("window");
    const next = new URLSearchParams(searchParams);
    next.delete("session");
    next.delete("window");
    setSearchParams(next, { replace: true });
    const deepLinkedTarget = pickDeepLinkedTarget(windows, session, window);
    if (deepLinkedTarget) setTarget(deepLinkedTarget);
  }, [loading, searchParams, setSearchParams, windows]);
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
        // Keep windowsJsonRef in sync so the background inventory poll can skip no-ops.
        windowsJsonRef.current = JSON.stringify(win.windows);
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
      const nextJson = JSON.stringify(win.windows);
      // Same payloadJson-style guard as fetchOverview — avoid fleet/session
      // re-renders when the tmux inventory poll returns an identical list.
      if (nextJson !== windowsJsonRef.current) {
        windowsJsonRef.current = nextJson;
        setWindows(win.windows);
        setTarget((previous) => pickInitialTarget(win.windows, selectedKind, previous));
      }
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

  const readOnlyContextActive = rightRail === "tools" || composerText.length > 0;

  useEffect(() => {
    if (!readOnlyContextActive) return;
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
  }, [readOnlyContextActive, refreshReadOnlyContext]);

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
      const response = await api.createAgentTerminalWindow(createKind, workdir, {
        start_mode: createStartMode,
        context_profile: createContextProfile,
      });
      setSelectedKind(createKind);
      selectPaneTarget(activePane, targetFromWindow(response.window));
      await refresh();
      setCreateSheetOpen(false);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreateBusy(false);
    }
  }, [activePane, createContextProfile, createKind, createStartMode, refresh, selectPaneTarget, workdir]);

  const respawnWindow = useCallback(
    async (win: { session: string; window: string }, action: AgentTerminalRespawnAction = respawnAction) => {
      setError(null);
      try {
        const response = await api.respawnAgentTerminalWindow(win.session, win.window, action);
        selectPaneTarget(activePane, targetFromWindow(response.window));
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [activePane, refresh, respawnAction, selectPaneTarget],
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
  // Managed windows keep the two-arg call (external defaults false). Foreign
  // (managed===false) sends external:true so the backend skips identity guards.
  const confirmTerminate = useCallback(
    async (win: AgentTerminalWindow) => {
      actionErrorRef.current = null;
      setError(null);
      setTerminateBusy(true);
      try {
        const external = !isManagedWindow(win);
        if (external) {
          await api.terminateAgentTerminalWindow(win.session, win.window, true);
        } else {
          await api.terminateAgentTerminalWindow(win.session, win.window);
        }
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

  const openExecutionCapsule = useCallback(() => {
    if (!selectedWindow) return;
    setExecutionCapsuleTaskId(selectedWindow.task_id ?? "");
    setExecutionCapsuleRunId(selectedWindow.run_id ? String(selectedWindow.run_id) : "");
    setExecutionCapsuleProfile("implementation");
    setExecutionCapsuleSummary("");
    setExecutionCapsuleDecisions("");
    setExecutionCapsuleNextSteps("");
    setExecutionCapsuleRisks("");
    setExecutionCapsuleError(null);
    setExecutionCapsuleOpen(true);
    setSessionSheetOpen(false);
  }, [selectedWindow]);

  const bindExecutionCapsule = useCallback(async () => {
    if (!selectedWindow || selectedWindow.correlation_id) return;
    const taskId = executionCapsuleTaskId.trim();
    const runId = Number(executionCapsuleRunId);
    const summary = executionCapsuleSummary.trim();
    const lines = (value: string) =>
      value
        .split(/\r?\n/u)
        .map((item) => item.trim())
        .filter(Boolean);
    const decisions = lines(executionCapsuleDecisions);
    const nextSteps = lines(executionCapsuleNextSteps);
    const risks = lines(executionCapsuleRisks);
    if (!taskId || /\s/u.test(taskId)) {
      setExecutionCapsuleError("Bitte eine gültige Task-ID ohne Leerzeichen angeben.");
      return;
    }
    if (!Number.isSafeInteger(runId) || runId <= 0) {
      setExecutionCapsuleError("Bitte eine gültige positive Run-ID angeben.");
      return;
    }
    if (!summary || summary.length > 1200) {
      setExecutionCapsuleError("Der Kurz-Handoff muss 1–1200 Zeichen lang sein.");
      return;
    }
    if ([decisions, nextSteps, risks].some((items) => items.length > 8)) {
      setExecutionCapsuleError("Je Liste sind höchstens acht nichtleere Zeilen erlaubt.");
      return;
    }
    if ([decisions, nextSteps, risks].some((items) => items.some((item) => item.length > 240))) {
      setExecutionCapsuleError("Eine Listenzeile darf höchstens 240 Zeichen enthalten.");
      return;
    }

    setExecutionCapsuleBusy(true);
    setExecutionCapsuleError(null);
    try {
      const response = await api.bindAgentTerminalExecutionCapsule(
        selectedWindow.session,
        selectedWindow.window,
        taskId,
        runId,
        {
          profile: executionCapsuleProfile,
          summary,
          decisions,
          next_steps: nextSteps,
          risks,
        },
      );
      setWindows((current) => {
        const next = current.map((candidate) =>
          candidate.session === response.window.session &&
          candidate.window === response.window.window
            ? response.window
            : candidate,
        );
        windowsJsonRef.current = JSON.stringify(next);
        return next;
      });
      setExecutionCapsuleOpen(false);
    } catch (err) {
      setExecutionCapsuleError(err instanceof Error ? err.message : String(err));
    } finally {
      setExecutionCapsuleBusy(false);
    }
  }, [
    executionCapsuleDecisions,
    executionCapsuleNextSteps,
    executionCapsuleProfile,
    executionCapsuleRisks,
    executionCapsuleRunId,
    executionCapsuleSummary,
    executionCapsuleTaskId,
    selectedWindow,
  ]);

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
    // "lädt…" nur beim Erstladen zeigen: der 5s-Hintergrund-Refresh würde die
    // Kopfzeile sonst im Sekundentakt flackern lassen (2 Renders pro Tick).
    if (overviewJsonRef.current === "") setOverviewLoading(true);
    try {
      const response = await api.getAgentTerminalOverview();
      // Skip setOverview when payload is unchanged (pollingStore payloadJson pattern).
      // Fresh array refs every 5s were invalidating orderedOverview and re-rendering the fleet strip.
      const nextJson = JSON.stringify(response.windows);
      if (nextJson !== overviewJsonRef.current) {
        overviewJsonRef.current = nextJson;
        setOverview(response.windows);
      }
      setOverviewNow(response.now);
      setOverviewError(null);
    } catch (err) {
      setOverviewError(err instanceof Error ? err.message : String(err));
    } finally {
      setOverviewLoading(false);
    }
  }, []);

  // Fleet-Polling: desktop always (persistent fleet strip). On compactLayout
  // also while view === "terminal" so the mobile chip strip can show per-window
  // overview state (frage/läuft/…) without opening Flotte first. Same
  // visibility-aware timer pattern as refreshReadOnlyContext above.
  useEffect(() => {
    if (compactLayout && view !== "flotte" && view !== "terminal") return;
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
                              title="Externes Fenster — gehört einem anderen Agenten/Prozess"
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
                    {/* Live close: managed keeps the original two-step labels; extern gets a
                        sharper confirm (external:true) and stronger status-alert chrome. */}
                    {!dead && (pendingTerminate === `${win.session}:${win.window}` ? (
                      <>
                        <button
                          type="button"
                          aria-label={managed ? `Beenden bestätigen ${win.session}:${win.window}` : `Externes Fenster wirklich beenden? Gehört einem anderen Agenten/Prozess. ${win.session}:${win.window}`}
                          title={managed ? "Wirklich beenden — die laufende Agent-Arbeit geht verloren" : "Externes Fenster wirklich beenden? Gehört einem anderen Agenten/Prozess."}
                          disabled={terminateBusy}
                          onClick={() => void confirmTerminate(win)}
                          className={cn(
                            "grid w-8 shrink-0 place-items-center border-l border-line-soft disabled:cursor-not-allowed disabled:opacity-40",
                            managed
                              ? "bg-status-alert/20 text-status-alert hover:bg-status-alert/30"
                              : "bg-status-alert/30 text-status-alert ring-1 ring-inset ring-status-alert/50 hover:bg-status-alert/40",
                          )}
                        >
                          <Check className="h-3.5 w-3.5" />
                        </button>
                        <button type="button" aria-label={`Beenden abbrechen ${win.session}:${win.window}`} title="Abbrechen" disabled={terminateBusy} onClick={cancelTerminate} className="grid w-8 shrink-0 place-items-center border-l border-line-soft text-ink-3 hover:bg-surface-3 hover:text-ink-2 disabled:cursor-not-allowed disabled:opacity-40">
                          <X className="h-3.5 w-3.5" />
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        aria-label={managed ? `Session beenden ${win.session}:${win.window}` : `Externes Fenster beenden ${win.session}:${win.window}`}
                        title={managed ? "Laufende Session beenden" : "Externes Fenster beenden — gehört einem anderen Agenten/Prozess"}
                        onClick={() => requestTerminate(win)}
                        className={cn(
                          "grid w-8 shrink-0 place-items-center border-l border-line-soft",
                          managed
                            ? "text-status-alert/70 hover:bg-status-alert/10 hover:text-status-alert"
                            : "bg-status-alert/10 text-status-alert hover:bg-status-alert/20",
                        )}
                      >
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
    <div className="flex min-h-[44px] shrink-0 items-stretch border-b border-line-soft bg-surface-1">
      <button
        type="button"
        aria-label={view === "flotte" ? "Terminal-Ansicht" : "Flotten-Übersicht"}
        onClick={() => setView((current) => (current === "flotte" ? "terminal" : "flotte"))}
        className="grid min-h-[44px] min-w-[44px] shrink-0 place-items-center border-r border-line-soft px-3 text-ink-2 hover:bg-surface-3"
      >
        {view === "flotte" ? <TerminalSquare className="h-4 w-4" /> : <LayoutGrid className="h-4 w-4" />}
      </button>
      <button
        type="button"
        aria-label="Zurück zum Dashboard"
        onClick={() => navigate("/control")}
        className="grid min-h-[44px] min-w-[44px] shrink-0 place-items-center border-r border-line-soft px-3 text-ink-2 hover:bg-surface-3"
      >
        <ChevronLeft className="h-4 w-4" />
      </button>
      <div className="flex min-w-0 flex-1 items-stretch gap-1.5 overflow-x-auto px-1.5 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        <QuestionPill
          count={openQuestions.length}
          standingSinceTs={oldestOpenTs}
          onClick={() => {
            setAnswerFocusId(null);
            setAnswerClosedHint(null);
            setAnswerSheetOpen(true);
          }}
          className="min-h-[44px] text-micro"
        />
        {orderedWindows.map((win) => {
          const active = activeTarget?.session === win.session && activeTarget.window === win.window;
          const dead = isDeadWindow(win);
          const key = `${win.session}:${win.window}`;
          const unseen = !active && hasUnseenActivity(win, lastSeen);
          const chipOverview = overviewByKey.get(key);
          const chipState: AgentTerminalOverviewState = chipOverview?.state ?? (dead ? "dead" : "idle");
          const chipMeta = STRIP_STATE_META[chipState] ?? STRIP_STATE_META.idle;
          const chipName = `${chipLabel(win)} — ${chipMeta.label}`;
          // Operator directive 2026-07-16: chips look like before (green alive,
          // red dead) — ONLY the attention case (frage) gets a distinct color.
          const dotToneClass =
            chipState === "frage"
              ? "bg-status-warn"
              : dead
                ? "bg-status-alert"
                : "bg-status-ok";
          return (
            <button
              key={key}
              ref={(el) => {
                chipRefs.current[key] = el;
              }}
              type="button"
              aria-label={chipName}
              title={chipName}
              onClick={() => (active ? setSessionSheetOpen(true) : selectPaneTarget(activePane, targetFromWindow(win)))}
              className={cn(
                "inline-flex min-h-[44px] shrink-0 items-center gap-1.5 rounded-full border px-3 text-micro font-medium transition",
                active ? "border-live/60 bg-live/10 text-live" : "border-line bg-surface-2 text-ink-2",
              )}
            >
              <span className="relative grid h-2 w-2 shrink-0 place-items-center" aria-hidden>
                {chipState === "frage" && (
                  <span className="absolute h-2 w-2 animate-ping rounded-full bg-status-warn/50" />
                )}
                <span className={cn("h-2 w-2 rounded-full", dotToneClass)} />
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
        className="grid min-h-[44px] min-w-[44px] shrink-0 place-items-center border-l border-line-soft px-3 text-live hover:bg-live/10"
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
                title="Externes Fenster — gehört einem anderen Agenten/Prozess"
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
        {selectedWindow.task_id && selectedWindow.run_id && (
          <div
            data-testid="mobile-execution-capsule-binding"
            className="flex items-center justify-between gap-2"
          >
            <span>Kanban-Run</span>
            <span className="min-w-0 truncate font-mono text-live">
              {selectedWindow.task_id} · #{selectedWindow.run_id}
            </span>
          </div>
        )}
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 text-sec">
        <button type="button" onClick={() => { if (activePane > 0) extraPaneRefs[activePane - 1]?.current?.reconnect(); else setAttachNonce((n) => n + 1); }} className="flex min-h-[44px] flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
          <PlugZap className="h-4 w-4" /><span>Neu verbinden</span>
        </button>
        <button type="button" disabled={!activeSocketReady} onClick={() => sendKey("\x03")} className="flex min-h-[44px] flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3 disabled:cursor-not-allowed disabled:opacity-35">
          <span className="font-data text-sec">^C</span><span>^C senden</span>
        </button>
        {sessionSheetDead && sessionSheetManaged && (
          <button type="button" onClick={() => { void respawnWindow(selectedWindow); setSessionSheetOpen(false); }} className="flex min-h-[44px] flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
            <RotateCcw className="h-4 w-4" /><span>Neu starten</span>
          </button>
        )}
        {sessionSheetDead && (
          <button type="button" onClick={() => { void killWindow(selectedWindow); setSessionSheetOpen(false); }} className="flex min-h-[44px] flex-col items-center gap-1 rounded-card border border-status-alert/25 px-2 py-2.5 text-center leading-tight text-status-alert hover:bg-status-alert/10">
            <Trash2 className="h-4 w-4" /><span>Fenster entfernen</span>
          </button>
        )}
        {/* Same two-step guard as the desktop rail: the first tap arms, the second kills.
            The sheet stays open in between so the armed state is visible where it was armed.
            Extern (managed===false) uses sharper German confirm labels + external:true. */}
        {!sessionSheetDead && (pendingTerminate === `${selectedWindow.session}:${selectedWindow.window}` ? (
          <button
            type="button"
            aria-label={sessionSheetManaged ? `Beenden bestätigen ${selectedWindow.session}:${selectedWindow.window}` : `Externes Fenster wirklich beenden? Gehört einem anderen Agenten/Prozess. ${selectedWindow.session}:${selectedWindow.window}`}
            title={sessionSheetManaged ? undefined : "Externes Fenster wirklich beenden? Gehört einem anderen Agenten/Prozess."}
            disabled={terminateBusy}
            onClick={() => { void confirmTerminate(selectedWindow).then(() => setSessionSheetOpen(false)); }}
            className={cn(
              "flex min-h-[44px] flex-col items-center gap-1 rounded-card px-2 py-2.5 text-center leading-tight text-status-alert disabled:cursor-not-allowed disabled:opacity-40",
              sessionSheetManaged
                ? "border border-status-alert/50 bg-status-alert/15 hover:bg-status-alert/25"
                : "border-2 border-status-alert/70 bg-status-alert/25 hover:bg-status-alert/35",
            )}
          >
            <Check className="h-4 w-4" /><span>{sessionSheetManaged ? "Wirklich beenden" : "Extern wirklich?"}</span>
          </button>
        ) : (
          <button
            type="button"
            aria-label={sessionSheetManaged ? `Session beenden ${selectedWindow.session}:${selectedWindow.window}` : `Externes Fenster beenden ${selectedWindow.session}:${selectedWindow.window}`}
            title={sessionSheetManaged ? undefined : "Externes Fenster beenden — gehört einem anderen Agenten/Prozess"}
            onClick={() => requestTerminate(selectedWindow)}
            className={cn(
              "flex min-h-[44px] flex-col items-center gap-1 rounded-card px-2 py-2.5 text-center leading-tight text-status-alert",
              sessionSheetManaged
                ? "border border-status-alert/25 hover:bg-status-alert/10"
                : "border-2 border-status-alert/50 bg-status-alert/10 hover:bg-status-alert/20",
            )}
          >
            <Trash2 className="h-4 w-4" /><span>{sessionSheetManaged ? "Session beenden" : "Extern beenden"}</span>
          </button>
        ))}
        <button type="button" onClick={() => { setHandoffOpen(true); setSessionSheetOpen(false); }} className="flex min-h-[44px] flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
          <Share2 className="h-4 w-4" /><span>Handoff öffnen</span>
        </button>
        {!sessionSheetDead && (
          <button type="button" onClick={openExecutionCapsule} className="flex min-h-[44px] flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:border-live/40 hover:text-live">
            <Link2 className="h-4 w-4" />
            <span>{selectedWindow.correlation_id ? "Kanban-Run anzeigen" : "Kanban-Run verknüpfen"}</span>
          </button>
        )}
        <button type="button" onClick={() => adjustFont(-1)} className="flex min-h-[44px] flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
          <span className="font-data text-sec">A−</span><span>Schrift kleiner</span>
        </button>
        <button type="button" onClick={() => adjustFont(1)} className="flex min-h-[44px] flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
          <span className="font-data text-sec">A+</span><span>Schrift größer</span>
        </button>
        <button type="button" onClick={() => { setToolsOpen(true); setSessionSheetOpen(false); }} className="flex min-h-[44px] flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
          <Wrench className="h-4 w-4" /><span>Tools / Tageslage</span>
        </button>
        <button type="button" onClick={() => void refresh()} className="flex min-h-[44px] flex-col items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-2.5 text-center leading-tight text-ink-2 hover:bg-surface-3">
          <RefreshCw className="h-4 w-4" /><span>Liste aktualisieren</span>
        </button>
      </div>
    </div>
  );

  const createWorkdirOptions = capability?.workdirs?.length ? capability.workdirs : FALLBACK_WORKDIRS;
  const selectedCreateWorkdir = createWorkdirOptions.find((option) => option.key === workdir) ?? null;
  const createAgentCapability = capability?.agents?.[createKind];
  const createActions = createAgentCapability?.actions;
  const leanEnabled = Boolean(createActions?.lean);
  const leanPrerequisite = (createAgentCapability?.prerequisites ?? []).find((item) =>
    (item.blocks ?? []).includes("lean"),
  );

  const createSessionForm = (
    <div className="grid gap-3">
      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-6">
        {AGENTS.map((agent) => (
          <button
            key={agent.kind}
            type="button"
            onClick={() => setCreateKind(agent.kind)}
            className={cn(
              "min-h-[44px] rounded-card border px-2 py-2 text-left text-sec transition sm:min-h-0",
              createKind === agent.kind ? "border-live/60 bg-live/10 text-live" : "border-line bg-surface-2 text-ink-2 hover:bg-surface-3",
            )}
          >
            <span className="flex min-w-0 items-center justify-between gap-1.5">
              <span className="flex min-w-0 items-center gap-1.5">
                <span className={cn("h-2.5 w-2.5 shrink-0 rounded-full", AGENT_IDENTITY_DOT_CLASS[agent.kind])} aria-hidden />
                <span className="truncate">{agent.label}</span>
              </span>
              <CapabilityPill capability={capability} agent={agent} />
            </span>
          </button>
        ))}
      </div>
      <label className="grid gap-1 text-ink-3">
        <span className="font-display text-micro font-semibold uppercase tracking-[0.08em]">{de.agentTerminals.workdirLabel}</span>
        <select
          aria-label="Arbeitsverzeichnis für neue Terminals"
          value={workdir}
          onChange={(event) => {
            setWorkdirResetNote(null);
            selectWorkdir(event.target.value);
          }}
          className="min-h-[44px] rounded-card border border-line bg-surface-2 px-2 py-2 text-sec text-ink-2 focus:border-live/50 focus:outline-none sm:min-h-0"
        >
          {([
            ["standard", de.agentTerminals.workdirGroupStandard],
            ["projekt", de.agentTerminals.workdirGroupProjects],
            ["worktree", de.agentTerminals.workdirGroupWorktrees],
            ["terminal_worktree", "Terminal-Worktrees"],
          ] as const).map(([group, label]) => {
            const options = createWorkdirOptions
              .filter((option) => (option.group ?? "standard") === group);
            return options.length ? (
              <optgroup key={group} label={label}>
                {options.map((option) => (
                  <option key={option.key} value={option.key}>{option.label}</option>
                ))}
              </optgroup>
            ) : null;
          })}
        </select>
        <span className="truncate font-data text-micro text-ink-3" title={selectedCreateWorkdir?.path}>
          <span className="sr-only">{de.agentTerminals.workdirPathLabel}: </span>
          {selectedCreateWorkdir?.path ?? "—"}
        </span>
      </label>
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="grid gap-1 text-ink-3">
          <span className="font-display text-micro font-semibold uppercase tracking-[0.08em]">Startmodus</span>
          <select
            aria-label="Terminal-Startmodus"
            value={createStartMode}
            onChange={(event) => setCreateStartMode(event.target.value as AgentTerminalStartMode)}
            className="min-h-[44px] rounded-card border border-line bg-surface-2 px-2 py-2 text-sec text-ink-2 focus:border-live/50 focus:outline-none sm:min-h-0"
          >
            <option value="free">Free / Exploration</option>
            <option value="isolated_write">Isolated Write</option>
          </select>
        </label>
        <label className="grid gap-1 text-ink-3">
          <span className="font-display text-micro font-semibold uppercase tracking-[0.08em]">Kontext</span>
          <select
            aria-label="Terminal-Kontextprofil"
            value={createContextProfile}
            onChange={(event) => setCreateContextProfile(event.target.value as AgentTerminalContextProfile)}
            className="min-h-[44px] rounded-card border border-line bg-surface-2 px-2 py-2 text-sec text-ink-2 focus:border-live/50 focus:outline-none sm:min-h-0"
          >
            <option value="full">Full</option>
            <option value="lean" disabled={!leanEnabled}>
              Lean / Fresh{leanEnabled ? "" : " (nicht verfügbar)"}
            </option>
          </select>
        </label>
      </div>
      {createContextProfile === "lean" && !leanEnabled && (
        <div className="rounded-card border border-status-warn/30 bg-status-warn/10 p-2 text-micro text-status-warn" role="status">
          Lean/Fresh ist für {createKind} disabled — kein belegter sicherer Lean-Adapter (oder nicht allowlisted).
        </div>
      )}
      {leanPrerequisite && (
        <div className="rounded-card border border-line bg-surface-2 p-2 text-micro text-ink-3" role="note">
          Operator-Prerequisite: {leanPrerequisite.message}
        </div>
      )}
      {workdirResetNote && (
        <div className="rounded-card border border-line bg-surface-2 p-2 text-micro text-ink-3" role="status">
          {workdirResetNote}
        </div>
      )}
      {createError && <div className="rounded-card border border-status-alert/30 bg-status-alert/10 p-2 text-sec text-status-alert">{createError}</div>}
      <button
        type="button"
        onClick={() => void submitCreateSession()}
        disabled={createBusy || (createContextProfile === "lean" && !leanEnabled)}
        className="inline-flex min-h-[44px] items-center justify-center gap-1.5 rounded-card border border-live/50 bg-live/15 px-3 py-2.5 text-sec font-medium text-live hover:bg-live/25 disabled:cursor-not-allowed disabled:opacity-50 sm:min-h-0"
      >
        {createBusy ? "Startet…" : "Session starten"}
      </button>
    </div>
  );

  const createSheetHeader = (
    <div className="mb-3 flex items-center justify-between gap-2">
      <div><Eyebrow>Neue Session</Eyebrow><h2 className="text-sec font-semibold text-ink">Agent wählen</h2></div>
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
            <QuestionPill
              count={openQuestions.length}
              standingSinceTs={oldestOpenTs}
              onClick={() => {
                setAnswerFocusId(null);
                setAnswerClosedHint(null);
                setAnswerSheetOpen(true);
              }}
            />
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
            <div className="flex shrink-0 items-center justify-between gap-2 border-b border-line-soft bg-surface-1 px-2 py-1 text-sec text-ink-2">
              <div className="flex min-w-0 items-center gap-2">
                {/* Mobile is the operator's primary surface — the desktop rail/identity
                    bar with the cwd chips is CSS-hidden here, so surface it in the strip. */}
                {selectedWindow && copyState === "idle" && (
                  <span
                    data-testid="mobile-cwd-chip"
                    className="min-w-0 truncate font-data text-micro text-ink-2"
                    title={selectedWindow.cwd?.trim() || undefined}
                  >
                    {formatCwdShort(selectedWindow.cwd)}
                  </span>
                )}
                {copyState !== "idle" && (
                  <span role="status" className={cn("shrink-0 text-micro", copyState === "copied" ? "text-live" : copyState === "error" ? "text-status-alert" : "text-ink-3")}>
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
                  className="grid min-h-[44px] min-w-[44px] place-items-center rounded-card border border-line bg-surface-2 text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live"
                >
                  <Copy className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  aria-label="Text auswählen"
                  title="Text auswählen"
                  onClick={openSelectOverlay}
                  className="inline-flex min-h-[44px] min-w-[44px] items-center gap-1 rounded-card border border-line bg-surface-2 px-2.5 text-micro font-medium text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live"
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
                {selectedWindow && (
                  <button
                    type="button"
                    aria-label={selectedWindow.correlation_id ? "Kanban-Run-Verknüpfung anzeigen" : "Mit Kanban-Run verknüpfen"}
                    title={selectedWindow.correlation_id ? "Kanban-Run-Verknüpfung anzeigen" : "Mit Kanban-Run verknüpfen"}
                    disabled={isDeadWindow(selectedWindow)}
                    onClick={openExecutionCapsule}
                    className={cn(
                      "grid h-12 w-12 place-items-center rounded-card border text-ink-2 transition hover:border-live/40 hover:bg-surface-3 hover:text-live disabled:cursor-not-allowed disabled:opacity-35",
                      selectedWindow.correlation_id ? "border-live/50 bg-live/10 text-live" : "border-line bg-surface-2",
                    )}
                  >
                    <Link2 className="h-3.5 w-3.5" />
                  </button>
                )}
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
              {!compactLayout && selectedWindow?.task_id && selectedWindow.run_id && (
                <button
                  type="button"
                  data-testid="desktop-execution-capsule-binding"
                  onClick={openExecutionCapsule}
                  className="flex shrink-0 items-center gap-2 border-b border-live/15 bg-live/[0.04] px-3 py-1.5 text-left text-[10px] text-ink-3 hover:bg-live/[0.08]"
                >
                  <Link2 className="h-3 w-3 shrink-0 text-live" />
                  <span>Kanban</span>
                  <span className="truncate font-mono text-live">{selectedWindow.task_id}</span>
                  <span className="font-mono text-ink-2">Run #{selectedWindow.run_id}</span>
                </button>
              )}
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

      {executionCapsuleOpen && selectedWindow && (
        <div className="fixed inset-0 z-[70] grid place-items-end bg-black/65 p-0 sm:place-items-center sm:p-4">
          <section
            role="dialog"
            aria-modal="true"
            aria-labelledby="execution-capsule-title"
            className="max-h-[92svh] w-full overflow-y-auto rounded-t-panel border border-line bg-surface-1 p-4 shadow-2xl sm:max-w-2xl sm:rounded-panel sm:p-5"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <Eyebrow>Execution Capsule</Eyebrow>
                <h2 id="execution-capsule-title" className="mt-1 truncate font-display text-base font-semibold text-ink">
                  Kanban-Run mit {selectedWindow.session}:{selectedWindow.window} verknüpfen
                </h2>
                <p className="mt-1 text-xs leading-relaxed text-ink-3">
                  tmux speichert nur Task-, Run- und Korrelationszeiger. Der begrenzte Handoff bleibt in der Run-Historie.
                </p>
              </div>
              <button
                type="button"
                aria-label="Execution Capsule schließen"
                disabled={executionCapsuleBusy}
                onClick={() => setExecutionCapsuleOpen(false)}
                className="grid min-h-[44px] min-w-[44px] shrink-0 place-items-center rounded-card border border-line text-ink-2 hover:bg-surface-3 disabled:opacity-40"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            {selectedWindow.correlation_id && selectedWindow.task_id && selectedWindow.run_id ? (
              <div className="mt-4 grid gap-2 rounded-card border border-live/25 bg-live/[0.06] p-4 text-xs">
                <div className="flex items-center gap-2 text-live"><CheckCircle2 className="h-4 w-4" /><strong>Aktiv verknüpft</strong></div>
                <div className="grid gap-1.5 text-ink-2 sm:grid-cols-[7rem_minmax(0,1fr)]">
                  <span className="text-ink-3">Task</span><code className="truncate">{selectedWindow.task_id}</code>
                  <span className="text-ink-3">Run</span><code>#{selectedWindow.run_id}</code>
                  <span className="text-ink-3">Pane</span><code>{selectedWindow.pane_id}</code>
                  <span className="text-ink-3">Korrelation</span><code className="truncate">{selectedWindow.correlation_id}</code>
                </div>
              </div>
            ) : (
              <div className="mt-4 grid gap-3">
                <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_9rem_12rem]">
                  <label className="grid gap-1 text-xs text-ink-2">
                    <span>Task-ID</span>
                    <input
                      aria-label="Execution Capsule Task-ID"
                      value={executionCapsuleTaskId}
                      maxLength={128}
                      disabled={executionCapsuleBusy}
                      onChange={(event) => setExecutionCapsuleTaskId(event.target.value)}
                      placeholder="t_…"
                      className="min-h-[44px] rounded-card border border-line bg-surface-2 px-3 font-mono text-xs text-ink focus:border-live/50 focus:outline-none"
                    />
                  </label>
                  <label className="grid gap-1 text-xs text-ink-2">
                    <span>Run-ID</span>
                    <input
                      aria-label="Execution Capsule Run-ID"
                      inputMode="numeric"
                      value={executionCapsuleRunId}
                      disabled={executionCapsuleBusy}
                      onChange={(event) => setExecutionCapsuleRunId(event.target.value)}
                      placeholder="42"
                      className="min-h-[44px] rounded-card border border-line bg-surface-2 px-3 font-mono text-xs text-ink focus:border-live/50 focus:outline-none"
                    />
                  </label>
                  <label className="grid gap-1 text-xs text-ink-2">
                    <span>Handoff-Profil</span>
                    <select
                      aria-label="Execution Capsule Profil"
                      value={executionCapsuleProfile}
                      disabled={executionCapsuleBusy}
                      onChange={(event) => setExecutionCapsuleProfile(event.target.value as AgentTerminalExecutionProfile)}
                      className="min-h-[44px] rounded-card border border-line bg-surface-2 px-3 text-xs text-ink focus:border-live/50 focus:outline-none"
                    >
                      <option value="implementation">Umsetzung</option>
                      <option value="review">Review</option>
                      <option value="recovery">Recovery</option>
                      <option value="operator_handoff">Operator-Handoff</option>
                    </select>
                  </label>
                </div>
                <label className="grid gap-1 text-xs text-ink-2">
                  <span>Kurz-Handoff <span className="text-ink-3">(max. 1.200 Zeichen)</span></span>
                  <textarea
                    aria-label="Execution Capsule Kurz-Handoff"
                    value={executionCapsuleSummary}
                    maxLength={1200}
                    rows={4}
                    disabled={executionCapsuleBusy}
                    onChange={(event) => setExecutionCapsuleSummary(event.target.value)}
                    placeholder="Was ist verifiziert, und wo soll diese Session weiterarbeiten?"
                    className="resize-y rounded-card border border-line bg-surface-2 px-3 py-2 text-sm text-ink focus:border-live/50 focus:outline-none"
                  />
                </label>
                <div className="grid gap-3 sm:grid-cols-3">
                  {([
                    ["Entscheidungen", executionCapsuleDecisions, setExecutionCapsuleDecisions],
                    ["Nächste Schritte", executionCapsuleNextSteps, setExecutionCapsuleNextSteps],
                    ["Risiken", executionCapsuleRisks, setExecutionCapsuleRisks],
                  ] as const).map(([label, value, setter]) => (
                    <label key={label} className="grid gap-1 text-xs text-ink-2">
                      <span>{label} <span className="text-ink-3">(eine Zeile je Punkt)</span></span>
                      <textarea
                        aria-label={`Execution Capsule ${label}`}
                        value={value}
                        rows={4}
                        disabled={executionCapsuleBusy}
                        onChange={(event) => setter(event.target.value)}
                        className="resize-y rounded-card border border-line bg-surface-2 px-3 py-2 text-xs text-ink focus:border-live/50 focus:outline-none"
                      />
                    </label>
                  ))}
                </div>
                {executionCapsuleError && (
                  <div role="alert" className="rounded-card border border-status-alert/30 bg-status-alert/10 p-3 text-xs text-status-alert">
                    {executionCapsuleError}
                  </div>
                )}
                <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                  <button
                    type="button"
                    disabled={executionCapsuleBusy}
                    onClick={() => setExecutionCapsuleOpen(false)}
                    className="min-h-[44px] rounded-card border border-line px-4 text-xs font-medium text-ink-2 hover:bg-surface-3 disabled:opacity-40"
                  >
                    Abbrechen
                  </button>
                  <button
                    type="button"
                    disabled={executionCapsuleBusy}
                    onClick={() => void bindExecutionCapsule()}
                    className="inline-flex min-h-[44px] items-center justify-center gap-2 rounded-card border border-live/50 bg-live/10 px-4 text-xs font-semibold text-live hover:bg-live/20 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {executionCapsuleBusy ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Link2 className="h-4 w-4" />}
                    {executionCapsuleBusy ? "Verknüpfe …" : "Run verknüpfen"}
                  </button>
                </div>
              </div>
            )}
          </section>
        </div>
      )}

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

      {answerSheetOpen && (
        <AnswerSheet
          questions={openQuestions}
          focusId={answerFocusId}
          closedHint={answerClosedHint}
          onClose={() => {
            setAnswerSheetOpen(false);
            setAnswerFocusId(null);
            setAnswerClosedHint(null);
          }}
          reload={agentQuestions.reload}
          updateData={agentQuestions.updateData}
        />
      )}
    </div>
  );
}
