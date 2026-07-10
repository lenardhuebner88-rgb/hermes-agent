import { useCallback, useEffect, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { FleetEmptyState, FleetPanel, KpiTile, SignalLabel, type SignalTone } from "../components/leitstand";
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

const outcomeTone: Record<string, SignalTone> = {
  blocked: "neutral",
  crashed: "alert",
  timed_out: "alert",
  spawn_failed: "alert",
  gave_up: "warn",
  iteration_budget_exhausted: "warn",
};

export function IssueRow({ issue }: { issue: IssueGroup }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="rounded-card border border-line px-3 py-2.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex min-h-12 w-full flex-wrap items-center gap-2 text-left"
        aria-expanded={open}
      >
        <span className="shrink-0 rounded-card border border-line px-2 py-0.5 font-data text-micro tabular-nums text-ink">
          {t.count(issue.count)}
        </span>
        <span className="min-w-0 flex-1 basis-64 truncate text-sec font-medium text-ink">
          {issue.signature}
        </span>
        <span className="shrink-0 text-micro text-ink-2">
          {profileLabel[issue.profile] ?? issue.profile}
        </span>
        <span className="flex shrink-0 flex-wrap gap-1.5 text-micro">
          {Object.entries(issue.outcomes).map(([k, n]) => (
            <SignalLabel key={k} tone={outcomeTone[k] ?? "neutral"} label={`${k}·${n}`} className="font-data tabular-nums" />
          ))}
        </span>
        <span className="shrink-0 font-data text-micro tabular-nums text-ink-3">
          {t.lastSeen} {fmtClock(issue.last_seen)}
        </span>
      </button>
      {open ? (
        <div className="mt-2 space-y-2 border-t border-line pt-2">
          <pre className="overflow-x-auto whitespace-pre-wrap break-words font-data text-sec leading-relaxed text-ink-2">
            {issue.example_text || "—"}
          </pre>
          <a
            href={`/control/runs/${issue.example_run_id}`}
            className="inline-flex min-h-12 items-center rounded-card border border-line px-2.5 py-1 text-sec text-live hover:border-live hover:bg-live/10"
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
        <KpiTile label={t.podGroups} value={data.group_count} />
        <KpiTile label={t.podRuns} value={data.total_failed_runs} />
      </div>
      {data.truncated ? <div className="mt-2 flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{t.truncated}</div> : null}
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
        <h2 className="font-display text-h2 font-semibold text-ink">{t.title}</h2>
        <a
          href="/control/statistik"
          className="inline-flex min-h-12 items-center rounded-card border border-line px-3 py-1.5 text-sec text-live hover:border-live hover:bg-live/10"
        >
          {t.back}
        </a>
      </div>
      {error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{error}</div> : null}
      {data === null && !error ? <p className="text-sec text-ink-3">{t.loading}</p> : null}
      {data !== null ? <IssuesPanel data={data} /> : null}
    </section>
  );
}
