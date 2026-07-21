/**
 * useSystemHistory — G4: 10-Minuten-Microsparks für CPU/RAM.
 *
 * Versucht zuerst GET /api/system/stats/history?minutes=120&step=1 (Vertrag:
 * {interval_s, window_s, samples:[{ts, cpu_percent, mem_percent}]}). Bei 404
 * oder Netzfehler: Client-Ringpuffer über den bestehenden 15s-Poll von
 * useSystemStats (~40 Samples ≈ 10 min, In-Memory, wächst mit der Sitzung).
 * Endpoint-Daten haben Vorrang, sobald verfügbar. Jeder Fehlerpfad führt zum
 * Ringpuffer-/Sammel-Zustand — niemals Exception oder leerer Rahmen.
 */
import { useEffect, useRef, useState } from "react";

import { api, type SystemStatsHistory } from "@/lib/api";
import { useSystemStats } from "./useSystemStats";

/** ~10 min bei 15 s Poll-Intervall. */
const RING_MAX = 40;

export interface SystemHistoryResult {
  cpu: number[];
  mem: number[];
  live: boolean;
  source: "endpoint" | "ring";
  cpuNow: number | null;
  memNow: number | null;
}

/**
 * Pure: SVG-Linienpfad (viewBox 0 0 100 24) aus einer Prozent-Serie
 * (älteste zuerst). Leer → "", 1 Punkt → horizontale Linie auf der Höhe.
 * Kein NaN: Werte werden auf 0–100 geclampt, Nicht-Zahlen als 0 behandelt.
 */
export function sparkPathFromSeries(series: number[]): string {
  const W = 100;
  const H = 24;
  if (series.length === 0) return "";
  const clamp = (v: number) => Math.max(0, Math.min(100, Number.isFinite(v) ? v : 0));
  const toY = (v: number) => Math.round((H - (clamp(v) / 100) * H) * 10) / 10;
  if (series.length === 1) {
    const y = toY(series[0]);
    return `M0 ${y} L${W} ${y}`;
  }
  const step = W / (series.length - 1);
  return series
    .map((v, i) => {
      const x = Math.round(i * step * 10) / 10;
      return `${i === 0 ? "M" : "L"}${x} ${toY(v)}`;
    })
    .join(" ");
}

export function useSystemHistory(): SystemHistoryResult {
  const stats = useSystemStats();
  const [endpoint, setEndpoint] = useState<SystemStatsHistory | null>(null);
  const ringRef = useRef<{ cpu: number[]; mem: number[] }>({ cpu: [], mem: [] });
  const [, setRingTick] = useState(0);

  // Einmaliger Endpoint-Versuch beim Mount; 404/Netzfehler → Ringpuffer.
  useEffect(() => {
    let cancelled = false;
    api
      .getSystemStatsHistory({ skipStaleTokenReload: true })
      .then((data) => {
        if (!cancelled) setEndpoint(data);
      })
      .catch(() => {
        /* 404 / Netzfehler → Ringpuffer (endpoint bleibt null). */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Ringpuffer: bei jedem erfolgreichen 15s-Poll ein Sample anhängen.
  // lastUpdated ändert sich bei jedem erfolgreichen Load → sicherer Trigger.
  const lastUpdated = stats.lastUpdated;
  const cpuNow = stats.data?.cpu_percent ?? null;
  const memNow = stats.data?.memory?.percent ?? null;

  useEffect(() => {
    if (!lastUpdated || endpoint) return;
    const cpu = stats.data?.cpu_percent;
    const mem = stats.data?.memory?.percent;
    if (cpu === undefined && mem === undefined) return;
    const ring = ringRef.current;
    ring.cpu = [...ring.cpu.slice(-(RING_MAX - 1)), cpu ?? 0];
    ring.mem = [...ring.mem.slice(-(RING_MAX - 1)), mem ?? 0];
    setRingTick((t) => t + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastUpdated, endpoint]);

  // Endpoint-Daten haben Vorrang, sobald verfügbar.
  if (endpoint && endpoint.samples.length > 0) {
    return {
      cpu: endpoint.samples.map((s) => s.cpu_percent),
      mem: endpoint.samples.map((s) => s.mem_percent),
      live: true,
      source: "endpoint",
      cpuNow,
      memNow,
    };
  }

  return {
    cpu: ringRef.current.cpu,
    mem: ringRef.current.mem,
    live: cpuNow !== null || memNow !== null,
    source: "ring",
    cpuNow,
    memNow,
  };
}
