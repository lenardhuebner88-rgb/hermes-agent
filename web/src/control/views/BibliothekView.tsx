import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ListTree } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import {
  DrawerShell,
  FleetEmptyState,
  FleetPanel,
  ListRow,
  SectionHeader,
  SignalChip,
  SignalLabel,
  SubtabChips,
  TwoPane,
} from "../components/leitstand";
import { Eyebrow, SkeletonCard } from "../components/primitives";
import { ProseMarkdown } from "../components/ProseMarkdown";
import { fmtClock } from "../lib/derive";
import { de } from "../i18n/de";
import { extractToc, type TocEntry } from "../lib/slug";
import type { Density } from "../hooks/useDensity";
import type { VaultProvenanceResponse } from "../lib/types";
import {
  CATEGORY_LABEL,
  countByCategory,
  dedupeById,
  groupBySeries,
  newestPerCategory,
  seriesNeighbors,
  sortItems,
  type LesesaalSort,
} from "./BibliothekView.helpers";
import { KnowledgeShelf } from "./knowledge/KnowledgeShelf";
import { BriefingsShelf } from "./briefings/BriefingsShelf";
import { ModelleShelf } from "./models/ModelleShelf";
import { ErgebnisseShelf } from "./results/ErgebnisseShelf";
import { TocNav } from "./knowledge/KnowledgeReader";
import { useExpandedLibraryPane } from "./knowledge/useExpandedLibraryPane";
import "./BibliothekView.css";

// Bibliothek = zwei klar getrennte Bereiche (Programm 3, Next-Level):
//   • Nachschlagewerk (Wissen/Kanon) — kuratiertes, thema-geordnetes Referenz-
//     wissen (Canon, Orchestrierung, Skills, Rollen). Zum Nachschlagen.
//   • Lesesaal (Ausgaben) — alles zeitlich Produzierte (Digests, Recherchen,
//     Receipts), Zeitungs-Metapher, chronologisch.
// Ein gemeinsamer Hero mit Segmented-Control schaltet zwischen beiden; jeder
// Bereich bringt seine eigenen Filter/Suche mit.
const t = {
  eyebrow: "Bibliothek",
  modeWissen: "Nachschlagewerk",
  modeLesesaal: "Lesesaal",
  wissenTitle: "Nachschlagewerk",
  wissenSubtitle: "Dauerhaftes Serverwissen, geordnet zum Nachschlagen.",
  lesesaalTitle: "Der Lesesaal",
  lesesaalSubtitle: "Alles, was Hermes produziert, menschenlesbar an einem Ort — Digests, Recherchen, Receipts.",
  searchPlaceholder: "Suche in Titel + Text …",
  frontpage: "Heute",
  empty: "Noch nichts zu lesen.",
  emptyDesc: "Sobald Crons/Recherchen Ausgaben produzieren, füllt sich der Lesesaal.",
  loadError: "Lesesaal konnte nicht geladen werden.",
  newBadge: "neu",
  issues: (n: number) => `${n} Ausgaben`,
  back: "← Übersicht",
  prev: "← ältere",
  next: "neuere →",
  topicsTitle: "Themen folgen",
  topicsMeta: "Beobachtungsliste für deine Bibliothek",
  topicFollow: "Thema folgen",
  topicFollowing: "Folge ich",
  topicUnfollow: "Entfolgen",
  topicPending: "Speichern …",
  savedTitle: "Smart Shelves",
  savedMeta: "Gespeicherte Suchen",
  savedEmpty: "Noch keine gespeicherten Suchen.",
  savedApply: "Suche öffnen",
  sortLabel: "Sortierung",
  sortNewest: "Neueste",
  sortOldest: "Älteste",
  sortAz: "A–Z",
  sortListEyebrow: "Sortierte Liste",
  loadMore: "Mehr laden",
  loadingMore: "Lade …",
  toc: "Inhalt",
};

const ALL_CATEGORY_TAB = "__all";

export interface LibraryItem {
  id: string;
  category: string;
  series_id: string;
  series: string;
  title: string;
  ts: number;
  preview: string;
  source_ref: string;
  series_meta: string;
  structured?: boolean;
  structured_brief?: StructuredModelBrief;
}

export interface StructuredBriefSource {
  title: string;
  url: string;
}

export interface StructuredModelNewsItem {
  title: string;
  summary: string;
  source_title: string;
  source_url: string;
}

