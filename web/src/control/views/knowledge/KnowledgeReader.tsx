import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, FileText, ListTree } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { ToneCallout } from "../../components/atoms";
import { ProseMarkdown } from "../../components/ProseMarkdown";
import { fmtClock } from "../../lib/derive";
import { extractToc, type TocEntry } from "../../lib/slug";
import type { KnowledgeDoc, KnowledgeDocDetail } from "./knowledge.helpers";
import { knowledgeType, knowledgeTypeLabel, sectionsLabel } from "./knowledge.helpers";

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
  const [detail, setDetail] = useState<KnowledgeDocDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const typeLabel = knowledgeTypeLabel(knowledgeType(doc));
  // Reset beim Doc-Wechsel als Render-Phase-Anpassung (React-Doku-Muster),
  // wie ReadingView im Lesesaal.
  const [detailFor, setDetailFor] = useState<string>(doc.id);
  if (detailFor !== doc.id) {
    setDetailFor(doc.id);
    setDetail(null);
    setError(null);
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const d = await fetchJSON<KnowledgeDocDetail>(`/api/library/knowledge/doc?id=${encodeURIComponent(doc.id)}`);
        if (!cancelled) {
          setError(null);
          setDetail(d);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [doc.id]);

  const toc = useMemo(() => (detail ? extractToc(detail.body_md) : []), [detail]);

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
        <h2 className="mt-1 text-lg font-semibold text-white">{doc.title}</h2>
        <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.72rem] hc-dim">
          <span>{typeLabel}</span>
          <span className="inline-flex min-w-0 items-center gap-1.5">
            <FileText className="h-3.5 w-3.5" />
            <span className="hc-mono break-all">{doc.source_ref}</span>
          </span>
          {doc.heading_count > 0 ? <span>{sectionsLabel(doc.heading_count)}</span> : null}
          {doc.updated_ts > 0 ? <span>{t.updated} {fmtClock(doc.updated_ts)}</span> : null}
        </p>
        {doc.tags.length > 0 ? (
          <div className="mt-3 flex flex-wrap gap-1">
            {doc.tags.slice(0, 10).map((tag) => (
              <span key={tag} className="rounded-full border border-white/10 px-1.5 py-0.5 text-[0.62rem] hc-dim">{tag}</span>
            ))}
          </div>
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
            <ProseMarkdown slugHeadings>{detail.body_md}</ProseMarkdown>
          </article>
        </div>
      ) : error ? null : (
        <p className="text-sm hc-dim">{t.loading}</p>
      )}
    </div>
  );
}
