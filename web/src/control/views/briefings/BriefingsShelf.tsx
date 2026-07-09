import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { fetchJSON } from "@/lib/api";
import { Hero } from "../../components/Hero";
import { Disclosure } from "../../components/primitives";
import { SectionHeader } from "../../components/leitstand";
import { ToneCallout } from "../../components/atoms";
import { fmtClock } from "../../lib/derive";
import { heroAccent } from "../../lib/tones";
import { useVaultProvenance, useKnowledgeCatalog } from "../../hooks/useControlData";
import type { Density } from "../../hooks/useDensity";
import { VaultProvenanceShelf } from "../BibliothekView";
import type { LibraryItem, LibraryListResponse, StructuredModelBrief } from "../BibliothekView";
import { CollectionGlyph } from "../knowledge/KnowledgeShelf";
import type { KnowledgeCatalog } from "../knowledge/knowledge.helpers";

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
    <article className="hc-surface-card relative overflow-hidden p-5 sm:p-6 lg:col-span-2 lg:row-span-2">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(120%_100%_at_100%_0%,rgba(79,216,235,.08),transparent_55%)]" />
      <div className="relative space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="inline-flex items-center rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2.5 py-1 text-[0.62rem] font-bold uppercase tracking-wider text-[var(--hc-accent)]">
            {t.topStory}
          </span>
          <span className="hc-mono text-[0.7rem] text-[var(--hc-text-dim)]">
            Stand {berlinTimeParts(item.ts).label} · nächster Lauf {nextBriefRun(item.ts)}
          </span>
        </div>

        <h3 className="max-w-4xl text-xl font-semibold leading-snug text-[var(--hc-text)] sm:text-2xl">
          {brief.top_story}
        </h3>

        {brief.model_news.length > 0 ? (
          <section>
            <p className="mb-2 hc-eyebrow">{t.modelNews}</p>
            <div className="grid gap-2 sm:grid-cols-2">
              {brief.model_news.slice(0, 4).map((news) => (
                <a
                  key={`${news.title}-${news.source_url}`}
                  href={news.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="rounded-xl border border-white/10 bg-white/[.025] p-3 transition-colors hover:border-[var(--hc-border-strong)]"
                >
                  <span className="block text-sm font-semibold text-[var(--hc-text)]">{news.title}</span>
                  <span className="mt-1 block line-clamp-3 text-xs leading-relaxed text-[var(--hc-text-soft)]">{news.summary}</span>
                  <span className="mt-2 block truncate text-[0.68rem] text-[var(--hc-accent)]">{news.source_title} ↗</span>
                </a>
              ))}
            </div>
          </section>
        ) : null}

        {brief.watchlist_delta.length > 0 ? (
          <div
            className="rounded-xl border px-3 py-2.5"
            style={{
              borderColor: "color-mix(in srgb, var(--hc-amber) 30%, transparent)",
              background: "color-mix(in srgb, var(--hc-amber) 9%, transparent)",
            }}
          >
            <p className="text-[0.62rem] font-bold uppercase tracking-wider text-[var(--hc-amber)]">{t.watchlistDelta}</p>
            {brief.watchlist_delta.slice(0, 2).map((delta) => (
              <p key={delta} className="mt-1 text-xs leading-relaxed text-[var(--hc-text-soft)]">{delta}</p>
            ))}
          </div>
        ) : null}

        <button
          type="button"
          onClick={() => onOpen(item)}
          className="text-sm font-semibold text-[var(--hc-accent)] hover:underline"
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
      className={`hc-surface-card cursor-pointer transition-all hover:-translate-y-0.5 hover:border-[var(--hc-border-strong)] ${featured ? "col-span-2 row-span-2 relative overflow-hidden" : ""}`}
    >
      {featured ? <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(120%_100%_at_100%_0%,rgba(79,216,235,.08),transparent_55%)]" /> : null}
      <div className={`relative ${featured ? "p-6" : "p-5"}`}>
        <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[0.62rem] font-bold uppercase tracking-wider ${featured ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent)]" : "border-white/10 text-[var(--hc-text-soft)]"}`}>
          {featured ? t.featuredLabel : t.briefingLabel}
        </span>
        <h3 className={`mt-4 font-semibold leading-snug text-[var(--hc-text)] ${featured ? "text-2xl" : "text-lg"}`}>{item.title}</h3>
        <p className={`mt-2 text-[var(--hc-text-soft)] leading-relaxed ${featured ? "text-base line-clamp-4" : "text-sm line-clamp-3"}`}>{item.preview}</p>
        <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.72rem] text-[var(--hc-text-dim)]">
          <span className="hc-mono">{item.series}</span>
          <span>·</span>
          <span>{fmtClock(item.ts)}</span>
        </div>
      </div>
    </article>
  );
}

// Kleine, farbcodierte Sektions-Marke im Stil der KnowledgeQuickShelf-Icons
// (Inline-Style statt Tailwind-Arbitrary, damit color-mix gate-sicher bleibt
// und kein Roh-Hex in den DESIGN-Ratchet läuft).
function PreviewLabel({ label, tone }: { label: string; tone: "violet" | "amber" }) {
  const toneVar = `var(--hc-${tone})`;
  return (
    <span
      className="inline-flex items-center rounded-full border px-2.5 py-1 text-[0.62rem] font-bold uppercase tracking-wider"
      style={{ borderColor: `color-mix(in srgb, ${toneVar} 30%, transparent)`, color: toneVar }}
    >
      {label}
    </span>
  );
}

