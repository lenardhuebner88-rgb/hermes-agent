/**
 * useSystemStats — S6.4c: System-Stats (GET /api/system/stats) für die
 * Sparklines im HUD-Panel.
 *
 * Der Endpoint liefert Punkt-Werte (CPU/RAM/DISK %), keine Zeitreihe — die
 * Sparkline-Pfade werden aus dem aktuellen Wert als horizontale Linie auf der
 * entsprechenden Höhe generiert. psutil ist serverseitig optional; fehlen die
 * Felder, bleibt das Panel beim Mock-Fallback.
 */
import { api, type SystemStats } from "@/lib/api";
import { usePolling } from "../hooks/internal";

export const SYSTEM_STATS_KEY = "system/stats";
/** Punkt-Werte altern schnell — 15 s Frische für die Sparklines. */
export const SYSTEM_STATS_POLL_INTERVAL_MS = 15_000;

export function useSystemStats() {
  return usePolling<SystemStats>(
    SYSTEM_STATS_KEY,
    () => api.getSystemStats(),
    SYSTEM_STATS_POLL_INTERVAL_MS,
  );
}

/** S6: Sparkline-Linienpfad aus einem Prozentwert — horizontale Linie auf
 *  der Höhe (100 − percent) in der viewBox 0 0 100 22. */
export function sparkLinePath(percent: number): string {
  const y = Math.max(0, Math.min(22, 22 - (percent / 100) * 22));
  const r = Math.round(y * 10) / 10;
  return `M0 ${r} L100 ${r}`;
}

/** S6: Sparkline-Flächenpfad (fill) aus einem Prozentwert. */
export function sparkAreaPath(percent: number): string {
  const y = Math.max(0, Math.min(22, 22 - (percent / 100) * 22));
  const r = Math.round(y * 10) / 10;
  return `M0 ${r} L100 ${r} L100 22 L0 22 Z`;
}
