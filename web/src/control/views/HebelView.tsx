import type { ReactNode } from "react";
import { KpiTile, ListRow, SectionHeader } from "../components/leitstand";
import {
  USAGE_FACTS_CONTRACT_VERSION,
  useUsageFacts,
  type UsageFactsBillingCategory,
  type UsageFactsResponse,
} from "../hooks/usageFacts";
import {
  foldAuxGroups,
  foldGroupsByModel,
  foldGroupsByOrigin,
  share,
} from "../lib/usageFactsDerive";

// Zum Vertrag: der Tab zeigt ausschließlich, was das Backend rechnet
// (usage-facts.v1). Prozentwerte sind Anzeige-Formatierung zweier gelieferter
// Felder; Aggregate entstehen nur durch Faltung der voraggregierten Gruppen
// über origin/model (Brief §3a). `raw_input_tokens` wird bewusst nirgends
// gezeigt — die naive Summe halbiert die Wahrheit (Faktor ~2,09).

const fmtNum = (value: number) => Math.round(value).toLocaleString("de-DE");

const fmtPct = (ratio: number | null) =>
  ratio == null
    ? "—"
    : `${(ratio * 100).toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} %`;

/** Geldbetrag aus einem Preis-Block: `partial` ist eine Untergrenze (≥),
 *  `unknown` wird als unbekannt beschriftet — nie als "$0,00" verkauft. */
const fmtPrice = (price: {
  known_amount_usd: string;
  status: "complete" | "partial" | "unknown";
}) => {
  if (price.status === "unknown") return "unbekannt";
  const amount = Number(price.known_amount_usd);
  const rendered = Number.isFinite(amount)
    ? amount.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : price.known_amount_usd;
  return `${price.status === "partial" ? "≥ " : ""}$${rendered}`;
};

const fmtSemantics = (semantics: string) =>
  semantics === "cache_exclusive"
    ? "Cache exklusiv"
    : semantics === "cache_inclusive"
      ? "Cache inklusiv"
      : semantics;

const fmtGeneratedAt = (iso: string) => {
  const time = Date.parse(iso);
  return Number.isNaN(time) ? iso : new Date(time).toLocaleString("de-DE");
};

/** Datenserien-Palette (Identität, kein Statusvokabular — DESIGN.md W6-4).
 *  Volle Literal-Klassen, damit Tailwind sie generiert. */
const SERIES_DOT_CLASSES = [
  "bg-[var(--color-data-1)]",
  "bg-[var(--color-data-2)]",
  "bg-[var(--color-data-3)]",
  "bg-[var(--color-data-4)]",
  "bg-[var(--color-data-5)]",
  "bg-[var(--color-data-7)]",
];

export function HebelView() {
  const { data, loading, error } = useUsageFacts();
  if (loading && !data) {
    return <div className="hc-dim p-6">Hebel werden geladen …</div>;
  }
  if (error || !data) {
    return <div className="hc-dim p-6">Hebel sind derzeit nicht verfügbar.</div>;
  }
  if (data.contract_version !== USAGE_FACTS_CONTRACT_VERSION) {
    return (
      <main className="mx-auto flex max-w-6xl flex-col gap-8 p-4 md:p-6">
        <HebelHeader data={data} />
        <div className="hc-surface-card flex flex-col gap-1 p-4 text-sm" data-testid="hebel-contract-mismatch">
          <p className="font-semibold">Vertrag unbekannt — der Tab rechnet nicht dagegen.</p>
          <p className="hc-dim">
            Erwartet wird {USAGE_FACTS_CONTRACT_VERSION}, geliefert wurde „{data.contract_version || "ohne Angabe"}“.
            Token-Felder tragen je Origin eine andere Semantik; gegen einen unbekannten Vertrag wäre jede Zahl geraten.
          </p>
          <p className="hc-dim">Nächste Aktion: Read-Pfad und Tab auf denselben Vertragsstand bringen.</p>
        </div>
      </main>
    );
  }
  return <HebelBody data={data} />;
}