export interface StructuredModelBrief {
  run_kind: "morgen" | "breaking" | "abend";
  generated: string;
  top_story: string;
  model_news: StructuredModelNewsItem[];
  sources: StructuredBriefSource[];
  watchlist_delta: string[];
}

export interface LibraryListResponse {
  items: LibraryItem[];
  count: number;
  truncated: boolean;
  /** S6 ("Mehr laden"): true, solange nach dieser Seite (offset+limit) noch
   *  weitere Treffer folgen. */
  has_more: boolean;
  categories: string[];
  now?: number;
}

type LibraryDetail = LibraryItem & { body_md: string };

export interface LibraryTopic {
  id: string;
  title: string;
  followed: boolean;
  subscribed: boolean;
  seeded: boolean;
  created_at: number;
  updated_at: number;
}

export interface LibrarySavedSearch {
  id: string;
  name: string;
  title: string;
  query: string;
  topic_tags: string[];
  person_tags: string[];
  created_at: number;
  updated_at: number;
}

interface LibraryTopicsResponse {
  items: LibraryTopic[];
  count: number;
}

interface LibrarySavedSearchesResponse {
  items: LibrarySavedSearch[];
  count: number;
}

const LAST_VISIT_KEY = "hc-bibliothek-last-visit";


export function ItemRow({ item, unreadSince, onOpen, selected = false }: {
  item: LibraryItem;
  unreadSince: number;
  onOpen: (item: LibraryItem) => void;
  selected?: boolean;
}) {
  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpen(item);
    }
  };
  return (
    <li>
      <div
        role="button"
        tabIndex={0}
        aria-expanded={selected}
        onClick={() => onOpen(item)}
        onKeyDown={handleKeyDown}
        className="cursor-pointer"
      >
        <ListRow
          title={item.title}
          meta={fmtClock(item.ts)}
          trailing={item.ts > unreadSince ? <SignalChip tone="neutral" label={t.newBadge} /> : null}
          className={selected ? "shadow-[inset_3px_0_0_var(--color-bronze)] bg-surface-3" : "hover:bg-surface-3"}
        >
          {CATEGORY_LABEL[item.category] ?? item.category} · {item.series}
        </ListRow>
      </div>
    </li>
  );
}

export function TopicFollowCard({ topic, onToggle, pending }: { topic: LibraryTopic; onToggle: (topic: LibraryTopic) => void; pending: boolean }) {
  return (
    <article className="rounded-card border border-line bg-surface-2 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-sec font-semibold text-ink">{topic.title}</h3>
          {topic.followed ? (
            <SignalLabel className="mt-1" tone="ok" label={t.topicFollowing} />
          ) : (
            <p className="mt-1 text-micro text-ink-3">{t.topicFollow}</p>
          )}
        </div>
        <button
          type="button"
          disabled={pending}
          onClick={() => onToggle(topic)}
          aria-pressed={topic.followed}
          className="inline-flex min-h-12 shrink-0 items-center rounded-card border border-live/40 bg-live/10 px-3 text-micro text-bronze-hi transition hover:bg-live/15 disabled:opacity-50"
        >
          {pending ? t.topicPending : topic.followed ? t.topicUnfollow : t.topicFollow}
        </button>
      </div>
    </article>
  );
}

export function TopicFollowSection({ topics, onToggle, pendingTopicId }: { topics: LibraryTopic[]; onToggle: (topic: LibraryTopic) => void; pendingTopicId: string | null }) {
  return (
    <FleetPanel eyebrow={t.topicsTitle} meta={t.topicsMeta}>
      <div className="bibliothek-grid-host">
        <div className="bibliothek-card-grid bibliothek-card-grid--four gap-2">
          {topics.map((topic) => (
            <TopicFollowCard key={topic.id} topic={topic} onToggle={onToggle} pending={pendingTopicId === topic.id} />
          ))}
        </div>
      </div>
    </FleetPanel>
  );
}

