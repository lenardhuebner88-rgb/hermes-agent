/**
 * Worker-Subtab — V2-Redesign (Mockup 2026-07-27, abgenommen).
 *
 * Der Tab ist ein Kontrollraum: eine Puls-Strip-Kopfzeile (Slots/Queue,
 * heute fertig, ehrliche Token-Kachel), darunter ein Grid von Worker-Karten
 * (Elapsed-Ring gegen das p90-Fenster, p50-Marke, Heartbeat-Sparkline,
 * Liveness-Orb, aktuelle Activity-Note) und ein deduplizierter Live-Ereignis-
 * Ticker. Ohne Worker: freie Slot-Lanes mit Queue-Vorschau + der Ticker als
 * Verlaufsspur — nie ein schwarzes Loch. Tap auf eine Karte öffnet den
 * Fokus-Drawer (Band, Claim/Worktree/Branch/Retries/Tier, Run-Timeline,
 * deduplizierte Notiz-Historie, Outcome nach Run-Ende, „Andere Lanes").
 *
 * Referenz: /home/piet/.hermes/briefs/2026-07-27-worker-tab-mockup/mockup-v2.html
 */
import { useEffect, useState } from "react";
import {
  claimCountdownSeconds,
  dedupeConsecutive,
  derivePulse,
  eventKindLabel,
  fmtClockTime,
  fmtDurationClock,
  fmtSeconds,
  fmtTokens,
  fmtUsd,
  heartbeatAge,
  isPinnedEventKind,
  profileColorClass,
  profileInitial,
  premiumLaneMarker,
  workerHasLiveTokenSample,
  workerLivenessTone,
} from "../../lib/fleetHub";
import { de } from "../../i18n/de";
import type { Worker, BoardResponse, BoardTask } from "../../lib/types";
import type { ReliabilityResponse, RunTimelineItem } from "../../lib/schemas";
import { Overlay } from "../../components/Overlay";
import { WorkerLogTail } from "../../components/WorkerCard";
import { useWorkerLifecycle } from "../../hooks/taskActions";
import { useWorkerActivity, useRunLiveEvents, useWorkerTaskDetail, useRunDetail, useRunTimeline } from "../../hooks/workersBoard";
import { WorkerBand } from "./WorkerBand";
import { WorkerCard, LivenessOrb, livenessLabel } from "./WorkerCard";
import { FreeSlotLane, MiniLane, nextQueueTasks } from "./SlotLane";
import { LiveTicker } from "./LiveTicker";
import { PulseStrip } from "./PulseStrip";
import { BoardBadge } from "../../components/fleet/BoardIdentity";
import { ModelRouteBadge } from "../../components/fleet/ModelRouteBadge";
import { FleetSourceFreshness } from "./FleetSourceFreshness";
import { elapsedSeconds } from "../../lib/derive";

// ─── Worker-Subtab ────────────────────────────────────────────────────────────

interface WorkerTabProps {
  activeWorkers: Worker[];
  board: BoardResponse | null;
  reliability: ReliabilityResponse | null;
  now: number;
  initialOpen: Worker | null;
  onOpenChain: (rootId: string) => void;
  /** F4: Live-Concurrency-Cap (kanban.max_in_progress) für den Pulse-Strip. */
  cap?: number | null;
  /** Heute abgeschlossene Läufe (costs.today.runs) für den Pulse-Strip. */
  doneToday?: number | null;
  /** Token-Summe (ein+aus) abgeschlossener Runs heute (costs.today) — Sub-Zeile
   * der Token-Kachel, damit „0 live" nicht als „0 verbraucht" misverstanden wird. */
  tokensToday?: number | null;
  /** Nach einer Lifecycle-Aktion (Nudge/Hold/Restart/Terminate) sofort neu
   * pollen statt bis zum nächsten 5-s-Takt zu warten. */
  onWorkersChanged?: () => void;
  currentBoard?: string;
  eventBoards?: string[];
}

interface WorkerSelection {
  key: string;
  snapshot: Worker;
}

function workerIdentity(worker: Worker): string {
  return `${worker.board_slug ?? "current"}:${worker.task_id}`;
}

