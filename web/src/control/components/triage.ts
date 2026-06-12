// Ein Klick auf „Nochmal" stellt den Task nur auf ready — dispatcht wird er
// erst, wenn der 60s-Dispatcher-Tick UND die Lane-Kapazität (max_in_progress
// pro Profil) es zulassen. Ohne sichtbaren Zustand sah das nach einem toten
// Button aus (Operator-Befund 2026-06-12, t_748896f7): die Karte blieb
// unverändert „blocked" in der Liste stehen. Darum spiegelt die Karte den
// LIVE-Task-Status: eingereihte Karten zeigen eine Warte-Plakette statt des
// sinnlosen Retry-Buttons (Eskalieren bleibt möglich — model_override und
// Lane-Wechsel wirken auch vor dem Dispatch).
export interface TriageRequeueState {
  requeued: boolean;
  label: string | null;
}

export function triageRequeueState(taskStatus: string): TriageRequeueState {
  if (taskStatus === "ready") {
    return { requeued: true, label: "wieder eingereiht — wartet auf Dispatcher-Tick + freie Lane" };
  }
  if (taskStatus === "scheduled") {
    return { requeued: true, label: "zurückgestellt — wartet auf den geplanten Termin" };
  }
  return { requeued: false, label: null };
}
