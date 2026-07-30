/**
 * FreshnessStrip — die ruhige Daten-Ehrlichkeitszeile des Leitstands: ein
 * gedämpftes "aktualisiert vor X" (font-data, micro, ink-3) plus einem
 * Refresh-Icon-Button, der das `reload()` des Polling-Hooks auslöst. Kein
 * Banner, keine Karte — ein Einzeiler, der sich an der Listengeometrie
 * ausrichtet, die er beschreibt.
 *
 * Datenquelle: `lastUpdated` (Epoch-Sekunden des letzten ERFOLGREICHEN Loads)
 * und `reload()` des usePolling-Hooks. Solange nie erfolgreich geladen wurde,
 * steht "—" (Ehrlichkeits-Regel: kein erfundener Zeitstempel). Das Alter wird
 * alle 30 s nachgeführt, damit die Zeile zwischen zwei Polls nicht lügt.
 *
 * Der Spin-Indikator läuft NUR, während ein manueller Refetch tatsächlich in
 * der Luft ist (lokaler State um das Promise des reload) — kein Dauer-Spinner.
 * `animate-spin` ist eine CSS-Animation und fällt damit unter den zentralen
 * prefers-reduced-motion-Kill-Switch (control-tokens.css); die Zeile selbst
 * animiert nichts.
 *
 * NICHT für: Aktions-Feedback (POST-Ergebnisse — das ist kein "aktualisiert"),
 * und nicht als Ersatz für den StaleBadge: der Badge meldet den degradierten
 * Zustand (stale/Fehler), die Strip meldet das normale Alter — beide dürfen
 * nebeneinander stehen.
 */
import { RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import { fmtRelativeTime, nowSec } from "../../lib/derive";

export function FreshnessStrip({
  lastUpdated,
  onRefresh,
  className,
}: {
  /** Epoch-Sekunden des letzten erfolgreichen Loads; `null` bis zum ersten ok. */
  lastUpdated: number | null;
  /** In der Regel `() => void reload()` des Hooks; ohne ihn entfällt der Button. */
  onRefresh?: () => Promise<unknown> | void;
  className?: string;
}) {
  const [refreshing, setRefreshing] = useState(false);
  // Das Alter ("vor X") wird alle 30 s nachgeführt — zwischen zwei Polls bleibt
  // die Zeile sonst auf dem Stand des letzten Re-Render stehen.
  const [now, setNow] = useState(nowSec);
  useEffect(() => {
    const id = window.setInterval(() => setNow(nowSec()), 30_000);
    return () => window.clearInterval(id);
  }, []);

  const handleRefresh = () => {
    if (!onRefresh || refreshing) return;
    setRefreshing(true);
    Promise.resolve(onRefresh()).finally(() => setRefreshing(false));
  };

  return (
    <div className={cn("flex items-center gap-2 font-data text-micro tabular-nums text-ink-3", className)}>
      <span>{lastUpdated != null ? `aktualisiert ${fmtRelativeTime(lastUpdated, now)}` : "aktualisiert —"}</span>
      {onRefresh ? (
        <button
          type="button"
          onClick={handleRefresh}
          disabled={refreshing}
          aria-label="Jetzt aktualisieren"
          title="Jetzt aktualisieren"
          className="inline-flex size-7 items-center justify-center rounded-card border border-line bg-surface-2 text-ink-2 transition-colors duration-150 ease-out hover:border-live hover:text-live focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:opacity-50"
        >
          <RefreshCw aria-hidden className={cn("size-3.5", refreshing && "animate-spin")} />
        </button>
      ) : null}
    </div>
  );
}
