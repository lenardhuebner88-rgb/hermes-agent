import { useState } from "react";
import { AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { DrawerShell } from "../../components/leitstand";
import { extractDetail } from "../../hooks/internal";
import { de } from "../../i18n/de";
import { fmtAge } from "../../lib/derive";
import type { ProjectAgent } from "../../lib/schemas";
import { AGENT_KIND_STYLES } from "./agentKinds";
import { killTarget } from "./derive";

const t = de.projekte;

export interface SessionKillSheetProps {
  /** The tmux-sourced agent whose session is about to be terminated. */
  agent: ProjectAgent;
  /** Display name of the agent's project (null = Unzugeordnet). */
  projectName: string | null;
  now: number;
  /** Close without killing (Abbrechen, Escape, backdrop). */
  onClose: () => void;
  /** Called once after a successful terminate — parent reloads the agents poll. */
  onKilled: () => void;
}

/** Kill confirmation for one live session row (Projekte-Tab). Rendered as a
 *  DrawerShell → bottom sheet on Compact, side sheet from `tab` — the approved
 *  mockup's "1 kleiner Klick" flow: ✕ on the row opens this sheet, one more tap
 *  on "Session beenden" fires POST /api/agent-terminals/terminate with
 *  external=true (Projekte rows come from raw `tmux list-panes`, not the
 *  dashboard-managed ensure/create flow — same call shape AgentTerminalsView
 *  uses for non-managed windows). Errors surface inside the open sheet; on
 *  success the parent closes and reloads immediately (no 12s poll wait). */
export function SessionKillSheet({ agent, projectName, now, onClose, onKilled }: SessionKillSheetProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const target = killTarget(agent);
  const style = AGENT_KIND_STYLES[agent.kind] ?? AGENT_KIND_STYLES.unknown;
  const Icon = style.icon;
  const label = agent.label || `${target?.session ?? "?"}:${target?.window ?? "?"}`;

  // Parent only opens this sheet for killable rows; stay silent otherwise
  // (defensive — a stale agent from a previous poll could have lost its fields).
  if (!target) return null;

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.terminateAgentTerminalWindow(target.session, target.window, true);
      onKilled();
      onClose();
    } catch (e) {
      setError(`${t.killSheetError} ${extractDetail(e)}`);
      setBusy(false);
    }
  };

  return (
    <DrawerShell
      eyebrow={t.killSheetEyebrow}
      title={t.killSheetTitle}
      onClose={onClose}
      ariaLabel={t.killSheetTitle}
      footer={
        <div className="flex gap-2.5">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="flex-1 rounded-card border border-line px-4 py-2.5 text-sec font-semibold text-ink-2 hover:bg-surface-3 disabled:opacity-40"
          >
            {t.killSheetCancel}
          </button>
          <button
            type="button"
            onClick={confirm}
            disabled={busy}
            className="flex-1 rounded-card bg-status-alert px-4 py-2.5 text-sec font-semibold text-surface-0 hover:brightness-110 disabled:opacity-40"
          >
            {busy ? t.killSheetBusy : t.killSheetConfirm}
          </button>
        </div>
      }
    >
      <div className="space-y-4">
        <div className="flex items-center gap-3 rounded-card border border-line bg-surface-2 px-3.5 py-3">
          <span className={cn("grid size-9 shrink-0 place-items-center rounded-card", style.tone, "bg-surface-3")}>
            <Icon className="size-5" aria-hidden />
          </span>
          <div className="min-w-0">
            <p className="truncate font-data text-sec text-ink">{label}</p>
            <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-micro text-ink-2">
              <span className="inline-flex items-center rounded-card border border-line px-1.5 py-0.5 text-micro text-ink-3">
                {projectName ?? t.unassigned}
              </span>
              <span className="font-data">
                tmux {target.session}:{target.window}
              </span>
              {agent.since != null && Number.isFinite(agent.since) ? (
                <>
                  <span aria-hidden>·</span>
                  <span>
                    {t.runsFor} {fmtAge(agent.since, now)}
                  </span>
                </>
              ) : null}
            </p>
          </div>
        </div>

        <div className="flex items-start gap-2 text-sec">
          <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0 text-status-alert" />
          <p className="text-status-alert/90">{t.killSheetWarning}</p>
        </div>

        {error ? (
          <div
            role="alert"
            className="rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"
          >
            {error}
          </div>
        ) : null}

        <p className="text-micro text-ink-3">{t.killSheetHint}</p>
      </div>
    </DrawerShell>
  );
}
