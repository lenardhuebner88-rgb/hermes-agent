import { CheckCircle2, MinusCircle } from "lucide-react";
import { KpiTile, ListRow, SectionHeader, StatusChip } from "../components/leitstand";
import { useScorecard } from "../hooks/scorecard";
import type { MaterializedScore } from "../lib/schemas";

const percent = (value: number | null) => value == null ? "–" : `${(value * 100).toFixed(1)} %`;

/** Schlichte Zahl nach Hauskonvention de-DE (vgl. autoresearch.ts). */
const plainNumber = (value: number) =>
  value.toLocaleString("de-DE", { maximumFractionDigits: 2 });

/** Dauer lesbar skaliert: Sekunden, darueber Minuten, ab einer Stunde Stunden. */
const formatDuration = (seconds: number) => {
  if (seconds < 120) return `${Math.round(seconds).toLocaleString("de-DE")} s`;
  if (seconds < 3600) return `${(seconds / 60).toLocaleString("de-DE", { maximumFractionDigits: 1 })} min`;
  return `${(seconds / 3600).toLocaleString("de-DE", { maximumFractionDigits: 1 })} h`;
};

/**
 * Einheitengerechte Formatierung je bekannter Serie (SC-S5). Ein Score-Name,
 * der hier nicht vorkommt, faellt auf die schlichte Zahl zurueck — er
 * verschwindet nie und wird nie faelschlich als Prozentwert gezeigt.
 */
const SERIES_FORMATTERS: Record<string, (value: number) => string> = {
  run_cost_usd: (value) =>
    value.toLocaleString("de-DE", { style: "currency", currency: "USD", maximumSignificantDigits: 4 }),
  run_duration_seconds: formatDuration,
  run_tokens_total: (value) => Math.round(value).toLocaleString("de-DE"),
  run_attempt_index: plainNumber,
  review_iterations_to_approval: plainNumber,
};

const formatSeriesValue = (name: string, value: number) =>
  (SERIES_FORMATTERS[name] ?? plainNumber)(value);

/**
 * Kategoriale Haeufigkeitskarte, absteigend nach Anzahl. Labels werden
 * unveraendert uebernommen — auch `unknown_outcome_code:*`-Eintraege, die
 * bewusst vom Backend erhalten bleiben.
 */
const formatFrequencyMap = (map: Record<string, number>) =>
  Object.entries(map)
    .sort(([, a], [, b]) => b - a)
    .map(([label, count]) => `${label} (${Math.round(count).toLocaleString("de-DE")})`)
    .join(" · ");

/**
 * Datenserien-Palette (DESIGN.md: mehrere Serien = Datenfarben, kein
 * Statusvokabular). Volle Literal-Klassen, damit Tailwind sie generiert.
 */
const SERIES_DOT_CLASSES = [
  "bg-[var(--color-data-1)]",
  "bg-[var(--color-data-2)]",
  "bg-[var(--color-data-3)]",
  "bg-[var(--color-data-4)]",
  "bg-[var(--color-data-5)]",
  "bg-[var(--color-data-7)]",
];
/** Neutraler Serien-Token fuer dünne Datenlage — bewusst kein Status-Gruen/-Rot. */
const THIN_DOT_CLASS = "bg-[var(--color-data-6)]";
/** Unterhalb dieser Row-Anzahl gilt ein Score-Mittelwert als nicht belastbar. */
const MIN_EVIDENCE_ROWS = 5;

