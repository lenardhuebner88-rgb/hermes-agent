import { fetchJSON } from "@/lib/api";
import type { KnowledgeCatalog } from "../views/knowledge/knowledge.helpers";
import { usePolling } from "./internal";

const LIBRARY_LAST_VISIT_KEY = "hc-bibliothek-last-visit";


interface LibraryItemsLite {
  items?: { ts?: number; category?: string }[];
}


// Pure Zähllogik (testbar): "ungelesen" = neuer als der letzte Besuch UND
// kein wartung-Routine-Rauschen — das Badge soll "Neues, das dich
// interessiert" bedeuten.
export function countLibraryUnread(
  items: { ts?: number; category?: string }[],
  since: number,
): number {
  if (!since) return 0;
  return items.filter(
    (i) => (i.ts ?? 0) > since && i.category !== "wartung",
  ).length;
}


export function useLibraryUnread(): number {
  const state = usePolling<LibraryItemsLite>(
    "library/items-badge",
    () => fetchJSON<LibraryItemsLite>("/api/library/items?limit=60"),
    120000,
  );
  let since = 0;
  try {
    since = Number(window.localStorage.getItem(LIBRARY_LAST_VISIT_KEY) ?? 0) || 0;
  } catch {
    /* private mode */
  }
  // Erstbesuch (kein Stempel): nichts anbrüllen — der Tab ist Einladung genug.
  return countLibraryUnread(state.data?.items ?? [], since);
}


// S6 (2026-07-09): der komplette Regal-Katalog, ungefiltert (kein `q` — die
// Volltextsuche im Nachschlagewerk bleibt ihr eigener, debouncter Fetch,
// s. KnowledgeShelf). Speist sowohl die BriefingsShelf-Schnellauswahl-Kacheln
// als auch KnowledgeShelfs Baseline-Ansicht; geteilter pollingStore-Key → EIN
// Timer/Request für beide statt zwei. Bewusst ohne zod-Schema (wie die
// übrigen Library-Endpoints, z. B. useLibraryUnread direkt darüber) — die
// Bibliothek validiert ihre Payloads nirgends per parseOrThrow.
export function useKnowledgeCatalog() {
  return usePolling<KnowledgeCatalog>(
    "knowledge-catalog",
    () => fetchJSON<KnowledgeCatalog>("/api/library/knowledge"),
    60000,
  );
}
