/**
 * usePaInbox — S2.4 Entscheidungs-Inbox (GET /api/pa/inbox) über den
 * geteilten pollingStore. Ein Endpoint für offene Fragen + pa_action-Cards +
 * held chains + freigabe-Gates (serverseitig nach block_radius, dann ts
 * sortiert). Teilquellen-Ausfälle kommen als errors[] mit — sie werden dezent
 * angezeigt und lassen die restlichen Items unberührt (kein Crash).
 */
import { api, type PaInboxResponse } from "@/lib/api";
import { usePolling } from "../hooks/internal";

export const PA_INBOX_KEY = "pa/inbox";
/** Kadenz-Parität mit dem bisherigen Fragen-Poll (5 s). */
export const PA_INBOX_POLL_INTERVAL_MS = 5_000;

export function usePaInbox() {
  return usePolling<PaInboxResponse>(
    PA_INBOX_KEY,
    () => api.getPaInbox(),
    PA_INBOX_POLL_INTERVAL_MS,
  );
}
