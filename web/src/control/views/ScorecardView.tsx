import { AlertTriangle, CheckCircle2, Database, Radio } from "lucide-react";
import { useMemo } from "react";
import { ErrorNote, FreshnessStrip, KpiTile, SectionHeader, ViewHeader } from "../components/leitstand";
import { SkeletonCard } from "../components/primitives";
import { useScorecard } from "../hooks/scorecard";
import type { QualitySnapshot, ScorecardResponse } from "../lib/schemas";
import { cn } from "@/lib/utils";

const percent = (value: number | null) =>
  value == null ? "—" : `${(value * 100).toLocaleString("de-DE", { maximumFractionDigits: 1 })} %`;

const number = (value: number) => value.toLocaleString("de-DE");

const OUTCOME_LABELS: Record<string, string> = {
  completed: "Abgeschlossen",
  blocked: "Blockiert",
  scheduled: "Geplant",
  unknown: "Unbekannt",
  "unknown_outcome_code:0.0": "Unbekannt",
  iteration_budget_exhausted: "Iterationsbudget erschöpft",
  spawn_failed: "Spawn fehlgeschlagen",
  gave_up: "Aufgegeben",
  timed_out: "Zeitlimit erreicht",
  reclaimed: "Zurückgeholt",
  crashed: "Abgestürzt",
};

/**
 * Rückfallform für eine Payload ohne `quality` — das passiert im Deploy-Versatz,
 * wenn das neue Frontend schon ausgeliefert ist, der Dashboard-Dienst aber noch
 * die alte Route im Speicher hält.
 *
 * Diese Form darf NICHTS behaupten, was v1 nicht liefert:
 *  - `coverage` gab es in v1 nicht. Eine frühere Fassung setzte hier
 *    `review_verdicts: data.overall.runs` neben `runs: data.overall.runs` —
 *    Zähler gleich Nenner, also stets exakt 100 % samt grünem Haken. Das war
 *    ein erfundener Vollständigkeitsbeweis über einer Zahl, die in v2 bei rund
 *    29 % liegt. Nenner 0 lässt Aufrufer und `CoverageRail` die Quote korrekt
 *    als unbekannt behandeln.
 *  - `overall` aggregiert in v1 die GESAMTE Score-Tabelle ohne Zeitfilter.
 *    Ein hartes `window_days: 7` hätte eine All-Time-Zahl als Wochenzahl
 *    beschriftet; 0 heißt "kein Fenster" und wird im Kopf so gerendert.
 */
function legacyQuality(data: ScorecardResponse): QualitySnapshot {
  return {
    window_days: 0,
    overall: data.overall,
    verdicts: data.verdicts,
    outcomes: {},
    profiles: data.profiles,
    models: data.models,
    daily_verdicts: [],
    review_iterations: { average: null, count: 0, distribution: {} },
    coverage: {
      runs: 0,
      review_verdicts: 0,
      run_outcomes: 0,
      review_iterations: 0,
    },
    source: "kanban.metric_scores",
    captured_at: data.checked_at,
  };
}

