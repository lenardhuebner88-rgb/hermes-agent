import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Eye, Lock, OctagonX, PauseCircle, RotateCw, ScrollText, Send, TriangleAlert, Zap } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { fetchJSON } from "@/lib/api";
import {
  STUCK_HEARTBEAT_S,
  elapsedSeconds,
  fmtDur,
  fmtMB,
  fmtTokens,
  workerBurnRate,
  workerHeartbeatAge,
  workerRemaining,
  workerRunaway,
  workerRuntime,
  inspectEpochSeconds,
  workerTimeAxisState,
  timeAxisScaleMax,
} from "../lib/derive";
import { profileLabel, taskStatusLabel } from "../lib/tones";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import type { Worker, WorkerHealth } from "../lib/types";
import { SignalChip, signalToneFromLegacy } from "./leitstand";
import { Stat, Text } from "./primitives";
import { useWorkerActivity } from "../hooks/useControlData";

interface Props {
  worker: Worker;
  health: WorkerHealth;
  density: Density;
  now: number;
  inspectLoading?: boolean;
  onInspect: (runId: string) => void;
  /** Worker-Steuerung: unlock/nudge/restart/hold/dispatch laufen über
   *  POST /workers/{run}/action, "terminate" über POST /runs/{run}/terminate —
   *  der Aufrufer routet; die Karte kennt nur den Aktions-Schlüssel.
   *  Optionales extra wird in den POST-Body gemerged (model_override, assignee). */
  onAction?: (runId: string, action: WorkerActionKey, extra?: { model_override?: string; assignee?: string }) => void | Promise<void>;
  actionBusy?: boolean;
  /** Kompakt-Modus: nur Kopfzeile (Profil · Status · Laufzeit/Budget) sichtbar,
   *  Stats/Meter/Aktionen erst nach Aufklappen. Problemfälle starten offen. */
  collapsible?: boolean;
}

export type WorkerActionKey = "terminate" | "unlock" | "nudge" | "restart" | "dispatch" | "hold";

const ACTION_ORDER: WorkerActionKey[] = ["unlock", "nudge", "restart", "hold", "dispatch", "terminate"];

// Einzeiler-Wirkungstext pro Aktion (Sublabel unter dem Button).
const ACTION_EFFECT: Record<WorkerActionKey, string> = {
  nudge: de.worker.actions.nudgeEffect,
  restart: de.worker.actions.restartEffect,
  unlock: de.worker.actions.unlockEffect,
  hold: de.worker.actions.holdEffect,
  terminate: de.worker.actions.terminateEffect,
  dispatch: de.worker.actions.dispatchEffect,
};

// Lane-Optionen für F3 Neu-Starten auf anderer Lane.
const WORKER_LANE_OPTIONS = ["coder", "coder-claude", "premium", "verifier", "research", "admin"] as const;

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

const WORKER_CALLOUT_CLASS: Record<"warn" | "alert", string> = {
  warn: "border-status-warn/30 bg-status-warn/10 text-status-warn",
  alert: "border-status-alert/30 bg-status-alert/10 text-status-alert",
};

function WorkerCallout({ tone, children }: { tone: "warn" | "alert"; children: React.ReactNode }) {
  return <div role="alert" className={cn("flex items-start gap-2 rounded-card border px-3 py-2 text-sec", WORKER_CALLOUT_CLASS[tone])}><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" /><span className="min-w-0">{children}</span></div>;
}

