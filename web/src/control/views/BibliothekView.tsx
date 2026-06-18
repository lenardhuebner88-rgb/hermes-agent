import { useCallback, useEffect, useMemo, useState } from "react";
import { Library, Newspaper } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { Hero } from "../components/Hero";
import { ToneCallout } from "../components/atoms";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import { SkeletonCard } from "../components/primitives";
import { ProseMarkdown } from "../components/ProseMarkdown";
import { fmtClock } from "../lib/derive";
import type { Density } from "../hooks/useDensity";
import { CATEGORY_LABEL, countByCategory, groupBySeries, newestPerCategory, seriesNeighbors } from "./BibliothekView.helpers";
import { KnowledgeShelf } from "./knowledge/KnowledgeShelf";

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
  wissenSubtitle: "Das dauerhafte Wissen des Servers — sauber geordnet zum Nachschlagen, für Agents und dich.",
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
  truncated: "Liste gekappt — neueste zuerst.",
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
};


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
}

interface LibraryListResponse {
  items: LibraryItem[];
  count: number;
  truncated: boolean;
  categories: string[];
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


export function ItemRow({ item, unreadSince, onOpen }: { item: LibraryItem; unreadSince: number; onOpen: (item: LibraryItem) => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(item)}
        className="flex w-full flex-wrap items-center gap-2 rounded-md border border-[var(--hc-border)] px-3 py-2 text-left hover:bg-white/5"
      >
        <span className="min-w-0 flex-1 basis-64 truncate text-[0.86rem] text-white">{item.title}</span>
        {item.ts > unreadSince ? (
          <span className="rounded-full border border-cyan-500/40 px-1.5 py-0.5 text-[0.62rem] text-cyan-300">{t.newBadge}</span>
        ) : null}
        <span className="hc-mono shrink-0 text-[0.7rem] hc-dim">{fmtClock(item.ts)}</span>
      </button>
    </li>
  );
}

export function TopicFollowCard({ topic, onToggle, pending }: { topic: LibraryTopic; onToggle: (topic: LibraryTopic) => void; pending: boolean }) {
  return (
    <article className="rounded-xl border border-[var(--hc-border)] bg-black/20 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-[0.9rem] font-semibold text-white">{topic.title}</h3>
          {topic.followed ? (
            <p className="mt-1 text-[0.72rem] text-emerald-300">{t.topicFollowing}</p>
          ) : (
            <p className="mt-1 text-[0.72rem] hc-dim">{t.topicFollow}</p>
          )}
        </div>
        <button
          type="button"
          disabled={pending}
          onClick={() => onToggle(topic)}
          aria-pressed={topic.followed}
          className={`inline-flex min-h-8 shrink-0 items-center rounded-full border px-2.5 py-1 text-[0.72rem] ${
            topic.followed
              ? "border-emerald-500/40 text-emerald-200"
              : "border-[var(--hc-accent-border)] text-[var(--hc-accent-text)]"
          } disabled:opacity-50`}
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
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
        {topics.map((topic) => (
          <TopicFollowCard key={topic.id} topic={topic} onToggle={onToggle} pending={pendingTopicId === topic.id} />
        ))}
      </div>
    </FleetPanel>
  );
}