// Wie viele freie Slot-Lanes der Leerzustand zeigt (Cap, sonst 3; gedeckelt bei
// 5, damit ein hoher Cap den Tab nicht mit Leer-Lanes flutet).
function freeSlotCount(cap: number | null | undefined): number {
  const base = cap && cap >= 1 ? cap : 3;
  return Math.max(1, Math.min(base, 5));
}

export function WorkerTab({
  activeWorkers,
  board,
  reliability,
  now,
  initialOpen,
  onOpenChain,
  cap = null,
  doneToday = null,
  tokensToday = null,
  onWorkersChanged,
  currentBoard = "default",
  eventBoards = [currentBoard],
}: WorkerTabProps) {
  const [selection, setSelection] = useState<WorkerSelection | null>(
    () => (initialOpen ? { key: workerIdentity(initialOpen), snapshot: initialOpen } : null),
  );
  const liveEvents = useRunLiveEvents(true, 40, eventBoards);

  const selectedWorker = selection ? activeWorkers.find((w) => workerIdentity(w) === selection.key) ?? null : null;
  const drawerWorker = selectedWorker ?? selection?.snapshot ?? null;

  // Pulse-Ableitung: queue = ready+scheduled, blocked = blocked-Spalte.
  const columns = board?.columns ?? [];
  const queue = columns
    .filter((c) => c.name === "ready" || c.name === "scheduled")
    .reduce((n, c) => n + c.tasks.length, 0);
  const blocked = (columns.find((c) => c.name === "blocked")?.tasks ?? []).length;
  const pulse = derivePulse({ activeWorkers, cap, queue, doneToday, blocked });

  // Queue-Vorschau für freie Slots + Branch-Lookup für die Karten (aktuelles Board).
  const queuePreview = nextQueueTasks(columns, 2);
  const branchByTaskId = new Map<string, string>(
    columns.flatMap((c) => c.tasks).flatMap((t) => (t.branch_name ? [[t.id, t.branch_name] as const] : [])),
  );

  const otherWorkers = drawerWorker
    ? activeWorkers.filter((w) => workerIdentity(w) !== workerIdentity(drawerWorker))
    : [];

  const select = (w: Worker) => setSelection({ key: workerIdentity(w), snapshot: w });

  const drawer = drawerWorker ? (
    <WorkerDrawer
      worker={drawerWorker}
      active={selectedWorker != null}
      board={board}
      reliability={reliability}
      now={now}
      otherWorkers={otherWorkers}
      onOpenWorker={select}
      onClose={() => setSelection(null)}
      onOpenChain={onOpenChain}
      onWorkersChanged={onWorkersChanged}
      currentBoard={currentBoard}
    />
  ) : null;

  return (
    <div className="fleet-worker-tab">
      <FleetSourceFreshness sources={[{ label: de.fleet.liveEventsSource, ...liveEvents }]} />
      <PulseStrip pulse={pulse} tokensToday={tokensToday} />

      {activeWorkers.length > 0 ? (
        <>
          <div className="fleet-wgrid">
            {activeWorkers.map((w) => (
              <WorkerCard
                key={workerIdentity(w)}
                worker={w}
                now={now}
                branchName={branchByTaskId.get(w.task_id) ?? null}
                onOpen={() => select(w)}
              />
            ))}
          </div>
          <LiveTicker
            events={liveEvents.events}
            title={de.fleet.tickerLive}
            loading={liveEvents.loading}
            emptyLabel={de.fleet.tickerEmpty}
          />
        </>
      ) : (
        <>
          <div className="fleet-laneswrap">
            {Array.from({ length: freeSlotCount(cap) }, (_, i) => (
              <FreeSlotLane
                key={`free-${i}`}
                index={i + 1}
                label={i === 0 && queue > 0 ? de.fleet.slotFreeWaiting : de.fleet.slotFree}
                queuePreview={i === 0 ? queuePreview : { ready: [], scheduled: [] }}
              />
            ))}
          </div>
          <LiveTicker
            events={liveEvents.events}
            title={de.fleet.tickerHistory}
            loading={liveEvents.loading}
            emptyLabel={de.fleet.tickerEmpty}
          />
        </>
      )}

      {drawer}
    </div>
  );
}