export function ScorecardView() {
  const { data, loading, error, lastUpdated, reload } = useScorecard();
  // Leer ≠ ladend ≠ Fehler (DESIGN.md W4-7): Skeleton beim Erst-Load statt
  // Dauer-"wird geladen", ErrorNote mit Retry statt passivem Text.
  if (loading && !data) {
    return (
      <main data-scorecard className="mx-auto flex w-full max-w-[2100px] flex-col gap-4 p-4 md:p-6">
        <SkeletonCard rows={6} />
      </main>
    );
  }
  if (error || !data) {
    return (
      <main data-scorecard className="mx-auto flex w-full max-w-[2100px] flex-col gap-4 p-4 md:p-6">
        <ErrorNote
          message="Scorecard ist derzeit nicht verfügbar."
          detail={error ?? undefined}
          onRetry={() => void reload()}
        />
      </main>
    );
  }

  const quality = data.quality ?? legacyQuality(data);
  const completed = quality.outcomes.completed ?? 0;
  const coverage = quality.coverage.runs
    ? quality.coverage.review_verdicts / quality.coverage.runs
    : null;
  const sourceState = data.observability?.state ?? "absent";
  const sourceLabel = sourceState === "fresh"
    ? "Langfuse frisch"
    : sourceState === "stale"
      ? `Langfuse veraltet · ${data.observability?.cache.age_seconds ?? "?"} s`
      : sourceState === "partial"
        ? `Langfuse teilweise · Cache ${data.observability?.cache.age_seconds ?? "?"} s`
        : sourceState === "unknown"
          ? "Langfuse unknown · lokale Scores"
          : "Langfuse absent · lokale Scores";

  return (
    <main data-scorecard className="mx-auto flex w-full max-w-[2100px] flex-col gap-4 p-4 md:p-6">
      <ViewHeader
        eyebrow={`QUALITÄT · ENTSCHEIDUNGEN · ${quality.window_days > 0 ? `${quality.window_days} TAGE` : "GESAMTZEITRAUM"}`}
        title="Scorecard"
        description="Eine Entscheidung, dann ihre Begründung."
        actions={(
          <div className={cn(
            "inline-flex min-h-10 w-fit items-center gap-2 rounded-full border px-3 text-micro font-semibold",
            sourceState === "fresh"
              ? "border-status-ok/40 text-status-ok"
              : "border-status-warn/40 text-status-warn",
          )}>
            <span className="size-2 rounded-full bg-current" aria-hidden />
            {sourceLabel}
          </div>
        )}
      />
      <FreshnessStrip lastUpdated={lastUpdated} onRefresh={() => reload()} />

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Qualitäts-Kernzahlen">
        <KpiTile
          label="Freigabe"
          value={percent(quality.overall.approval_rate)}
          delta={`${number(quality.overall.approved)} / ${number(quality.overall.runs)} entschiedene Reviews`}
          dot="live"
        />
        <KpiTile
          label="Abgeschlossen"
          value={number(completed)}
          delta={`${number(quality.coverage.run_outcomes)} Outcome-Zeilen`}
        />
        <KpiTile
          label="Review-Abdeckung"
          value={percent(coverage)}
          delta={`${number(quality.coverage.review_verdicts)} Verdicts / ${number(quality.coverage.runs)} Runs`}
          deltaTone={coverage != null && coverage < 0.5 ? "down" : "neutral"}
        />
        <KpiTile
          label="Abgelehnt"
          value={number(quality.verdicts.rejected)}
          delta={`${number(quality.verdicts.approved)} freigegeben`}
          deltaTone={quality.verdicts.rejected > quality.verdicts.approved ? "down" : "neutral"}
        />
      </section>

      <section className="grid gap-4 2xl:grid-cols-[minmax(0,1.45fr)_minmax(360px,.55fr)]">
        <DecisionPanel quality={quality} />
        <IterationPanel quality={quality} />
      </section>

      <ModelQualityPanel quality={quality} />
      <CoverageRail quality={quality} state={sourceState} />

      <footer className="flex justify-between gap-4 text-micro font-semibold text-ink-3">
        <span>Quelle: {quality.source} · Stand {new Date(quality.captured_at * 1000).toLocaleString("de-DE")}</span>
        <span>/control/scorecard</span>
      </footer>
    </main>
  );
}

