/**
 * Slot-Lanes des Worker-Tabs (V2, 2026-07-27).
 *
 * Die aktive Swimlane (SlotLane) ist durch die V2-Worker-Karte (WorkerCard.tsx,
 * Elapsed-Ring + Sparkline) ersetzt. Übrig bleiben:
 *
 * - FreeSlotLane: freier Slot im Leerzustand. Zeigt statt nur „wartet auf
 *   Dispatch" die nächsten 1–2 wartenden Tasks aus dem Board-Payload
 *   (Queue-Vorschau), damit der Operator sieht, was als Nächstes startet.
 * - MiniLane: die kompakte „Andere Lanes"-Variante im Fokus-Drawer.
 *
 * Rein präsentational; alle Farben aus fleet.css-Klassen.
 */
import { WorkerBand } from "./WorkerBand";
import { laneTint, profileInitial } from "../../lib/fleetHub";
import type { BoardTask, Worker } from "../../lib/types";
import { de } from "../../i18n/de";
import { BoardBadge } from "../../components/fleet/BoardIdentity";

export interface QueuePreviewTask {
  id: string;
  title: string;
  assignee: string | null;
  priority: number;
}

export interface QueuePreview {
  /** Dispatchen-bare Tasks (status=ready) — Sortierung wie der Dispatcher
   *  (priority DESC, created_at ASC, kanban_db.py dispatch_once ready-pull). */
  ready: QueuePreviewTask[];
  /** Geparkte/terminierte Tasks (status=scheduled) — der Dispatcher zieht sie
   *  NICHT (F2); sie werden separat beschriftet, nie als „Nächste in Queue". */
  scheduled: QueuePreviewTask[];
}

function toPreviewTask(t: BoardTask): QueuePreviewTask {
  return { id: t.id, title: t.title, assignee: t.assignee, priority: t.priority };
}

function byDispatcherOrder(a: BoardTask, b: BoardTask): number {
  return b.priority - a.priority || a.created_at - b.created_at;
}

/**
 * nextQueueTasks: Queue-Vorschau für freie Slots. Der Dispatcher zieht
 * ausschließlich status='ready' — deshalb landet nur ready in der Hauptliste;
 * scheduled kommt als separat beschriftete Zeile daneben.
 */
export function nextQueueTasks(columns: Array<{ name: string; tasks: BoardTask[] }>, limit = 2): QueuePreview {
  const ready = (columns.find((c) => c.name === "ready")?.tasks ?? [])
    .slice()
    .sort(byDispatcherOrder)
    .slice(0, Math.max(0, limit))
    .map(toPreviewTask);
  const scheduled = (columns.find((c) => c.name === "scheduled")?.tasks ?? [])
    .slice()
    .sort(byDispatcherOrder)
    .slice(0, Math.max(0, limit))
    .map(toPreviewTask);
  return { ready, scheduled };
}

function QueueTaskRow({ t }: { t: QueuePreviewTask }) {
  return (
    <span className="fleet-qnext-task" title={t.title}>
      <span className="fleet-qnext-title">{t.title}</span>
      {t.assignee ? <span className="fleet-wchip">{t.assignee}</span> : null}
      {t.priority > 0 ? <span className="fleet-wchip fleet-wchip-mono">P{t.priority}</span> : null}
    </span>
  );
}

export function FreeSlotLane({
  index,
  label,
  queuePreview = { ready: [], scheduled: [] },
}: {
  index: number;
  label: string;
  /** Wartende Tasks (Queue-Vorschau) — ready = dispatchbar, scheduled separat. */
  queuePreview?: QueuePreview;
}) {
  return (
    <div className="fleet-lane">
      <div className="fleet-lav fleet-lav-free">
        <span className="fleet-lav-a">{index}</span>
      </div>
      <div className="fleet-lbody">
        <div className="fleet-band fleet-band-free">
          <span className="fleet-band-free-label">{label}</span>
        </div>
        {queuePreview.ready.length > 0 || queuePreview.scheduled.length > 0 ? (
          <div className="fleet-qnext">
            {queuePreview.ready.length > 0 ? (
              <>
                <span className="fleet-qnext-k">{de.fleet.queueNextLabel}</span>
                {queuePreview.ready.map((t) => <QueueTaskRow key={t.id} t={t} />)}
              </>
            ) : null}
            {queuePreview.scheduled.length > 0 ? (
              <>
                <span className="fleet-qnext-k">{de.fleet.queueScheduledLabel}</span>
                {queuePreview.scheduled.map((t) => <QueueTaskRow key={t.id} t={t} />)}
              </>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function MiniLane({ worker, now, onOpen }: { worker: Worker; now: number; onOpen: () => void }) {
  const tint = laneTint(worker.profile);
  return (
    <button
      type="button"
      className="fleet-lane fleet-lane-btn fleet-lane-mini"
      onClick={onOpen}
      aria-label={`Worker ${worker.profile} öffnen`}
    >
      <div className={`fleet-lav fleet-lav-${tint}`}>
        <span className="fleet-lav-a">{profileInitial(worker.profile)}</span>
        <span className="fleet-lav-n" title={worker.profile}>{worker.profile}</span>
      </div>
      <div className="fleet-lbody">
        <BoardBadge slug={worker.board_slug} />
        <WorkerBand worker={worker} now={now} size="mini" />
      </div>
    </button>
  );
}
