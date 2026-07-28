/**
 * OutcomesPanel — die Sektion „Letzte 24 Stunden" des Worker-Tabs (Mockup V2,
 * 2026-07-27). Links der gestapelte Outcome-Balken mit Legende (Anteile aus
 * outcomeLegend, Status-Trio + Bronze für laufende Runs), rechts die kompakte
 * Runs-Liste: Uhrzeit, Run-Nr, Titel · Profil, Verdict-Chip (APPROVED=ok,
 * sonst warn), Outcome-Chip, Dauer, Tokens und bei Fehlschlägen eine rote
 * Fehlerzeile.
 *
 * Daten: GET /runs/outcomes?hours=24 (useRunsOutcomes, 60-s-Poll). Rein
 * präsentational, Farben nur via fleet.css-Klassen (DESIGN.md Regel 9);
 * Mono nur für Daten (Zeiten, Nummern, Tokens, Dauer).
 */
import {
  fmtClockHM,
  fmtDurationClock,
  fmtTokens,
  outcomeLegend,
  outcomeTone,
  verdictTone,
} from "../../lib/fleetHub";
import type { RunsOutcomesResponse, RunsOutcomeEntry } from "../../lib/schemas";
import { de } from "../../i18n/de";

function OutcomeBar({ outcomes }: { outcomes: Record<string, number> }) {
  const legend = outcomeLegend(outcomes);
  if (legend.length === 0) return null;
  return (
    <>
      <div className="fleet-obar" role="img" aria-label={de.fleet.outcomesDistribution}>
        {legend.map((row) => (
          <span
            key={row.outcome}
            className={`fleet-obar-seg fleet-oc-bg-${row.tone}`}
            style={{ width: `${(row.fraction * 100).toFixed(2)}%` }}
            title={`${row.outcome}: ${row.count}`}
          />
        ))}
      </div>
      <div className="fleet-oleg">
        {legend.map((row) => (
          <div key={row.outcome} className="fleet-oleg-row">
            <span className={`fleet-oleg-sw fleet-oc-bg-${row.tone}`} />
            {row.outcome}
            <span className="fleet-oleg-n">{row.count}</span>
            <span className="fleet-oleg-pct">
              {(row.fraction * 100).toLocaleString("de-DE", { maximumFractionDigits: 1 })} %
            </span>
          </div>
        ))}
      </div>
    </>
  );
}

function OutcomeRunRow({ run, now }: { run: RunsOutcomeEntry; now: number }) {
  const vTone = verdictTone(run.verdict);
  const oTone = outcomeTone(run.outcome);
  const dur =
    run.started_at != null
      ? fmtDurationClock((run.ended_at ?? now) - run.started_at)
      : "—";
  const hasTokens = run.input_tokens != null || run.output_tokens != null;
  return (
    <>
      <div className="fleet-orow">
        <span className="fleet-orow-time">{run.started_at != null ? fmtClockHM(run.started_at) : "—"}</span>
        <span className="fleet-orow-id">#{run.run_id}</span>
        <span className="fleet-orow-title" title={run.task_title}>
          <b>{run.task_title}</b>
          {run.profile ? ` · ${run.profile}` : ""}
        </span>
        <span className="fleet-orow-right">
          {vTone ? <span className={`fleet-vd fleet-vd-${vTone}`}>{run.verdict}</span> : null}
          <span className={`fleet-oc fleet-oc-${oTone}`}>{run.outcome}</span>
          <span className="fleet-orow-dur">{dur}</span>
          <span className="fleet-orow-tok">
            {hasTokens ? `${fmtTokens(run.input_tokens)}↓ ${fmtTokens(run.output_tokens)}↑` : "—"}
          </span>
        </span>
      </div>
      {run.error ? <div className="fleet-oerr">{run.error}</div> : null}
    </>
  );
}

export function OutcomesPanel({
  data,
  loading,
  error = null,
  now,
}: {
  data: RunsOutcomesResponse | null;
  loading: boolean;
  /** T3: Fetch-Fehler ist ein eigener Zustand, nicht der Leerzustand. */
  error?: string | null;
  now: number;
}) {
  const runs = data?.runs ?? [];
  // T4: die Liste kappt serverseitig (limit), der Kopf nennt die Gesamtzahl —
  // die Differenz wird als „+N weitere" ausgewiesen.
  const moreCount = (data?.total ?? 0) - runs.length;
  return (
    <section className="fleet-outcomes" aria-label={de.fleet.outcomesTitle}>
      <div className="fleet-sec">
        {de.fleet.outcomesTitle}
        <span className="fleet-sec-hint">
          {data ? de.fleet.outcomesRunsCount(data.total) : loading ? de.fleet.outcomesLoading : ""}
          {/* T5: die Sektion ist board-lokal — Scope wie bei den Pulse-Kacheln. */}
          {data ? ` · ${de.fleet.pulseScopeBoard}` : ""}
        </span>
      </div>
      {error ? (
        <div className="fleet-fx-note fleet-fx-note-empty" role="alert">
          {de.fleet.outcomesError}
        </div>
      ) : runs.length === 0 ? (
        <div className="fleet-fx-note fleet-fx-note-empty">
          {loading ? de.fleet.outcomesLoading : de.fleet.outcomesEmpty}
        </div>
      ) : (
        <div className="fleet-outcomes-grid">
          <div className="fleet-panel">
            <div className="fleet-fx-notes-head">{de.fleet.outcomesDistribution}</div>
            <OutcomeBar outcomes={data?.outcomes ?? {}} />
          </div>
          <div className="fleet-panel">
            <div className="fleet-fx-notes-head">{de.fleet.outcomesRuns}</div>
            <div className="fleet-oruns">
              {runs.map((run) => (
                <OutcomeRunRow key={run.run_id} run={run} now={now} />
              ))}
              {moreCount > 0 ? (
                <div className="fleet-fx-note fleet-fx-note-empty">{de.fleet.notesMore(moreCount)}</div>
              ) : null}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
