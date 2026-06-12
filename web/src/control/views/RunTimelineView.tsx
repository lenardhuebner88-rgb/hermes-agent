import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchJSON } from "@/lib/api";
import { StatusPill, ToneCallout } from "../components/atoms";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
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
      <span className="hc-mono w-20 shrink-0 text-right text-xs hc-dim">{fmtOffset(item.offset_seconds)}</span>
      <span className="w-24 shrink-0">
        <span
          className="block h-1.5 rounded bg-white/15"
          style={{ width: `${pct}%` }}
          aria-hidden="true"
        />
        <span className="hc-mono hc-type-label hc-dim">{item.delta_seconds > 0 ? fmtDur(item.delta_seconds) : ""}</span>
      </span>
      <StatusPill tone={tone} label={item.kind} size="sm" />
      {snippet ? <span className="min-w-0 truncate text-xs hc-soft">{snippet}</span> : null}
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
            <span className="truncate normal-case tracking-normal text-white">Run #{run.id}</span>
            <StatusPill tone={headerTone} label={run.outcome || run.status || "running"} size="sm" />
          </span>
        }
        meta={
          <span className="inline-flex items-center gap-3 hc-mono text-xs">
            <span>{t.task}: {run.task_id}</span>
            {run.profile ? <span>{t.profile}: {run.profile}</span> : null}
            {run.started_at != null ? <span>{fmtClock(run.started_at)}</span> : null}
            {run.duration_seconds != null ? <span>{t.duration}: {fmtDur(run.duration_seconds)}</span> : null}
          </span>
        }
      >
        {run.error ? <ToneCallout tone="red">{run.error}</ToneCallout> : null}
        {data.truncated ? <p className="mt-2 text-xs text-amber-200">{t.truncated}</p> : null}
        {data.items.length === 0 ? (
          <FleetEmptyState title={t.empty} desc={t.emptyDesc} />
        ) : (
          <ol className="mt-2 divide-y divide-white/5">
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
        <h2 className="text-lg font-semibold text-white">{t.title}</h2>
        <a
          href="/control/workstreams"
          className="inline-flex min-h-11 items-center rounded-md border border-white/10 px-3 py-1.5 text-sm hc-soft hover:bg-white/5"
        >
          {t.back}
        </a>
      </div>
      {error ? <ToneCallout tone="red">{error}</ToneCallout> : null}
      {data === null && !error ? <p className="text-sm hc-dim">{t.loading}</p> : null}
      {data !== null ? <RunTimelinePanel data={data} /> : null}
    </section>
  );
}
