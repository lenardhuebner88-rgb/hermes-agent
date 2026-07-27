/**
 * LiveTicker — der Cross-Worker-Ereignis-Ticker unter den Worker-Karten.
 * Streamt die Heartbeat-Notizen / Statuswechsel aus /runs/live-events wie eine
 * Konsole: neueste Zeile oben, Zeitstempel + getönter Rollenname + formatierter
 * Text. Trägt auch ohne aktive Worker die Verlaufsspur — kein schwarzes Loch
 * (AC-2).
 *
 * V2 (2026-07-27): aufeinanderfolgende identische Notizen desselben Workers
 * werden zu einer Zeile mit ×N-Badge dedupliziert (dedupeLiveEvents);
 * Sonder-Kinds (claimed, blocked, completed, model_*, closeout_*, timed_out …)
 * behalten ihren Marker und werden nie wegdedupliziert.
 *
 * Rein präsentational: Events + Loading kommen vom useRunLiveEvents-Hook,
 * Formatierung aus formatLiveEvent (lib/fleetHub). Farben nur via fleet.css.
 */
import { dedupeLiveEvents, formatLiveEvent, laneTint, fmtClockTime } from "../../lib/fleetHub";
import type { LiveEvent } from "../../lib/types";
import { de } from "../../i18n/de";
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
  const rows = dedupeLiveEvents(events);
  return (
    <section className="fleet-ticker" aria-label={title}>
      <div className="fleet-ticker-head">
        <span className="fleet-ticker-dot" aria-hidden="true" />
        {title}
      </div>
      <div className="fleet-ticker-body">
        {rows.length === 0 ? (
          <div className="fleet-ticker-empty">{loading ? de.fleet.tickerLoading : emptyLabel}</div>
        ) : (
          rows.map(({ item: e, count }, i) => {
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
                {count > 1 ? <span className="fleet-xbadge" aria-label={de.fleet.tickerRepeated(count)}>×{count}</span> : null}
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}
