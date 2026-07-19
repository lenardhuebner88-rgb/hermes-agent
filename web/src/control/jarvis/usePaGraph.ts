/**
 * usePaGraph — S2.7 Estate-Graph (GET /api/pa/graph) über den geteilten
 * pollingStore (Key `pa/graph`). Kadenz 30 s = `refresh.interval_s` des
 * pa-graph/v1-Kontrakts (Backend-Snapshot-TTL ist 60 s). Der Store liefert
 * keep-last-good + isStale bei Fetch-Fehlern von selbst; die eigentliche
 * Fallback-Hierarchie steckt in usePaGraphView, damit Graph, Footer-Tag und
 * Stats-Tag denselben Zustand sehen (ein Poll, beliebig viele Subscriber).
 */
import { api, type PaInboxError, type PaGraphResponse } from "@/lib/api";
import { usePolling } from "../hooks/internal";
import { PA_GRAPH_MOCK } from "./graphMock";

export const PA_GRAPH_KEY = "pa/graph";
/** Kontrakt refresh.interval_s = 30 s (S2.7). */
export const PA_GRAPH_POLL_INTERVAL_MS = 30_000;

export function usePaGraph() {
  return usePolling<PaGraphResponse>(
    PA_GRAPH_KEY,
    () => api.getPaGraph(),
    PA_GRAPH_POLL_INTERVAL_MS,
  );
}

export interface PaGraphViewState {
  /** Der zu rendernde Datensatz: Live (ggf. last-good) oder der A4-Mock. */
  graph: PaGraphResponse;
  /** true, wenn echte Backend-Daten mit mindestens einem Knoten vorliegen. */
  isLive: boolean;
  /** true, wenn gezeigte Live-Daten nach einem Fetch-Fehler last-good sind. */
  isStale: boolean;
  /** Transport-/HTTP-Fehler des letzten Polls (null bei Erfolg). */
  error: string | null;
  /** Teilquellen-Fehler des Datensatzes (dezent: Console/Tooltip, kein Panel). */
  sourceErrors: PaInboxError[];
}

/**
 * Fallback-Hierarchie (Brief S2.7-FE, bindend):
 *  1. Live-Daten mit Knoten → live rendern; bei Fetch-Fehler hält der
 *     pollingStore den letzten guten Datensatz (isStale) → dezenter Hinweis.
 *  2. Noch nie Live-Daten (Poll hängt oder Fehler vor dem ersten Erfolg)
 *     ODER die Antwort hat leere `nodes` (Backend-Gesamtausfall ist bewusst
 *     HTTP 200 + errors[]) → PA_GRAPH_MOCK mit dem Vorschau/Mock-Label.
 */
export function usePaGraphView(): PaGraphViewState {
  const { data, error, isStale } = usePaGraph();
  const isLive = data != null && data.nodes.length > 0;
  const graph: PaGraphResponse = isLive ? data : PA_GRAPH_MOCK;
  return {
    graph,
    isLive,
    isStale: isLive && isStale === true,
    error,
    sourceErrors: isLive ? (data.errors ?? []) : [],
  };
}

/** „Stand <HH:MM>" aus dem ISO-generated_at; unlesbare Werte fallen auf den
 *  Rohstring zurück (Anzeige darf nie crashen). */
export function formatGraphStand(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}