// A3: Live-Log-Tail über den existierenden GET /tasks/{id}/log — gepollt NUR
// solange das Panel offen ist, letzte ~100 Zeilen, monospace.
export function WorkerLogTail({ taskId }: { taskId: string }) {
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

  if (error) return <WorkerCallout tone="alert">{error}</WorkerCallout>;
  if (log === null) return <Text variant="label" className="text-ink-3">…</Text>;
  if (!log.exists || !log.content.trim()) {
    return <Text variant="label" className="text-ink-3">{de.worker.logEmpty}</Text>;
  }
  const lines = log.content.split("\n");
  const tail = lines.slice(-LOG_MAX_LINES).join("\n");
  return (
    <div className="space-y-1">
      {log.truncated || lines.length > LOG_MAX_LINES ? (
        <Text variant="label" className="text-ink-3">{de.worker.logTruncated}</Text>
      ) : null}
      <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded-card border border-line bg-surface-1 p-2 font-data text-micro leading-relaxed text-ink-2">
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
  if (key === "hold") return <PauseCircle className="h-4 w-4" />;
  return <Zap className="h-4 w-4" />;
}

// Zeit-Achse: ein SVG-Track mit Markern für jetzt / p50 / p90 / Budget.
// Markers die null sind werden nicht gerendert (ehrlich).
function TimeAxisTrack({
  elapsed,
  p50,
  p90,
  budget,
  scaleMax,
}: {
  elapsed: number;
  p50: number | null | undefined;
  p90: number | null | undefined;
  budget: number;
  scaleMax: number;
}) {
  const pct = (v: number) => Math.max(0, Math.min(100, (v / scaleMax) * 100));
  const nowPct = pct(elapsed);
  const p50Pct = p50 != null && p50 > 0 ? pct(p50) : null;
  const p90Pct = p90 != null && p90 > 0 ? pct(p90) : null;
  const budgetPct = budget > 0 ? pct(budget) : null;

  const ariaParts = [
    `${de.worker.timeAxisNow} ${fmtDur(elapsed)}`,
    p50 != null && p50 > 0 ? `${de.worker.timeAxisP50} ${fmtDur(p50)}` : null,
    p90 != null && p90 > 0 ? `${de.worker.timeAxisP90} ${fmtDur(p90)}` : null,
    budget > 0 ? `${de.worker.timeAxisBudget} ${fmtDur(budget)}` : null,
  ].filter((part): part is string => part != null);
  const ariaLabel = `${de.worker.timeAxisLabel}: ${ariaParts.join(", ")}`;

  return (
    <div className="relative h-6 w-full" role="img" aria-label={ariaLabel}>
      {/* Track-Hintergrund */}
      <div className="absolute inset-x-0 top-1/2 h-1.5 -translate-y-1/2 overflow-hidden rounded-full bg-line">
        {/* Elapsed-Füllung */}
        <div
          className="h-full rounded-full bg-live/60"
          style={{ width: `${nowPct}%` }}
        />
      </div>
      {/* p50-Marker */}
      {p50Pct != null ? (
        <div
          className="absolute top-0 h-full w-px bg-status-ok/70"
          style={{ left: `${p50Pct}%` }}
          title={`${de.worker.timeAxisP50}: ${fmtDur(p50!)}`}
        >
          <span className="absolute -top-0.5 left-1 text-[0.6rem] text-status-ok">{de.worker.timeAxisP50}</span>
        </div>
      ) : null}
      {/* p90-Marker */}
      {p90Pct != null ? (
        <div
          className="absolute top-0 h-full w-px bg-status-warn/70"
          style={{ left: `${p90Pct}%` }}
          title={`${de.worker.timeAxisP90}: ${fmtDur(p90!)}`}
        >
          <span className="absolute -top-0.5 left-1 text-[0.6rem] text-status-warn">{de.worker.timeAxisP90}</span>
        </div>
      ) : null}
      {/* Budget-Marker */}
      {budgetPct != null ? (
        <div
          className="absolute top-0 h-full w-0.5 bg-status-alert/70"
          style={{ left: `${budgetPct}%` }}
          title={`${de.worker.timeAxisBudget}: ${fmtDur(budget)}`}
        >
          <span className="absolute -top-0.5 left-1 text-[0.6rem] text-status-alert">{de.worker.timeAxisBudget}</span>
        </div>
      ) : null}
      {/* „Jetzt"-Marker */}
      <div
        className="absolute top-0 h-full w-0.5 rounded bg-ink"
        style={{ left: `${nowPct}%` }}
        title={`${de.worker.timeAxisNow}: ${fmtDur(elapsed)}`}
      />
    </div>
  );
}

// F1: Aktivitäts-Timeline — letzte ~10 Heartbeat-Notizen.
// taskId=null → kein Poll (Karte eingeklappt); hook pausiert automatisch.
function ActivityTimeline({ taskId, now }: { taskId: string | null; now: number }) {
  const { data, loading } = useWorkerActivity(taskId);
  const heartbeatNotes = (data?.events ?? [])
    .filter((e) => e.kind === "heartbeat" && e.note != null)
    .slice(0, 10);

  if (loading && !data) {
    return <Text variant="label" className="text-ink-3">{de.worker.activityLoading}</Text>;
  }
  if (heartbeatNotes.length === 0) {
    return <Text variant="label" className="text-ink-3">{de.worker.activityEmpty}</Text>;
  }

  const latestAt = heartbeatNotes[0]?.at ?? 0;
  const latestAge = latestAt > 0 ? elapsedSeconds(latestAt, now) : null;
  const latestStale = latestAge != null && latestAge > STUCK_HEARTBEAT_S;

  return (
    <div className="space-y-1.5">
      <Text variant="eyebrow" className="text-ink-3">{de.worker.activityHeading}</Text>
      {latestStale ? (
        <Text variant="label" className="text-status-warn">{de.worker.activityStaleHint}</Text>
      ) : null}
      <ol className="space-y-1">
        {heartbeatNotes.map((ev) => {
          const age = ev.at > 0 ? elapsedSeconds(ev.at, now) : null;
          return (
            <li key={ev.id} className="flex items-start gap-2">
              <span className="mt-0.5 min-w-[3.5rem] shrink-0 text-right font-data text-micro text-ink-3">
                {age != null ? de.worker.activityAgo(fmtDur(age)) : "Zeit ungültig"}
              </span>
              <span className="text-micro leading-snug text-ink-2">{ev.note}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export function WorkerCard({ worker, health, density, now, inspectLoading, onInspect, onAction, actionBusy, collapsible = false }: Props) {
  // Welche Aktion gerade auf Bestätigung wartet (eine zur Zeit).
  const [confirming, setConfirming] = useState<WorkerActionKey | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  // F3: Lane-Auswahl für "Neu starten auf anderer Lane".
  const [restartLane, setRestartLane] = useState<string>("");
  const runaway = workerRunaway(worker, now);
  // Aufklapp-Zustand im Kompakt-Modus: Problemfälle starten offen. Bewusst nur
  // der INITIALWERT — kein Auto-Aufklappen pro Poll-Tick, genau diese
  // Layout-Sprünge soll der Kompakt-Modus beseitigen.
  const [open, setOpen] = useState(() => runaway.level !== "none" || health.key !== "healthy");
  const expanded = !collapsible || open;

  const inspect = worker.inspect ?? null;
  const remaining = workerRemaining(worker, now);
  const runtime = workerRuntime(worker, now);
  const runtimeValid = Number.isFinite(runtime);

  // Phase A: Tätigkeits-Note + ehrliche ETA.
  const note = worker.last_heartbeat_note ?? null;
  const noteAge = worker.last_heartbeat_note_at ? elapsedSeconds(worker.last_heartbeat_note_at, now) : null;
  const etaP50 = worker.eta_p50_seconds ?? null;
  const etaP90 = worker.eta_p90_seconds ?? null;
  const heartbeatProvided = worker.last_heartbeat_at !== 0;
  const hasHeartbeat = heartbeatProvided && inspectEpochSeconds(worker.last_heartbeat_at, now).valid;
  const heartbeatAge = workerHeartbeatAge(worker, now);

  // Phase B: Zeit-Achse + Telemetrie-Chips.
  const budget = worker.max_runtime_seconds ?? 0;
  const axisState = runtimeValid
    ? workerTimeAxisState(runtime, etaP50, etaP90, budget, heartbeatAge, hasHeartbeat)
    : { key: "no_eta" as const, tone: "amber" as const, label: "Zeit ungültig", noEta: true };
  const axisScaleMax = runtimeValid ? timeAxisScaleMax(runtime, etaP90, budget) : 1;

  // Telemetrie-Chips: effektives Modell + Schritt + Tokens.
  const effectiveModel = worker.effective_model ?? null;
  const modelOverride = worker.model_override ?? null;
  const stepKey = worker.step_key ?? null;
  const inputTokens = worker.input_tokens ?? null;
  const outputTokens = worker.output_tokens ?? null;
  const hasLiveTokens = inputTokens != null || outputTokens != null;

  // F2: Burn-Wächter — aus echten Zahlen abgeleitet, KEINE erfundenen Schwellen.
  const burn = workerBurnRate(inputTokens, outputTokens, runtime, etaP50, budget > 0 ? budget : undefined);
  // Ampel-Farbe aus dem schon abgeleiteten Zeit-Zustand (kein Magie-Token-Schwellwert).
  const burnTone = axisState.tone === "red" || axisState.tone === "amber" ? axisState.tone : "zinc";

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
  const problemText = worker.block_reason || (!runtimeValid
    ? "Start-Zeitstempel ungültig"
    : heartbeatProvided && !hasHeartbeat
      ? "Heartbeat-Zeitstempel ungültig"
      : health.key === "offline"
        ? de.worker.offlineReason
        : health.key === "stuck"
          ? stuckReason
          : null);

  // Mini-Balken der Kopfzeile: Budget-Anteil (bevorzugt), sonst ETA-Anteil.
  const headerMeterPct = runtimeValid && worker.max_runtime_seconds > 0
    ? Math.min(1, runtime / worker.max_runtime_seconds)
    : etaP50 != null && etaP50 > 0 ? Math.min(1, runtime / etaP50) : null;

  const runawayBadge = runaway.level !== "none" ? (
    <SignalChip
      tone={runaway.level === "critical" ? "alert" : "warn"}
      label={runaway.level === "critical"
        ? `${de.worker.runawayCritical} — ${de.worker.runawayCriticalDetail}`
        : `${de.worker.runawayWarn} — ${de.worker.runawayWarnDetail}`}
    />
  ) : null;

  return (
    // min-w-0: the card is a grid item (FleetPanel `grid lg:grid-cols-2`, chain-node
    // cockpit). Grid/flex items default to min-width:auto and refuse to shrink below
    // their content width — on a narrow single-column (mobile) the card would overflow
    // the viewport to the right (clipped by the page's overflow-x-hidden → content cut
    // off). min-w-0 lets it shrink; the truncate/line-clamp children then reflow.
    <article className={cn("hc-surface-card min-w-0", collapsible ? "space-y-3 p-3" : "space-y-4 p-4", density === "compact" && "p-3", runaway.level === "critical" && "border-status-alert/40", runaway.level === "warn" && "border-status-warn/40")}>
      {collapsible ? (
        <button type="button" aria-expanded={expanded} onClick={() => setOpen((v) => !v)} className="block w-full text-left">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-card border border-line bg-surface-2 px-2 py-0.5 text-xs text-ink-2">{profileLabel[worker.profile] ?? worker.profile}</span>
            {runawayBadge}
            <span className="ml-auto inline-flex items-center gap-1.5">
              <SignalChip tone={signalToneFromLegacy(health.tone)} label={health.label} />
              {expanded ? <ChevronDown className="h-3.5 w-3.5 text-ink-2" /> : <ChevronRight className="h-3.5 w-3.5 text-ink-2" />}
            </span>
          </div>
          <h3 className="mt-1.5 line-clamp-1 text-sm font-semibold leading-snug text-ink">{worker.task_title}</h3>
          <p className="mt-1 truncate font-data text-micro text-ink-3">
            ⏱ {fmtDur(runtime)}
            {worker.max_runtime_seconds > 0 ? ` / ${fmtDur(worker.max_runtime_seconds)}` : ""}
            {note ? ` · ${note}` : ""}
          </p>
          {headerMeterPct != null ? (
            <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-line">
              <div
                className={cn("h-full rounded-full", runaway.level === "critical" ? "bg-status-alert" : runaway.level === "warn" ? "bg-status-warn" : "bg-live")}
                style={{ width: `${Math.round(headerMeterPct * 100)}%` }}
              />
            </div>
          ) : null}
        </button>
      ) : (
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-card border border-line bg-surface-2 px-2 py-0.5 text-xs text-ink-2">{profileLabel[worker.profile] ?? worker.profile}</span>
            <SignalChip tone="neutral" label={taskStatusLabel[worker.task_status] ?? worker.task_status} />
            {runawayBadge}
          </div>
          <h3 className="line-clamp-2 text-base font-semibold leading-snug text-ink">{worker.task_title}</h3>
          {note ? (
            <Text variant="label" className="text-ink-2">
              <span className="mr-1.5 font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3">{de.worker.doingNow}:</span>
              {note}
              {noteAge != null ? <span className="text-ink-3"> · {de.worker.noteAge(fmtDur(noteAge))}</span> : null}
            </Text>
          ) : null}
        </div>
        <SignalChip tone={signalToneFromLegacy(health.tone)} label={health.label} />
      </div>
      )}

      {!expanded ? (
        problemText || runaway.level !== "none" ? (
          <p className="line-clamp-2 text-micro text-status-warn">{problemText ?? runaway.reasons.join(" · ")}</p>
        ) : null
      ) : (
      <>
      {collapsible && note ? (
        <Text variant="label" className="text-ink-2">
          <span className="mr-1.5 font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3">{de.worker.doingNow}:</span>
          {note}
          {noteAge != null ? <span className="text-ink-3"> · {de.worker.noteAge(fmtDur(noteAge))}</span> : null}
        </Text>
      ) : null}
      <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
        <Stat label={de.worker.runtime} value={fmtDur(runtime)} />
        <Stat label={de.worker.heartbeat} value={hasHeartbeat ? fmtDur(heartbeatAge) : "—"} tone={hasHeartbeat && heartbeatAge > STUCK_HEARTBEAT_S ? "warn" : undefined} />
        <Stat label={de.worker.remaining} value={remaining <= 0 ? "0s" : fmtDur(remaining)} tone={remaining <= 0 ? "warn" : undefined} />
        <Stat label="PID" value={worker.worker_pid ? String(worker.worker_pid) : "—"} />
      </div>

      {/* Phase B: ehrliche Zeit-Achse — ersetzt den saturierenden ETA/Budget-Balken. */}
      <div className="space-y-1.5">
        {/* Zustandswort */}
        <div className="flex items-center gap-2">
          <SignalChip tone={signalToneFromLegacy(axisState.tone)} label={axisState.label} />
          <span className="font-data text-micro text-ink-3">{fmtDur(runtime)}</span>
          {budget > 0 ? <span className="text-micro text-ink-3">/ {fmtDur(budget)}</span> : null}
        </div>
        {/* Zeit-Achsen-Track */}
        {runtimeValid ? (
          <TimeAxisTrack
            elapsed={runtime}
            p50={etaP50}
            p90={etaP90}
            budget={budget}
            scaleMax={axisScaleMax}
          />
        ) : (
          <Text variant="label" className="text-status-warn">Zeitachse nicht verfügbar: Start-Zeitstempel ungültig.</Text>
        )}
        {/* Kein ETA-Hinweis */}
        {axisState.noEta ? (
          <Text variant="label" className="text-ink-3">{de.worker.timeAxisNoEta}</Text>
        ) : null}
      </div>

      {/* Phase B: Telemetrie-Chips (Modell, Schritt, Tokens). */}
      <div className="flex flex-wrap gap-2">
        {/* Modell-Chip */}
        {effectiveModel ? (
          <div className="inline-flex items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-0.5 text-xs">
            <span className="text-ink-3">{de.worker.chipModel}:</span>
            <span className="font-data text-ink">{effectiveModel}</span>
            {modelOverride == null ? <span className="text-ink-3">{de.worker.chipModelFromLane}</span> : null}
          </div>
        ) : null}
        {/* Schritt-Chip */}
        {stepKey ? (
          <div className="inline-flex items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-0.5 text-xs">
            <span className="text-ink-3">{de.worker.chipStep}:</span>
            <span className="font-data text-ink">{stepKey}</span>
          </div>
        ) : null}
        {/* Token-Chips */}
        {hasLiveTokens ? (
          <>
            <div className="inline-flex items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-0.5 text-xs" title={de.worker.chipTokensIn}>
              <span className="text-ink-3">↑</span>
              <span className="font-data text-ink">{inputTokens != null ? fmtTokens(inputTokens) : de.worker.chipNoValue}</span>
            </div>
            <div className="inline-flex items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-0.5 text-xs" title={de.worker.chipTokensOut}>
              <span className="text-ink-3">↓</span>
              <span className="font-data text-ink">{outputTokens != null ? fmtTokens(outputTokens) : de.worker.chipNoValue}</span>
            </div>
          </>
        ) : (
          <div className="inline-flex items-center gap-1 rounded-card border border-line bg-surface-2 px-2 py-0.5 text-xs text-ink-3" title={de.worker.chipTokensNoLiveTitle}>
            {de.worker.chipTokensNoLive}
          </div>
        )}
      </div>

      {inspect ? (
        inspect.alive ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <div className="flex items-center justify-between text-xs text-ink-2"><span>CPU</span><span className="inline-flex items-center gap-1"><span className="font-data">{Math.round(inspect.cpu_percent)}%</span>{inspect.cpu_percent > 80 ? <TriangleAlert aria-label="hoch" className="h-3 w-3 shrink-0 text-status-warn" /> : null}</span></div>
              <div className="h-1.5 overflow-hidden rounded-full bg-line"><div className={cn("h-full rounded-full", inspect.cpu_percent > 80 ? "bg-status-warn" : "bg-live")} style={{ width: `${Math.min(100, inspect.cpu_percent)}%` }} /></div>
            </div>
            <div className="space-y-1">
              <div className="flex items-center justify-between text-xs text-ink-2"><span>RAM</span><span className="inline-flex items-center gap-1"><span className="font-data">{fmtMB(inspect.rss)}</span>{inspect.rss > 1536 * 1048576 ? <TriangleAlert aria-label="hoch" className="h-3 w-3 shrink-0 text-status-warn" /> : null}</span></div>
              <div className="h-1.5 overflow-hidden rounded-full bg-line"><div className={cn("h-full rounded-full", inspect.rss > 1536 * 1048576 ? "bg-status-warn" : "bg-live")} style={{ width: `${Math.min(100, (inspect.rss / (2048 * 1048576)) * 100)}%` }} /></div>
            </div>
            <Text variant="label" className="text-ink-2 sm:col-span-2">{de.worker.process}: {inspect.status} · {fmtMB(inspect.rss)} · {inspect.num_threads} Threads · {inspect.num_fds} FDs</Text>
          </div>
        ) : (
          // Kein greifbarer Prozess (z.B. claude-cli ohne erfasste PID):
          // ehrliche Begründung statt irreführender 0-Meter.
          <Text variant="label" className="text-ink-2">{de.worker.inspectUnavailable}{inspect.reason ? ` — ${inspect.reason}` : ""}</Text>
        )
      ) : null}

      {runaway.level !== "none" ? <WorkerCallout tone={runaway.level === "critical" ? "alert" : "warn"}>{runaway.reasons.join(" · ")}</WorkerCallout> : null}
      {problemText ? <WorkerCallout tone={signalToneFromLegacy(health.tone) === "alert" ? "alert" : "warn"}>{problemText}</WorkerCallout> : null}

      {/* F2: Burn-Wächter-Chip — nur aus echten Zahlen. Ohne Live-Tokens (Normalfall
          für laufende Worker) wird KEIN leerer Chip gezeigt — der Token-Hinweis oben
          sagt bereits, dass Zahlen erst nach Abschluss kommen. */}
      <div className="flex flex-wrap gap-2">
        {burn.noData ? null : burn.ratePerMin != null ? (
          <SignalChip
            tone={burnTone === "red" ? "alert" : burnTone === "amber" ? "warn" : "neutral"}
            label={`${de.worker.chipBurnRate(Math.round(burn.ratePerMin).toLocaleString("de-DE"))}${burn.projectedTotal != null ? ` · ${de.worker.chipBurnProjected(fmtTokens(Math.round(burn.projectedTotal)))}` : ""}`}
          />
        ) : null}
      </div>

      {/* F1: Aktivitäts-Timeline (Heartbeat-Notizen). Poll NUR wenn expandiert. */}
      <ActivityTimeline taskId={expanded ? worker.task_id : null} now={now} />

      {confirming ? (
        <div className="space-y-2">
          {/* F3: Lane-Auswahl beim Neu-Starten (optional). */}
          {confirming === "restart" ? (
            <div className="flex items-center gap-2">
              <Text variant="label" className="shrink-0 text-ink-2">{de.worker.restartLaneLabel}:</Text>
              <select
                value={restartLane}
                onChange={(e) => setRestartLane(e.target.value)}
                className="rounded-card border border-line bg-surface-1 px-2 py-0.5 text-xs text-ink focus:outline-none"
              >
                <option value="">{de.worker.restartLaneNone}</option>
                {WORKER_LANE_OPTIONS.map((lane) => (
                  <option key={lane} value={lane}>{lane}</option>
                ))}
              </select>
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              disabled={actionBusy}
              onClick={async () => {
                if (onAction) {
                  const extra = confirming === "restart" && restartLane
                    ? { assignee: restartLane }
                    : undefined;
                  await onAction(worker.run_id, confirming, extra);
                }
                setConfirming(null);
                setRestartLane("");
              }}
              prefix={actionBusy ? <Spinner /> : actionIcon(confirming)}
            >
              {de.worker.actions[confirming]} · {de.worker.actions.confirm}
            </Button>
            <Button outlined size="sm" disabled={actionBusy} onClick={() => { setConfirming(null); setRestartLane(""); }}>
              {de.worker.actions.cancel}
            </Button>
          </div>
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
            <div key={key} className="flex flex-col items-start gap-0.5">
              <Button
                outlined
                size="sm"
                disabled={actionBusy}
                onClick={() => setConfirming(key)}
                prefix={actionBusy ? <Spinner /> : actionIcon(key)}
                className={cn(
                  key === primary && "border-live/32",
                )}
              >
                {de.worker.actions[key]}
              </Button>
              <span className="px-1 text-[0.6rem] text-ink-3">{ACTION_EFFECT[key]}</span>
            </div>
          )) : null}
        </div>
      )}
      {confirming ? (
        <Text variant="label" className="text-ink-2">
          {confirming === "terminate" ? de.worker.terminateHint
            : confirming === "restart" ? de.worker.restartHint
            : confirming === "hold" ? de.worker.holdHint
            : de.worker.confirmHint}
        </Text>
      ) : null}
      {logOpen ? <WorkerLogTail taskId={worker.task_id} /> : null}
      </>
      )}
    </article>
  );
}
