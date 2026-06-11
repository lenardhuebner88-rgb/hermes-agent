import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { Hero } from "../components/Hero";
import { ToneCallout } from "../components/atoms";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import { ProseMarkdown } from "../components/ProseMarkdown";
import { fmtClock } from "../lib/derive";
import type { Density } from "../hooks/useDensity";

// Phase D/E (Programm 3): Bibliothek — der Lesesaal. Zeitungs-Metapher statt
// Tabellen-Metapher: „Heute"-Frontpage (Neuestes pro Kategorie), Kategorie-
// Regale (Serie = Abo eines Crons, Ausgaben chronologisch), Lese-Ansicht mit
// gerendertem Markdown und ←/→ innerhalb der Serie. Read-only; Ungelesen-
// Status lebt rein im localStorage (kein Server-State).
const t = {
  eyebrow: "Bibliothek",
  title: "Der Lesesaal",
  subtitle: "Alles, was Hermes produziert, menschenlesbar an einem Ort — Digests, Recherchen, Receipts.",
  searchPlaceholder: "Suche in Titel + Text …",
  frontpage: "Heute",
  all: "Alle",
  empty: "Noch nichts zu lesen.",
  emptyDesc: "Sobald Crons/Recherchen Ausgaben produzieren, füllt sich die Bibliothek.",
  loadError: "Bibliothek konnte nicht geladen werden.",
  newBadge: "neu",
  issues: (n: number) => `${n} Ausgaben`,
  back: "← Übersicht",
  prev: "← ältere",
  next: "neuere →",
  truncated: "Liste gekappt — neueste zuerst.",
};

export const CATEGORY_LABEL: Record<string, string> = {
  news: "News",
  briefings: "Briefings",
  recherchen: "Recherchen",
  arbeit: "Arbeit & Receipts",
  wartung: "Wartung",
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

const LAST_VISIT_KEY = "hc-bibliothek-last-visit";

/** Serien-Gruppierung fürs Regal (exportiert für den Test). */
export function groupBySeries(items: LibraryItem[]): { seriesId: string; series: string; meta: string; items: LibraryItem[] }[] {
  const groups = new Map<string, { seriesId: string; series: string; meta: string; items: LibraryItem[] }>();
  for (const item of items) {
    let g = groups.get(item.series_id);
    if (!g) {
      g = { seriesId: item.series_id, series: item.series, meta: item.series_meta, items: [] };
      groups.set(item.series_id, g);
    }
    g.items.push(item);
  }
  return [...groups.values()].sort((a, b) => (b.items[0]?.ts ?? 0) - (a.items[0]?.ts ?? 0));
}

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

function ReadingView({ item, neighbors, onNavigate, onBack }: {
  item: LibraryItem;
  neighbors: { prev: LibraryItem | null; next: LibraryItem | null };
  onNavigate: (item: LibraryItem) => void;
  onBack: () => void;
}) {
  const [detail, setDetail] = useState<LibraryDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    void (async () => {
      try {
        const d = await fetchJSON<LibraryDetail>(`/api/library/item?id=${encodeURIComponent(item.id)}`);
        if (!cancelled) setError(null), setDetail(d);
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
      {detail ? <ProseMarkdown>{detail.body_md}</ProseMarkdown> : error ? null : <p className="text-sm hc-dim">…</p>}
    </FleetPanel>
  );
}

export function BibliothekView(_props: { density?: Density }) {
  const [category, setCategory] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [data, setData] = useState<LibraryListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reading, setReading] = useState<LibraryItem | null>(null);
  // Ungelesen v1: Zeitstempel des letzten Besuchs aus localStorage; beim
  // Mount einfrieren, dann sofort fortschreiben.
  const unreadSinceRef = useRef<number>(0);
  if (unreadSinceRef.current === 0) {
    const raw = typeof window !== "undefined" ? window.localStorage.getItem(LAST_VISIT_KEY) : null;
    unreadSinceRef.current = raw ? Number(raw) || 1 : 1;
    try { window.localStorage.setItem(LAST_VISIT_KEY, String(Math.floor(Date.now() / 1000))); } catch { /* private mode */ }
  }
  const unreadSince = unreadSinceRef.current;

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

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), 60000);
    return () => window.clearInterval(id);
  }, [load]);

  const items = useMemo(() => data?.items ?? [], [data]);
  const isFrontpage = !category && !q.trim();

  // Frontpage: das Neueste pro Kategorie (Items kommen ts-absteigend).
  const frontpage = useMemo(() => {
    const seen = new Set<string>();
    const top: LibraryItem[] = [];
    for (const item of items) {
      if (!seen.has(item.category)) {
        seen.add(item.category);
        top.push(item);
      }
    }
    return top;
  }, [items]);

  const shelves = useMemo(() => groupBySeries(items), [items]);

  const neighbors = useMemo(() => {
    if (!reading) return { prev: null, next: null };
    const series = items.filter((i) => i.series_id === reading.series_id);
    const idx = series.findIndex((i) => i.id === reading.id);
    // Liste ist neueste-zuerst: "ältere" = idx+1, "neuere" = idx-1.
    return {
      prev: idx >= 0 && idx + 1 < series.length ? series[idx + 1] : null,
      next: idx > 0 ? series[idx - 1] : null,
    };
  }, [reading, items]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const item of items) c[item.category] = (c[item.category] ?? 0) + 1;
    return c;
  }, [items]);

  return (
    <div className="space-y-4">
      <Hero eyebrow={t.eyebrow} title={t.title} subtitle={t.subtitle} count={data?.count ?? "—"} countHint={t.issues(data?.count ?? 0)} tone="amber">
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
      </Hero>

      {error ? <ToneCallout tone="red">{t.loadError}<br />{error}</ToneCallout> : null}
      {data?.truncated ? <p className="text-xs text-amber-200">{t.truncated}</p> : null}

      {reading ? (
        <ReadingView item={reading} neighbors={neighbors} onNavigate={setReading} onBack={() => setReading(null)} />
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
