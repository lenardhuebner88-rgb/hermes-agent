import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { fetchJSON } from "@/lib/api";
import { Disclosure, Eyebrow } from "../../components/primitives";
import { FleetEmptyState, KpiTile, SectionHeader, SignalLabel } from "../../components/leitstand";
import { fmtClock } from "../../lib/derive";
import { useVaultProvenance, useKnowledgeCatalog } from "../../hooks/useControlData";
import type { Density } from "../../hooks/useDensity";
import { VaultProvenanceShelf } from "../BibliothekView";
import type { LibraryItem, LibraryListResponse, StructuredModelBrief } from "../BibliothekView";
import { CollectionGlyph } from "../knowledge/KnowledgeShelf";
import type { KnowledgeCatalog } from "../knowledge/knowledge.helpers";
import { plainMarkdownPreview } from "../BibliothekView.helpers";

const t = {
  heroEyebrow: "Bibliothek",
  heroTitle: "Willkommen in deiner Bibliothek",
  heroSubtitle: "Hier sammelt sich alles Wichtige: aktuelle Briefings, dauerhaftes Wissen zum Nachschlagen und alles, was Hermes für dich produziert.",
  newBriefings: (n: number) => `${n} neue Briefing${n === 1 ? "" : "s"} heute`,
  docsCount: (n: number) => `${n} Dokumente im Nachschlagewerk`,
  todayForYou: "Heute für dich",
  todayMeta: (shown: number, total: number) => `Neueste ${shown} von ${total} Briefings`,
  featuredLabel: "Featured Briefing",
  briefingLabel: "Briefing",
  knowledgeTitle: "Nachschlagewerk",
  knowledgeSubtitle: "Dauerhaftes Wissen, geordnet",
  provenanceTitle: "Provenienz",
  topicsTitle: "Themen folgen",
  issuesLabel: "Lesesaal",
  recentIssuesTitle: "Neueste Ausgaben",
  recentIssuesEmpty: "Noch keine Ausgaben.",
  workingLabel: "Provenienz",
  workingNowTitle: "Wer arbeitet gerade",
  workingNowEmpty: "Gerade arbeitet niemand.",
  loadError: "Briefings konnten nicht geladen werden.",
  emptyTitle: "Noch keine Briefings",
  emptyDesc: "Sobald Crons oder Recherchen Ausgaben produzieren, erscheinen sie hier.",
  topStory: "Top-Story",
  modelNews: "Modell-News",
  watchlistDelta: "Watchlist-Update",
  readFullBrief: "Ganzes Briefing lesen",
};