function DecisionPanel({ quality }: { quality: QualitySnapshot }) {
  const totalVerdicts = quality.verdicts.approved + quality.verdicts.rejected;
  const approvedShare = totalVerdicts
    ? (quality.verdicts.approved / totalVerdicts) * 100
    : 0;
  const outcomes = Object.entries(quality.outcomes)
    .sort(([, left], [, right]) => right - left);
  const topOutcome = Math.max(1, ...outcomes.map(([, count]) => count));
  return (
    <article className="hc-surface-card p-4 md:p-5">
      <SectionHeader label="Entscheidungen" meta={`Review verdict · n ${number(totalVerdicts)}`} />
      {totalVerdicts ? (
        <div className="mt-4 flex h-4 overflow-hidden rounded-full bg-surface-3" aria-label={`${approvedShare.toFixed(1)} Prozent freigegeben`}>
          <i className="bg-status-ok" style={{ width: `${approvedShare}%` }} />
          <i className="flex-1 bg-status-alert" />
        </div>
      ) : (
        <div className="mt-4 h-4 rounded-full bg-surface-3" aria-label="Keine Verdict-Daten" />
      )}
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-micro font-semibold text-ink-2">
        <span><b className="text-status-ok">{number(quality.verdicts.approved)}</b> freigegeben</span>
        <span><b className="text-status-alert">{number(quality.verdicts.rejected)}</b> abgelehnt</span>
        <span>{totalVerdicts ? "Nenner sichtbar" : "Keine Verdict-Daten"}</span>
      </div>
      <DailyVerdictChart rows={quality.daily_verdicts} />
      <div className="mt-6 flex flex-col gap-3">
        {outcomes.map(([name, count]) => (
          <div key={name} className="grid grid-cols-[minmax(108px,1fr)_minmax(100px,2.6fr)_48px] items-center gap-2 text-micro font-semibold">
            <span className="min-w-0 whitespace-normal text-ink-2">{OUTCOME_LABELS[name] ?? name}</span>
            <span className="h-2 overflow-hidden rounded-full bg-surface-3">
              <i className="block h-full rounded-full bg-gradient-to-r from-data-1 to-live" style={{ width: `${Math.max(2, (count / topOutcome) * 100)}%` }} />
            </span>
            <b className="text-right font-data">{number(count)}</b>
          </div>
        ))}
        {outcomes.length === 0 ? <p className="hc-dim text-sm">Noch keine Outcome-Scores im gewählten Fenster.</p> : null}
      </div>
    </article>
  );
}

function DailyVerdictChart({ rows }: { rows: QualitySnapshot["daily_verdicts"] }) {
  const peak = Math.max(1, ...rows.flatMap((row) => [row.approved, row.rejected]));
  if (!rows.length) return null;
  return (
    <div className="mt-5">
      <div className="flex h-28 items-end gap-2 border-b border-line px-1" aria-label="Verdicts je Tag">
        {rows.map((row) => (
          <div key={row.date} className="relative flex h-full flex-1 items-end gap-0.5 pt-5">
            <i className="min-h-0.5 flex-1 rounded-t bg-status-ok" style={{ height: `${Math.max(2, (row.approved / peak) * 100)}%` }} title={`${row.approved} freigegeben`} />
            <i className="min-h-0.5 flex-1 rounded-t bg-status-alert" style={{ height: `${Math.max(2, (row.rejected / peak) * 100)}%` }} title={`${row.rejected} abgelehnt`} />
            <span className="absolute -bottom-4 left-1/2 -translate-x-1/2 text-micro font-semibold text-ink-3">{row.date.slice(8)}</span>
          </div>
        ))}
      </div>
      <div className="mt-5 flex justify-between text-micro font-semibold text-ink-3">
        <span>Verdicts je Tag</span><span>freigegeben / abgelehnt</span>
      </div>
    </div>
  );
}

