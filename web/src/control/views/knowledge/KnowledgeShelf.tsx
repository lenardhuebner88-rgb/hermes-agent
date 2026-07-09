import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { BookOpen, Brain, Landmark, Newspaper, Search, Sparkles, Users, Workflow } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { FleetEmptyState } from "../../components/fleet/atoms";
import { ToneCallout } from "../../components/atoms";
import { fmtAge, nowSec } from "../../lib/derive";
import { toneClasses } from "../../lib/tones";
import { useKnowledgeCatalog } from "../../hooks/useControlData";
import {
  filterCatalog,
  knowledgeType,
  knowledgeTypeLabel,
  sectionsLabel,
  totalDocs,
  typeCounts,
  type KnowledgeCatalog,
  type KnowledgeCollection,
  type KnowledgeDoc,
  type KnowledgeTypeCount,
} from "./knowledge.helpers";
import { KnowledgeReader } from "./KnowledgeReader";

/** Sammlungs-Icon als statisches JSX (Backend liefert nur den Namen). Bewusst
 *  ein Switch statt dynamischer Komponenten-Auflösung → react-hooks/static-
 *  components-konform und für jeden Namen explizit. */
export function CollectionGlyph({ name, className }: { name: string; className?: string }) {
  switch (name) {
    case "Landmark":
      return <Landmark className={className} />;
    case "Workflow":
      return <Workflow className={className} />;
    case "Sparkles":
      return <Sparkles className={className} />;
    case "Users":
      return <Users className={className} />;
    case "Newspaper":
      return <Newspaper className={className} />;
    case "Brain":
      return <Brain className={className} />;
    default:
      return <BookOpen className={className} />;
  }
}

const t = {
  searchPlaceholder: "Im Nachschlagewerk suchen — Titel, Text, Tags …",
  searchAria: "Im Nachschlagewerk suchen",
  loadError: "Nachschlagewerk konnte nicht geladen werden.",
  loading: "Lade Nachschlagewerk …",
  emptyTitle: "Kein Treffer",
  emptyDesc: "Keine Sammlung enthält diesen Suchbegriff. Tipp anpassen oder Suche leeren.",
  hitsFor: (n: number, q: string) => `${n} Treffer für „${q}“`,
  docs: (n: number) => `${n} ${n === 1 ? "Dokument" : "Dokumente"}`,
  all: "Alle",
  collections: "Regale",
  types: "Typen",
  total: (n: number) => `${n} Dokumente`,
  updatedAgo: (age: string) => `aktualisiert vor ${age}`,
  pulsePrefix: "Neu entdeckt:",
};

function FilterButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`inline-flex min-h-8 items-center gap-1.5 rounded-full border px-2.5 py-1 text-[0.74rem] transition-colors ${
        active
          ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]"
          : "border-white/10 hc-soft hover:bg-white/5"
      }`}
    >
      {children}
    </button>
  );
}

function CountBadge({ count }: { count: number }) {
  return <span className="hc-mono text-[0.66rem] hc-dim">{count}</span>;
}