function berlinTimeParts(ts: number): { hour: number; minute: number; label: string } {
  const parts = new Intl.DateTimeFormat("de-DE", {
    timeZone: "Europe/Berlin",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(new Date(ts * 1000));
  const hour = Number(parts.find((part) => part.type === "hour")?.value ?? 0) % 24;
  const minute = Number(parts.find((part) => part.type === "minute")?.value ?? 0);
  return { hour, minute, label: `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}` };
}

function berlinDay(ts: number): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Berlin",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(ts * 1000));
}

function nextBriefRun(ts: number): string {
  const { hour, minute } = berlinTimeParts(ts);
  const minutes = hour * 60 + minute;
  const next = [8 * 60, 14 * 60, 20 * 60].find((slot) => slot > minutes) ?? 8 * 60;
  return `${String(Math.floor(next / 60)).padStart(2, "0")}:00`;
}

function StructuredBriefCard({
  item,
  brief,
  onOpen,
}: {
  item: LibraryItem;
  brief: StructuredModelBrief;
  onOpen: (item: LibraryItem) => void;
}) {
  return (
    <article className="relative overflow-hidden rounded-panel border border-line bg-surface-1 p-5 sm:p-6 lg:col-span-2 lg:row-span-2">
      <div className="space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Eyebrow>{t.topStory}</Eyebrow>
          <span className="font-data text-micro tabular-nums text-ink-3">
            Stand {berlinTimeParts(item.ts).label} · nächster Lauf {nextBriefRun(item.ts)}
          </span>
        </div>

        <h3 className="max-w-4xl text-h2 font-semibold leading-snug text-ink">
          {brief.top_story}
        </h3>

        {brief.model_news.length > 0 ? (
          <section>
            <Eyebrow className="mb-2">{t.modelNews}</Eyebrow>
            <div className="grid gap-2 sm:grid-cols-2">
              {brief.model_news.slice(0, 4).map((news) => (
                <a
                  key={`${news.title}-${news.source_url}`}
                  href={news.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="min-h-12 rounded-card border border-line bg-surface-2 p-3 transition-colors hover:border-live/40 hover:bg-surface-3"
                >
                  <span className="block text-sec font-semibold text-ink">{news.title}</span>
                  <span className="mt-1 block line-clamp-3 text-sec leading-relaxed text-ink-2">{news.summary}</span>
                  <span className="mt-2 block truncate text-micro text-bronze-hi">{news.source_title} ↗</span>
                </a>
              ))}
            </div>
          </section>
        ) : null}

        {brief.watchlist_delta.length > 0 ? (
          <div
            className="rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2.5"
          >
            <SignalLabel tone="warn" label={t.watchlistDelta} />
            {brief.watchlist_delta.slice(0, 2).map((delta) => (
              <p key={delta} className="mt-1 text-sec leading-relaxed text-ink-2">{delta}</p>
            ))}
          </div>
        ) : null}

        <button
          type="button"
          onClick={() => onOpen(item)}
          className="inline-flex min-h-12 items-center rounded-card px-1 text-sec font-semibold text-bronze-hi hover:underline"
        >
          {t.readFullBrief} →
        </button>
      </div>
    </article>
  );
}

function BriefingCard({ item, featured = false, onOpen }: { item: LibraryItem; featured?: boolean; onOpen: (item: LibraryItem) => void }) {
  return (
    <article
      role="button"
      tabIndex={0}
      onClick={() => onOpen(item)}
      onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onOpen(item); } }}
      className={`min-h-12 cursor-pointer rounded-card border border-line bg-surface-2 transition-all hover:-translate-y-0.5 hover:border-live/40 hover:bg-surface-3 ${featured ? "col-span-2 row-span-2 relative overflow-hidden" : ""}`}
    >
      <div className={featured ? "p-6" : "p-5"}>
        <Eyebrow>{featured ? t.featuredLabel : t.briefingLabel}</Eyebrow>
        <h3 className={`mt-4 font-semibold leading-snug text-ink ${featured ? "text-h2" : "text-emph"}`}>{item.title}</h3>
        <p className={`mt-2 leading-relaxed text-ink-2 ${featured ? "line-clamp-4 text-body" : "line-clamp-3 text-sec"}`}>{plainMarkdownPreview(item.preview)}</p>
        <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-micro text-ink-3">
          <span className="font-data">{item.series}</span>
          <span>·</span>
          <span>{fmtClock(item.ts)}</span>
        </div>
      </div>
    </article>
  );
}

// Mittelzeile (Desktop-only, laut Mockup): Lesesaal-Vorschau + Provenienz-
// Vorschau unter dem Featured-Briefing. Beide speisen sich aus bereits
// vorhandenen Endpoints/Hooks — kein Backend-Change.
function RecentIssuesCard({ items, onOpen }: { items: LibraryItem[]; onOpen: (item: LibraryItem) => void }) {
  return (
    <article className="rounded-card border border-line bg-surface-2 p-5">
      <Eyebrow>{t.issuesLabel}</Eyebrow>
      <h3 className="mt-3 text-body font-semibold text-ink">{t.recentIssuesTitle}</h3>
      <div className="mt-3 space-y-0.5">
        {items.length === 0 ? (
          <p className="text-sec text-ink-3">{t.recentIssuesEmpty}</p>
        ) : (
          items.slice(0, 3).map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => onOpen(item)}
              className="flex min-h-12 w-full items-center justify-between gap-3 rounded-card px-2 text-left hover:bg-surface-3"
            >
              <span className="truncate text-sec text-ink-2">{item.title}</span>
              <span className="shrink-0 font-data text-micro tabular-nums text-ink-3">{fmtClock(item.ts)}</span>
            </button>
          ))
        )}
      </div>
    </article>
  );
}

