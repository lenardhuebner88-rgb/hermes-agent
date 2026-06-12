import { useEffect, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, Eye, Lock, OctagonX, RotateCw, ScrollText, Send, Zap } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { fetchJSON } from "@/lib/api";
import {
  STUCK_HEARTBEAT_S,
  fmtDur,
  fmtMB,
  workerHeartbeatAge,
  workerRemaining,
  workerRunaway,
  workerRuntime,
} from "../lib/derive";
import { profileLabel, taskStatusLabel } from "../lib/tones";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import type { Worker, WorkerHealth } from "../lib/types";
import { MeterBar, StatusPill, ToneCallout } from "./atoms";
import { Stat, Text } from "./primitives";

interface Props {
  worker: Worker;
  health: WorkerHealth;
  density: Density;
  now: number;
  inspectLoading?: boolean;
  onInspect: (runId: string) => void;
  /** Worker-Steuerung: unlock/nudge/restart/dispatch laufen über
   *  POST /workers/{run}/action, "terminate" über POST /runs/{run}/terminate —
   *  der Aufrufer routet; die Karte kennt nur den Aktions-Schlüssel. */
  onAction?: (runId: string, action: WorkerActionKey) => void | Promise<void>;
  actionBusy?: boolean;
  /** Kompakt-Modus: nur Kopfzeile (Profil · Status · Laufzeit/Budget) sichtbar,
   *  Stats/Meter/Aktionen erst nach Aufklappen. Problemfälle starten offen. */
  collapsible?: boolean;
}

export type WorkerActionKey = "terminate" | "unlock" | "nudge" | "restart" | "dispatch";

const ACTION_ORDER: WorkerActionKey[] = ["unlock", "nudge", "restart", "dispatch", "terminate"];

interface TaskLogResponse {
  task_id: string;
  exists: boolean;
  size_bytes: number;
  content: string;
  truncated: boolean;
}

const LOG_TAIL_BYTES = 16384;
const LOG_POLL_MS = 4000;
const LOG_MAX_LINES = 100;

// A3: Live-Log-Tail über den existierenden GET /tasks/{id}/log — gepollt NUR
// solange das Panel offen ist, letzte ~100 Zeilen, monospace.
function WorkerLogTail({ taskId }: { taskId: string }) {
  const [log, setLog] = useState<TaskLogResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await fetchJSON<TaskLogResponse>(
          `/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}/log?tail=${LOG_TAIL_BYTES}`,
        );
        if (!cancelled) { setLog(data); setError(null); }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    };
    void load();
    const id = window.setInterval(() => void load(), LOG_POLL_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [taskId]);

  if (error) return <ToneCallout tone="red">{error}</ToneCallout>;
  if (log === null) return <Text variant="label" className="hc-dim">…</Text>;
  if (!log.exists || !log.content.trim()) {
    return <Text variant="label" className="hc-dim">{de.worker.logEmpty}</Text>;
  }
  const lines = log.content.split("\n");
  const tail = lines.slice(-LOG_MAX_LINES).join("\n");
  return (
    <div className="space-y-1">
      {log.truncated || lines.length > LOG_MAX_LINES ? (
        <Text variant="label" className="hc-dim">{de.worker.logTruncated}</Text>
      ) : null}
      <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-[var(--hc-border)] bg-black/30 p-2 text-[0.7rem] leading-relaxed hc-soft">
        {tail}
      </pre>
    </div>
  );
}

function actionIcon(key: WorkerActionKey) {
  if (key === "unlock") return <Lock className="h-4 w-4" />;
  if (key === "restart") return <RotateCw className="h-4 w-4" />;
  if (key === "dispatch") return <Send className="h-4 w-4" />;
  if (key === "terminate") return <OctagonX className="h-4 w-4" />;
  return <Zap className="h-4 w-4" />;
}