function HebelHeader({ data }: { data: UsageFactsResponse }) {
  return (
    <header>
      <p className="hc-type-label hc-dim">KOSTEN · FAKTENSCHICHT</p>
      <h1 className="hc-type-display">Hebel</h1>
      <p className="hc-dim">
        Normalisierter Kontext-Input über alle Welten — Cache-Read ist die Geschichte, nicht der Roh-Input.
        Vertrag {data.contract_version} · Normalisierung {data.normalization.version || "unbekannt"}.
      </p>
    </header>
  );
}

function HebelBody({ data }: { data: UsageFactsResponse }) {
  const totals = data.summary.tokens;
  const origins = foldGroupsByOrigin(data.groups);
  const models = foldGroupsByModel(data.groups);
  const aux = foldAuxGroups(data.groups);
  const auxShare = share(aux.contextInput, totals.context_input_tokens);

  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-8 p-4 md:p-6" data-testid="hebel-view">
      <HebelHeader data={data} />

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" data-testid="hebel-kpis">
        <KpiTile
          label="Kontext-Input gesamt"
          value={`${totals.has_lower_bounds ? "≥ " : ""}${fmtNum(totals.context_input_tokens)}`}
          delta={
            totals.has_lower_bounds
              ? "Untergrenze — Quellzeilen ohne Cache-Bucket"
              : `${fmtNum(data.summary.fact_rows)} Faktenzeilen · normalisiert`
          }
        />
        <KpiTile
          label="Cache-Read-Anteil"
          value={fmtPct(share(totals.cache_read_tokens, totals.context_input_tokens))}
          delta={`${fmtNum(totals.cache_read_tokens)} von ${fmtNum(totals.context_input_tokens)} Token`}
        />
        <KpiTile
          label="Neuer Input"
          value={fmtNum(totals.new_input_tokens)}
          delta="Kontext ohne Cache-Reads"
        />
        <KpiTile
          label="Kompression (Aux)"
          value={aux.factRows > 0 ? fmtPct(auxShare) : "—"}
          delta={
            aux.factRows > 0
              ? `${fmtNum(aux.factRows)} Faktenzeilen${aux.hasLowerBounds ? " · Untergrenze" : ""} — Erfassung neu, dünne Datenlage`
              : "Noch keine Aux-Fakten erfasst — keine Null-Aussage"
          }
        />
      </section>

      <BillingSection data={data} />

      <section className="flex flex-col gap-3" data-testid="hebel-origins">
        <SectionHeader label="Welten" meta={`${origins.length} Origins · nach Kontext-Input`} />
        {origins.length === 0 ? (
          <EmptyNote
            title="Keine token-tragenden Faktenzeilen im Scope."
            body="Der Endpunkt aggregiert über alle vorhandenen Zeilen; ohne Zeilen gibt es keinen Hebel zu zeigen — das ist eine Datenlücke, kein Nullverbrauch."
          />
        ) : (
          origins.map((fold, index) => (
            <ListRow
              key={fold.origin}
              leading={<span className={`size-2 rounded-full ${SERIES_DOT_CLASSES[index % SERIES_DOT_CLASSES.length]}`} aria-hidden />}
              title={fold.origin}
              meta={`${fold.hasLowerBounds ? "≥ " : ""}${fmtNum(fold.contextInput)} Kontext · ${fmtPct(share(fold.cacheRead, fold.contextInput))} Cache-Read · ${fmtNum(fold.newInput)} neu · n = ${fmtNum(fold.factRows)}`}
              trailing={
                <span className="text-xs text-ink-3">
                  {fmtSemantics(data.normalization.origins[fold.origin]?.input_semantics ?? "unknown")}
                </span>
              }
            />
          ))
        )}
      </section>

      <section className="flex flex-col gap-3" data-testid="hebel-models">
        <SectionHeader label="Modelle" meta={`Top ${models.rows.length} nach Kontext-Input`} />
        {models.rows.map((fold, index) => (
          <ListRow
            key={fold.label}
            leading={<span className={`size-2 rounded-full ${SERIES_DOT_CLASSES[index % SERIES_DOT_CLASSES.length]}`} aria-hidden />}
            title={fold.label}
            meta={`${fold.hasLowerBounds ? "≥ " : ""}${fmtNum(fold.contextInput)} Kontext · n = ${fmtNum(fold.factRows)}`}
          />
        ))}
        {models.restRows > 0 ? (
          <p className="hc-dim text-sm">
            … und {fmtNum(models.restRows)} weitere Modelle ({fmtNum(models.restContextInput)} Kontext).
          </p>
        ) : null}
        <UnattributedNote data={data} />
      </section>

      <KanbanSection data={data} />

      <p className="hc-dim text-xs" data-testid="hebel-meta">
        Stand {fmtGeneratedAt(data.generated_at)} · {fmtNum(data.summary.fact_rows)} Faktenzeilen ·{" "}
        {fmtNum(data.summary.group_count)} Gruppen (origin × profile × lane × model) · Vertrag {data.contract_version}
      </p>
    </main>
  );
}

