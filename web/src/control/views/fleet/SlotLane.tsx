/**
 * SlotLane — eine Swimlane des Puls-Leitstands: der Rollen-Avatar (getönt nach
 * laneTint) links, darüber der Task-Titel + Heartbeat-Alter, darunter das
 * WorkerBand gegen die Zeitachse. Tap öffnet den Fokus-Drawer.
 *
 * FreeSlotLane rendert einen freien Slot (nummerierter Avatar + gestricheltes
 * „Slot frei"-Band) für den Leerzustand. MiniLane ist die kompakte Variante der
 * „Andere Lanes"-Leiste im Drawer.
 *
 * Rein präsentational; alle Farben aus fleet.css-Klassen.
 */
import { WorkerBand } from "./WorkerBand";
import { heartbeatAge, fmtSeconds, laneTint, profileInitial } from "../../lib/fleetHub";
import type { Worker } from "../../lib/types";
import { BoardBadge } from "../../components/fleet/BoardIdentity";

export function SlotLane({ worker, now, onOpen }: { worker: Worker; now: number; onOpen: () => void }) {
  const hbAge = heartbeatAge(worker.last_heartbeat_at, now);
  const tint = laneTint(worker.profile);
  return (
    <button
      type="button"
      className="fleet-lane fleet-lane-btn"
      onClick={onOpen}
      aria-label={`Worker ${worker.profile} öffnen`}
    >
      <div className={`fleet-lav fleet-lav-${tint}`}>
        <span className="fleet-lav-a">{profileInitial(worker.profile)}</span>
        <span className="fleet-lav-n" title={worker.profile}>{worker.profile}</span>
      </div>
      <div className="fleet-lbody">
        <div className="fleet-lhead">
          <span className="fleet-lhead-t" title={worker.task_title}>{worker.task_title}</span>
          <BoardBadge slug={worker.board_slug} />
          {hbAge != null ? <span className="fleet-lhead-hb">♥ {fmtSeconds(hbAge)}</span> : null}
        </div>
        <WorkerBand worker={worker} now={now} size="lane" />
      </div>
    </button>
  );
}

export function FreeSlotLane({ index, label }: { index: number; label: string }) {
  return (
    <div className="fleet-lane">
      <div className="fleet-lav fleet-lav-free">
        <span className="fleet-lav-a">{index}</span>
      </div>
      <div className="fleet-lbody">
        <div className="fleet-band fleet-band-free">
          <span className="fleet-band-free-label">{label}</span>
        </div>
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