// ─── Worker-Lifecycle-Steuerung (Gap 1) ──────────────────────────────────────
// Nur Nudge feuert direkt (Kommentar am Task, kein Kill — plugin_api.py). Alle
// anderen Aktionen nehmen den Worker-Prozess weg (unlock/hold/restart/terminate
// laufen über reclaim_task bzw. hold_task) und sind deshalb zwei-Klick-scharf
// wie FleetTaskActions (fleet-ta-btn-Hausmuster, TaskActions.tsx). Fehler
// werden wörtlich gezeigt (AC-2 — nie verschlucken); Erfolg zeigt den deutschen
// detail-Einzeiler des Backends. Nach JEDER erfolgreichen Aktion wird der
// Worker-Poll sofort erneuert (onActionDone), nicht erst beim nächsten Takt.

type ArmableWorkerAction = "unlock" | "hold" | "restart" | "terminate";

const ARMED_META: Record<ArmableWorkerAction, { label: () => string; confirm: () => string }> = {
  unlock: { label: () => de.fleet.workerUnlock, confirm: () => de.fleet.workerUnlockConfirm },
  hold: { label: () => de.fleet.workerHold, confirm: () => de.fleet.workerHoldConfirm },
  restart: { label: () => de.fleet.workerRestart, confirm: () => de.fleet.workerRestartConfirm },
  terminate: { label: () => de.fleet.workerTerminate, confirm: () => de.fleet.workerTerminateConfirm },
};

export function WorkerLifecycleActions({ runId, onActionDone }: { runId: string; onActionDone?: () => void }) {
  const { busyId, errorById, run, terminate, clearError } = useWorkerLifecycle();
  const [armed, setArmed] = useState<ArmableWorkerAction | null>(null);
  const [note, setNote] = useState("");

  const busy = busyId === runId;
  const error = errorById[runId] || "";

  const fireNudge = async () => {
    clearError(runId);
    setNote("");
    const res = await run(runId, "nudge");
    if (res.ok) {
      setNote(res.detail || "");
      onActionDone?.();
    }
  };

  const fireArmed = async () => {
    const action = armed;
    setArmed(null);
    if (!action) return;
    setNote("");
    const res = action === "terminate" ? await terminate(runId) : await run(runId, action);
    if (res.ok) {
      if ("detail" in res && res.detail) setNote(res.detail);
      onActionDone?.();
    }
  };

  const arm = (action: ArmableWorkerAction) => {
    clearError(runId);
    setNote("");
    setArmed(action);
  };

  return (
    <div className="fleet-task-actions">
      {armed ? (
        <div className="fleet-ta-confirm">
          <span className="fleet-ta-confirm-text">{ARMED_META[armed].confirm()}</span>
          <button
            type="button"
            className="fleet-ta-btn"
            style={{ color: "var(--fleet-rot)", borderColor: "rgba(255,93,115,.5)" }}
            disabled={busy}
            onClick={() => void fireArmed()}
          >
            {busy ? de.fleet.workerActionBusy : de.fleet.actionConfirm}
          </button>
          <button type="button" className="fleet-ta-btn" disabled={busy} onClick={() => setArmed(null)}>
            {de.fleet.actionDismiss}
          </button>
        </div>
      ) : (
        <div className="fleet-ta-row">
          <button
            type="button"
            className="fleet-ta-btn"
            style={{ color: "var(--fleet-puls)", borderColor: "rgba(55,224,255,.35)" }}
            disabled={busy}
            onClick={() => void fireNudge()}
          >
            {busy ? de.fleet.workerActionBusy : de.fleet.workerNudge}
          </button>
          {(["unlock", "hold", "restart", "terminate"] as ArmableWorkerAction[]).map((action) => (
            <button
              key={action}
              type="button"
              className="fleet-ta-btn"
              style={
                action === "terminate"
                  ? { color: "var(--fleet-rot)", borderColor: "rgba(255,93,115,.4)" }
                  : { color: "var(--fleet-puls)", borderColor: "rgba(55,224,255,.35)" }
              }
              disabled={busy}
              onClick={() => arm(action)}
            >
              {ARMED_META[action].label()}
            </button>
          ))}
        </div>
      )}
      {error ? <p className="fleet-ta-error" role="alert">{error}</p> : null}
      {!error && note ? <p className="fleet-ta-note">{note}</p> : null}
    </div>
  );
}

// ─── Copy-Button (Branch/Worktree) ────────────────────────────────────────────
// F6: navigator.clipboard fehlt auf non-secure Origins (http://<LAN-IP>:9119) —
// dann execCommand-Fallback über ein temporäres textarea; schlägt auch der fehl,
// sagt der Button es sichtbar statt stumm zu no-open.

