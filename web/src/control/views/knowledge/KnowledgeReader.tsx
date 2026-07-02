import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, FileText, ListTree } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { ToneCallout } from "../../components/atoms";
import { ProseMarkdown } from "../../components/ProseMarkdown";
import { fmtClock } from "../../lib/derive";
import { extractToc, type TocEntry } from "../../lib/slug";
import type { KnowledgeDoc, KnowledgeDocDetail } from "./knowledge.helpers";
import { knowledgeType, knowledgeTypeLabel, resolveWikiLinks, sectionsLabel } from "./knowledge.helpers";

/** llm-wiki-Docs sind an ihrer Id erkennbar (S2: Wikilinks nur hier auflösen). */
function isLlmWikiDoc(id: string): boolean {
  return id.startsWith("kb::llm::");
}

const t = {
  back: "Alle Regale",
  toc: "Inhalt",
  noToc: "Keine Abschnitte",
  source: "Quelle",
  updated: "Stand",
  loadError: "Dokument konnte nicht geladen werden.",
  loading: "Lade …",
};

/** Sticky-Inhaltsverzeichnis. Klick scrollt zur Überschrift (id = Slug, von
 *  ProseMarkdown vergeben). Exportiert für Unit-Tests. */
export function TocNav({ entries, onJump }: { entries: TocEntry[]; onJump: (slug: string) => void }) {
  if (entries.length === 0) {
    return <p className="text-[0.78rem] hc-dim">{t.noToc}</p>;
  }
  return (
    <nav aria-label={t.toc} className="space-y-0.5">
      {entries.map((e, i) => (
        <button
          key={`${e.slug}-${i}`}
          type="button"
          onClick={() => onJump(e.slug)}
          style={{ paddingLeft: `${(e.level - 1) * 0.75 + 0.5}rem` }}
          className={`block w-full truncate rounded py-1 pr-2 text-left text-[0.78rem] hover:bg-white/5 ${
            e.level === 1 ? "font-medium text-white" : "hc-soft"
          }`}
        >
          {e.text}
        </button>
      ))}
    </nav>
  );
}

export function KnowledgeReader({ doc, collectionTitle, onBack }: {
  doc: KnowledgeDoc;
  collectionTitle: string;
  onBack: () => void;
}) {
  // Offene Doc-Id: startet bei der Eltern-Auswahl (`doc.id`), kann sich aber
  // durch einen internen Wikilink-Klick lösen (llm-wiki, S2). Wählt das
  // Regal (Eltern) einen anderen Doc, fällt sie per Render-Phase-Reset wieder
  // auf `doc.id` zurück — wie das bestehende `detailFor`-Muster unten.
  const [openId, setOpenId] = useState(doc.id);
  const [openIdSourceFor, setOpenIdSourceFor] = useState(doc.id);
  if (openIdSourceFor !== doc.id) {
    setOpenIdSourceFor(doc.id);
    setOpenId(doc.id);
  }

  const [detail, setDetail] = useState<KnowledgeDocDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Reset beim Doc-Wechsel als Render-Phase-Anpassung (React-Doku-Muster),
  // wie ReadingView im Lesesaal.
  const [detailFor, setDetailFor] = useState<string>(openId);
  if (detailFor !== openId) {
    setDetailFor(openId);
    setDetail(null);
    setError(null);
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const d = await fetchJSON<KnowledgeDocDetail>(`/api/library/knowledge/doc?id=${encodeURIComponent(openId)}`);
        if (!cancelled) {
          setError(null);
          setDetail(d);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [openId]);

  // Header-Metadaten: sobald `detail` (für die offene Id) geladen ist, gilt
  // es als Quelle der Wahrheit (auch nach einem internen Sprung); bis dahin
  // nur beim initial übergebenen Doc aus der Eltern-Karte. Nach einem Sprung
  // ohne Karten-Metadaten zeigt der Header bewusst "Lade …" statt der
  // Metadaten des vorigen Docs.
  const headerDoc: KnowledgeDoc | null =
    detail && detail.id === openId ? detail : doc.id === openId ? doc : null;

  const isLlmWiki = isLlmWikiDoc(openId);
  const toc = useMemo(() => (detail ? extractToc(detail.body_md) : []), [detail]);
  const renderedBody = useMemo(
    () => (detail ? (isLlmWiki ? resolveWikiLinks(detail.body_md) : detail.body_md) : ""),
    [detail, isLlmWiki],
  );

  const jump = (slug: string) => {
    const el = typeof document !== "undefined" ? document.getElementById(slug) : null;
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="space-y-3">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-white/10 px-2.5 py-1 text-[0.78rem] hc-soft hover:bg-white/5"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        {t.back}
      </button>

      <header className="hc-surface-card p-4">
        <p className="hc-eyebrow">{collectionTitle}</p>
        <h2 className="mt-1 text-lg font-semibold text-white">{headerDoc?.title ?? (error ? t.loadError : t.loading)}</h2>
        {headerDoc ? (
          <>
            <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.72rem] hc-dim">
              <span>{knowledgeTypeLabel(knowledgeType(headerDoc))}</span>
              <span className="inline-flex min-w-0 items-center gap-1.5">
                <FileText className="h-3.5 w-3.5" />
                <span className="hc-mono break-all">{headerDoc.source_ref}</span>
              </span>
              {headerDoc.heading_count > 0 ? <span>{sectionsLabel(headerDoc.heading_count)}</span> : null}
              {headerDoc.updated_ts > 0 ? <span>{t.updated} {fmtClock(headerDoc.updated_ts)}</span> : null}
            </p>
            {headerDoc.tags.length > 0 ? (
              <div className="mt-3 flex flex-wrap gap-1">
                {headerDoc.tags.slice(0, 10).map((tag) => (
                  <span key={tag} className="rounded-full border border-white/10 px-1.5 py-0.5 text-[0.62rem] hc-dim">{tag}</span>
                ))}
              </div>
            ) : null}
          </>
        ) : null}
      </header>

      {error ? <ToneCallout tone="red">{t.loadError}<br />{error}</ToneCallout> : null}

      {detail ? (
        <div className="grid gap-4 xl:grid-cols-[16rem_minmax(0,1fr)]">
          <aside className="hidden xl:block">
            <div className="hc-surface-card sticky top-4 p-3">
              <p className="mb-2 inline-flex items-center gap-1.5 hc-eyebrow">
                <ListTree className="h-3.5 w-3.5" />
                {t.toc}
              </p>
              <TocNav entries={toc} onJump={jump} />
            </div>
          </aside>
          <article className="hc-surface-card min-w-0 p-4 sm:p-5">
            <ProseMarkdown slugHeadings wrapTables onInternalLink={isLlmWiki ? setOpenId : undefined}>
              {renderedBody}
            </ProseMarkdown>
          </article>
        </div>
      ) : error ? null : (
        <p className="text-sm hc-dim">{t.loading}</p>
      )}
    </div>
  );
}
