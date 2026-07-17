import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { EpicsResponseSchema, parseOrThrow } from "../lib/schemas";
import type { EpicsResponse } from "../lib/schemas";
import { usePolling, extractDetail } from "./internal";

// Epics (Vorhaben-Ebene): Rollup pro Epic für die Flow-Gruppierung und die
// Statistik-Kompaktübersicht. 15s — Epics ändern sich selten; ein Fehler hier
// darf das Board nie blanken (die Gruppierung degradiert auf rohe IDs).
export const epicsLoader = async () =>
  parseOrThrow(EpicsResponseSchema, await fetchJSON<unknown>("/api/plugins/kanban/epics"), "kanban/epics");


export function useEpics() {
  // M1: Epics ändern sich selten (Vorhaben-Ebene) — 60 s statt 15 s spart Polls.
  return usePolling<EpicsResponse>("kanban/epics", epicsLoader, 60000);
}


// Epic-Schreibpfade (Phase-1-API): anlegen, schließen, ganze Kette zuordnen.
// Die Ketten-Zuordnung patcht jedes Mitglied einzeln (PATCH epic_id) — Ketten
// sind klein, und der getestete Einzel-Task-Pfad bleibt die einzige Wahrheit.
// `busyKey` trägt die Epic-/Root-ID der laufenden Aktion (ein Operator,
// eine Aktion zur Zeit).
export function useEpicActions(onDone?: () => void | Promise<void>) {
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const createEpic = useCallback(async (title: string, body?: string) => {
    setBusyKey("create");
    setError(null);
    try {
      const res = await fetchJSON<{ epic?: { id?: string } }>(
        "/api/plugins/kanban/epics",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body?.trim() ? { title, body } : { title }) },
      );
      await onDone?.();
      return { ok: true as const, id: res.epic?.id ?? null };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyKey(null);
    }
  }, [onDone]);

  const closeEpic = useCallback(async (epicId: string) => {
    setBusyKey(epicId);
    setError(null);
    try {
      await fetchJSON<{ epic?: unknown }>(
        `/api/plugins/kanban/epics/${encodeURIComponent(epicId)}/close`,
        { method: "POST" },
      );
      await onDone?.();
      return { ok: true as const };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyKey(null);
    }
  }, [onDone]);

  const assignChain = useCallback(async (rootId: string, taskIds: string[], epicId: string | null) => {
    setBusyKey(rootId);
    setError(null);
    try {
      for (const id of taskIds) {
        await fetchJSON<{ task?: unknown }>(
          `/api/plugins/kanban/tasks/${encodeURIComponent(id)}`,
          { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ epic_id: epicId }) },
        );
      }
      await onDone?.();
      return { ok: true as const };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyKey(null);
    }
  }, [onDone]);

  const clearError = useCallback(() => setError(null), []);
  return { busyKey, error, createEpic, closeEpic, assignChain, clearError };
}


// Roster size — how many profiles are installed (the "/ N" denominator on the
// Worker pod). Tolerant + slow (60s): the roster changes rarely and a failure
// here must never blank the Fleet, so it degrades to null (pod shows just the
// active count).
export function useRosterCount() {
  return usePolling<number | null>(
    "kanban/profiles-count",
    async () => {
      const data = await fetchJSON<{ profiles?: unknown[] }>("/api/plugins/kanban/profiles");
      return Array.isArray(data.profiles) ? data.profiles.length : null;
    },
    60000,
  );
}