function CatalogFilters({
  catalog,
  activeCollection,
  activeType,
  onCollection,
  onType,
}: {
  catalog: KnowledgeCatalog;
  activeCollection: string | null;
  activeType: string | null;
  onCollection: (id: string | null) => void;
  onType: (id: string | null) => void;
}) {
  const types: KnowledgeTypeCount[] = typeCounts(catalog.collections);
  return (
    <div className="space-y-3 rounded-lg border border-[var(--hc-border)] bg-black/20 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="hc-eyebrow">{t.total(totalDocs(catalog.collections))}</p>
        <button
          type="button"
          onClick={() => {
            onCollection(null);
            onType(null);
          }}
          className="inline-flex min-h-8 items-center rounded-md border border-white/10 px-2.5 py-1 text-[0.72rem] hc-soft hover:bg-white/5"
        >
          Filter leeren
        </button>
      </div>

      <div className="space-y-2">
        <p className="text-[0.72rem] font-medium uppercase text-[var(--hc-muted)]">{t.collections}</p>
        <div className="flex flex-wrap gap-1.5">
          <FilterButton active={activeCollection === null} onClick={() => onCollection(null)}>
            {t.all}
            <CountBadge count={totalDocs(catalog.collections)} />
          </FilterButton>
          {catalog.collections.map((collection) => (
            <FilterButton
              key={collection.id}
              active={activeCollection === collection.id}
              onClick={() => onCollection(activeCollection === collection.id ? null : collection.id)}
            >
              {collection.title}
              <CountBadge count={collection.docs.length} />
            </FilterButton>
          ))}
        </div>
      </div>

      {types.length > 0 ? (
        <div className="space-y-2">
          <p className="text-[0.72rem] font-medium uppercase text-[var(--hc-muted)]">{t.types}</p>
          <div className="flex flex-wrap gap-1.5">
            <FilterButton active={activeType === null} onClick={() => onType(null)}>
              {t.all}
              <CountBadge count={totalDocs(catalog.collections)} />
            </FilterButton>
            {types.map((type) => (
              <FilterButton
                key={type.id}
                active={activeType === type.id}
                onClick={() => onType(activeType === type.id ? null : type.id)}
              >
                {type.label}
                <CountBadge count={type.count} />
              </FilterButton>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** Eine Doc-Karte im Regal. Exportiert für Unit-Tests. */
export function DocCard({ doc, onOpen }: { doc: KnowledgeDoc; onOpen: (doc: KnowledgeDoc) => void }) {
  const typeLabel = knowledgeTypeLabel(knowledgeType(doc));
  return (
    <button
      type="button"
      onClick={() => onOpen(doc)}
      className="hc-surface-card flex h-full min-h-[11rem] flex-col gap-2 p-4 text-left transition-colors hover:bg-white/5"
    >
      <p className="hc-eyebrow">{typeLabel}</p>
      <h4 className="text-[0.95rem] font-semibold leading-snug text-white">{doc.title}</h4>
      <p className="line-clamp-2 flex-1 text-[0.8rem] leading-relaxed hc-soft">{doc.summary}</p>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.68rem] hc-dim">
        <span className="hc-mono truncate">{doc.source_ref}</span>
        {doc.heading_count > 0 ? <span>· {sectionsLabel(doc.heading_count)}</span> : null}
      </div>
      {doc.tags.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {doc.tags.map((tag) => (
            <span key={tag} className="rounded-full border border-white/10 px-1.5 py-0.5 text-[0.6rem] hc-dim">{tag}</span>
          ))}
        </div>
      ) : null}
    </button>
  );
}

/** Kompakter, mobil einzeilig scrollbarer Puls-Strip: die letzten
 *  Modell-Discovery-Einträge des llm-wiki (siehe `_model_log_pulse`). */
function PulseStrip({ pulse }: { pulse: NonNullable<KnowledgeCollection["pulse"]> }) {
  return (
    <div className="mt-2 flex items-center gap-1.5 overflow-x-auto whitespace-nowrap pb-0.5">
      <span className="shrink-0 text-[0.68rem] hc-dim">{t.pulsePrefix}</span>
      {pulse.map((item, i) => (
        <span
          key={`${item.model}-${i}`}
          className="shrink-0 rounded-full border border-white/10 px-2 py-0.5 text-[0.66rem] hc-soft"
        >
          <span className="hc-mono">{item.model}</span> · {item.date}
        </span>
      ))}
    </div>
  );
}

/** Eine Sammlung (ein Regal): Akzent-Icon, Titel, Beschreibung, Doc-Raster.
 *  Exportiert für Unit-Tests. */
export function CollectionSection({ collection, now, onOpen }: {
  collection: KnowledgeCollection;
  /** epoch-Sekunden "jetzt" (aus `catalog.now`) für den "aktualisiert vor X"-Chip. */
  now: number;
  onOpen: (doc: KnowledgeDoc) => void;
}) {
  return (
    <section className="space-y-3">
      <header className="flex items-start gap-3 rounded-lg border border-[var(--hc-border)] bg-black/20 p-3">
        <span className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl border ${toneClasses(collection.accent)}`}>
          <CollectionGlyph name={collection.icon} className="h-5 w-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <h3 className="text-[1.02rem] font-semibold text-white">{collection.title}</h3>
            <span className="hc-mono text-[0.7rem] hc-dim">{t.docs(collection.docs.length)}</span>
            {collection.updated_ts > 0 ? (
              <span className="hc-mono text-[0.7rem] hc-dim">· {t.updatedAgo(fmtAge(collection.updated_ts, now))}</span>
            ) : null}
          </div>
          <p className="mt-0.5 text-[0.8rem] leading-relaxed hc-soft">{collection.description}</p>
          {collection.pulse && collection.pulse.length > 0 ? <PulseStrip pulse={collection.pulse} /> : null}
        </div>
      </header>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {collection.docs.map((doc) => (
          <DocCard key={doc.id} doc={doc} onOpen={onOpen} />
        ))}
      </div>
    </section>
  );
}

export function KnowledgeShelf() {
  const [searchParams] = useSearchParams();
  // Baseline-Katalog: dauerhaft gepollt (60 s, geteilter pollingStore-Key mit
  // der BriefingsShelf-Schnellauswahl — s. useKnowledgeCatalog). Zeigt die
  // Sammlungen, solange nicht gesucht wird.
  const baseline = useKnowledgeCatalog();
  const [q, setQ] = useState("");
  const [searchCatalog, setSearchCatalog] = useState<KnowledgeCatalog | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [activeCollection, setActiveCollection] = useState<string | null>(null);
  const [activeType, setActiveType] = useState<string | null>(null);
  const [reading, setReading] = useState<KnowledgeDoc | null>(null);

  // Deep-Link aus der Bibliothek-Schnellauswahl (BriefingsShelf-Kacheln
  // setzen `?collection=<id>`): jede neue Collection-Id preselektiert das
  // Regal-Filter, ohne die bestehende Filter-UX (manuelles Wechseln/Leeren)
  // zu stören — reagiert nur auf eine tatsächliche Änderung des Werts.
  // setTimeout(0) statt synchronem setState im Effect-Body — Hauskonvention
  // (BibliothekView-Deep-Link), s. react-hooks/set-state-in-effect.
  const collectionParam = searchParams.get("collection");
  useEffect(() => {
    if (!collectionParam) return;
    const handle = window.setTimeout(() => setActiveCollection(collectionParam), 0);
    return () => window.clearTimeout(handle);
  }, [collectionParam]);

  const searchLoad = useCallback(async (query: string) => {
    try {
      const res = await fetchJSON<KnowledgeCatalog>(`/api/library/knowledge?q=${encodeURIComponent(query)}`);
      setSearchCatalog(res);
      setSearchError(null);
    } catch (e) {
      setSearchError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  // Such-Eingaben entprellt (250 ms) — Hauskonvention setTimeout statt
  // synchronem setState im Effect-Body. Ohne Suchtext zeigt das Regal den
  // dauerhaft gepollten Baseline-Katalog statt selbst zu laden.
  useEffect(() => {
    const trimmed = q.trim();
    if (!trimmed) {
      const handle = window.setTimeout(() => { setSearchCatalog(null); setSearchError(null); }, 0);
      return () => window.clearTimeout(handle);
    }
    const handle = window.setTimeout(() => void searchLoad(trimmed), 250);
    return () => window.clearTimeout(handle);
  }, [q, searchLoad]);

  const searching = q.trim().length > 0;
  const catalog = searching ? searchCatalog : baseline.data;
  const error = searching ? searchError : baseline.error;

  const visibleCatalog = useMemo(
    () => (catalog ? filterCatalog(catalog, activeCollection, activeType) : null),
    [activeCollection, activeType, catalog],
  );

  if (reading) {
    const collectionTitle =
      catalog?.collections.find((c) => c.id === reading.collection)?.title ?? "";
    return <KnowledgeReader doc={reading} collectionTitle={collectionTitle} onBack={() => setReading(null)} />;
  }

  const collections = visibleCatalog?.collections ?? [];
  const nowTs = visibleCatalog?.now ?? nowSec();

  return (
    <div className="space-y-4">
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 hc-dim" />
        <input
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t.searchPlaceholder}
          aria-label={t.searchAria}
          className="w-full rounded-lg border border-[var(--hc-border)] bg-black/25 py-2 pl-9 pr-3 text-sm text-white placeholder:hc-dim"
        />
      </div>

      {error ? <ToneCallout tone="red">{t.loadError}<br />{error}</ToneCallout> : null}

      {catalog ? (
        <CatalogFilters
          catalog={catalog}
          activeCollection={activeCollection}
          activeType={activeType}
          onCollection={setActiveCollection}
          onType={setActiveType}
        />
      ) : null}

      {searching && visibleCatalog ? (
        <p className="text-[0.78rem] hc-dim">{t.hitsFor(totalDocs(collections), q.trim())}</p>
      ) : null}

      {catalog === null && !error ? (
        <p className="text-sm hc-dim">{t.loading}</p>
      ) : collections.length === 0 ? (
        <FleetEmptyState title={t.emptyTitle} desc={t.emptyDesc} />
      ) : (
        <div className="space-y-4">
          {collections.map((collection) => (
            <CollectionSection key={collection.id} collection={collection} now={nowTs} onOpen={setReading} />
          ))}
        </div>
      )}
    </div>
  );
}
