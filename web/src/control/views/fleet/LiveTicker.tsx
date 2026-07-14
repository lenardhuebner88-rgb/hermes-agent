/**
 * LiveTicker — der Cross-Worker-Ereignis-Ticker unter den Swimlanes. Streamt
 * die Heartbeat-Notizen / Statuswechsel aus /runs/live-events wie eine Konsole:
 * neueste Zeile oben, Zeitstempel + getönter Rollenname + formatierter Text.
 * Trägt auch ohne aktive Worker die Verlaufsspur — kein schwarzes Loch (AC-2).
 *
 * Rein präsentational: Events + Loading kommen vom useRunLiveEvents-Hook,
 * Formatierung aus formatLiveEvent (lib/fleetHub). Farben nur via fleet.css.
 */
import { formatLiveEvent, laneTint, fmtClockTime } from "../../lib/fleetHub";
import type { LiveEvent } from "../../lib/types";
import { BoardBadge } from "../../components/fleet/BoardIdentity";

export function LiveTicker({
  events,
  title,
  loading,
  emptyLabel,
}: {
  events: LiveEvent[];
  title: string;
  loading: boolean;
  emptyLabel: string;
}) {
  return (
    <section className="fleet-ticker" aria-label={title}>
      <div className="fleet-ticker-head">
        <span className="fleet-ticker-dot" aria-hidden="true" />
        {title}
      </div>
      <div className="fleet-ticker-body">
        {events.length === 0 ? (
          <div className="fleet-ticker-empty">{loading ? "Lädt Ereignisse …" : emptyLabel}</div>
        ) : (
          events.map((e, i) => {
            const f = formatLiveEvent(e);
            const tint = laneTint(e.profile);
            return (
              <div key={`${e.board_slug ?? "current"}:${e.id}`} className={`fleet-tl${i === 0 ? " fleet-tl-new" : ""}`}>
                <span className="fleet-tl-ts">{fmtClockTime(e.at)}</span>{" "}
                <BoardBadge slug={e.board_slug} />{" "}
                <span className={`fleet-tl-who fleet-tl-who-${tint}`}>{e.profile ?? "—"}</span>{" "}
                <span className={f.tone !== "none" ? `fleet-tl-${f.tone}` : undefined}>
                  {f.mark ? `${f.mark} ` : ""}
                  {f.text}
                </span>
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}
