import { memo, type ReactNode } from "react";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";
import type {
  AgentTerminalCapabilityState,
  AgentTerminalKind,
  AgentTerminalOverviewState,
  AgentTerminalOverviewWindow,
  AgentTerminalWindow,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { de } from "../../i18n/de";
import { KpiTile, SignalChip, type SignalTone } from "../../components/leitstand";
import {
  AGENTS,
  AGENT_LABELS,
  chipLabel,
  formatActivityAge,
  formatCwdShort,
  isManagedWindow,
  kindFromWindow,
  terminalProcessLabel,
  type TerminalUiState,
} from "./terminalHelpers";

export function TerminalStatusChip({ state }: { state: TerminalUiState }) {
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

export function CapabilityPill({ capability, agent }: { capability: AgentTerminalCapabilityState | null; agent: (typeof AGENTS)[number] }) {
  const windowsOk = capability?.tmux_available ?? false;
  const agentState = capability?.agents?.[agent.kind] ?? null;
  const agentOk = agentState ? agentState.available : agent.kind === "hermes" ? (capability?.hermes_tui_available ?? false) : windowsOk;
  const ok = windowsOk && agentOk;
  const reason = agentState?.reason ?? capability?.reason ?? null;
  if (ok) return null;
  const label = reason?.includes("symlink") || reason?.includes("resolvable")
    ? de.agentTerminals.agentBinaryBroken
    : reason
      ? de.agentTerminals.agentCliMissing
      : de.agentTerminals.agentUnavailable;
  const title = reason ?? agentState?.binary ?? agent.hint;
  return (
    <span title={title} className="inline-flex shrink-0 items-center gap-1 rounded-card border border-status-warn/40 bg-status-warn/10 px-1.5 py-0.5 text-micro text-status-warn">
      <AlertTriangle className="h-3 w-3" aria-hidden />
      {label}
    </span>
  );
}

export function TerminalIdentityBar({
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

export function TerminalControlButton({
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
export const STRIP_STATE_META: Record<AgentTerminalOverviewState, { label: string; tone: SignalTone }> = {
  laeuft: { label: "läuft", tone: "ok" },
  frage: { label: "frage", tone: "warn" },
  wartet: { label: "wartet", tone: "ok" },
  idle: { label: "idle", tone: "neutral" },
  dead: { label: "tot", tone: "alert" },
};

export function StatTile({
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
export const FleetStripCard = memo(function FleetStripCard({
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
});

export const FleetCard = memo(function FleetCard({
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
            title="Externes Fenster — gehört einem anderen Agenten/Prozess"
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
          Extern (managed===false) keeps the affordance with sharper confirm chrome. */}
      {!dead && (
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
                className={cn(
                  "inline-flex flex-1 items-center justify-center gap-1 rounded-card px-2 py-1.5 text-[11px] text-status-alert disabled:cursor-not-allowed disabled:opacity-40",
                  managed
                    ? "border border-status-alert/60 bg-status-alert/15 hover:bg-status-alert/25"
                    : "border-2 border-status-alert/70 bg-status-alert/25 hover:bg-status-alert/35",
                )}
                aria-label={managed ? `Beenden bestätigen ${win.session}:${win.window}` : `Externes Fenster wirklich beenden? Gehört einem anderen Agenten/Prozess. ${win.session}:${win.window}`}
                title={managed ? undefined : "Externes Fenster wirklich beenden? Gehört einem anderen Agenten/Prozess."}
              >
                <Check className="h-3 w-3" />{managed ? "Wirklich beenden" : "Extern wirklich?"}
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
              className={cn(
                "inline-flex flex-1 items-center justify-center gap-1 rounded-card px-2 py-1.5 text-[11px] text-status-alert",
                managed
                  ? "border border-status-alert/30 hover:border-status-alert/60 hover:bg-status-alert/10"
                  : "border-2 border-status-alert/50 bg-status-alert/10 hover:border-status-alert/70 hover:bg-status-alert/20",
              )}
              aria-label={managed ? `Session beenden ${win.session}:${win.window}` : `Externes Fenster beenden ${win.session}:${win.window}`}
              title={managed ? undefined : "Externes Fenster beenden — gehört einem anderen Agenten/Prozess"}
            >
              <Trash2 className="h-3 w-3" />{managed ? "Session beenden" : "Extern beenden"}
            </button>
          )}
        </div>
      )}
    </div>
  );
});