function IterationPanel({ quality }: { quality: QualitySnapshot }) {
  const distribution = Object.entries(quality.review_iterations.distribution)
    .sort(([left], [right]) => Number(left) - Number(right));
  const peak = Math.max(1, ...distribution.map(([, count]) => count));
  return (
    <article className="hc-surface-card p-4 md:p-5">
      <SectionHeader label="Review-Iterationen" meta={`bis Freigabe · n ${number(quality.review_iterations.count)}`} />
      <div className="mt-4 flex h-40 items-end gap-3 border-b border-line px-1">
        {distribution.map(([label, count]) => (
          <div key={label} className="relative flex h-full flex-1 items-end pt-6">
            <i className="block w-full rounded-t bg-gradient-to-b from-live to-live/25" style={{ height: `${Math.max(2, (count / peak) * 100)}%` }} />
            <b className="absolute left-1/2 top-1 -translate-x-1/2 text-micro font-data text-ink-2">{number(count)}</b>
            <span className="absolute -bottom-4 left-1/2 -translate-x-1/2 text-micro font-semibold text-ink-3">{label}</span>
          </div>
        ))}
      </div>
      <div className="mt-7 rounded-xl border border-status-warn/30 bg-status-warn/5 p-3 text-micro font-semibold text-ink-2">
        Ø {quality.review_iterations.average?.toLocaleString("de-DE", { maximumFractionDigits: 2 }) ?? "—"} Iterationen · dünne Nenner bleiben sichtbar.
      </div>
    </article>
  );
}

function ModelQualityPanel({ quality }: { quality: QualitySnapshot }) {
  const models = useMemo(
    () => [...quality.models].sort((left, right) => (right.approval_rate ?? -1) - (left.approval_rate ?? -1)),
    [quality.models],
  );
  return (
    <section className="hc-surface-card p-4 md:p-5">
      <SectionHeader label="Modellqualität" meta="Freigabe · nur entschiedene Reviews" />
      <div className="mt-3">
        {models.map((row) => {
          const thin = row.runs < 50;
          return (
            <div key={row.name} className="grid min-h-12 grid-cols-[minmax(110px,1fr)_minmax(100px,2.5fr)_68px_44px] items-center gap-2 border-b border-line-soft text-[11px] font-semibold last:border-0">
              <span className="min-w-0 break-words">
                {row.name}
                {thin ? <span className="ml-1 text-status-warn">· dünn</span> : null}
              </span>
              <span className="h-1.5 overflow-hidden rounded-full bg-surface-3">
                <i className={cn("block h-full", thin ? "bg-status-warn" : "bg-data-4")} style={{ width: `${Math.max(0, (row.approval_rate ?? 0) * 100)}%` }} />
              </span>
              <b className={cn("text-right font-data", thin && "text-status-warn")}>{percent(row.approval_rate)}</b>
              <small className="text-right text-ink-3">n {number(row.runs)}</small>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function CoverageRail({ quality, state }: { quality: QualitySnapshot; state: string }) {
  const entries = [
    ["Review", quality.coverage.review_verdicts, quality.coverage.runs],
    ["Outcomes", quality.coverage.run_outcomes, quality.coverage.runs],
    ["Iterationen", quality.coverage.review_iterations, quality.coverage.review_verdicts],
  ] as const;
  return (
    <section className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4" aria-label="Datenabdeckung">
      {entries.map(([label, observed, denominator]) => {
        const ratio = denominator ? observed / denominator : null;
        return (
          <div key={label} className="rounded-xl border border-line bg-surface-1 p-3">
            <span className="text-micro font-medium hc-dim">{label}-Coverage</span>
            <div className="mt-2 flex items-center gap-2">
              {ratio != null && ratio >= 0.8
                ? <CheckCircle2 className="size-4 text-status-ok" />
                : <AlertTriangle className="size-4 text-status-warn" />}
              <b className="font-data text-sm">{number(observed)} / {number(denominator)}</b>
            </div>
          </div>
        );
      })}
      <div className="rounded-xl border border-line bg-surface-1 p-3">
        <span className="text-micro font-medium hc-dim">Data Plane</span>
        <div className="mt-2 flex items-center gap-2">
          {state === "fresh" ? <Radio className="size-4 text-status-ok" /> : <Database className="size-4 text-status-alert" />}
          <b className={cn("font-data text-sm", state === "fresh" ? "text-status-ok" : "text-status-alert")}>Langfuse {state}</b>
        </div>
      </div>
    </section>
  );
}