export function WorkerCard({ worker, health, density, now, inspectLoading, onInspect, onAction, actionBusy, collapsible = false }: Props) {
  // Welche Aktion gerade auf Bestätigung wartet (eine zur Zeit).
  const [confirming, setConfirming] = useState<WorkerActionKey | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const runaway = workerRunaway(worker, now);
  // Aufklapp-Zustand im Kompakt-Modus: Problemfälle starten offen. Bewusst nur
  // der INITIALWERT — kein Auto-Aufklappen pro Poll-Tick, genau diese
  // Layout-Sprünge soll der Kompakt-Modus beseitigen.
  const [open, setOpen] = useState(() => runaway.level !== "none" || health.key !== "healthy");
  const expanded = !collapsible || open;
  const inspect = worker.inspect ?? null;
  const remaining = workerRemaining(worker, now);
  const runtime = workerRuntime(worker, now);
  // Phase A: Tätigkeits-Note + ehrliche ETA. Kein Fake-Prozent — der Balken
  // ist elapsed÷p50 gedeckelt, jenseits p90 wird der Zustand benannt.
  const note = worker.last_heartbeat_note ?? null;
  const noteAge = worker.last_heartbeat_note_at ? Math.max(0, now - worker.last_heartbeat_note_at) : null;
  const etaP50 = worker.eta_p50_seconds ?? null;
  const etaP90 = worker.eta_p90_seconds ?? null;
  const overP90 = etaP90 != null && runtime > etaP90;
  const hasHeartbeat = worker.last_heartbeat_at > 0;
  const heartbeatAge = workerHeartbeatAge(worker, now);
  // Die situativ wahrscheinlichste Aktion steht vorn; ein Runaway-Kandidat
  // bekommt "Beenden" als Primärvorschlag.
  const primary: WorkerActionKey =
    runaway.level === "critical" ? "terminate"
    : health.key === "blocked" ? "unlock"
    : health.key === "stuck" ? "nudge"
    : health.key === "offline" ? "restart"
    : "dispatch";
  const orderedActions: WorkerActionKey[] = [primary, ...ACTION_ORDER.filter((a) => a !== primary)];
  const stuckReason = hasHeartbeat ? de.worker.stuckReason(fmtDur(heartbeatAge)) : de.worker.expiredReason;
  const problemText = worker.block_reason || (health.key === "offline" ? de.worker.offlineReason : health.key === "stuck" ? stuckReason : null);
  // Mini-Balken der Kopfzeile: Budget-Anteil (bevorzugt), sonst ETA-Anteil.
  const headerMeterPct = worker.max_runtime_seconds > 0
    ? Math.min(1, runtime / worker.max_runtime_seconds)
    : etaP50 != null && etaP50 > 0 ? Math.min(1, runtime / etaP50) : null;

  const runawayBadge = runaway.level !== "none" ? (
    <span className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium", runaway.level === "critical" ? "border-red-500/40 bg-red-500/10 text-red-200" : "border-amber-500/40 bg-amber-500/10 text-amber-200")}>
      <AlertTriangle className="h-3 w-3" />{runaway.level === "critical" ? de.worker.runawayCritical : de.worker.runawayWarn}
    </span>
  ) : null;

  return (
    <article className={cn("hc-surface-card", collapsible ? "space-y-3 p-3" : "space-y-4 p-4", density === "compact" && "p-3", runaway.level === "critical" && "border-red-500/40", runaway.level === "warn" && "border-amber-500/40")}>
      {collapsible ? (
        <button type="button" aria-expanded={expanded} onClick={() => setOpen((v) => !v)} className="block w-full text-left">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2 py-0.5 text-xs text-[var(--hc-accent-text)]">{profileLabel[worker.profile] ?? worker.profile}</span>
            {runawayBadge}
            <span className="ml-auto inline-flex items-center gap-1.5">
              <StatusPill tone={health.tone} label={health.label} dot={health.dot} />
              {expanded ? <ChevronDown className="h-3.5 w-3.5 hc-soft" /> : <ChevronRight className="h-3.5 w-3.5 hc-soft" />}
            </span>
          </div>
          <h3 className="mt-1.5 line-clamp-1 text-sm font-semibold leading-snug text-white">{worker.task_title}</h3>
          <p className="mt-1 truncate hc-mono hc-type-label hc-dim">
            ⏱ {fmtDur(runtime)}
            {worker.max_runtime_seconds > 0 ? ` / ${fmtDur(worker.max_runtime_seconds)}` : ""}
            {note ? ` · ${note}` : ""}
          </p>
          {headerMeterPct != null ? (
            <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-white/10">
              <div
                className={cn("h-full rounded-full", runaway.level === "critical" ? "bg-red-400" : runaway.level === "warn" || overP90 ? "bg-amber-400" : "bg-cyan-400")}
                style={{ width: `${Math.round(headerMeterPct * 100)}%` }}
              />
            </div>
          ) : null}
        </button>
      ) : (
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2 py-0.5 text-xs text-[var(--hc-accent-text)]">{profileLabel[worker.profile] ?? worker.profile}</span>
            <span className="rounded-full border border-white/10 px-2 py-0.5 text-xs hc-soft">{taskStatusLabel[worker.task_status] ?? worker.task_status}</span>
            {runawayBadge}
          </div>
          <h3 className="line-clamp-2 text-base font-semibold leading-snug text-white">{worker.task_title}</h3>
          {note ? (
            <Text variant="label" className="hc-soft">
              <span className="hc-eyebrow mr-1.5">{de.worker.doingNow}:</span>
              {note}
              {noteAge != null ? <span className="hc-dim"> · {de.worker.noteAge(fmtDur(noteAge))}</span> : null}
            </Text>
          ) : null}
        </div>
        <StatusPill tone={health.tone} label={health.label} dot={health.dot} />
      </div>
      )}

      {!expanded ? (
        problemText || runaway.level !== "none" ? (
          <p className="line-clamp-2 hc-type-label text-amber-200">{problemText ?? runaway.reasons.join(" · ")}</p>
        ) : null
      ) : (
      <>
      {collapsible && note ? (
        <Text variant="label" className="hc-soft">
          <span className="hc-eyebrow mr-1.5">{de.worker.doingNow}:</span>
          {note}
          {noteAge != null ? <span className="hc-dim"> · {de.worker.noteAge(fmtDur(noteAge))}</span> : null}
        </Text>
      ) : null}
      <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
        <Stat label={de.worker.runtime} value={fmtDur(runtime)} />
        <Stat label={de.worker.heartbeat} value={hasHeartbeat ? fmtDur(heartbeatAge) : "—"} tone={hasHeartbeat && heartbeatAge > STUCK_HEARTBEAT_S ? "amber" : undefined} />
        <Stat label={de.worker.remaining} value={remaining <= 0 ? "0s" : fmtDur(remaining)} tone={remaining <= 0 ? "amber" : undefined} />
        <Stat label="PID" value={worker.worker_pid ? String(worker.worker_pid) : "—"} />
      </div>

      {/* Phase A: ehrliche ETA — elapsed ÷ p50, gedeckelt; > p90 = Amber. */}
      {etaP50 != null && etaP50 > 0 ? (
        <MeterBar
          label={`${de.worker.etaLine(fmtDur(etaP50), fmtDur(runtime))}${overP90 ? ` · ${de.worker.etaLonger}` : ""}`}
          value={Math.min(runtime, etaP50)}
          max={etaP50}
          tone={overP90 ? "amber" : "cyan"}
        />
      ) : (
        <Text variant="label" className="hc-dim">{de.worker.etaNoData}</Text>
      )}

      {/* Laufzeit-Budget: verbraucht/max — der Runaway-Blick auf einen Blick. */}
      {worker.max_runtime_seconds > 0 ? (
        <MeterBar
          label={`${de.worker.runtimeBudget} · ${fmtDur(runtime)} / ${fmtDur(worker.max_runtime_seconds)}`}
          value={runtime}
          max={worker.max_runtime_seconds}
          tone={runaway.level === "critical" ? "red" : runaway.level === "warn" ? "amber" : "cyan"}
        />
      ) : null}

      {inspect ? (
        inspect.alive ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <MeterBar label="CPU" value={inspect.cpu_percent} max={100} tone={inspect.cpu_percent > 80 ? "amber" : "cyan"} />
            <MeterBar label="RAM" value={inspect.rss / 1048576} max={2048} tone={inspect.rss > 1536 * 1048576 ? "amber" : "cyan"} />
            <Text variant="label" className="hc-soft sm:col-span-2">{de.worker.process}: {inspect.status} · {fmtMB(inspect.rss)} · {inspect.num_threads} Threads · {inspect.num_fds} FDs</Text>
          </div>
        ) : (
          // Kein greifbarer Prozess (z.B. claude-cli ohne erfasste PID):
          // ehrliche Begründung statt irreführender 0-Meter.
          <Text variant="label" className="hc-soft">{de.worker.inspectUnavailable}{inspect.reason ? ` — ${inspect.reason}` : ""}</Text>
        )
      ) : null}

      {runaway.level !== "none" ? (
        <ToneCallout tone={runaway.level === "critical" ? "red" : "amber"}>
          <AlertTriangle className="mr-2 inline h-4 w-4" />{runaway.reasons.join(" · ")}
        </ToneCallout>
      ) : null}
      {problemText ? <ToneCallout tone={health.tone}><AlertTriangle className="mr-2 inline h-4 w-4" />{problemText}</ToneCallout> : null}

      {confirming ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            disabled={actionBusy}
            onClick={async () => { if (onAction) await onAction(worker.run_id, confirming); setConfirming(null); }}
            prefix={actionBusy ? <Spinner /> : actionIcon(confirming)}
          >
            {de.worker.actions[confirming]} · {de.worker.actions.confirm}
          </Button>
          <Button outlined size="sm" disabled={actionBusy} onClick={() => setConfirming(null)}>
            {de.worker.actions.cancel}
          </Button>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button outlined size="sm" onClick={() => onInspect(worker.run_id)} disabled={inspectLoading} prefix={inspectLoading ? <Spinner /> : <Eye className="h-4 w-4" />}>
            {de.worker.actions.inspect}
          </Button>
          <Button outlined size="sm" onClick={() => setLogOpen((v) => !v)} prefix={<ScrollText className="h-4 w-4" />}>
            {logOpen ? de.worker.logHide : de.worker.logShow}
          </Button>
          {onAction ? orderedActions.map((key) => (
            <Button
              key={key}
              outlined
              size="sm"
              disabled={actionBusy}
              onClick={() => setConfirming(key)}
              prefix={actionBusy ? <Spinner /> : actionIcon(key)}
              className={cn(key === "terminate" && "text-red-300", key === primary && "border-[var(--hc-accent-border)]")}
            >
              {de.worker.actions[key]}
            </Button>
          )) : null}
        </div>
      )}
      {confirming ? (
        <Text variant="label" className="hc-soft">
          {confirming === "terminate" ? de.worker.terminateHint : confirming === "restart" ? de.worker.restartHint : de.worker.confirmHint}
        </Text>
      ) : null}
      {logOpen ? <WorkerLogTail taskId={worker.task_id} /> : null}
      </>
      )}
    </article>
  );
}
