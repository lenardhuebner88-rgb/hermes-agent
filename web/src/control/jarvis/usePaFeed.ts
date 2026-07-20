/**
 * usePaFeed — S6.4a: KI-LAGE-Panel live (GET /api/pa/feed).
 *
 * Der Feed-Endpoint liefert eine aufsteigende, bounded Page (since_id-Cursor
 * für Polling-Clients, Kontrakt: PAStore.feed_page in pa_chat.py). Das
 * KI-LAGE-Panel zeigt die letzten ~5 Einträge (Titel + relatives Alter).
 * Polling über den geteilten pollingStore (Key „pa/feed") — dieselbe
 * Deduplizierung/stale-while-error-Infrastruktur wie die anderen Jarvis-Polls.
 */
import { api, type PaFeedPage } from "@/lib/api";
import { usePolling } from "../hooks/internal";

export const PA_FEED_KEY = "pa/feed";
/** KI-LAGE ändert sich selten — 30 s Frische reichen (wie der Graph-Poll). */
export const PA_FEED_POLL_INTERVAL_MS = 30_000;

export function usePaFeed() {
  return usePolling<PaFeedPage>(
    PA_FEED_KEY,
    () => api.getPaFeed(),
    PA_FEED_POLL_INTERVAL_MS,
  );
}