export function SavedSearchShelf({ searches, onApply }: { searches: LibrarySavedSearch[]; onApply: (search: LibrarySavedSearch) => void }) {
  return (
    <FleetPanel eyebrow={t.savedTitle} meta={t.savedMeta}>
      {searches.length === 0 ? (
        <p className="text-sec text-ink-3">{t.savedEmpty}</p>
      ) : (
        <div className="bibliothek-grid-host">
          <ul className="bibliothek-card-grid bibliothek-card-grid--three gap-2">
            {searches.map((search) => (
              <li key={search.id} className="rounded-card border border-line bg-surface-2 p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 className="truncate text-sec font-semibold text-ink">{search.title || search.name}</h3>
                    <p className="mt-1 line-clamp-2 text-sec text-ink-2">{search.query}</p>
                    {[...search.topic_tags, ...search.person_tags].length ? (
                      <p className="mt-2 flex flex-wrap gap-1">
                        {[...search.topic_tags, ...search.person_tags].map((tag) => (
                          <span key={tag} className="rounded-card border border-line px-1.5 py-0.5 text-micro text-ink-3">{tag}</span>
                        ))}
                      </p>
                    ) : null}
                  </div>
                  <button type="button" onClick={() => onApply(search)} className="inline-flex min-h-12 shrink-0 items-center rounded-card border border-line px-3 text-micro text-ink-2 transition hover:border-live/40 hover:bg-surface-3">
                    {t.savedApply}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </FleetPanel>
  );
}

export function ReadingContent({ item, neighbors, onNavigate, onBack }: {
  item: LibraryItem;
  neighbors: { prev: LibraryItem | null; next: LibraryItem | null };
  onNavigate: (item: LibraryItem) => void;
  onBack: () => void;
}) {
  const [detail, setDetail] = useState<LibraryDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Reset beim Item-Wechsel als Render-Phase-Anpassung (React-Doku
  // "adjusting state when props change") statt setState im Effect-Body.
  const [detailFor, setDetailFor] = useState<string>(item.id);
  if (detailFor !== item.id) {
    setDetailFor(item.id);
    setDetail(null);
    setError(null);
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const d = await fetchJSON<LibraryDetail>(`/api/library/item?id=${encodeURIComponent(item.id)}`);
        if (!cancelled) {
          setError(null);
          setDetail(d);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [item.id]);

  // Inhaltsverzeichnis wie im Nachschlagewerk (KnowledgeReader): dieselbe
  // extractToc/TocNav-Kombination, hier mobil einklappbar statt sticky-aside
  // (der Lesesaal ist die Zeitungs-/Mobil-first-Metapher). Erst ab ≥3
  // Überschriften — bei kürzeren Ausgaben lohnt ein Inhaltsverzeichnis nicht.
  const toc: TocEntry[] = useMemo(() => (detail ? extractToc(detail.body_md) : []), [detail]);
  const jumpToHeading = (slug: string) => {
    const el = typeof document !== "undefined" ? document.getElementById(slug) : null;
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <FleetPanel eyebrow={item.series} meta={`${CATEGORY_LABEL[item.category] ?? item.category} · ${fmtClock(item.ts)} · ${item.source_ref}`}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <button type="button" onClick={onBack} className="inline-flex min-h-12 items-center rounded-card border border-line px-3 text-sec text-ink-2 transition hover:border-live/40 hover:bg-surface-3">{t.back}</button>
        <button type="button" disabled={!neighbors.prev} onClick={() => neighbors.prev && onNavigate(neighbors.prev)} className="inline-flex min-h-12 items-center rounded-card border border-line px-3 text-sec text-ink-2 transition hover:border-live/40 hover:bg-surface-3 disabled:opacity-40">{t.prev}</button>
        <button type="button" disabled={!neighbors.next} onClick={() => neighbors.next && onNavigate(neighbors.next)} className="inline-flex min-h-12 items-center rounded-card border border-line px-3 text-sec text-ink-2 transition hover:border-live/40 hover:bg-surface-3 disabled:opacity-40">{t.next}</button>
      </div>
      <h3 className="mb-2 text-body font-semibold text-ink">{item.title}</h3>
      {error ? <div role="alert" className="rounded-card border border-status-alert/30 bg-status-alert/10 p-3"><SignalLabel tone="alert" label={error} /></div> : null}
      {toc.length >= 3 ? (
        <details className="mb-3 rounded-card border border-line bg-surface-2 p-3">
          <summary className="flex min-h-12 cursor-pointer list-none items-center gap-1.5 font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3">
            <ListTree className="h-3.5 w-3.5" />
            {t.toc}
          </summary>
          <div className="mt-2">
            <TocNav entries={toc} onJump={jumpToHeading} />
          </div>
        </details>
      ) : null}
      {detail ? <ProseMarkdown slugHeadings>{detail.body_md}</ProseMarkdown> : error ? null : <SkeletonCard rows={5} />}
    </FleetPanel>
  );
}

export const ReadingView = ReadingContent;

// Lesesaal (Ausgaben) — der bisherige Bibliothek-Inhalt, unverändert in Logik.
// Der Hero lebt jetzt im Eltern-`BibliothekView`; die Filter (Kategorie-Chips +
// Suche) sitzen darum in einer eigenen Filterleiste statt im Hero.
const LESESAAL_PAGE_SIZE = 120;

export function LesesaalBody() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [category, setCategory] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<LesesaalSort>("newest");
  // `data` trägt die Meta der zuletzt geladenen Seite (categories/has_more/
  // truncated/count); `items` akkumuliert über "Mehr laden" (S6) hinweg.
  const [data, setData] = useState<LibraryListResponse | null>(null);
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [topics, setTopics] = useState<LibraryTopic[]>([]);
  const [savedSearches, setSavedSearches] = useState<LibrarySavedSearch[]>([]);
  const [pendingTopicId, setPendingTopicId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reading, setReading] = useState<LibraryItem | null>(null);
  const isExpanded = useExpandedLibraryPane();
  const readingTriggerRef = useRef<HTMLElement | null>(null);
  // Ungelesen v1: Zeitstempel des letzten Besuchs aus localStorage; beim
  // Mount einfrieren (Lazy-Initializer), das Fortschreiben passiert im
  // Mount-Effekt (localStorage-Write + Date.now sind impure → nicht im Render).
  const [unreadSince] = useState<number>(() => {
    const raw = typeof window !== "undefined" ? window.localStorage.getItem(LAST_VISIT_KEY) : null;
    return raw ? Number(raw) || 1 : 1;
  });
  useEffect(() => {
    try { window.localStorage.setItem(LAST_VISIT_KEY, String(Math.floor(Date.now() / 1000))); } catch { /* private mode */ }
  }, []);

  // Deep-Links (S2): geöffnetes Dokument als `item`-Search-Param — öffnen ist
  // ein push (Back-Button schließt das Dokument), schließen/Filterwechsel ist
  // ein replace (kein Verlauf-Wachstum für reine Zustands-Aufräumarbeit).
  const navigateToItem = useCallback((next: LibraryItem) => {
    setReading(next);
    setSearchParams((prev) => {
      const p = new URLSearchParams(prev);
      p.set("item", next.id);
      return p;
    });
  }, [setSearchParams]);

  const openItem = useCallback((next: LibraryItem) => {
    const active = document.activeElement;
    readingTriggerRef.current = active instanceof HTMLElement && active !== document.body ? active : null;
    navigateToItem(next);
  }, [navigateToItem]);

  const closeItem = useCallback(() => {
    setReading(null);
    setSearchParams((prev) => {
      const p = new URLSearchParams(prev);
      p.delete("item");
      return p;
    }, { replace: true });
    // A Briefings card can open the Lesesaal drawer while switching the view
    // mode. Its trigger then remains mounted inside a `hidden` tabpanel, so
    // DrawerShell's normal focus restoration cannot move focus back to it.
    // Preserve the normal visible-trigger path, but fall back to the now-active
    // Lesesaal tab when the portalled drawer removal leaves focus on BODY or
    // on the original trigger inside a now-hidden tabpanel.
    window.setTimeout(() => {
      const active = document.activeElement;
      if (active === document.body || (active instanceof HTMLElement && active.closest("[hidden]"))) {
        document.getElementById("bibliothek-tab-lesesaal")?.focus();
      }
    }, 0);
  }, [setSearchParams]);

  const closePaneItem = useCallback(() => {
    const trigger = readingTriggerRef.current;
    readingTriggerRef.current = null;
    closeItem();
    window.setTimeout(() => {
      if (trigger?.isConnected && !trigger.closest("[hidden]")) trigger.focus();
      else document.getElementById("bibliothek-tab-lesesaal")?.focus();
    }, 0);
  }, [closeItem]);

  const fetchPage = useCallback(async (offset: number) => {
    const params = new URLSearchParams();
    if (category) params.set("category", category);
    if (q.trim()) params.set("q", q.trim());
    params.set("limit", String(LESESAAL_PAGE_SIZE));
    params.set("offset", String(offset));
    return fetchJSON<LibraryListResponse>(`/api/library/items?${params.toString()}`);
  }, [category, q]);

  const load = useCallback(async () => {
    try {
      const res = await fetchPage(0);
      setData(res);
      setItems(res.items ?? []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [fetchPage]);

  const loadMore = useCallback(async () => {
    setLoadingMore(true);
    try {
      const res = await fetchPage(items.length);
      setData(res);
      setItems((current) => dedupeById([...current, ...(res.items ?? [])]));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingMore(false);
    }
  }, [fetchPage, items.length]);

  const loadPreferences = useCallback(async () => {
    try {
      const [topicRes, savedRes] = await Promise.all([
        fetchJSON<LibraryTopicsResponse>("/api/library/topics"),
        fetchJSON<LibrarySavedSearchesResponse>("/api/library/saved-searches"),
      ]);
      setTopics(topicRes.items);
      setSavedSearches(savedRes.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const toggleTopicFollow = useCallback(async (topic: LibraryTopic) => {
    setPendingTopicId(topic.id);
    try {
      const updated = await fetchJSON<LibraryTopic>(
        `/api/library/topics/${encodeURIComponent(topic.id)}/follow`,
        { method: topic.followed ? "DELETE" : "POST" },
      );
      setTopics((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPendingTopicId(null);
    }
  }, []);

  const applySavedSearch = useCallback((search: LibrarySavedSearch) => {
    setCategory(null);
    closeItem();
    setQ(search.query);
  }, [closeItem]);

  useEffect(() => {
    // Erst-Load per setTimeout(0) — Hauskonvention (TriageStrip): synchrones
    // setState im Effect-Body verletzt react-hooks/set-state-in-effect.
    const firstLoad = window.setTimeout(() => {
      void load();
      void loadPreferences();
    }, 0);
    const id = window.setInterval(() => {
      if (document.hidden) return;
      void load();
    }, 60000);
    return () => {
      window.clearTimeout(firstLoad);
      window.clearInterval(id);
    };
  }, [load, loadPreferences]);

  // Deep-Link wiederherstellen (Reload/Link-Teilen, S2): das Item steht ggf.
  // schon in den geladenen Seiten — sonst direkt nachladen (funktioniert auch,
  // wenn das Ziel jenseits der aktuell geladenen Seiten liegt). Der "found"-
  // Zweig löst setTimeout(0) statt synchronem setState im Effect-Body aus —
  // Hauskonvention (TriageStrip/LesesaalBody-Erst-Load), siehe react-hooks/
  // set-state-in-effect.
  useEffect(() => {
    const id = searchParams.get("item");
    if (!id) return;
    if (reading && reading.id === id) return;
    const active = document.activeElement;
    const trigger = active instanceof HTMLElement && active !== document.body ? active : null;
    const found = items.find((i) => i.id === id);
    if (found) {
      const handle = window.setTimeout(() => {
        readingTriggerRef.current = trigger;
        setReading(found);
      }, 0);
      return () => window.clearTimeout(handle);
    }
    let cancelled = false;
    void (async () => {
      try {
        const d = await fetchJSON<LibraryDetail>(`/api/library/item?id=${encodeURIComponent(id)}`);
        if (!cancelled) {
          readingTriggerRef.current = trigger;
          setReading(d);
        }
      } catch {
        // Deep-Link zeigt auf ein verschwundenes/ungültiges Item — Liste bleibt sichtbar.
      }
    })();
    return () => { cancelled = true; };
  }, [searchParams, items, reading]);

  const isFrontpage = !category && !q.trim();

  const frontpage = useMemo(() => newestPerCategory(items), [items]);
  const shelves = useMemo(() => groupBySeries(items), [items]);
  const sortedItems = useMemo(() => sortItems(items, sort), [items, sort]);
  const neighbors = useMemo(() => seriesNeighbors(items, reading), [reading, items]);
  const counts = useMemo(() => countByCategory(items), [items]);
  const categoryTabs = useMemo(() => [
    { id: ALL_CATEGORY_TAB, label: t.frontpage },
    ...((data?.categories ?? []).map((c) => ({ id: c, label: CATEGORY_LABEL[c] ?? c, count: counts[c] || undefined }))),
  ], [data?.categories, counts]);
  const hasMore = data?.has_more ?? false;

  const shelf = (
    <div className="space-y-4">
      <div className="rounded-card border border-line bg-surface-1 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <SubtabChips
            items={categoryTabs}
            active={category ?? ALL_CATEGORY_TAB}
            onSelect={(next) => { setCategory(next === ALL_CATEGORY_TAB ? null : next); closeItem(); }}
            ariaLabelPrefix={t.eyebrow}
            className="[&_button]:min-h-12"
          />
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={t.searchPlaceholder}
            aria-label={t.searchPlaceholder}
            className="min-h-12 min-w-48 flex-1 rounded-card border border-line bg-surface-2 px-3 text-sec text-ink placeholder:text-ink-3"
          />
        </div>
        <div className="mt-2 flex items-center gap-2">
          <SortToggle sort={sort} onChange={setSort} />
        </div>
      </div>

      <TopicFollowSection topics={topics} onToggle={toggleTopicFollow} pendingTopicId={pendingTopicId} />
      <SavedSearchShelf searches={savedSearches} onApply={applySavedSearch} />

      {error ? <div role="alert" className="rounded-card border border-status-alert/30 bg-status-alert/10 p-3"><SignalLabel tone="alert" label={t.loadError} /><p className="mt-1 text-sec text-ink-2">{error}</p></div> : null}

      {data === null && !error ? (
        <SkeletonCard rows={4} />
      ) : data !== null && items.length === 0 ? (
        <FleetEmptyState title={t.empty} desc={t.emptyDesc} />
      ) : sort !== "newest" ? (
        <FleetPanel eyebrow={t.sortListEyebrow} meta={t.issues(sortedItems.length)}>
          <ul className="space-y-1.5">
            {sortedItems.map((item) => (
              <ItemRow key={item.id} item={item} unreadSince={unreadSince} onOpen={openItem} selected={reading?.id === item.id} />
            ))}
          </ul>
        </FleetPanel>
      ) : isFrontpage ? (
        <div className="bibliothek-grid-host">
          <div className="bibliothek-card-grid bibliothek-card-grid--three gap-3">
            {frontpage.map((item) => {
              const selected = reading?.id === item.id;
              return (
                <div
                  key={item.id}
                  role="button"
                  tabIndex={0}
                  aria-expanded={selected}
                  onClick={() => openItem(item)}
                  onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openItem(item); } }}
                  className="cursor-pointer"
                >
                  <ListRow
                    title={item.title}
                    meta={fmtClock(item.ts)}
                    leading={<Eyebrow>{CATEGORY_LABEL[item.category] ?? item.category}</Eyebrow>}
                    trailing={item.ts > unreadSince ? <SignalChip tone="neutral" label={t.newBadge} /> : null}
                    className={selected ? "h-full shadow-[inset_3px_0_0_var(--color-bronze)] bg-surface-3" : "h-full hover:bg-surface-3"}
                  >
                    <span className="line-clamp-3">{item.preview}</span>
                    <span className="mt-2 block font-data text-micro text-ink-3">{item.series}</span>
                  </ListRow>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          {shelves.map((shelf) => (
            <FleetPanel key={shelf.seriesId} eyebrow={shelf.series} meta={`${shelf.meta ? `${shelf.meta} · ` : ""}${t.issues(shelf.items.length)} · zuletzt ${fmtClock(shelf.items[0]?.ts ?? 0)}`}>
              <ul className="space-y-1.5">
                {shelf.items.map((item) => (
                  <ItemRow key={item.id} item={item} unreadSince={unreadSince} onOpen={openItem} selected={reading?.id === item.id} />
                ))}
              </ul>
            </FleetPanel>
          ))}
        </div>
      )}

      {hasMore ? (
        <div className="flex justify-center pt-1">
          <button
            type="button"
            onClick={() => void loadMore()}
            disabled={loadingMore}
            className="inline-flex min-h-12 items-center rounded-card border border-line px-4 text-sec text-ink-2 transition hover:border-live/40 hover:bg-surface-3 disabled:opacity-50"
          >
            {loadingMore ? t.loadingMore : t.loadMore}
          </button>
        </div>
      ) : null}
    </div>
  );

  return (
    <>
      <TwoPane
        list={shelf}
        detail={isExpanded && reading ? (
          <ReadingContent item={reading} neighbors={neighbors} onNavigate={navigateToItem} onBack={closePaneItem} />
        ) : undefined}
        detailLabel={reading ? `${t.modeLesesaal}: ${reading.title}` : t.modeLesesaal}
        onCloseDetail={isExpanded && reading ? closePaneItem : undefined}
      />

      {!isExpanded && reading ? (
        <DrawerShell
          eyebrow={CATEGORY_LABEL[reading.category] ?? reading.category}
          title={reading.title}
          ariaLabel={`${t.modeLesesaal}: ${reading.title}`}
          closeLabel={t.back}
          onClose={closeItem}
          widthClassName="tab:w-[min(900px,calc(100vw-2rem))]"
        >
          <ReadingContent item={reading} neighbors={neighbors} onNavigate={navigateToItem} onBack={closeItem} />
        </DrawerShell>
      ) : null}
    </>
  );
}

function SortToggle({ sort, onChange }: { sort: LesesaalSort; onChange: (sort: LesesaalSort) => void }) {
  return (
    <SubtabChips
      items={[
        { id: "newest", label: t.sortNewest },
        { id: "oldest", label: t.sortOldest },
        { id: "az", label: t.sortAz },
      ]}
      active={sort}
      onSelect={(next) => onChange(next as LesesaalSort)}
      ariaLabelPrefix={t.sortLabel}
      className="[&_button]:min-h-12"
    />
  );
}

interface VaultProvenanceShelfProps {
  data: VaultProvenanceResponse | null;
  error?: string | null;
}

export function VaultProvenanceShelf({ data, error }: VaultProvenanceShelfProps) {
  const detail = error ?? data?.error ?? null;
  const opens = data?.open_sessions ?? [];
  const receipts = data?.recent_receipts ?? [];
  const stale = data?.stale_count ?? 0;
  const isUnknown = !data || Boolean(detail);

  return (
    <section className="space-y-3 rounded-card border border-line bg-surface-1 p-3" aria-label={de.provenance.title}>
      <SectionHeader
        label={<SignalLabel tone={isUnknown ? "neutral" : stale > 0 ? "warn" : "ok"} label={de.provenance.title} />}
        meta={stale > 0 ? de.provenance.staleBadge(stale) : "Vault"}
        rule={false}
      />
      {detail ? (
        <FleetEmptyState title="Vault-Provenienz nicht verfügbar" desc={detail} />
      ) : (
        <div className="grid gap-3 lg:grid-cols-2">
          <div className="space-y-2">
            <SectionHeader label={de.provenance.openTitle} rule={false} />
            {opens.length === 0 ? (
              <p className="text-sec text-ink-3">{de.provenance.openEmpty}</p>
            ) : (
              <ul className="space-y-2">
                {opens.map((session) => (
                  <li key={session.path}>
                    <ListRow
                      title={session.task}
                      meta={session.started}
                      leading={<span className="font-data text-micro text-ink-3">[{session.agent}]</span>}
                      trailing={session.stale ? <SignalLabel tone="warn" label={de.provenance.staleInline} /> : null}
                    >
                      {session.path}
                    </ListRow>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="space-y-2">
            <SectionHeader label={de.provenance.recentTitle} rule={false} />
            {receipts.length === 0 ? (
              <p className="text-sec text-ink-3">—</p>
            ) : (
              <ul className="space-y-2">
                {receipts.slice(0, 5).map((receipt) => (
                  <li key={receipt.path}>
                    <ListRow
                      title={receipt.file}
                      meta={receipt.when}
                      leading={<span className="font-data text-micro text-ink-3">[{receipt.agent}]</span>}
                    >
                      {receipt.path}
                    </ListRow>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

type Mode = "briefings" | "wissen" | "lesesaal" | "ergebnisse" | "modelle";

const MODE_TABS: { id: Mode; label: string; count?: (data: LibraryListResponse | null) => number }[] = [
  { id: "briefings", label: "Briefings", count: (data) => data?.count ?? 0 },
  { id: "wissen", label: "Nachschlagewerk" },
  { id: "lesesaal", label: "Lesesaal", count: (data) => data?.count ?? 0 },
  { id: "ergebnisse", label: "Ergebnisse" },
  { id: "modelle", label: "Modelle" },
];

function TabBar({ mode, onChange, lesesaalData }: { mode: Mode; onChange: (mode: Mode) => void; lesesaalData: LibraryListResponse | null }) {
  return (
    <div role="tablist" aria-label={t.eyebrow} className="flex gap-1 overflow-x-auto border-b border-line">
      {MODE_TABS.map((tab) => {
        const active = mode === tab.id;
        const count = tab.count?.(tab.id === "lesesaal" ? lesesaalData : null) ?? 0;
        return (
          <button
            key={tab.id}
            type="button"
            id={`bibliothek-tab-${tab.id}`}
            role="tab"
            aria-selected={active}
            aria-controls={`bibliothek-panel-${tab.id}`}
            onClick={() => onChange(tab.id)}
            className={`relative min-h-12 shrink-0 px-4 text-sec font-medium transition-colors ${
              active
                ? "text-bronze-hi"
                : "text-ink-2 hover:text-ink"
            }`}
          >
            <span className="flex items-center gap-2">
              {tab.label}
              {count > 0 ? (
                <sup className={`font-data text-micro font-semibold tabular-nums ${active ? "text-bronze-hi" : "text-ink-3"}`}>
                  {count}
                </sup>
              ) : null}
            </span>
            {active ? <span className="absolute inset-x-0 -bottom-px h-0.5 bg-live" /> : null}
          </button>
        );
      })}
    </div>
  );
}

export function BibliothekView({ density }: { density?: Density }) {
  // Modus als URL-Search-Param (S2, Deep-Links): Reload/Link-Teilen stellt den
  // Modus wieder her. Moduswechsel ist ein "Filterwechsel" → replace, kein
  // Verlaufseintrag. Alle Panels bleiben IMMER gemountet (nur `hidden`
  // umschaltet) — so verwirft der Wechsel weder Suchtext/Filter noch das
  // offene Dokument des jeweils anderen Modus (S3).
  const [searchParams, setSearchParams] = useSearchParams();
  const mode: Mode = useMemo(() => {
    const m = searchParams.get("mode");
    if (m === "lesesaal" || m === "wissen" || m === "ergebnisse" || m === "modelle") return m;
    return "briefings";
  }, [searchParams]);
  const setMode = useCallback((next: Mode) => {
    setSearchParams((prev) => {
      const p = new URLSearchParams(prev);
      if (next === "briefings") p.delete("mode");
      else p.set("mode", next);
      return p;
    }, { replace: true });
  }, [setSearchParams]);

  // Lesesaal-Daten für den Badge (Live-Count) — lightweight, da LesesaalBody
  // selbstständig lädt. Hier nur für die Tab-Bar.
  const [lesesaalData, setLesesaalData] = useState<LibraryListResponse | null>(null);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetchJSON<LibraryListResponse>("/api/library/items?limit=1&offset=0");
        if (!cancelled) setLesesaalData(res);
      } catch {
        // Badge bleibt leer bei Fehler — kein Blocker.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const openItem = useCallback((item: LibraryItem) => {
    setSearchParams((prev) => {
      const p = new URLSearchParams(prev);
      p.set("mode", "lesesaal");
      p.set("item", item.id);
      return p;
    });
  }, [setSearchParams]);

  // Ergebnisse → Lesesaal deep-link (Artefakt-Links in ErgebnisseShelf tragen
  // bereits eine Lesesaal-Item-Id, z.B. "deliverable::t_x::RESULT.md") — nur
  // die Id, kein voller LibraryItem nötig, darum ein eigener schmaler Callback
  // statt `openItem`s Signatur anzufassen.
  const openLesesaalItemById = useCallback((id: string) => {
    setSearchParams((prev) => {
      const p = new URLSearchParams(prev);
      p.set("mode", "lesesaal");
      p.set("item", id);
      return p;
    });
  }, [setSearchParams]);

  return (
    <div className="space-y-5">
      <TabBar mode={mode} onChange={setMode} lesesaalData={lesesaalData} />
      <div id="bibliothek-panel-briefings" role="tabpanel" hidden={mode !== "briefings"}>
        <BriefingsShelf onOpenItem={openItem} density={density} />
      </div>
      <div id="bibliothek-panel-wissen" role="tabpanel" hidden={mode !== "wissen"}>
        <KnowledgeShelf />
      </div>
      <div id="bibliothek-panel-lesesaal" role="tabpanel" hidden={mode !== "lesesaal"}>
        <LesesaalBody />
      </div>
      <div id="bibliothek-panel-ergebnisse" role="tabpanel" hidden={mode !== "ergebnisse"}>
        <ErgebnisseShelf onOpenLesesaalItem={openLesesaalItemById} />
      </div>
      <div id="bibliothek-panel-modelle" role="tabpanel" hidden={mode !== "modelle"}>
        <ModelleShelf />
      </div>
    </div>
  );
}
