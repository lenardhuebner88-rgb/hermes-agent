/**
 * engineSelection — S2.2 Modell-Switcher: Roster-Hook + Wahl-State.
 *
 * Das Roster (GET /api/pa/engines) läuft über den geteilten pollingStore
 * (Key "pa/engines") — Switcher UND Chat-Hook lesen dieselbe deduplizierte
 * Quelle; getSnapshot macht das Roster auch außerhalb von React (im send-
 * Pfad von usePaChat) verfügbar.
 *
 * Die Wahl (engine+model) ist absichtlich NICHT im pollingStore: sie ist
 * lokaler UI-State, gilt für den NÄCHSTEN Turn und wird nie gepollt. Ein
 * minimaler Observable-Store (useSyncExternalStore) teilt sie zwischen dem
 * Switcher im Emblem und dem Chat, ohne Props durch die Shell zu ziehen.
 * null = Server-Default (kein engine/model-Feld im POST).
 */
import { useSyncExternalStore } from "react";

import { api, type PaEnginesResponse, type PaEngineSpec } from "@/lib/api";
import { getSnapshot } from "../hooks/pollingStore";
import { usePolling } from "../hooks/internal";

export const PA_ENGINES_KEY = "pa/engines";
/** Roster ändert sich nur bei Deploys — langsame Frische reicht. */
export const PA_ENGINES_POLL_INTERVAL_MS = 60_000;

export function usePaEngines() {
  return usePolling<PaEnginesResponse>(
    PA_ENGINES_KEY,
    () => api.getPaEngines(),
    PA_ENGINES_POLL_INTERVAL_MS,
  );
}

/** Roster außerhalb von React lesen (send-Pfad) — null bis zum ersten Load. */
export function getPaEnginesSnapshot(): PaEnginesResponse | null {
  return getSnapshot<PaEnginesResponse>(PA_ENGINES_KEY)?.data ?? null;
}

export interface EngineChoice {
  engine: string;
  model: string;
}

let currentChoice: EngineChoice | null = null;
const listeners = new Set<() => void>();

/** Test-Naht: Store zwischen Tests zurücksetzen. */
export function _resetEngineChoice(): void {
  currentChoice = null;
}

export function getEngineChoice(): EngineChoice | null {
  return currentChoice;
}

export function setEngineChoice(next: EngineChoice | null): void {
  currentChoice = next;
  for (const listener of listeners) listener();
}

function subscribeChoice(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useEngineChoice(): EngineChoice | null {
  return useSyncExternalStore(subscribeChoice, getEngineChoice);
}

/** Effektive Engine für den nächsten Turn: Wahl, sonst Roster-Default,
 *  sonst der Backend-Default "sol" (Roster noch nicht geladen). */
export function effectiveEngine(
  choice: EngineChoice | null,
  roster: PaEnginesResponse | null,
): string {
  return choice?.engine ?? roster?.default_engine ?? "sol";
}

export function findEngineSpec(
  roster: PaEnginesResponse | null,
  engine: string,
): PaEngineSpec | null {
  return roster?.engines.find((spec) => spec.engine === engine) ?? null;
}

/** Anzeige-Labels der bekannten Modelle (Brief: „Opus 4.8", „Fable 5",
 *  „gpt-5.6-sol", „Kimi K3"); unbekannte Modelle fallen auf die Roh-ID. */
const MODEL_LABELS: Record<string, string> = {
  "gpt-5.6-sol": "gpt-5.6-sol",
  "opus-4.8": "Opus 4.8",
  "fable-5": "Fable 5",
  k3: "Kimi K3",
};

export function modelLabel(model: string): string {
  return MODEL_LABELS[model] ?? model;
}

/** „Max"-Marker (Fork 19: Hinweis, kein Cap) gilt für Modelle der claude-
 *  Engine — roster-getrieben, kein hartkodierter Modellname. */
export function isClaudeModel(roster: PaEnginesResponse | null, model: string): boolean {
  const claude = roster?.engines.find((spec) => spec.engine === "claude");
  return claude?.models.includes(model) ?? false;
}