/** Dollar und Quota bleiben getrennte Größen (Brief §2.8): drei Töpfe,
 *  drei Darstellungen, keine gemeinsame $-Spalte. */
function BillingSection({ data }: { data: UsageFactsResponse }) {
  const { metered, quota, unclassified } = data.summary.billing;
  return (
    <section className="flex flex-col gap-3" data-testid="hebel-billing">
      <SectionHeader label="Abrechnung" meta="getrennte Töpfe — nicht addierbar" />
      <div className="grid gap-3 md:grid-cols-3">
        <BillingPot title="Metered" category={metered}>
          {metered.fact_rows === 0 ? (
            <PotLine>Kein metered Verbrauch erfasst — es gibt hier keinen echten Dollar-Spend.</PotLine>
          ) : (
            <PotValue value={metered.metered_usd ? fmtPrice(metered.metered_usd) : "—"} label="metered" />
          )}
        </BillingPot>
        <BillingPot title="Quota (Abo)" category={quota}>
          {quota.fact_rows === 0 ? (
            <PotLine>Keine Quota-Zeilen erfasst.</PotLine>
          ) : (
            <>
              <PotLine>Grenzkosten $0 — Abo-Fenster, kein Spend.</PotLine>
              {quota.list_equivalent_usd ? (
                <PotValue
                  value={fmtPrice(quota.list_equivalent_usd)}
                  label={`Listenpreis-Äquivalent — kein Spend · ${priceCoverage(quota.list_equivalent_usd)}`}
                />
              ) : null}
            </>
          )}
        </BillingPot>
        <BillingPot title="Nicht klassifiziert" category={unclassified}>
          {unclassified.fact_rows === 0 ? (
            <PotLine>Alle Zeilen klassifiziert.</PotLine>
          ) : (
            <>
              <PotValue value={fmtNum(unclassified.tokens.context_input_tokens)} label="Kontext-Input" />
              <PotLine>
                Abrechnungsart geprüft, aber nicht ableitbar ({unclassified.reason ?? "unbekannt"}) — keine
                stillschweigende Zuordnung.
              </PotLine>
            </>
          )}
        </BillingPot>
      </div>
    </section>
  );
}

function priceCoverage(price: { priced_breakdowns: number; unpriced_breakdowns: number }) {
  return `${fmtNum(price.priced_breakdowns)} bepreist · ${fmtNum(price.unpriced_breakdowns)} unbepreist`;
}

function BillingPot({
  title,
  category,
  children,
}: {
  title: string;
  category: UsageFactsBillingCategory;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5 rounded-card border border-line bg-surface-2 px-3 py-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <p className="hc-type-label hc-dim">{title}</p>
        <p className="font-data text-xs tabular-nums text-ink-3">n = {fmtNum(category.fact_rows)}</p>
      </div>
      {children}
    </div>
  );
}

function PotValue({ value, label }: { value: string; label: string }) {
  return (
    <p className="font-data text-lg font-semibold tabular-nums text-ink">
      <span className="whitespace-nowrap">{value}</span>{" "}
      <small className="hc-dim text-xs font-normal">{label}</small>
    </p>
  );
}

function PotLine({ children }: { children: ReactNode }) {
  return <p className="text-sm text-ink-2">{children}</p>;
}