export function SavedSearchShelf({ searches, onApply }: { searches: LibrarySavedSearch[]; onApply: (search: LibrarySavedSearch) => void }) {
  return (
    <FleetPanel eyebrow={t.savedTitle} meta={t.savedMeta}>
      {searches.length === 0 ? (
        <p className="text-sm hc-dim">{t.savedEmpty}</p>
      ) : (
        <ul className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {searches.map((search) => (
            <li key={search.id} className="rounded-xl border border-[var(--hc-border)] bg-black/20 p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="truncate text-[0.9rem] font-semibold text-white">{search.title || search.name}</h3>
                  <p className="mt-1 line-clamp-2 text-[0.78rem] hc-soft">{search.query}</p>
                  {[...search.topic_tags, ...search.person_tags].length ? (
                    <p className="mt-2 flex flex-wrap gap-1">
                      {[...search.topic_tags, ...search.person_tags].map((tag) => (
                        <span key={tag} className="rounded-full border border-white/10 px-1.5 py-0.5 text-[0.62rem] hc-dim">{tag}</span>
                      ))}
                    </p>
                  ) : null}
                </div>
                <button type="button" onClick={() => onApply(search)} className="inline-flex min-h-8 shrink-0 items-center rounded-full border border-white/10 px-2.5 py-1 text-[0.72rem] hc-soft hover:bg-white/5">
                  {t.savedApply}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </FleetPanel>
  );
}

export function ReadingView({ item, neighbors, onNavigate, onBack }: {
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

  return (
    <FleetPanel eyebrow={item.series} meta={`${CATEGORY_LABEL[item.category] ?? item.category} · ${fmtClock(item.ts)} · ${item.source_ref}`}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <button type="button" onClick={onBack} className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-2.5 py-1 text-[0.78rem] hc-soft hover:bg-white/5">{t.back}</button>
        <button type="button" disabled={!neighbors.prev} onClick={() => neighbors.prev && onNavigate(neighbors.prev)} className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-2.5 py-1 text-[0.78rem] hc-soft hover:bg-white/5 disabled:opacity-40">{t.prev}</button>
        <button type="button" disabled={!neighbors.next} onClick={() => neighbors.next && onNavigate(neighbors.next)} className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-2.5 py-1 text-[0.78rem] hc-soft hover:bg-white/5 disabled:opacity-40">{t.next}</button>
      </div>
      <h3 className="mb-2 text-base font-semibold text-white">{item.title}</h3>
      {error ? <ToneCallout tone="red">{error}</ToneCallout> : null}
      {detail ? <ProseMarkdown>{detail.body_md}</ProseMarkdown> : error ? null : <SkeletonCard rows={5} />}
    </FleetPanel>
  );
}

// Lesesaal (Ausgaben) — der bisherige Bibliothek-Inhalt, unverändert in Logik.
// Der Hero lebt jetzt im Eltern-`BibliothekView`; die Filter (Kategorie-Chips +
// Suche) sitzen darum in einer eigenen Filterleiste statt im Hero.
export function LesesaalBody() {
  const [category, setCategory] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [data, setData] = useState<LibraryListResponse | null>(null);
  const [topics, setTopics] = useState<LibraryTopic[]>([]);
  const [savedSearches, setSavedSearches] = useState<LibrarySavedSearch[]>([]);
  const [pendingTopicId, setPendingTopicId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reading, setReading] = useState<LibraryItem | null>(null);
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

  const load = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (category) params.set("category", category);
      if (q.trim()) params.set("q", q.trim());
      params.set("limit", "120");
      const res = await fetchJSON<LibraryListResponse>(`/api/library/items?${params.toString()}`);
      setData(res);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [category, q]);

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
    setReading(null);
    setQ(search.query);
  }, []);

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

  const items = useMemo(() => data?.items ?? [], [data]);
  const isFrontpage = !category && !q.trim();

  const frontpage = useMemo(() => newestPerCategory(items), [items]);
  const shelves = useMemo(() => groupBySeries(items), [items]);
  const neighbors = useMemo(() => seriesNeighbors(items, reading), [reading, items]);
  const counts = useMemo(() => countByCategory(items), [items]);

  return (
    <div className="space-y-4">
      <div className="hc-surface-card p-3">
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" onClick={() => { setCategory(null); setReading(null); }} className={`inline-flex min-h-9 items-center rounded-full border px-3 py-1 text-[0.78rem] ${!category ? "border-[var(--hc-accent-border)] text-[var(--hc-accent-text)]" : "border-white/10 hc-soft"}`}>{t.frontpage}</button>
          {(data?.categories ?? []).map((c) => (
            <button key={c} type="button" onClick={() => { setCategory(c); setReading(null); }} className={`inline-flex min-h-9 items-center gap-1.5 rounded-full border px-3 py-1 text-[0.78rem] ${category === c ? "border-[var(--hc-accent-border)] text-[var(--hc-accent-text)]" : "border-white/10 hc-soft"}`}>
              {CATEGORY_LABEL[c] ?? c}
              {counts[c] ? <span className="hc-mono text-[0.66rem] hc-dim">{counts[c]}</span> : null}
            </button>
          ))}
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={t.searchPlaceholder}
            aria-label={t.searchPlaceholder}
            className="min-w-48 flex-1 rounded-md border border-[var(--hc-border)] bg-black/25 px-3 py-1.5 text-sm text-white placeholder:hc-dim"
          />
        </div>
      </div>

      <TopicFollowSection topics={topics} onToggle={toggleTopicFollow} pendingTopicId={pendingTopicId} />
      <SavedSearchShelf searches={savedSearches} onApply={applySavedSearch} />

      {error ? <ToneCallout tone="red">{t.loadError}<br />{error}</ToneCallout> : null}
      {data?.truncated ? <p className="text-xs text-amber-200">{t.truncated}</p> : null}

      {reading ? (
        <ReadingView item={reading} neighbors={neighbors} onNavigate={setReading} onBack={() => setReading(null)} />
      ) : data === null && !error ? (
        <SkeletonCard rows={4} />
      ) : data !== null && items.length === 0 ? (
        <FleetEmptyState title={t.empty} desc={t.emptyDesc} />
      ) : isFrontpage ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {frontpage.map((item) => (
            <button key={item.id} type="button" onClick={() => setReading(item)} className="hc-surface-card space-y-2 p-4 text-left hover:bg-white/5">
              <p className="hc-eyebrow">{CATEGORY_LABEL[item.category] ?? item.category}</p>
              <h3 className="text-[0.95rem] font-semibold leading-snug text-white">{item.title}</h3>
              <p className="line-clamp-3 text-[0.8rem] leading-relaxed hc-soft">{item.preview}</p>
              <p className="hc-mono text-[0.7rem] hc-dim">
                {item.series} · {fmtClock(item.ts)}
                {item.ts > unreadSince ? <span className="ml-2 rounded-full border border-cyan-500/40 px-1.5 py-0.5 text-[0.62rem] text-cyan-300">{t.newBadge}</span> : null}
              </p>
            </button>
          ))}
        </div>
      ) : (
        <div className="space-y-3">
          {shelves.map((shelf) => (
            <FleetPanel key={shelf.seriesId} eyebrow={shelf.series} meta={`${shelf.meta ? `${shelf.meta} · ` : ""}${t.issues(shelf.items.length)} · zuletzt ${fmtClock(shelf.items[0]?.ts ?? 0)}`}>
              <ul className="space-y-1.5">
                {shelf.items.map((item) => (
                  <ItemRow key={item.id} item={item} unreadSince={unreadSince} onOpen={setReading} />
                ))}
              </ul>
            </FleetPanel>
          ))}
        </div>
      )}
    </div>
  );
}

type Mode = "wissen" | "lesesaal";

function ModeSwitch({ mode, onChange }: { mode: Mode; onChange: (mode: Mode) => void }) {
  const tab = (value: Mode, label: string, Icon: typeof Library) => (
    <button
      type="button"
      role="tab"
      aria-selected={mode === value}
      aria-controls={`bibliothek-panel-${value}`}
      onClick={() => onChange(value)}
      className={`inline-flex min-h-9 items-center gap-1.5 rounded-full px-3.5 py-1.5 text-[0.8rem] font-medium transition-colors ${
        mode === value
          ? "bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)] shadow-sm"
          : "hc-soft hover:bg-white/5"
      }`}
    >
      <Icon className="h-4 w-4" />
      {label}
    </button>
  );
  return (
    <div role="tablist" aria-label={t.eyebrow} className="inline-flex items-center gap-1 rounded-full border border-[var(--hc-border)] bg-black/20 p-1">
      {tab("wissen", t.modeWissen, Library)}
      {tab("lesesaal", t.modeLesesaal, Newspaper)}
    </div>
  );
}

export function BibliothekView({ density }: { density?: Density }) {
  const [mode, setMode] = useState<Mode>("wissen");
  const wissen = mode === "wissen";
  return (
    <div className="space-y-4">
      <Hero
        eyebrow={t.eyebrow}
        title={wissen ? t.wissenTitle : t.lesesaalTitle}
        subtitle={wissen ? t.wissenSubtitle : t.lesesaalSubtitle}
        tone={wissen ? "cyan" : "amber"}
        density={density}
      >
        <ModeSwitch mode={mode} onChange={setMode} />
      </Hero>
      {wissen ? (
        <div id="bibliothek-panel-wissen" role="tabpanel"><KnowledgeShelf /></div>
      ) : (
        <div id="bibliothek-panel-lesesaal" role="tabpanel"><LesesaalBody /></div>
      )}
    </div>
  );
}