function WorkingNowCard({ sessions }: { sessions: { agent: string; task: string; path: string }[] }) {
  return (
    <article className="rounded-card border border-line bg-surface-2 p-5">
      <Eyebrow>{t.workingLabel}</Eyebrow>
      <h3 className="mt-3 text-body font-semibold text-ink">{t.workingNowTitle}</h3>
      <div className="mt-3 space-y-0.5">
        {sessions.length === 0 ? (
          <p className="text-sec text-ink-3">{t.workingNowEmpty}</p>
        ) : (
          sessions.slice(0, 3).map((session) => (
            <div key={session.path} className="flex min-h-12 items-center justify-between gap-3 px-2">
              <span className="shrink-0 font-data text-micro text-ink-2">[{session.agent}]</span>
              <span className="truncate text-micro text-ink-3">{session.task}</span>
            </div>
          ))
        )}
      </div>
    </article>
  );
}

function KnowledgeQuickShelf({ catalog, onSelect }: { catalog: KnowledgeCatalog | null; onSelect: (id: string) => void }) {
  return (
    <section className="mt-6">
      <SectionHeader label={t.knowledgeTitle} meta={t.knowledgeSubtitle} rule={false} />
      <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {(catalog?.collections ?? []).map((collection) => {
          return (
            <div
              key={collection.id}
              role="button"
              tabIndex={0}
              onClick={() => onSelect(collection.id)}
              onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(collection.id); } }}
              className="min-h-12 cursor-pointer rounded-card border border-line bg-surface-2 p-4 transition-all hover:-translate-y-0.5 hover:border-live/40 hover:bg-surface-3"
            >
              <div className="grid size-12 place-items-center rounded-card border border-line bg-surface-1 text-brand">
                <CollectionGlyph name={collection.icon} className="h-5 w-5" />
              </div>
              <h4 className="mt-3 text-body font-semibold text-ink">{collection.title}</h4>
              <p className="mt-1 font-data text-micro tabular-nums text-ink-3">{collection.doc_count} Dokumente</p>
            </div>
          );
        })}
      </div>
    </section>
  );
}

interface BriefingsShelfProps {
  onOpenItem: (item: LibraryItem) => void;
  density?: Density;
}

