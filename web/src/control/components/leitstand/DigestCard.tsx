import { fetchJSON } from "@/lib/api";
import { usePolling } from "../../hooks/internal";
import { withBoardParam } from "../../lib/multiBoard";
import { parseOrThrow, ScoreDigestSchema, type ScoreDigest } from "../../lib/schemas";
import { KpiTile } from "./KpiTile";
import { ListRow } from "./ListRow";
import { SectionHeader } from "./SectionHeader";

/**
 * DigestCard — Wochen-Digest der Scorecard (SC-S4).
 *
 * Rendert ausschliesslich, was `/api/plugins/kanban/scores/digest` belegt
 * (Shape gemessen 2026-07-26 gegen einen Snapshot der Live-kanban.db):
 * Kopfzahlen mit WoW-Delta, Wochentrend, Aufschluesselung nach Profil und
 * Modell, Retry-Hotspots, Kosten/Dauer je freigegebenem Lauf und die
 * Top-Befunde. `top_findings` ist aktuell IMMER leer (die Score-
 * Materialisierung hat keinen Aufrufer) — dafuer gibt es einen ehrlichen
 * Empty-State statt erfundener Befunde.
 *
 * Darstellungs-Regeln: der Modellschluessel "?" wird als "ohne Modellangabe"
 * ausgewiesen (nie als Modellname durchgereicht), `cost_usd: null` wird als
 * "–" gezeigt (nie als 0), und Status wird nie nur ueber Farbe transportiert
 * (Vorzeichen + Pfeil im Delta, beschriftete Zustaende).
 */

/** Prozent de-DE, null tolerant. */
const formatPercent = (value: number | null) =>
  value == null
    ? "–"
    : `${(value * 100).toLocaleString("de-DE", { maximumFractionDigits: 1 })} %`;

/** Dauer lesbar skaliert: Sekunden, darueber Minuten, ab einer Stunde Stunden. */
const formatDuration = (seconds: number | null) => {
  if (seconds == null) return "–";
  if (seconds < 120) return `${Math.round(seconds).toLocaleString("de-DE")} s`;
  if (seconds < 3600) return `${(seconds / 60).toLocaleString("de-DE", { maximumFractionDigits: 1 })} min`;
  return `${(seconds / 3600).toLocaleString("de-DE", { maximumFractionDigits: 1 })} h`;
};

/** USD als "$1,23" (Hausstil wie fleetHub.fmtUsd); null bleibt "–", nie 0. */
const formatUsd = (value: number | null) =>
  value == null ? "–" : `$${value.toFixed(2).replace(".", ",")}`;

/** EUR de-DE fuer Befund-Impacts. */
const formatEur = (value: number) =>
  value.toLocaleString("de-DE", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });

/** WoW-Delta in Prozentpunkten: Pfeil + explizites Vorzeichen, nie nur Farbe. */
const formatWowDelta = (delta: number | null) => {
  if (delta == null) return "keine Vorwoche im Fenster";
  const pp = delta * 100;
  if (Math.abs(pp) < 0.05) return "→ ±0,0 pp zur Vorwoche";
  const arrow = pp > 0 ? "▲" : "▼";
  const sign = pp > 0 ? "+" : "−";
  return `${arrow} ${sign}${Math.abs(pp).toLocaleString("de-DE", { maximumFractionDigits: 1 })} pp zur Vorwoche`;
};

const wowDeltaTone = (delta: number | null): "up" | "down" | "neutral" => {
  if (delta == null || Math.abs(delta * 100) < 0.05) return "neutral";
  return delta > 0 ? "up" : "down";
};

/** "?" ist kein Modellname, sondern ein Lauf ohne aufgezeichnetes Modell. */
const MODEL_LABEL_FALLBACK = "ohne Modellangabe";
const modelLabel = (name: string) => (name === "?" ? MODEL_LABEL_FALLBACK : name);

/** Mehr als so viele Kosten-Zeilen traegt die Karte nicht auf einmal. */
const COST_ROWS_LIMIT = 10;

export function useScoreDigest(board: string | null = null) {
  const url = withBoardParam("/api/plugins/kanban/scores/digest", board);
  return usePolling<ScoreDigest>(
    board ? `score-digest:${board}` : "score-digest",
    async () => parseOrThrow(ScoreDigestSchema, await fetchJSON<unknown>(url), "score-digest"),
    60_000,
  );
}

/** Fetch-Huelle: laedt den Digest und delegiert an die reine Karte. */
export function DigestCard({ board = null }: { board?: string | null }) {
  const { data, loading, error } = useScoreDigest(board);
  if (loading && !data) return <div className="hc-dim p-6">Wochen-Digest wird geladen …</div>;
  if (error || !data) return <div className="hc-dim p-6">Wochen-Digest ist derzeit nicht verfügbar.</div>;
  return <DigestCardBody digest={data} />;
}