export function ScorecardView() {
  const { data, loading, error } = useScorecard();
  if (loading && !data) return <div className="hc-dim p-6">Scorecard wird geladen …</div>;
  if (error || !data) return <div className="hc-dim p-6">Scorecard ist derzeit nicht verfügbar.</div>;
  return <main className="mx-auto flex max-w-6xl flex-col gap-8 p-4 md:p-6">
    <header><p className="hc-type-label hc-dim">KANBAN · QUALITÄT</p><h1 className="hc-type-display">Lane Scorecard</h1><p className="hc-dim">Review-Entscheidungen, nach Lane und Modell aufgeschlüsselt.</p></header>
    <section className="grid gap-3 sm:grid-cols-3">
      <KpiTile label="Approval rate" value={percent(data.overall.approval_rate)} delta={`${data.overall.approved} freigegeben`} />
      <KpiTile label="Review runs" value={String(data.overall.runs)} delta="bewertete Läufe" />
      <KpiTile label="Verteilung" value={`${data.verdicts.approved} / ${data.verdicts.rejected}`} delta="approved / rejected" />
    </section>
    <ScoreSeriesSection scores={data.materialized_scores} />
    <section className="flex flex-col gap-3"><SectionHeader label="Lanes" meta={`${data.profiles.length} Profile`} />
      {data.profiles.map((row) => <ListRow key={row.name} leading={<span className="size-2 rounded-full bg-[var(--color-data-1)]" aria-hidden />} title={row.name} meta={`${percent(row.approval_rate)} · ${row.runs} Runs`} trailing={<StatusChip icon={CheckCircle2} label="Approval" value={percent(row.approval_rate)} hint={row.approval_rate != null && row.approval_rate >= .8 ? "stabil" : "prüfen"} tone={row.approval_rate != null && row.approval_rate >= .8 ? "emerald" : "amber"} />} />)}
    </section>
    <section className="flex flex-col gap-3"><SectionHeader label="Modelle" meta={`${data.models.length} aktiv`} />
      {data.models.map((row) => <ListRow key={row.name} title={row.name} meta={`${percent(row.approval_rate)} · ${row.runs} Runs`} />)}
    </section>
    <section className="flex flex-col gap-3"><SectionHeader label="Wochentrend" meta="ISO-Wochen" />
      {data.weeks.map((row) => <ListRow key={`${row.year}-${row.week}`} title={`${row.year} · W${String(row.week).padStart(2, "0")}`} meta={`${percent(row.approval_rate)} · ${row.approved}/${row.runs} approved`} />)}
    </section>
  </main>;
}

/**
 * Event- & Usage-Scores (SC-S1): materialisierte Serien einheitengerecht (SC-S5).
 * Numerische Serien zeigen Mittelwert, Maximum und Row-Anzahl; kategoriale
 * Serien zeigen ihre Haeufigkeitskarte. Dünne Nenner werden ausdruecklich als
 * solche beschriftet und nicht als Zielwert verkauft.
 */
function ScoreSeriesSection({ scores }: { scores: Record<string, MaterializedScore> }) {
  const entries = Object.entries(scores).sort(([a], [b]) => a.localeCompare(b));
  return (
    <section className="flex flex-col gap-3" data-testid="score-series">
      <SectionHeader label="Event- & Usage-Scores" meta={`${entries.length} Scores`} />
      {entries.length === 0 ? (
        <div className="hc-surface-card flex flex-col gap-1 p-4 text-sm" data-testid="score-series-empty">
          <p className="font-semibold">Keine materialisierten Event- und Usage-Scores.</p>
          <p className="hc-dim">Situation: Für keinen Score liegen Rows vor — der Endpunkt aggregiert ohne Zeitfenster über alle vorhandenen Rows.</p>
          <p className="hc-dim">Bewertung: Ohne Rows ist kein Zielwert bestätigt — das ist eine Datenlücke, kein Erfolg.</p>
          <p className="hc-dim">Nächste Aktion: Score-Materialisierung im Backend prüfen.</p>
        </div>
      ) : (
        entries.map(([name, score], index) => {
          const thin = score.count < MIN_EVIDENCE_ROWS;
          return (
            <ListRow
              key={name}
              leading={<span className={`size-2 rounded-full ${thin ? THIN_DOT_CLASS : SERIES_DOT_CLASSES[index % SERIES_DOT_CLASSES.length]}`} aria-hidden />}
              title={name}
              meta={seriesMeta(name, score, thin)}
              trailing={thin
                ? <StatusChip icon={MinusCircle} label="Zielwert" value="nicht bestätigt" hint={score.count === 0 ? "keine Rows" : `n < ${MIN_EVIDENCE_ROWS}`} tone="zinc" />
                : undefined}
            />
          );
        })
      )}
    </section>
  );
}

/** Meta-Zeile je Serie: leere Serie, numerische Serie oder Haeufigkeitskarte. */
function seriesMeta(name: string, score: MaterializedScore, thin: boolean) {
  if (score.count === 0) return "keine Rows · dünne Datenlage";
  const suffix = thin ? " · dünne Datenlage" : "";
  if (typeof score.value === "number") {
    const parts = [`Ø ${formatSeriesValue(name, score.value)}`];
    if (score.max != null) parts.push(`max ${formatSeriesValue(name, score.max)}`);
    parts.push(`n = ${score.count.toLocaleString("de-DE")}`);
    return parts.join(" · ") + suffix;
  }
  if (score.value != null) {
    return `${formatFrequencyMap(score.value)} · n = ${score.count.toLocaleString("de-DE")}${suffix}`;
  }
  return `n = ${score.count.toLocaleString("de-DE")}${suffix}`;
}