function legacyCopy(text: string): boolean {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

function CopyValueButton({ value, what }: { value: string; what: string }) {
  // "idle" | "copied" | "unavailable"
  const [state, setState] = useState<"idle" | "copied" | "unavailable">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const t = window.setTimeout(() => setState("idle"), 2000);
    return () => window.clearTimeout(t);
  }, [state]);
  return (
    <button
      type="button"
      className="fleet-copy"
      aria-label={de.fleet.copyAriaLabel(what)}
      title={state === "unavailable" ? de.fleet.copyUnavailable : undefined}
      onClick={() => {
        if (navigator.clipboard) {
          navigator.clipboard.writeText(value).then(
            () => setState("copied"),
            () => setState(legacyCopy(value) ? "copied" : "unavailable"),
          );
        } else {
          setState(legacyCopy(value) ? "copied" : "unavailable");
        }
      }}
    >
      {state === "copied" ? de.fleet.copiedValue : state === "unavailable" ? de.fleet.copyUnavailable : de.fleet.copyValue}
    </button>
  );
}

// ─── Notiz-Historie (V2: dedupliziert, Kinds sichtbar) ────────────────────────
// Per-Worker Event-Verlauf aus GET /tasks/{task_id}/activity — nur wenn der
// Drawer offen und der Worker aktiv ist (der Hook pausiert bei null-taskId).
// Aufeinanderfolgende identische Notizen fallen zu einer Zeile mit ×N-Badge
// zusammen; Sonder-Kinds (claimed, blocked, model_* …) bleiben mit Marker
// eigenständig statt wie bisher clientseitig weggefiltert zu werden.