// Mittelzeile (Desktop-only, laut Mockup): Lesesaal-Vorschau + Provenienz-
// Vorschau unter dem Featured-Briefing. Beide speisen sich aus bereits
// vorhandenen Endpoints/Hooks — kein Backend-Change.
function RecentIssuesCard({ items, onOpen }: { items: LibraryItem[]; onOpen: (item: LibraryItem) => void }) {
  return (
    <article className="hc-surface-card p-5">
      <PreviewLabel label={t.issuesLabel} tone="violet" />
      <h3 className="mt-3 text-base font-semibold text-[var(--hc-text)]">{t.recentIssuesTitle}</h3>
      <div className="mt-3 space-y-0.5">
        {items.length === 0 ? (
          <p className="text-sm text-[var(--hc-text-dim)]">{t.recentIssuesEmpty}</p>
        ) : (
          items.slice(0, 3).map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => onOpen(item)}
              className="flex w-full items-center justify-between gap-3 rounded-lg px-2 py-1.5 text-left hover:bg-white/5"
            >
              <span className="truncate text-sm text-[var(--hc-text-soft)]">{item.title}</span>
              <span className="shrink-0 hc-mono text-[0.7rem] text-[var(--hc-text-dim)]">{fmtClock(item.ts)}</span>
            </button>
          ))
        )}
      </div>
    </article>
  );
}

function WorkingNowCard({ sessions }: { sessions: { agent: string; task: string; path: string }[] }) {
  return (
    <article className="hc-surface-card p-5">
      <PreviewLabel label={t.workingLabel} tone="amber" />
      <h3 className="mt-3 text-base font-semibold text-[var(--hc-text)]">{t.workingNowTitle}</h3>
      <div className="mt-3 space-y-0.5">
        {sessions.length === 0 ? (
          <p className="text-sm text-[var(--hc-text-dim)]">{t.workingNowEmpty}</p>
        ) : (
          sessions.slice(0, 3).map((session) => (
            <div key={session.path} className="flex items-center justify-between gap-3 px-2 py-1.5">
              <span className="shrink-0 hc-mono text-[0.72rem] text-[var(--hc-text-soft)]">[{session.agent}]</span>
              <span className="truncate text-[0.72rem] text-[var(--hc-text-dim)]">{session.task}</span>
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
          // Kollabiert die (theoretisch offenere) Backend-Palette auf die
          // vier Leitstand-Statusfarben — dieselbe Kanonisierung wie Hero
          // (siehe heroAccent): keine neuen --hc-*-Tokens für vereinzelte Töne.
          const toneVar = heroAccent(collection.accent);
          return (
            <div
              key={collection.id}
              role="button"
              tabIndex={0}
              onClick={() => onSelect(collection.id)}
              onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(collection.id); } }}
              className="hc-surface-card cursor-pointer p-4 transition-all hover:-translate-y-0.5 hover:border-[var(--hc-border-strong)]"
            >
              <div
                className="grid h-10 w-10 place-items-center rounded-xl border"
                style={{
                  borderColor: `color-mix(in srgb, ${toneVar} 30%, transparent)`,
                  background: `color-mix(in srgb, ${toneVar} 12%, transparent)`,
                  color: toneVar,
                }}
              >
                <CollectionGlyph name={collection.icon} className="h-5 w-5" />
              </div>
              <h4 className="mt-3 text-[0.95rem] font-semibold text-[var(--hc-text)]">{collection.title}</h4>
              <p className="mt-1 text-xs text-[var(--hc-text-dim)]">{collection.doc_count} Dokumente</p>
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

export function BriefingsShelf({ onOpenItem, density }: BriefingsShelfProps) {
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
      <Hero
        eyebrow={t.heroEyebrow}
        title={t.heroTitle}
        subtitle={t.heroSubtitle}
        tone="cyan"
        density={density}
        status={{ label: t.newBriefings(todayCount), tone: "cyan", dot: "live" }}
        action={<span className="rounded-full border border-white/10 bg-white/[.03] px-3 py-1.5 text-xs text-[var(--hc-text-soft)]">{knowledge.data ? t.docsCount(knowledge.data.count) : "…"}</span>}
      />

      {error ? <ToneCallout tone="red">{t.loadError}<br />{error}</ToneCallout> : null}

      <div>
        <SectionHeader label={t.todayForYou} meta={data === null && !error ? "…" : t.todayMeta(Math.min(briefings.length, 3), total)} rule={false} />
        {data !== null && briefings.length === 0 && !error ? (
          <div className="hc-fleet-empty mt-3">
            <span className="hc-fleet-empty-title">{t.emptyTitle}</span>
            <span className="hc-fleet-empty-desc">{t.emptyDesc}</span>
          </div>
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

      <Disclosure summary={<span className="text-sm font-semibold text-[var(--hc-text)]">{t.provenanceTitle}</span>} defaultOpen={false}>
        <div className="pt-2">
          <VaultProvenanceShelf data={provenance.data} error={provenance.error} />
        </div>
      </Disclosure>

      <Disclosure summary={<span className="text-sm font-semibold text-[var(--hc-text)]">{t.topicsTitle}</span>} defaultOpen={false}>
        <div className="pt-2 text-sm text-[var(--hc-text-soft)]">
          <p>Beobachtete Themen erscheinen hier, sobald du Themen im Lesesaal folgst.</p>
        </div>
      </Disclosure>
    </div>
  );
}
