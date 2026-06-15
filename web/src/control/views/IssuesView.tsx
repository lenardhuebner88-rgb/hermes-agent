import { useCallback, useEffect, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { ToneCallout } from "../components/atoms";
import { FleetEmptyState, FleetPanel, FleetPod } from "../components/fleet/atoms";
import { fmtClock } from "../lib/derive";
import { profileLabel } from "../lib/tones";
import type { Density } from "../hooks/useDensity";

// F6 night-sprint: Issue-Board light — wiederkehrende Fehler regelbasiert
// gruppiert (gleiche normalisierte Fehlerzeile + gleiches Profil = ein Issue
// mit Zähler) statt 20 einzelner roter Runs. Rein lesend, kein KI-Clustering,
// keine Auto-Tasks. Strings lokal wie bei RunTimelineView (F3).
const t = {
  title: "Issues",
  subtitle: "Wiederkehrende Fehler der letzten 30 Tage — gruppiert nach Fehlertyp + Profil.",
  loading: "Lade Issues …",
  empty: "Keine wiederkehrenden Fehler im Fenster.",
  emptyDesc: "Sobald Runs scheitern, gruppieren sie sich hier.",
  truncated: "Liste gekappt — häufigste zuerst.",
  back: "← Statistik",
  podGroups: "Issue-Gruppen",
  podRuns: "Fehl-Runs · 30d",
  count: (n: number) => `${n}×`,
  lastSeen: "zuletzt",
  exampleRun: "Beispiel-Run →",
};

export interface IssueGroup {
  signature: string;
  profile: string;
  count: number;
  first_seen: number;
  last_seen: number;
  outcomes: Record<string, number>;
  example_run_id: number;
  example_task_id: string;
  example_text: string;
}

export interface RunsIssuesResponse {
  days: number;
  now: number;
  total_failed_runs: number;
  group_count: number;
  truncated: boolean;
  issues: IssueGroup[];
}

const outcomeTone: Record<string, string> = {
  blocked: "text-zinc-300",
  crashed: "text-red-300",
  timed_out: "text-red-300",
  spawn_failed: "text-red-300",
  gave_up: "text-amber-300",
  iteration_budget_exhausted: "text-amber-300",
};

export function IssueRow({ issue }: { issue: IssueGroup }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="rounded-md border border-[var(--hc-border)] px-3 py-2.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center gap-2 text-left"
        aria-expanded={open}
      >
        <span className="hc-mono shrink-0 rounded-full border border-[var(--hc-border)] px-2 py-0.5 text-[0.72rem] text-white">
          {t.count(issue.count)}
        </span>
        <span className="min-w-0 flex-1 basis-64 truncate text-[0.85rem] font-medium text-white">
          {issue.signature}
        </span>
        <span className="hc-mono shrink-0 text-[0.72rem] hc-soft">
          {profileLabel[issue.profile] ?? issue.profile}
        </span>
        <span className="shrink-0 space-x-1.5 text-[0.72rem]">
          {Object.entries(issue.outcomes).map(([k, n]) => (
            <span key={k} className={`hc-mono ${outcomeTone[k] ?? "hc-dim"}`}>{k}·{n}</span>
          ))}
        </span>
        <span className="hc-mono shrink-0 text-[0.72rem] hc-dim">
          {t.lastSeen} {fmtClock(issue.last_seen)}
        </span>
      </button>
      {open ? (
        <div className="mt-2 space-y-2 border-t border-[var(--hc-border)] pt-2">
          <pre className="overflow-x-auto whitespace-pre-wrap break-words text-[0.74rem] leading-relaxed hc-soft">
            {issue.example_text || "—"}
          </pre>
          <a
            href={`/control/runs/${issue.example_run_id}`}
            className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-2.5 py-1 text-[0.78rem] hc-soft hover:bg-white/5"
          >
            {t.exampleRun}
          </a>
        </div>
      ) : null}
    </li>
  );
}

export function IssuesPanel({ data }: { data: RunsIssuesResponse }) {
  return (
    <FleetPanel
      eyebrow={t.title}
      meta={t.subtitle}
    >
      <div className="grid grid-cols-2 gap-2 sm:max-w-xs">
        <FleetPod label={t.podGroups} value={data.group_count} />
        <FleetPod label={t.podRuns} value={data.total_failed_runs} />
      </div>
      {data.truncated ? <p className="mt-2 text-xs text-amber-200">{t.truncated}</p> : null}
      {data.issues.length === 0 ? (
        <FleetEmptyState title={t.empty} desc={t.emptyDesc} />
      ) : (
        <ul className="mt-3 space-y-1.5">
          {data.issues.map((issue) => (
            <IssueRow key={`${issue.profile}:${issue.signature}`} issue={issue} />
          ))}
        </ul>
      )}
    </FleetPanel>
  );
}

export function IssuesView(_props: { density?: Density }) {
  const [data, setData] = useState<RunsIssuesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(
        await fetchJSON<RunsIssuesResponse>(
          "/api/plugins/kanban/runs/issues?days=30&limit=50",
        ),
      );
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    // Erst-Load per setTimeout(0) statt direkt im Effect-Body — Hauskonvention
    // (TriageStrip): synchrones setState im Effect verletzt react-hooks/
    // set-state-in-effect und kaskadiert Renders.
    const firstLoad = window.setTimeout(() => void load(), 0);
    const id = window.setInterval(() => {
      if (document.hidden) return;
      void load();
    }, 60000);
    return () => {
      window.clearTimeout(firstLoad);
      window.clearInterval(id);
    };
  }, [load]);

  return (
    <section aria-label={t.title} className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white">{t.title}</h2>
        <a
          href="/control/statistik"
          className="inline-flex min-h-11 items-center rounded-md border border-white/10 px-3 py-1.5 text-sm hc-soft hover:bg-white/5"
        >
          {t.back}
        </a>
      </div>
      {error ? <ToneCallout tone="red">{error}</ToneCallout> : null}
      {data === null && !error ? <p className="text-sm hc-dim">{t.loading}</p> : null}
      {data !== null ? <IssuesPanel data={data} /> : null}
    </section>
  );
}