function WorkerNotesHistory({ taskId, board }: { taskId: string; board: string | null }) {
  const activity = useWorkerActivity(taskId, board);
  const events = activity.data?.events ?? [];
  const rows = dedupeConsecutive(
    events,
    (e) => `${e.kind}:${(e.note ?? "").trim()}`,
    (e) => !isPinnedEventKind(e.kind) && (e.note ?? "").trim().length > 0,
  );

  return (
    <div className="fleet-fx-notes-wrap">
      <div className="fleet-fx-notes-head">{de.fleet.drawerNotes}</div>
      {rows.length === 0 ? (
        <div className="fleet-fx-note fleet-fx-note-empty">
          {activity.loading ? de.fleet.notesLoading : de.fleet.drawerNotesEmpty}
        </div>
      ) : (
        <div className="fleet-fx-notes">
          {rows.map(({ item: e, count }, i) => {
            const pinned = isPinnedEventKind(e.kind);
            const text = (e.note ?? "").trim() || eventKindLabel(e.kind, de.fleet.eventKindLabels);
            return (
              <div key={e.id} className="fleet-fx-note">
                <span className="fleet-fx-note-ts">{fmtClockTime(e.at)}</span>{" "}
                {pinned ? <span className="fleet-kind">{eventKindLabel(e.kind, de.fleet.eventKindLabels)}</span> : null}
                <span className={i === 0 ? "fleet-fx-note-cur" : undefined}>
                  {i === 0 ? "▸ " : ""}
                  {text}
                </span>
                {count > 1 ? <span className="fleet-xbadge">×{count}</span> : null}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─── Run-Timeline (Drawer) ────────────────────────────────────────────────────
// GET /runs/{run_id}/timeline — flache, zeitsortierte Ereignisliste mit
// offset/delta. Zeigt die jüngsten 12 Einträge chronologisch; bei laufendem
// Run schließt eine „jetzt"-Marke ab.

const TIMELINE_DISPLAY_LIMIT = 12;

function timelineItemText(item: RunTimelineItem): string {
  const label = eventKindLabel(item.kind, de.fleet.eventKindLabels);
  const payload = item.payload;
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    const note = (payload as Record<string, unknown>).note;
    if (typeof note === "string" && note.trim()) return `${label} — ${note.trim()}`;
  }
  return label;
}

function WorkerRunTimeline({ runId, board, active, now }: { runId: string; board: string | null; active: boolean; now: number }) {
  const timeline = useRunTimeline(runId, board);
  const data = timeline.data;
  const items = data?.items ?? [];
  const shown = items.slice(-TIMELINE_DISPLAY_LIMIT);
  const hiddenCount = items.length - shown.length;

  return (
    <div className="fleet-tl2-wrap">
      <div className="fleet-fx-notes-head">{de.fleet.timelineTitle}</div>
      {items.length === 0 ? (
        <div className="fleet-fx-note fleet-fx-note-empty">
          {timeline.loading ? de.fleet.timelineLoading : de.fleet.timelineEmpty}
        </div>
      ) : (
        <div className="fleet-tl2" role="list">
          {hiddenCount > 0 || data?.truncated ? (
            <div className="fleet-tl2-cap">{de.fleet.timelineTruncated(shown.length)}</div>
          ) : null}
          {shown.map((item, i) => (
            <div key={`${item.at}-${i}`} className="fleet-tl2-ev" role="listitem">
              <span className="fleet-tl2-t">{fmtClockTime(item.at)}</span>
              <span className="fleet-tl2-l">{timelineItemText(item)}</span>
            </div>
          ))}
          {active ? (
            <div className="fleet-tl2-ev fleet-tl2-now" role="listitem">
              <span className="fleet-tl2-t">{fmtClockTime(now)}</span>
              <span className="fleet-tl2-l">{de.fleet.timelineNow}</span>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

// ─── Run-Ausgang (Drawer, wenn der Worker nicht mehr läuft) ──────────────────
// Statt nur „Worker beendet" wird GET /runs/{run_id} nachgeladen: outcome,
// ended_at, error, summary, verdict (best-effort aus metadata), Tokens/Kosten.

function WorkerOutcomeBlock({ runId, board }: { runId: string; board: string | null }) {
  const detail = useRunDetail(runId, board);
  const run = detail.data?.run ?? null;

  if (!run) {
    return (
      <div className="fleet-kv" role="status">
        <div className="fleet-kv-k">{de.fleet.workerEndedTitle}</div>
        <div className="fleet-kv-v">
          {de.fleet.workerEndedDesc}
          {detail.loading ? <span className="fleet-dim"> · {de.fleet.outcomeLoading}</span> : null}
        </div>
      </div>
    );
  }

  const verdict = run.metadata && typeof run.metadata.verdict === "string" ? run.metadata.verdict : null;
  return (
    <div className="fleet-outcome" role="status">
      <div className="fleet-fx-notes-head">{de.fleet.outcomeTitle}</div>
      <div className="fleet-grid2">
        <div className="fleet-kv">
          <div className="fleet-kv-k">{de.fleet.outcomeState}</div>
          <div className="fleet-kv-v">
            {run.outcome ?? run.status}
            {run.ended_at ? <span className="fleet-dim"> · {de.fleet.outcomeEndedAt(fmtClockTime(run.ended_at))}</span> : null}
          </div>
        </div>
        <div className="fleet-kv">
          <div className="fleet-kv-k">{de.fleet.drawerTokens}</div>
          <div className="fleet-kv-v">
            {run.input_tokens != null || run.output_tokens != null
              ? `${fmtTokens(run.input_tokens)} → ${fmtTokens(run.output_tokens)}`
              : "—"}
            {run.cost_usd != null && run.cost_usd > 0 ? ` · ${fmtUsd(run.cost_usd)}` : ""}
          </div>
        </div>
        {verdict ? (
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.outcomeVerdict}</div>
            <div className="fleet-kv-v">{verdict}</div>
          </div>
        ) : null}
      </div>
      {run.error ? (
        <div className="fleet-kv">
          <div className="fleet-kv-k">{de.fleet.outcomeError}</div>
          <div className="fleet-kv-v fleet-outcome-err">{run.error}</div>
        </div>
      ) : null}
      {run.summary ? (
        <div className="fleet-kv">
          <div className="fleet-kv-k">{de.fleet.outcomeSummary}</div>
          <div className="fleet-kv-v">{run.summary}</div>
        </div>
      ) : null}
    </div>
  );
}

// ─── Worker-Drawer (Fokus) ────────────────────────────────────────────────────

interface WorkerDrawerProps {
  worker: Worker;
  active: boolean;
  board: BoardResponse | null;
  reliability: ReliabilityResponse | null;
  now: number;
  otherWorkers: Worker[];
  onOpenWorker: (w: Worker) => void;
  onClose: () => void;
  onOpenChain: (rootId: string) => void;
  onWorkersChanged?: () => void;
  currentBoard: string;
}

function WorkerDrawer({
  worker: w,
  active,
  board,
  reliability,
  now,
  otherWorkers,
  onOpenWorker,
  onClose,
  onOpenChain,
  onWorkersChanged,
  currentBoard,
}: WorkerDrawerProps) {
  // F1: live-abgeleitete Werte (elapsed, Heartbeat-Alter, Liveness) rechnen
  // gegen das tickende now — sie sind nur wahr, solange der Worker aktiv ist.
  // Im Snapshot-Modus (!active) werden sie NICHT gerendert (Kopf, Orb, LED,
  // KV-Zeilen), sonst „läuft" ein beendeter Worker im Drawer ewig weiter.
  const elapsedSec = active ? elapsedSeconds(w.started_at, now) ?? Number.NaN : Number.NaN;
  const hbAge = active ? heartbeatAge(w.last_heartbeat_at, now) : null;
  const initial = profileInitial(w.profile);
  const colorCls = profileColorClass(w.profile);
  const workerBoard = w.board_slug ?? currentBoard;
  const foreignBoard = workerBoard !== currentBoard;

  // Drawer-Nachladungen (pausieren über null-Keys, solange der Drawer zu ist —
  // die Komponente existiert nur bei offenem Drawer).
  const taskDetail = useWorkerTaskDetail(w.task_id, workerBoard);
  const detailTask = taskDetail.data?.task ?? null;

  // Profil-Verlässlichkeit aus Reliability-Daten (ReliabilityResponse aus lib/schemas)
  const relProfile = reliability?.profiles?.find((p) => p.profile === w.profile);

  // Ketten-Position: root_id via Board-Lookup (BoardResponse aus lib/types)
  const allBoardTasks: BoardTask[] = (board?.columns ?? []).flatMap((c) => c.tasks);
  const boardTask = foreignBoard ? undefined : allBoardTasks.find((t) => t.id === w.task_id);

  const [logOpen, setLogOpen] = useState(false);
  // root_id ist entweder der eigene Task (Root) oder der Parent-Root
  const chainRootId = boardTask?.root_id ?? null;
  const branchName = detailTask?.branch_name ?? boardTask?.branch_name ?? null;
  const workspacePath = detailTask?.workspace_path ?? null;
  const autoRetries = detailTask?.auto_retry_count ?? null;
  const reviewTier = detailTask?.review_tier ?? boardTask?.review_tier ?? null;
  const chainMembers = chainRootId
    ? allBoardTasks.filter((t) => t.root_id === chainRootId || t.id === chainRootId)
    : [];
  const hasTokenSample = workerHasLiveTokenSample(w);
  const tokenDisplay = hasTokenSample
    ? `${fmtTokens(w.input_tokens)} → ${fmtTokens(w.output_tokens)}`
    : de.worker.tokenNoLiveSample;

  const livenessTone = active ? workerLivenessTone(w, now) : null;
  const claimIn = claimCountdownSeconds(w.claim_expires, now);
  const inspect = active && w.inspect && w.inspect.alive ? w.inspect : null;

  // Drawer via shared Overlay: garantiert zentriert ab sm, Escape-Handling,
  // Scroll-Lock und Portal via document.body — kein eigener portal nötig.
  // data-fleet-theme wird auf das Overlay-Kind gesetzt damit das dark Theme greift.
  return (
    <Overlay onClose={onClose} ariaLabel={de.fleet.drawerAriaLabel(w.profile)} maxWidthClassName="max-w-lg">
      <div data-fleet-theme className="fleet-drawer-inner">
        {/* Grab Handle */}
        <div className="fleet-grab" />

        {/* Header */}
        <div className="fleet-dr-head">
          <div className={`fleet-avatar fleet-avatar-gross ${colorCls}`} {...premiumLaneMarker(w.profile)}>{initial}</div>
          <div className="fleet-dr-title">
            {w.profile}
            <span>{active ? de.fleet.drawerRunningSince(fmtSeconds(elapsedSec), w.task_assignee) : de.fleet.workerEndedTitle}</span>
          </div>
          <BoardBadge slug={w.board_slug} />
          {active ? <LivenessOrb worker={w} now={now} /> : null}
          {hbAge != null ? (
            <div className="fleet-led" style={{ marginLeft: "auto" }}>
              <span className="fleet-led-dot" />
              ♥ {fmtSeconds(hbAge)}
            </div>
          ) : null}
        </div>

        {/* Task: title + task_id + branch_name (Requirement: title + task_id + branch) */}
        <div className="fleet-dr-task">
          {w.task_title}
          <code>{w.task_id}{branchName ? ` · ${branchName}` : ""}{active ? de.fleet.drawerRunSuffix(w.run_id) : ""}</code>
        </div>

        {/* Vergrößertes Zeitachsen-Band (AC-3) — nur solange der Worker läuft. */}
        {active ? <WorkerBand worker={w} now={now} size="big" /> : null}

        {/* KV-Grid */}
        <div className="fleet-grid2">
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.drawerModell}</div>
            <div className="fleet-kv-v">
              <ModelRouteBadge
                requestedProvider={w.requested_provider}
                requestedModel={w.requested_model}
                activeProvider={w.active_provider}
                activeModel={w.active_model}
                modelState={w.model_state}
                modelSource={w.model_source}
                observedAt={w.model_observed_at}
              />
            </div>
          </div>
          {active ? (
            <div className="fleet-kv">
              <div className="fleet-kv-k">{de.fleet.drawerHeartbeat}</div>
              <div className="fleet-kv-v">{hbAge != null ? fmtSeconds(hbAge) : "—"}</div>
            </div>
          ) : null}
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.drawerTokens}</div>
            <div className="fleet-kv-v">{tokenDisplay}</div>
          </div>
          {active ? (
            <div className="fleet-kv">
              <div className="fleet-kv-k">{de.fleet.drawerLaufzeit}</div>
              <div className="fleet-kv-v">{fmtSeconds(elapsedSec)}</div>
            </div>
          ) : null}
          {active ? (
            <div className="fleet-kv">
              <div className="fleet-kv-k">{de.fleet.drawerLiveness}</div>
              <div className="fleet-kv-v">
                {livenessLabel(livenessTone)}
                {w.liveness_reason ? <span className="fleet-dim"> · {w.liveness_reason}</span> : null}
                {w.liveness_observed_at ? (
                  <span className="fleet-dim"> · {de.fleet.livenessObserved(fmtClockTime(w.liveness_observed_at))}</span>
                ) : null}
              </div>
            </div>
          ) : null}
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.drawerClaim}</div>
            <div className="fleet-kv-v">{w.claim_lock || "—"}</div>
          </div>
          {claimIn != null ? (
            <div className="fleet-kv">
              <div className="fleet-kv-k">{de.fleet.drawerClaimExpires}</div>
              <div className="fleet-kv-v">
                {claimIn > 0 ? de.fleet.claimExpiresIn(fmtDurationClock(claimIn)) : de.fleet.claimExpired}
                <span className="fleet-dim"> · {fmtClockTime(w.claim_expires)}</span>
              </div>
            </div>
          ) : null}
          {inspect ? (
            <div className="fleet-kv">
              <div className="fleet-kv-k">{de.fleet.drawerInspect}</div>
              <div className="fleet-kv-v">
                {Math.round(inspect.cpu_percent)} % · {Math.round(inspect.rss / 1_000_000)} MB
              </div>
            </div>
          ) : null}
        </div>

        {/* Worktree / Branch / Retries / Tier (Task-Detail-Nachladung) */}
        {workspacePath || branchName || (autoRetries != null && autoRetries > 0) || reviewTier ? (
          <div className="fleet-grid2">
            {branchName ? (
              <div className="fleet-kv">
                <div className="fleet-kv-k">{de.fleet.drawerBranch}</div>
                <div className="fleet-kv-v">
                  <code className="fleet-mono-wrap">{branchName}</code>
                  <CopyValueButton value={branchName} what={de.fleet.drawerBranch} />
                </div>
              </div>
            ) : null}
            {workspacePath ? (
              <div className="fleet-kv">
                <div className="fleet-kv-k">{de.fleet.drawerWorktree}</div>
                <div className="fleet-kv-v">
                  <code className="fleet-mono-wrap">{workspacePath}</code>
                  <CopyValueButton value={workspacePath} what={de.fleet.drawerWorktree} />
                </div>
              </div>
            ) : null}
            {autoRetries != null && autoRetries > 0 ? (
              <div className="fleet-kv">
                <div className="fleet-kv-k">{de.fleet.drawerAutoRetries}</div>
                <div className="fleet-kv-v">{autoRetries}</div>
              </div>
            ) : null}
            {reviewTier ? (
              <div className="fleet-kv">
                <div className="fleet-kv-k">{de.fleet.drawerReviewTier}</div>
                <div className="fleet-kv-v">{reviewTier}</div>
              </div>
            ) : null}
          </div>
        ) : null}

        {/* Verlässlichkeit */}
        {relProfile ? (
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.drawerReliability}</div>
            <div className="fleet-kv-v" style={{ fontFamily: "var(--hc-font-mono)", fontSize: 11.5 }}>
              {relProfile.completed_rate != null
                ? de.fleet.reliabilityCompleted(Math.round(relProfile.completed_rate * 100))
                : "—"}
              {relProfile.retries > 0 ? de.fleet.reliabilityRetries(relProfile.retries) : ""}
            </div>
          </div>
        ) : null}

        {/* Ketten-Position */}
        {chainMembers.length > 0 ? (
          <div>
            {chainMembers.slice(0, 4).map((t) => {
              const isActive = t.id === w.task_id;
              const isDone = t.status === "done";
              return (
                <div key={t.id} className="fleet-mini">
                  <span
                    className={`fleet-mini-dot ${isActive ? "fleet-mini-dot-lauf" : isDone ? "fleet-mini-dot-done" : "fleet-mini-dot-offen"}`}
                  />
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {t.title}
                  </span>
                  <code>{isActive ? de.fleet.chainStateRunning : isDone ? de.fleet.chainStateDone : de.fleet.chainStateWaiting}</code>
                </div>
              );
            })}
          </div>
        ) : null}

        {/* Run-Timeline — solange der Run bekannt ist (aktiv oder beendet). */}
        <WorkerRunTimeline runId={w.run_id} board={workerBoard} active={active} now={now} />

        {/* Notiz-Historie (dedupliziert) — nur bei laufendem Worker. */}
        {active ? <WorkerNotesHistory taskId={w.task_id} board={workerBoard} /> : null}

        {/* Outcome nach Run-Ende statt bloßem „Worker beendet". */}
        {active ? null : <WorkerOutcomeBlock runId={w.run_id} board={workerBoard} />}

        {/* Worker-Steuerung: Unlock/Nudge/Restart/Terminate (Gap 1) */}
        {active && !foreignBoard ? <WorkerLifecycleActions runId={w.run_id} onActionDone={onWorkersChanged} /> : null}
        {active && foreignBoard ? (
          <p className="fleet-plan-hint">{de.fleet.foreignBoardHint}</p>
        ) : null}

        {/* Action-Buttons */}
        <div className="fleet-actions">
          {chainRootId && !foreignBoard ? (
            <button
              type="button"
              className="fleet-btn fleet-btn-primar"
              onClick={() => onOpenChain(chainRootId)}
            >
              {de.fleet.drawerKetteOeffnen}
            </button>
          ) : null}
          {active ? (
            <button
              type="button"
              className="fleet-btn"
              onClick={() => setLogOpen((v) => !v)}
            >
              {logOpen ? de.worker.logHide : de.fleet.drawerLog}
            </button>
          ) : null}
          <button type="button" className="fleet-btn" onClick={onClose}>
            {de.fleet.drawerSchliessen}
          </button>
        </div>

        {/* Log-Tail (nur bei offenem Drawer pollend, wie WorkerCard/NodeDetailDrawer) */}
        {active && logOpen ? <WorkerLogTail taskId={w.task_id} board={workerBoard} /> : null}

        {/* Andere Lanes: schneller Sprung zu den übrigen aktiven Workern. */}
        {otherWorkers.length > 0 ? (
          <div className="fleet-fx-other">
            <div className="fleet-sec">{de.fleet.drawerOtherLanes}</div>
            {otherWorkers.map((ow) => (
              <MiniLane key={workerIdentity(ow)} worker={ow} now={now} onOpen={() => onOpenWorker(ow)} />
            ))}
          </div>
        ) : null}
      </div>
    </Overlay>
  );
}
