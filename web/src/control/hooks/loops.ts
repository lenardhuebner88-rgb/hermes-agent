import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import {
  LoopsResponseSchema,
  LoopModelsResponseSchema,
  LoopDetailResponseSchema,
  LoopFilesResponseSchema,
  LoopFileSaveResultSchema,
  LoopDuplicateResultSchema,
  LoopLandResultSchema,
  parseOrThrow,
} from "../lib/schemas";
import type { LoopDetailResponse, LoopModelsResponse, LoopsResponse, LoopFilesResponse, LoopFileSaveResult, LoopDuplicateResult, LoopLandResult } from "../lib/types";
import { usePolling, extractDetail } from "./internal";

// ── Loop-Runner (/control Loops-Tab) — Vertrag: hermes_cli/control_loops.py ──
export const loopsLoader = async () =>
  parseOrThrow(LoopsResponseSchema, await fetchJSON<unknown>("/api/loops"), "loops");


export function useLoops() {
  return usePolling<LoopsResponse>("loops", loopsLoader, 5000);
}


export function useLoopModels() {
  return usePolling<LoopModelsResponse>(
    "loops/models",
    async () => parseOrThrow(LoopModelsResponseSchema, await fetchJSON<unknown>("/api/loops/models"), "loops/models"),
    60000,
  );
}


// Detail pollt nur, solange ein Pack ausgewählt ist (Karte aufgeklappt) — Muster
// wie useWorkerActivity: Intervall auf sehr groß setzen + null zurückgeben,
// statt den Hook bedingt aufzurufen (Rules-of-Hooks).
export function useLoopDetail(pack: string | null) {
  const key = pack ? `loops/${pack}/detail` : "loops/detail/__none__";
  const loader = useCallback(async (): Promise<LoopDetailResponse | null> => {
    if (!pack) return null;
    return parseOrThrow(
      LoopDetailResponseSchema,
      await fetchJSON<unknown>(`/api/loops/${encodeURIComponent(pack)}/detail`),
      `loops/${pack}/detail`,
    );
  }, [pack]);
  const result = usePolling<LoopDetailResponse | null>(key, loader, pack ? 5000 : 600_000);
  if (!pack) return { ...result, data: null };
  return result;
}


export interface LoopStartResult {
  started: boolean;
  pack: string;
  overrides_written: number;
}


export interface LoopStopResult {
  stop_requested: boolean;
  pack: string;
  note: string;
}


export interface LoopTimerResult {
  pack: string;
  timer_enabled: boolean;
  timer_schedule: string;
  timer_next_run: string | null;
}


export function startLoop(pack: string, overrides: Record<string, string | number>): Promise<LoopStartResult> {
  return fetchJSON<LoopStartResult>(`/api/loops/${encodeURIComponent(pack)}/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ overrides }),
  });
}


export function stopLoop(pack: string): Promise<LoopStopResult> {
  return fetchJSON<LoopStopResult>(`/api/loops/${encodeURIComponent(pack)}/stop`, { method: "POST" });
}


export function toggleLoopTimer(pack: string, enabled: boolean): Promise<LoopTimerResult> {
  return fetchJSON<LoopTimerResult>(`/api/loops/${encodeURIComponent(pack)}/timer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}


export function setLoopTimerSchedule(pack: string, time: string): Promise<LoopTimerResult> {
  return fetchJSON<LoopTimerResult>(`/api/loops/${encodeURIComponent(pack)}/timer/schedule`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ time }),
  });
}


export async function landLoop(pack: string): Promise<LoopLandResult> {
  return parseOrThrow(
    LoopLandResultSchema,
    await fetchJSON<unknown>(`/api/loops/${encodeURIComponent(pack)}/land`, { method: "POST" }),
    `loops/${pack}/land`,
  );
}


// Werkstatt: Pack-Dateien fetch-once laden (wie usePlanSpecDetail — kein Polling,
// die Dateien ändern sich nur durch die eigenen Save/Duplicate-Mutationen unten,
// die selbst reload() aufrufen).
export function useLoopFiles(pack: string | null): {
  data: LoopFilesResponse | null;
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
} {
  const [data, setData] = useState<LoopFilesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const load = useCallback(async (): Promise<void> => {
    if (!pack) {
      if (aliveRef.current) { setData(null); setError(null); setLoading(false); }
      return;
    }
    if (aliveRef.current) { setLoading(true); setError(null); }
    try {
      const parsed = parseOrThrow(
        LoopFilesResponseSchema,
        await fetchJSON<unknown>(`/api/loops/${encodeURIComponent(pack)}/files`),
        `loops/${pack}/files`,
      );
      if (aliveRef.current) setData(parsed);
    } catch (e) {
      if (aliveRef.current) setError(extractDetail(e));
    } finally {
      if (aliveRef.current) setLoading(false);
    }
  }, [pack]);
  useEffect(() => {
    const initial = window.setTimeout(() => { void load(); }, 0);
    return () => window.clearTimeout(initial);
  }, [load]);
  return { data, loading, error, reload: load };
}


export async function saveLoopFile(pack: string, filename: string, content: string): Promise<LoopFileSaveResult> {
  return parseOrThrow(
    LoopFileSaveResultSchema,
    await fetchJSON<unknown>(`/api/loops/${encodeURIComponent(pack)}/files/${encodeURIComponent(filename)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    }),
    `loops/${pack}/files/${filename}`,
  );
}


export async function duplicateLoop(source: string, name: string): Promise<LoopDuplicateResult> {
  return parseOrThrow(
    LoopDuplicateResultSchema,
    await fetchJSON<unknown>("/api/loops/duplicate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, name }),
    }),
    "loops/duplicate",
  );
}

// ─── Fleet Karten-Detail-Drawer: On-Demand Task-Body + Runs + Deliverables ───
//
// Wird NUR bei offenem Drawer geladen (task-body-on-demand/{id}) — kein Background-Poll.
// useTaskBodyOnDemand pollt alle 8s WENN taskId != null (= Drawer offen), ansonsten pausiert.
// Pattern analog zu useWorkerActivity (Null-Guard + leerer Fallback).