export function BriefingsShelf({ onOpenItem }: BriefingsShelfProps) {
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [issues, setIssues] = useState<LibraryItem[]>([]);
  const [data, setData] = useState<LibraryListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const provenance = useVaultProvenance();
  const knowledge = useKnowledgeCatalog();
  const [, setSearchParams] = useSearchParams();

  // Schnellauswahl-Kachel geklickt → Nachschlagewerk mit der Sammlung
  // preselektiert öffnen (KnowledgeShelf liest `collection` als Filter-Param).
  const openCollection = useCallback((id: string) => {
    setSearchParams((prev) => {
      const p = new URLSearchParams(prev);
      p.set("mode", "wissen");
      p.set("collection", id);
      return p;
    });
  }, [setSearchParams]);

  const load = useCallback(async () => {
    try {
      // Briefings (Featured-Grid) + neueste Ausgaben (Lesesaal-Vorschau der
      // Mittelzeile) in einem Rutsch — beide sind reine Reads.
      const [newsRes, briefRes, issuesRes] = await Promise.all([
        fetchJSON<LibraryListResponse>("/api/library/items?category=news&limit=20&offset=0"),
        fetchJSON<LibraryListResponse>("/api/library/items?category=briefings&limit=20&offset=0"),
        fetchJSON<LibraryListResponse>("/api/library/items?limit=3&offset=0"),
      ]);
      const frontPageItems = [...(newsRes.items ?? []), ...(briefRes.items ?? [])]
        .sort((a, b) => b.ts - a.ts);
      setData({
        ...briefRes,
        count: newsRes.count + briefRes.count,
        now: Math.max(newsRes.now ?? 0, briefRes.now ?? 0),
      });
      setItems(frontPageItems);
      setIssues(issuesRes.items ?? []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    const handle = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(handle);
  }, [load]);

  const briefings = useMemo(
    () => items.filter((item) => item.category === "news" || item.category === "briefings"),
    [items],
  );
  const featured = useMemo(
    () => briefings.find((item) => item.structured && item.structured_brief) ?? briefings[0] ?? null,
    [briefings],
  );
  const rest = useMemo(() => briefings.filter((b) => b.id !== featured?.id).slice(0, 2), [briefings, featured]);
  const total = data?.count ?? briefings.length;
  const todayCount = useMemo(() => {
    const today = berlinDay(data?.now ?? featured?.ts ?? 0);
    return briefings.filter((item) => berlinDay(item.ts) === today).length;
  }, [briefings, data?.now, featured?.ts]);

  return (
    <div className="space-y-6">
      <section className="rounded-panel border border-line bg-surface-1 p-5 sm:p-6">
        <Eyebrow>{t.heroEyebrow}</Eyebrow>
        <h2 className="mt-2 max-w-4xl font-display text-h2 font-semibold text-ink">{t.heroTitle}</h2>
        <p className="mt-2 max-w-4xl text-body text-ink-2">{t.heroSubtitle}</p>
        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          <KpiTile label="Briefings heute" value={todayCount} delta={t.newBriefings(todayCount)} />
          <KpiTile
            label="Nachschlagewerk"
            value={knowledge.data?.count ?? "…"}
            delta={typeof knowledge.data?.count === "number" ? t.docsCount(knowledge.data.count) : "…"}
          />
        </div>
      </section>

      {error ? <div role="alert" className="rounded-card border border-status-alert/30 bg-status-alert/10 p-3"><SignalLabel tone="alert" label={t.loadError} /><p className="mt-1 text-sec text-ink-2">{error}</p></div> : null}

      <div>
        <SectionHeader label={t.todayForYou} meta={data === null && !error ? "…" : t.todayMeta(Math.min(briefings.length, 3), total)} rule={false} />
        {data !== null && briefings.length === 0 && !error ? (
          <FleetEmptyState title={t.emptyTitle} desc={t.emptyDesc} />
        ) : (
          <div className="mt-3 grid gap-4 lg:grid-cols-3">
            {featured?.structured && featured.structured_brief ? (
              <StructuredBriefCard item={featured} brief={featured.structured_brief} onOpen={onOpenItem} />
            ) : featured ? (
              <BriefingCard item={featured} featured onOpen={onOpenItem} />
            ) : null}
            {rest.map((b) => <BriefingCard key={b.id} item={b} onOpen={onOpenItem} />)}
          </div>
        )}
      </div>

      {/* Mittelzeile aus dem Mockup: Lesesaal- + Provenienz-Vorschau unter dem
          Featured-Briefing. Desktop-only — das Mobil-Mockup lässt sie bewusst
          weg (dort führt der Weg direkt zum Nachschlagewerk). */}
      <div className="hidden gap-4 lg:grid lg:grid-cols-3">
        <RecentIssuesCard items={issues} onOpen={onOpenItem} />
        <WorkingNowCard sessions={provenance.data?.open_sessions ?? []} />
      </div>

      <KnowledgeQuickShelf catalog={knowledge.data} onSelect={openCollection} />

      <Disclosure className="[&>button]:min-h-12" summary={<span className="text-sec font-semibold text-ink">{t.provenanceTitle}</span>} defaultOpen={false}>
        <div className="pt-2">
          <VaultProvenanceShelf data={provenance.data} error={provenance.error} />
        </div>
      </Disclosure>

      <Disclosure className="[&>button]:min-h-12" summary={<span className="text-sec font-semibold text-ink">{t.topicsTitle}</span>} defaultOpen={false}>
        <div className="pt-2 text-sec text-ink-2">
          <p>Beobachtete Themen erscheinen hier, sobald du Themen im Lesesaal folgst.</p>
        </div>
      </Disclosure>
    </div>
  );
}
