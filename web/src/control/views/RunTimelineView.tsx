import { useCallback, useEffect, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { useParams } from "react-router-dom";
import { fetchJSON } from "@/lib/api";
import { FleetEmptyState, FleetPanel, SignalChip, signalToneFromLegacy } from "../components/leitstand";
import { fmtClock, fmtDur } from "../lib/derive";
import type { ToneName } from "../lib/types";
import type { Density } from "../hooks/useDensity";
import { eventTone } from "./RunTimelineView.helpers";

// F3 night-sprint: flacher Trace-Vorläufer — alle Events eines Runs als
// Zeitleiste mit relativen Dauer-Balken und Status-Farben. Strings lokal,
// keine Edits an i18n/de.ts (Shared-File paralleler Sessions).
const t = {
  title: "Run-Timeline",
  loading: "Lade Timeline …",
  empty: "Keine Events für diesen Run.",
  emptyDesc: "Ältere Runs können per Event-GC bereinigt sein.",
  truncated: "Event-Liste gekappt — älteste zuerst.",
  back: "← Workstreams",
  duration: "Dauer",
  profile: "Profil",
  status: "Status",
  task: "Task",
};

export interface TimelineItem {
  kind: string;
  at: number;
  source: "run" | "event" | "task";
  payload: Record<string, unknown> | unknown[] | null;
  offset_seconds: number | null;
  delta_seconds: number;
}

export interface RunTimelineResponse {
  run: {
    id: number;
    task_id: string;
    profile: string | null;
    status: string | null;
    outcome: string | null;
    error: string | null;
    summary: string | null;
    started_at: number | null;
    ended_at: number | null;
    duration_seconds: number | null;
  };
  items: TimelineItem[];
  count: number;
  truncated: boolean;
}


function payloadSnippet(item: TimelineItem): string | null {
  const p = item.payload;
  if (!p || Array.isArray(p)) return null;
  for (const key of ["error", "reason", "note", "summary", "preview", "assignee", "status"]) {
    const v = p[key];
    if (typeof v === "string" && v.trim()) {
      const s = v.trim();
      return s.length > 160 ? `${s.slice(0, 159)}…` : s;
    }
  }
  return null;
}

function fmtOffset(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `+${seconds}s`;
  return `+${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, "0")}s`;
}

function TimelineRow({ item, maxDelta }: { item: TimelineItem; maxDelta: number }) {
  const tone = eventTone(item);
  const snippet = payloadSnippet(item);
  // Relativer Dauer-Balken: Breite = Anteil des Deltas am größten Delta.
  const pct = maxDelta > 0 ? Math.max(2, Math.round((item.delta_seconds / maxDelta) * 100)) : 2;
  return (
    <li className="flex items-start gap-3 py-1.5">
      <span className="w-20 shrink-0 text-right font-data text-sec tabular-nums text-ink-3">{fmtOffset(item.offset_seconds)}</span>
      <span className="w-24 shrink-0">
        <span
          className="block h-1.5 rounded bg-line"
          style={{ width: `${pct}%` }}
          aria-hidden="true"
        />
        <span className="font-data text-micro tabular-nums text-ink-3">{item.delta_seconds > 0 ? fmtDur(item.delta_seconds) : ""}</span>
      </span>
      <SignalChip tone={signalToneFromLegacy(tone)} label={item.kind} />
      {snippet ? <span className="min-w-0 truncate text-sec text-ink-2">{snippet}</span> : null}
    </li>
  );
}

/** Pure Panel — für Tests mit Fixture renderbar. */
export function RunTimelinePanel({ data }: { data: RunTimelineResponse }) {
  const maxDelta = Math.max(0, ...data.items.map((it) => it.delta_seconds));
  const run = data.run;
  const headerTone: ToneName =
    run.outcome === "completed" ? "emerald"
    : run.ended_at == null ? "amber"
    : run.outcome === "blocked" ? "zinc"
    : "red";
  return (
    <div className="space-y-4">
      <FleetPanel
        eyebrow={
          <span className="inline-flex min-w-0 items-center gap-2">
            <span className="truncate normal-case tracking-normal text-ink">Run #{run.id}</span>
            <SignalChip tone={signalToneFromLegacy(headerTone)} label={run.outcome || run.status || "running"} />
          </span>
        }
        meta={
          <span className="inline-flex items-center gap-3 font-data text-sec tabular-nums">
            <span>{t.task}: {run.task_id}</span>
            {run.profile ? <span>{t.profile}: {run.profile}</span> : null}
            {run.started_at != null ? <span>{fmtClock(run.started_at)}</span> : null}
            {run.duration_seconds != null ? <span>{t.duration}: {fmtDur(run.duration_seconds)}</span> : null}
          </span>
        }
      >
        {run.error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{run.error}</div> : null}
        {data.truncated ? <div className="mt-2 flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{t.truncated}</div> : null}
        {data.items.length === 0 ? (
          <FleetEmptyState title={t.empty} desc={t.emptyDesc} />
        ) : (
          <ol className="mt-2 divide-y divide-line-soft">
            {data.items.map((item, i) => (
              <TimelineRow key={i} item={item} maxDelta={maxDelta} />
            ))}
          </ol>
        )}
      </FleetPanel>
    </div>
  );
}

export function RunTimelineView(_props: { density?: Density }) {
  const { runId } = useParams<{ runId: string }>();
  const [data, setData] = useState<RunTimelineResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!runId) return;
    try {
      setData(
        await fetchJSON<RunTimelineResponse>(
          `/api/plugins/kanban/runs/${encodeURIComponent(runId)}/timeline`,
        ),
      );
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [runId]);

  useEffect(() => {
    // Erst-Load per setTimeout(0) — Hauskonvention (TriageStrip), s.o.
    const firstLoad = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(firstLoad);
  }, [load]);

  return (
    <section aria-label={t.title} className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-h2 font-semibold text-ink">{t.title}</h2>
        <a
          href="/control/workstreams"
          className="inline-flex min-h-12 items-center rounded-card border border-line px-3 py-1.5 text-sec text-live hover:border-live hover:bg-live/10"
        >
          {t.back}
        </a>
      </div>
      {error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{error}</div> : null}
      {data === null && !error ? <p className="text-sec text-ink-3">{t.loading}</p> : null}
      {data !== null ? <RunTimelinePanel data={data} /> : null}
    </section>
  );
}