/** Reine Darstellung — direkt mit Fixture testbar. */
export function DigestCardBody({ digest }: { digest: ScoreDigest }) {
  const profiles = Object.entries(digest.by_profile).sort(([, a], [, b]) => b.total - a.total);
  const models = Object.entries(digest.by_model).sort(([, a], [, b]) => b.total - a.total);
  const costRows = digest.cost_duration_per_approved_run;
  const shownCostRows = costRows.slice(0, COST_ROWS_LIMIT);

  return (
    <section className="flex flex-col gap-3" data-testid="digest-card">
      <SectionHeader label="Wochen-Digest" meta={`${digest.weeks_requested} Wochen`} />

      <div className="grid gap-3 sm:grid-cols-3">
        <KpiTile
          label="Approval-Rate"
          value={formatPercent(digest.approval_rate)}
          delta={formatWowDelta(digest.wow_delta)}
          deltaTone={wowDeltaTone(digest.wow_delta)}
        />
        <KpiTile
          label="Review-Läufe"
          value={digest.rows_total.toLocaleString("de-DE")}
          delta="im Fenster bewertet"
        />
        <KpiTile
          label="Freigegeben"
          value={digest.approved_rows.toLocaleString("de-DE")}
          delta={`${formatPercent(digest.approval_rate)} Approval`}
        />
      </div>

      <section className="flex flex-col gap-3">
        <SectionHeader label="Wochentrend" meta="ISO-Wochen" />
        {digest.weekly.length === 0 ? (
          <p className="hc-dim text-sm">Keine Wochen im Fenster.</p>
        ) : (
          digest.weekly.map((row) => (
            <ListRow
              key={`${row.year}-${row.week}`}
              title={`KW ${String(row.week).padStart(2, "0")} · ${row.year}`}
              meta={`${row.approved_rows}/${row.rows_total} freigegeben · ${formatPercent(row.approval_rate)}`}
            />
          ))
        )}
      </section>

      <section className="flex flex-col gap-3">
        <SectionHeader label="Nach Profil" meta={`${profiles.length} Profile`} />
        {profiles.length === 0 ? (
          <p className="hc-dim text-sm">Keine Profil-Daten im Fenster.</p>
        ) : (
          profiles.map(([name, bucket]) => (
            <ListRow
              key={name}
              title={name}
              meta={`${bucket.approved}/${bucket.total} freigegeben · ${formatPercent(bucket.approval_rate)}`}
            />
          ))
        )}
      </section>

      <section className="flex flex-col gap-3">
        <SectionHeader label="Nach Modell" meta={`${models.length} Einträge`} />
        {models.length === 0 ? (
          <p className="hc-dim text-sm">Keine Modell-Daten im Fenster.</p>
        ) : (
          models.map(([name, bucket]) => (
            <ListRow
              key={name}
              title={modelLabel(name)}
              meta={`${bucket.approved}/${bucket.total} freigegeben · ${formatPercent(bucket.approval_rate)}`}
            />
          ))
        )}
      </section>

      <section className="flex flex-col gap-3">
        <SectionHeader label="Retry-Hotspots" meta="Rejections je Task" />
        {digest.retry_hotspots.length === 0 ? (
          <p className="hc-dim text-sm">Keine Retry-Hotspots im Fenster.</p>
        ) : (
          digest.retry_hotspots.map((spot) => (
            <ListRow
              key={spot.task_id}
              title={spot.task_id}
              meta={`${spot.rejections.toLocaleString("de-DE")} Rejections`}
            />
          ))
        )}
      </section>

      <section className="flex flex-col gap-3">
        <SectionHeader
          label="Kosten & Dauer je freigegebenem Lauf"
          meta={costRows.length > shownCostRows.length ? `${shownCostRows.length} von ${costRows.length}` : `${costRows.length} Läufe`}
        />
        {shownCostRows.length === 0 ? (
          <p className="hc-dim text-sm">Keine Kosten-Daten im Fenster.</p>
        ) : (
          shownCostRows.map((row) => (
            <ListRow
              key={row.run_id}
              title={row.task_id}
              meta={`Run ${row.run_id} · Dauer ${formatDuration(row.duration_seconds)} · Kosten ${formatUsd(row.cost_usd)}`}
            />
          ))
        )}
      </section>

      <section className="flex flex-col gap-3">
        <SectionHeader label="Top-Befunde" meta={`${digest.top_findings.length} Befunde`} />
        {digest.top_findings.length === 0 ? (
          <div
            className="hc-surface-card flex flex-col gap-1 p-4 text-sm"
            data-testid="digest-findings-empty"
          >
            <p className="font-semibold">Keine Top-Befunde vorhanden.</p>
            <p className="hc-dim">
              Situation: Die Befund-Liste ist leer, weil die zugrunde liegenden Score-Reihen
              (task_runs_to_done, operator_veto, cache_hit_ratio, queue_latency_seconds) derzeit
              nicht materialisiert werden.
            </p>
            <p className="hc-dim">
              Bewertung: Das ist eine Datenlücke, kein guter Zustand — fehlende Befunde bedeuten
              hier fehlende Daten, nicht Fehlerfreiheit.
            </p>
            <p className="hc-dim">
              Nächste Aktion: Score-Materialisierung im Backend verdrahten; bis dahin bleibt diese
              Sektion leer.
            </p>
          </div>
        ) : (
          digest.top_findings.map((finding, index) => (
            <ListRow
              key={`${finding.kind}-${index}`}
              title={finding.title}
              meta={`${finding.summary} · Impact ${formatEur(finding.impact_eur)} · Quelle ${finding.data_source}`}
            />
          ))
        )}
      </section>
    </section>
  );
}
