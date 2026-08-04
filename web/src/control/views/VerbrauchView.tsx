import { useState } from "react";
import { Link } from "react-router-dom";
import { ErrorNote, KpiTile, SectionHeader, SubtabChips, ViewHeader } from "../components/leitstand";
import { SkeletonCard } from "../components/primitives";
import { DayBarsCoverage, VsBars } from "../components/charts/charts";
import {
  USAGE_CONSUMPTION_CONTRACT,
  useUsageConsumption,
  type ConsumptionBreakdown,
  type UsageConsumptionLever,
  type UsageConsumptionResponse,
} from "../hooks/usageConsumption";

// Richtung C „Tageslichtung" (docs/observability/verbrauch-tab-design.md):
// Hebel-Sektion als Held oben (D5), Small Multiples je Origin mit Abdeckungs-
// Sättigung (D3), dann Breakdown/Fenster, Komponenten-Gegenüberstellung und
// Top-Läufe mit Sprüngen. Kein Pixel wird gegen einen unbekannten Vertrag
// gerechnet — wie HebelView: Contract-Mismatch ist ein eigener Zustand.

const fmtUsd = (v: number) =>
  `$${v.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const fmtUsd0 = (v: number) =>
  `$${Math.round(v).toLocaleString("de-DE")}`;
const fmtNum = (v: number) => Math.round(v).toLocaleString("de-DE");
const fmtTok = (v: number) =>
  v >= 1e9 ? `${(v / 1e9).toLocaleString("de-DE", { maximumFractionDigits: 1 })} Mrd`
    : v >= 1e6 ? `${(v / 1e6).toLocaleString("de-DE", { maximumFractionDigits: 1 })} Mio`
      : fmtNum(v);
const fmtPct = (ratio: number | "not applicable") =>
  ratio === "not applicable"
    ? "n. a."
    : `${(ratio * 100).toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} %`;

const SERIES_COLORS = [
  "var(--color-data-1)",
  "var(--color-data-2)",
  "var(--color-data-3)",
  "var(--color-data-4)",
  "var(--color-data-5)",
  "var(--color-data-6)",
  "var(--color-data-7)",
];

const WINDOW_OPTIONS = [7, 30, 90] as const;
const BREAKDOWN_OPTIONS: ReadonlyArray<{ id: ConsumptionBreakdown; label: string }> = [
  { id: "origin", label: "Quelle" },
  { id: "model", label: "Modell" },
  { id: "lane", label: "Lane" },
  { id: "buzz_unit", label: "Buzz-Agent" },
];

export function VerbrauchView() {
  const [days, setDays] = useState<number>(30);
  const [breakdown, setBreakdown] = useState<ConsumptionBreakdown>("origin");
  const { data, loading, error, reload } = useUsageConsumption(days, breakdown);

  if (loading && !data) {
    return (
      <main className="mx-auto flex max-w-6xl flex-col gap-8 p-4 md:p-6">
        <SkeletonCard rows={6} />
      </main>
    );
  }
  if (error || !data) {
    return (
      <main className="mx-auto flex max-w-6xl flex-col gap-8 p-4 md:p-6">
        <ErrorNote
          message="Verbrauch ist derzeit nicht verfügbar."
          detail={error ?? undefined}
          onRetry={() => void reload()}
        />
      </main>
    );
  }
  if (data.contract !== USAGE_CONSUMPTION_CONTRACT) {
    return (
      <main className="mx-auto flex max-w-6xl flex-col gap-8 p-4 md:p-6">
        <div className="hc-surface-card flex flex-col gap-1 p-4 text-sm" data-testid="verbrauch-contract-mismatch">
          <p className="font-semibold">Vertrag unbekannt — der Tab rechnet nicht dagegen.</p>
          <p className="hc-dim">
            Erwartet wird {USAGE_CONSUMPTION_CONTRACT}, geliefert wurde „{data.contract || "ohne Angabe"}“.
            Kosten werden im Lesepfad aus Rohfakten abgeleitet; gegen einen unbekannten Vertrag wäre jede Zahl geraten.
          </p>
        </div>
      </main>
    );
  }
  return (
    <VerbrauchBody
      data={data}
      days={days}
      onDays={setDays}
      breakdown={breakdown}
      onBreakdown={setBreakdown}
    />
  );
}

function VerbrauchBody({
  data,
  days,
  onDays,
  breakdown,
  onBreakdown,
}: {
  data: UsageConsumptionResponse;
  days: number;
  onDays: (days: number) => void;
  breakdown: ConsumptionBreakdown;
  onBreakdown: (breakdown: ConsumptionBreakdown) => void;
}) {
  const totals = data.totals;
  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-8 p-4 md:p-6" data-testid="verbrauch-view">
      <ViewHeader
        eyebrow={`VERBRAUCH · ${days} TAGE · UTC`}
        title="Verbrauch"
        description={
          <>
            Listenpreis-Äquivalent über Hermes und Buzz — als wäre alles über API abgerechnet.
            Vertrag {data.contract} · Tokens gemessen, Kosten abgeleitet · Abdeckung{" "}
            {fmtPct(data.totals.token_coverage.value)} der Tokens.
          </>
        }
        actions={
          <SubtabChips
            items={WINDOW_OPTIONS.map((w) => ({ id: w, label: `${w} Tage` }))}
            active={days}
            onSelect={onDays}
            ariaLabelPrefix="Zeitfenster"
          />
        }
      />

      {/* D5: die Hebel-Sektion ist der Held der Seite — sie steht oben. */}
      <LeverSection levers={data.levers} totalUsd={totals.equivalent_usd} />

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" data-testid="verbrauch-kpis">
        <KpiTile
          label={`Äquivalent ${days}d`}
          value={fmtUsd0(totals.equivalent_usd)}
          delta={`${fmtNum(totals.runs)} Läufe · bepreist ${fmtPct(totals.cost_coverage.value)}`}
        />
        <KpiTile
          label="Abo erspart"
          value={fmtUsd0(totals.subscription_saving_usd)}
          delta="metered $0,00 — das kostete der Wegfall des Abos"
        />
        <KpiTile
          label="Cache-Hit-Rate"
          value={fmtPct(data.cache_hit_rate.value)}
          delta={`${fmtTok(data.cache_hit_rate.numerator)} von ${fmtTok(data.cache_hit_rate.denominator)} Prompt-Tokens`}
        />
        <KpiTile
          label="Trend 7d vs. Fenster"
          value={data.trend.ratio_7d_vs_full.value === "not applicable" ? "n. a." : `${data.trend.ratio_7d_vs_full.value.toLocaleString("de-DE", { maximumFractionDigits: 2 })}×`}
          delta={`${fmtUsd0(data.trend.equivalent_usd_per_day_7d)}/Tag (7d) vs. ${fmtUsd0(data.trend.equivalent_usd_per_day_full)}/Tag`}
        />
      </section>

      <SmallMultiples data={data} />

      <section className="flex flex-col gap-3" data-testid="verbrauch-breakdown">
        <SectionHeader
          label="Aufriss"
          meta={
            <SubtabChips
              items={BREAKDOWN_OPTIONS.map((o) => ({ id: o.id, label: o.label }))}
              active={breakdown}
              onSelect={onBreakdown}
              ariaLabelPrefix="Aufriss"
            />
          }
        />
        {data.breakdown.series.length === 0 ? (
          <EmptyNote
            title="Keine Läufe im Fenster."
            body={`Im ${days}-Tage-Fenster liegt kein Verbrauch vor — ein leeres Fenster, kein Nullverbrauch.`}
          />
        ) : (
          <div className="flex flex-col gap-1.5">
            {data.breakdown.series.map((entry, index) => (
              <div
                key={entry.key}
                className="flex items-center gap-3 rounded-card border border-line bg-surface-2 px-3 py-2"
                data-testid={`verbrauch-breakdown-row-${entry.key}`}
              >
                <span className="size-2 shrink-0 rounded-full" style={{ background: SERIES_COLORS[index % SERIES_COLORS.length] }} aria-hidden />
                <span className="min-w-0 flex-1 truncate text-sm text-ink">{entry.key}</span>
                <span className="font-data text-sm tabular-nums text-ink">
                  {entry.equivalent_usd === "not applicable" ? "n. a." : fmtUsd(entry.equivalent_usd)}
                </span>
                <span className="hidden w-40 text-right text-xs text-ink-2 sm:inline">
                  {fmtTok(entry.tokens)} · {entry.runs} Läufe
                </span>
                <span className="w-24 text-right text-xs text-ink-3" title="Token-Abdeckung der Bepreisung">
                  {fmtPct(entry.token_coverage.value)} gedeckt
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      <ComponentShares data={data} />
      <TopRuns data={data} />

      <p className="hc-dim text-xs" data-testid="verbrauch-meta">
        Stand {new Date(data.generated_at).toLocaleString("de-DE")} · Fenster {data.window.days} Tage ab{" "}
        {data.window.cutoff_utc.slice(0, 10)} (UTC) · {data.confidence.tokens} Tokens / {data.confidence.costs} Kosten
      </p>
    </main>
  );
}

/** Held der Seite: die gerechneten Sparhebel mit Ist/Gegenrechnung. */
function LeverSection({ levers, totalUsd }: { levers: UsageConsumptionLever[]; totalUsd: number }) {
  return (
    <section className="flex flex-col gap-3" data-testid="verbrauch-levers">
      <SectionHeader label="Hebel" meta="gerechnet, nicht gemeint — Annahme und Plausibilität aufklappbar" />
      {levers.length === 0 ? (
        <EmptyNote
          title="Kein Hebel mit belastbarer Gegenrechnung."
          body="Ein Hebel ohne Gegenrechnung kommt nicht ins Ranking — das Fenster liefert derzeit keinen."
        />
      ) : (
        <div className="grid gap-3 lg:grid-cols-3">
          {levers.map((lever, index) => (
            <article
              key={lever.id}
              className="flex flex-col gap-3 rounded-panel border border-line bg-surface-2 p-4"
              data-testid={`verbrauch-lever-${lever.id}`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex flex-col gap-1">
                  <p className="hc-type-label hc-dim">{`Hebel ${index + 1}`}</p>
                  <p className="text-sm font-semibold text-ink">{lever.title}</p>
                </div>
                <VsBars current={lever.current_usd} counter={lever.counterfactual_usd} valueFmt={fmtUsd0} />
              </div>
              <p className="font-data text-2xl font-semibold tabular-nums text-ink">
                {fmtUsd(lever.savings_usd)}
                <small className="hc-dim text-xs font-normal">
                  {" "}pro {lever.id === "outlier-run-cap" ? "Lauf" : "Fenster"} ·{" "}
                  {lever.share_of_total.value === "not applicable" ? "n. a." : fmtPct(lever.share_of_total.value)} von {fmtUsd0(totalUsd)}
                </small>
              </p>
              <p className="text-xs text-ink-2">
                Ist {fmtUsd(lever.current_usd)} → Gegenrechnung {fmtUsd(lever.counterfactual_usd)}
              </p>
              <details className="text-xs text-ink-2">
                <summary className="cursor-pointer text-ink-2 underline decoration-dotted underline-offset-2">
                  Annahme und Plausibilität
                </summary>
                <p className="mt-1">Annahme: {lever.assumption}</p>
                <ul className="mt-1 list-inside list-disc text-ink-2">
                  {Object.entries(lever.plausibility).map(([key, value]) => (
                    <li key={key}>
                      {key}: {typeof value === "object" ? JSON.stringify(value) : String(value)}
                    </li>
                  ))}
                </ul>
              </details>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

/** Small Multiples: je Origin ein Tages-Chart, Abdeckung als Sättigung (D3).
 *  Direktbeschriftung statt Legende; jede Kachel trägt Summe + Abdeckung. */
function SmallMultiples({ data }: { data: UsageConsumptionResponse }) {
  const perOrigin = data.daily_by_origin;
  const origins = Object.keys(perOrigin);
  if (origins.length === 0) {
    return (
      <section className="flex flex-col gap-3" data-testid="verbrauch-small-multiples">
        <SectionHeader label="Verlauf" meta="Äquivalent pro Tag (UTC)" />
        <EmptyNote
          title="Kein Tagesverlauf im Fenster."
          body="Es gibt keine bepreisbaren Tageswerte — die Abdeckungszahlen oben sagen, ob das eine Daten- oder eine Preislücke ist."
        />
      </section>
    );
  }
  return (
    <section className="flex flex-col gap-3" data-testid="verbrauch-small-multiples">
      <SectionHeader label="Verlauf je Quelle" meta="Äquivalent pro Tag (UTC) · Sättigung = Preisabdeckung · eigene Skala je Kachel" />
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {origins.map((origin, index) => {
          const entries = perOrigin[origin];
          const total = entries.reduce((acc, d) => acc + d.equivalent_usd, 0);
          const coverageValues = entries
            .map((d) => d.token_coverage.value)
            .filter((v): v is number => typeof v === "number");
          const meanCoverage = coverageValues.length
            ? coverageValues.reduce((a, b) => a + b, 0) / coverageValues.length
            : null;
          return (
            <div key={origin} className="rounded-panel border border-line bg-surface-2 p-4" data-testid={`verbrauch-multiple-${origin}`}>
              <div className="flex items-baseline justify-between gap-2">
                <p className="truncate text-sm font-semibold text-ink">{origin}</p>
                <p className="font-data text-sm tabular-nums text-ink">{fmtUsd0(total)}</p>
              </div>
              <DayBarsCoverage
                points={entries.map((d) => ({
                  label: d.day,
                  value: d.equivalent_usd,
                  coverage: typeof d.token_coverage.value === "number" ? d.token_coverage.value : null,
                }))}
                color={SERIES_COLORS[index % SERIES_COLORS.length]}
                valueFmt={fmtUsd0}
              />
              <p className="mt-1 text-xs text-ink-3">
                {meanCoverage != null ? `${(meanCoverage * 100).toFixed(0)} % gedeckt` : "Abdeckung unbekannt"} · matte Balken = dünner gedeckt
              </p>
            </div>
          );
        })}
      </div>
    </section>
  );
}

/** Token- und Kostenanteil nebeneinander — Output ist der teure Teil. */
function ComponentShares({ data }: { data: UsageConsumptionResponse }) {
  const shares = data.component_shares;
  const keys = ["billable_input", "cache_read", "output", "cache_write_1h", "cache_write_5m", "cache_write_unsplit"] as const;
  const labels: Record<string, string> = {
    billable_input: "Input (neu)",
    cache_read: "Cache-Read",
    output: "Output",
    cache_write_1h: "Cache-Write 1h",
    cache_write_5m: "Cache-Write 5m",
    cache_write_unsplit: "Cache-Write",
  };
  const shareValue = (key: string, kind: "token_share" | "cost_share") => {
    const entry = shares[key];
    if (!entry) return null;
    const rate = entry[kind];
    if (rate === "not applicable" || rate == null) return null;
    return typeof rate.value === "number" ? rate.value : null;
  };
  const maxShare = Math.max(
    0.01,
    ...keys.flatMap((k) => [shareValue(k, "token_share") ?? 0, shareValue(k, "cost_share") ?? 0]),
  );
  return (
    <section className="flex flex-col gap-3" data-testid="verbrauch-components">
      <SectionHeader label="Zusammensetzung" meta="Anteil an Tokens vs. Anteil an Kosten — derselbe Maßstab" />
      <div className="flex flex-col gap-2 rounded-panel border border-line bg-surface-2 p-4">
        {keys.map((key) => {
          const token = shareValue(key, "token_share");
          const cost = shareValue(key, "cost_share");
          if (token == null && cost == null) return null;
          return (
            <div key={key} className="grid grid-cols-[7rem_1fr_1fr] items-center gap-2 sm:grid-cols-[9rem_1fr_1fr]">
              <span className="truncate text-xs text-ink-2">{labels[key]}</span>
              {[
                { value: token, color: "var(--color-data-6)", label: "Tokens" },
                { value: cost, color: "var(--color-data-5)", label: "Kosten" },
              ].map((bar) => (
                <div key={bar.label} className="flex items-center gap-2" title={`${labels[key]} — ${bar.label}`}>
                  <div className="h-2 flex-1 overflow-hidden rounded-[2px] bg-surface-3">
                    <div
                      className="h-full rounded-[2px]"
                      style={{
                        width: `${((bar.value ?? 0) / maxShare) * 100}%`,
                        background: bar.color,
                      }}
                    />
                  </div>
                  <span className="w-12 text-right font-data text-xs tabular-nums text-ink-2">
                    {bar.value == null ? "n. a." : `${(bar.value * 100).toFixed(0)} %`}
                  </span>
                </div>
              ))}
            </div>
          );
        })}
        <p className="text-xs text-ink-3">Links Tokenanteil, rechts Kostenanteil — Output kostet pro Token etwa das Fünffache.</p>
      </div>
    </section>
  );
}

/** Die teuersten Einzelläufe mit Sprung zur Task/Session (D4). */
function TopRuns({ data }: { data: UsageConsumptionResponse }) {
  const runs = data.top_runs;
  return (
    <section className="flex flex-col gap-3" data-testid="verbrauch-top-runs">
      <SectionHeader label="Teuerste Läufe" meta={`Top ${runs.length} im Fenster`} />
      {runs.length === 0 ? (
        <EmptyNote
          title="Keine bepreisbaren Läufe im Fenster."
          body="Entweder liegt kein Verbrauch vor oder die Läufe tragen Modelle ohne Preis — die Abdeckungszahlen oben sagen welches."
        />
      ) : (
        <div className="flex flex-col gap-1.5">
          {runs.map((run) => (
            <div
              key={run.run_id}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-card border border-line bg-surface-2 px-3 py-2"
            >
              <span className="font-data text-sm tabular-nums text-ink">{fmtUsd(run.equivalent_usd)}</span>
              <span className="min-w-0 flex-1 truncate font-data text-xs text-ink-2" title={run.run_id}>
                {run.model ?? "Modell unbekannt"} · {run.origin ?? "?"} · {run.day ?? "?"}
              </span>
              <span className="text-xs text-ink-3">{fmtTok(run.tokens)}</span>
              {run.task_id ? (
                <Link
                  to={`/control/fleet?task=${encodeURIComponent(run.task_id)}`}
                  className="text-xs text-live underline-offset-2 hover:underline"
                >
                  Task {run.task_id}
                </Link>
              ) : run.session_id ? (
                <Link
                  to={`/control/agent-terminals`}
                  className="text-xs text-live underline-offset-2 hover:underline"
                  title={`Session ${run.session_id}`}
                >
                  Session
                </Link>
              ) : (
                <span className="text-xs text-ink-3">kein Sprungziel</span>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function EmptyNote({ title, body }: { title: string; body: string }) {
  return (
    <div className="hc-surface-card flex flex-col gap-1 p-4 text-sm">
      <p className="font-semibold">{title}</p>
      <p className="hc-dim">{body}</p>
    </div>
  );
}
