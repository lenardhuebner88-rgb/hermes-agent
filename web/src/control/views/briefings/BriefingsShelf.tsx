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
import type { LibraryItem, LibraryListResponse } from "../BibliothekView";
import { filterBriefings, newestBriefing } from "../BibliothekView.helpers";
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
};

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
      const [briefRes, issuesRes] = await Promise.all([
        fetchJSON<LibraryListResponse>("/api/library/items?category=briefings&limit=20&offset=0"),
        fetchJSON<LibraryListResponse>("/api/library/items?limit=3&offset=0"),
      ]);
      setData(briefRes);
      setItems(briefRes.items ?? []);
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

  const briefings = useMemo(() => filterBriefings(items), [items]);
  const featured = useMemo(() => newestBriefing(briefings), [briefings]);
  const rest = useMemo(() => briefings.filter((b) => b.id !== featured?.id).slice(0, 2), [briefings, featured]);
  const total = data?.count ?? briefings.length;

  return (
    <div className="space-y-6">
      <Hero
        eyebrow={t.heroEyebrow}
        title={t.heroTitle}
        subtitle={t.heroSubtitle}
        tone="cyan"
        density={density}
        status={{ label: t.newBriefings(briefings.length), tone: "cyan", dot: "live" }}
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
            {featured ? <BriefingCard item={featured} featured onOpen={onOpenItem} /> : null}
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
