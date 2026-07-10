import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { BookOpen, Brain, Landmark, Newspaper, Search, Sparkles, Users, Workflow } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { FleetEmptyState, SignalLabel, TwoPane } from "../../components/leitstand";
import { Eyebrow } from "../../components/primitives";
import { fmtAge, nowSec } from "../../lib/derive";
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
import { useExpandedLibraryPane } from "./useExpandedLibraryPane";
import "./knowledge.css";

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
      className={`inline-flex min-h-12 items-center gap-1.5 rounded-card border px-3 text-sec transition-colors ${
        active
          ? "border-live bg-surface-3 text-ink"
          : "border-line bg-surface-2 text-ink-2 hover:bg-surface-3"
      }`}
    >
      {children}
    </button>
  );
}

function CountBadge({ count }: { count: number }) {
  return <span className="font-data text-micro tabular-nums text-ink-3">{count}</span>;
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
    <div className="space-y-3 rounded-panel border border-line bg-surface-1 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Eyebrow>{t.total(totalDocs(catalog.collections))}</Eyebrow>
        <button
          type="button"
          onClick={() => {
            onCollection(null);
            onType(null);
          }}
          className="inline-flex min-h-12 items-center rounded-card border border-line px-3 text-micro text-ink-2 hover:border-live/40 hover:bg-surface-3"
        >
          Filter leeren
        </button>
      </div>

      <div className="space-y-2">
        <Eyebrow>{t.collections}</Eyebrow>
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
          <Eyebrow>{t.types}</Eyebrow>
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
export function DocCard({ doc, onOpen, selected = false }: {
  doc: KnowledgeDoc;
  onOpen: (doc: KnowledgeDoc) => void;
  selected?: boolean;
}) {
  const typeLabel = knowledgeTypeLabel(knowledgeType(doc));
  return (
    <button
      type="button"
      onClick={() => onOpen(doc)}
      aria-expanded={selected}
      className={`flex h-full min-h-[11rem] flex-col gap-2 rounded-card border border-line bg-surface-2 p-4 text-left transition-colors ${
        selected ? "shadow-[inset_3px_0_0_var(--color-bronze)] bg-surface-3" : "hover:bg-surface-3"
      }`}
    >
      <Eyebrow>{typeLabel}</Eyebrow>
      <h4 className="text-body font-semibold leading-snug text-ink">{doc.title}</h4>
      <p className="line-clamp-2 flex-1 text-sec leading-relaxed text-ink-2">{doc.summary}</p>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-micro text-ink-3">
        <span className="truncate font-data">{doc.source_ref}</span>
        {doc.heading_count > 0 ? <span>· {sectionsLabel(doc.heading_count)}</span> : null}
      </div>
      {doc.tags.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {doc.tags.map((tag) => (
            <span key={tag} className="rounded-card border border-line px-1.5 py-0.5 text-micro text-ink-3">{tag}</span>
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
      <span className="shrink-0 text-micro text-ink-3">{t.pulsePrefix}</span>
      {pulse.map((item, i) => (
        <span
          key={`${item.model}-${i}`}
          className="shrink-0 rounded-card border border-line px-2 py-0.5 text-micro text-ink-2"
        >
          <span className="font-data">{item.model}</span> · {item.date}
        </span>
      ))}
    </div>
  );
}

/** Eine Sammlung (ein Regal): Akzent-Icon, Titel, Beschreibung, Doc-Raster.
 *  Exportiert für Unit-Tests. */
export function CollectionSection({ collection, now, onOpen, selectedId = null }: {
  collection: KnowledgeCollection;
  /** epoch-Sekunden "jetzt" (aus `catalog.now`) für den "aktualisiert vor X"-Chip. */
  now: number;
  onOpen: (doc: KnowledgeDoc) => void;
  selectedId?: string | null;
}) {
  return (
    <section className="space-y-3">
      <header className="flex items-start gap-3 rounded-panel border border-line bg-surface-1 p-3">
        <span className="grid size-12 shrink-0 place-items-center rounded-card border border-line bg-surface-2 text-brand">
          <CollectionGlyph name={collection.icon} className="h-5 w-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <h3 className="text-body font-semibold text-ink">{collection.title}</h3>
            <span className="font-data text-micro tabular-nums text-ink-3">{t.docs(collection.docs.length)}</span>
            {collection.updated_ts > 0 ? (
              <span className="font-data text-micro tabular-nums text-ink-3">· {t.updatedAgo(fmtAge(collection.updated_ts, now))}</span>
            ) : null}
          </div>
          <p className="mt-0.5 text-sec leading-relaxed text-ink-2">{collection.description}</p>
          {collection.pulse && collection.pulse.length > 0 ? <PulseStrip pulse={collection.pulse} /> : null}
        </div>
      </header>
      <div className="knowledge-grid-host">
        <div className="knowledge-card-grid">
          {collection.docs.map((doc) => (
            <DocCard key={doc.id} doc={doc} onOpen={onOpen} selected={selectedId === doc.id} />
          ))}
        </div>
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
  const isExpanded = useExpandedLibraryPane();
  const shelfRef = useRef<HTMLDivElement>(null);
  const readingTriggerRef = useRef<HTMLElement | null>(null);

  const openDoc = useCallback((doc: KnowledgeDoc) => {
    const active = document.activeElement;
    readingTriggerRef.current = active instanceof HTMLElement && active !== document.body ? active : null;
    setReading(doc);
  }, []);

  const closeReading = useCallback(() => {
    const trigger = readingTriggerRef.current;
    readingTriggerRef.current = null;
    setReading(null);
    window.setTimeout(() => {
      if (trigger?.isConnected && !trigger.closest("[hidden]")) trigger.focus();
      else shelfRef.current?.focus();
    }, 0);
  }, []);

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

  const collections = visibleCatalog?.collections ?? [];
  const nowTs = visibleCatalog?.now ?? nowSec();
  const collectionTitle = reading
    ? catalog?.collections.find((collection) => collection.id === reading.collection)?.title ?? ""
    : "";

  if (reading && !isExpanded) {
    return <KnowledgeReader doc={reading} collectionTitle={collectionTitle} onBack={closeReading} />;
  }

  const shelf = (
    <div ref={shelfRef} className="space-y-4" tabIndex={-1}>
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" />
        <input
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t.searchPlaceholder}
          aria-label={t.searchAria}
          className="min-h-12 w-full rounded-card border border-line bg-surface-2 pl-9 pr-3 text-sec text-ink placeholder:text-ink-3"
        />
      </div>

      {error ? <div role="alert" className="rounded-card border border-status-alert/30 bg-status-alert/10 p-3"><SignalLabel tone="alert" label={t.loadError} /><p className="mt-1 text-sec text-ink-2">{error}</p></div> : null}

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
        <p className="text-sec text-ink-3">{t.hitsFor(totalDocs(collections), q.trim())}</p>
      ) : null}

      {catalog === null && !error ? (
        <p className="text-sec text-ink-3">{t.loading}</p>
      ) : collections.length === 0 ? (
        <FleetEmptyState title={t.emptyTitle} desc={t.emptyDesc} />
      ) : (
        <div className="space-y-4">
          {collections.map((collection) => (
            <CollectionSection
              key={collection.id}
              collection={collection}
              now={nowTs}
              onOpen={openDoc}
              selectedId={reading?.id}
            />
          ))}
        </div>
      )}
    </div>
  );

  return (
    <TwoPane
      list={shelf}
      detail={reading ? <KnowledgeReader doc={reading} collectionTitle={collectionTitle} onBack={closeReading} /> : undefined}
      detailLabel={reading ? `${collectionTitle}: ${reading.title}` : "Wissensdokument"}
      onCloseDetail={reading ? closeReading : undefined}
    />
  );
}