/** `model IS NULL` bleibt sichtbar — er fällt weder aus Gruppen noch aus Summen. */
function UnattributedNote({ data }: { data: UsageFactsResponse }) {
  const unattributed = data.unattributed;
  if (unattributed.fact_rows === 0) return null;
  return (
    <div className="hc-surface-card flex flex-col gap-1 p-4 text-sm" data-testid="hebel-unattributed">
      <p className="font-semibold">
        {unattributed.label}: {fmtNum(unattributed.tokens.context_input_tokens)} Kontext-Input ohne Modellangabe.
      </p>
      <p className="hc-dim">
        Grund „{unattributed.reason}“ — die Modellidentität ist nicht belegbar, die Token bleiben trotzdem in allen Summen.
      </p>
      {unattributed.by_origin.length > 0 ? (
        <p className="hc-dim">
          {unattributed.by_origin
            .map((entry) => `${entry.origin}: ${fmtNum(entry.tokens.context_input_tokens)} Kontext · n = ${fmtNum(entry.fact_rows)}`)
            .join(" · ")}
        </p>
      ) : null}
    </div>
  );
}

/** Die Kanban-Spalte ist strukturell dünn (Hooks neu) — sichtbar als dünne
 *  Abdeckung, nie als „kostet nichts“. */
function KanbanSection({ data }: { data: UsageFactsResponse }) {
  const kanban = data.kanban;
  return (
    <section className="flex flex-col gap-3" data-testid="hebel-kanban">
      <SectionHeader label="Kanban-Board" meta="S5-Providerprojektion · read-only" />
      {!kanban.available || !kanban.usage_coverage ? (
        <EmptyNote
          title="Kanban-Projektion nicht verfügbar."
          body={`Grund „${kanban.reason ?? "unbekannt"}“ — die Board-Spalte fehlt, sie wird nicht als Nullverbrauch dargestellt.`}
        />
      ) : kanban.total_runs === 0 ? (
        <EmptyNote
          title="Keine Board-Runs im Scope."
          body="Die Providerprojektion fand keine Runs — das ist ein leeres Board, kein Nullverbrauch und keine dünne Hook-Abdeckung."
        />
      ) : (
        <KanbanCoverage
          totalRuns={kanban.total_runs}
          coverage={kanban.usage_coverage}
          classification={kanban.provider_classification}
        />
      )}
    </section>
  );
}

function KanbanCoverage({
  totalRuns,
  coverage,
  classification,
}: {
  totalRuns: number;
  coverage: {
    token_bearing_runs: number;
    provider_only_fact_runs: number;
    runs_without_fact: number;
    state: string;
  };
  classification: Record<string, number>;
}) {
  const thin = coverage.state === "thin";
  return (
    <>
      <div className="grid gap-3 sm:grid-cols-3">
        <KpiTile
          label="Token-tragende Runs"
          value={fmtNum(coverage.token_bearing_runs)}
          delta={`von ${fmtNum(totalRuns)} Board-Runs`}
        />
        <KpiTile
          label="Runs ohne Faktenzeile"
          value={fmtNum(coverage.runs_without_fact)}
          delta={thin ? "strukturell leer — Hooks laufen erst seit Kurzem" : "ohne Faktenzeile"}
        />
        <KpiTile
          label="Abdeckung"
          value={thin ? "dünn" : coverage.state}
          delta={thin ? "historische Hook-Lücke, kein Nullverbrauch" : "Usage-Coverage-State"}
        />
      </div>
      {thin ? (
        <div className="hc-surface-card flex flex-col gap-1 p-4 text-sm" data-testid="hebel-kanban-thin">
          <p className="font-semibold">Dünne Datenlage — keine Null-Kosten-Aussage.</p>
          <p className="hc-dim">
            Nur {fmtNum(coverage.token_bearing_runs)} von {fmtNum(totalRuns)} Board-Runs tragen Token-Fakten; die
            Hook-Erfassung ist neu und alles davor ist strukturell leer. Der Board-Verbrauch lebt primär in den
            ETL-Backfills, nicht in dieser Spalte.
          </p>
        </div>
      ) : null}
      <p className="hc-dim text-sm">
        Provider-Klassifikation:{" "}
        {Object.entries(classification)
          .map(([label, count]) => `${label} ${fmtNum(count)}`)
          .join(" · ") || "keine"}
      </p>
    </>
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
