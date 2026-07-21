/**
 * KiLageTicker — 1-zeiliger KI-LAGE-Ticker in der rechten Chat-Säule (G2 + B2b).
 *
 * Primär: GET /api/pa/news?limit=5 (Frontier Desk/Flash). Bei 404/Netzfehler/leer
 * → bisheriger usePaFeed-Pfad; bei Feed-Fehler/leer → JARVIS_NEWS_ITEMS-Mock
 * (Degraded-Mode: Feature dunkel statt Crash). News-Items: Tag-Badge + klickbares
 * Akkordeon (summary + markdown-Anfang plain). Feed-Items mit href bleiben klickbar.
 */
import { useEffect, useMemo, useState } from "react";

import { api, type PaFeedItem, type PaNewsItem } from "@/lib/api";
import { fmtRelativeTime, nowSec } from "../lib/derive";
import { JARVIS_NEWS_ITEMS } from "./mockContent";
import { usePaFeed } from "./usePaFeed";

const MARKDOWN_PREVIEW_MAX = 600;

export interface KiLageTickerItem {
  id: string;
  title: string;
  /** Unix-Sekunden (Feed/News); relativ gerendert. */
  ts?: number;
  /** Statische Mock-Quelle, wenn kein ts. */
  source?: string;
  href?: string | null;
  /** News-Endpoint: Tag-Badge (z. B. „Frontier Desk"). */
  tag?: string;
  summary?: string;
  markdown?: string;
}

function newsToItems(items: PaNewsItem[]): KiLageTickerItem[] {
  return items.slice(0, 5).map((item, index) => ({
    id: `news-${index}-${item.ts}`,
    title: item.title,
    ts: item.ts,
    tag: item.tag,
    summary: item.summary,
    markdown: item.markdown,
  }));
}

function feedToItems(items: PaFeedItem[]): KiLageTickerItem[] {
  // usePaFeed-Page ist aufsteigend (id/ts); letzte 5, neueste zuerst.
  return items
    .slice(-5)
    .reverse()
    .map((item) => ({
      id: String(item.id),
      title: item.title,
      ts: item.ts,
      // ref ist oft kein HTTP-Link — nur echte URLs klickbar machen.
      href: item.ref && /^https?:\/\//i.test(item.ref) ? item.ref : null,
    }));
}

function mockFallbackItems(): KiLageTickerItem[] {
  return JARVIS_NEWS_ITEMS.slice(0, 5).map((item, index) => ({
    id: `mock-${index}`,
    title: item.text,
    source: item.source,
  }));
}

/** Plain-Text-Anfang des Markdown-Bodies (kein HTML). */
function markdownPreview(md: string, max = MARKDOWN_PREVIEW_MAX): string {
  const text = md.trim();
  if (text.length <= max) return text;
  return `${text.slice(0, max).trimEnd()}…`;
}

function isExpandableNews(item: KiLageTickerItem): boolean {
  return Boolean(item.tag || item.summary || item.markdown);
}

export function KiLageTicker() {
  const feed = usePaFeed();
  const [open, setOpen] = useState(false);
  const [newsItems, setNewsItems] = useState<KiLageTickerItem[] | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const now = nowSec();

  // Einmaliger News-Endpoint-Versuch beim Mount; 404/Netz/leer → Feed-Pfad.
  useEffect(() => {
    let cancelled = false;
    api
      .getPaNews(5, { skipStaleTokenReload: true })
      .then((data) => {
        if (cancelled) return;
        const raw = data?.items ?? [];
        if (raw.length > 0) setNewsItems(newsToItems(raw));
      })
      .catch(() => {
        /* 404 / Netzfehler → usePaFeed (newsItems bleibt null). */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const items = useMemo(() => {
    if (newsItems && newsItems.length > 0) return newsItems;
    const raw = feed.data?.items ?? [];
    if (raw.length > 0) return feedToItems(raw);
    // Fehler ODER leerer Feed → Mock-Fallback (Feature dunkel, kein Crash).
    return mockFallbackItems();
  }, [newsItems, feed.data]);

  const latest = items[0];
  const latestLabel = latest ? `KI-LAGE · ${latest.title}` : "KI-LAGE";

  const metaFor = (item: KiLageTickerItem): string => {
    if (item.ts != null) return fmtRelativeTime(item.ts, now);
    return item.source ?? "";
  };

  const toggleExpand = (id: string) => {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  return (
    <div className="jv-ticker" data-testid="jv-ticker">
      <button
        type="button"
        className="jv-ticker-toggle"
        aria-expanded={open}
        aria-controls="jv-ticker-panel"
        onClick={() => setOpen((current) => !current)}
      >
        <span className="jv-ticker-label">{latestLabel}</span>
        <span className="jv-ticker-chev" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open ? (
        <ul className="jv-ticker-panel" id="jv-ticker-panel" role="list">
          {items.map((item) => {
            const meta = metaFor(item);
            const isOpen = Boolean(expanded[item.id]);

            if (isExpandableNews(item)) {
              const detailId = `jv-ticker-detail-${item.id}`;
              return (
                <li key={item.id} className="jv-ticker-item jv-ticker-item--news">
                  <button
                    type="button"
                    className="jv-ticker-newsbtn"
                    aria-expanded={isOpen}
                    aria-controls={detailId}
                    onClick={() => toggleExpand(item.id)}
                  >
                    <span className="jv-ticker-head">
                      <span className="jv-ticker-title">{item.title}</span>
                      {item.tag ? (
                        <span className="jv-ticker-tag">{item.tag}</span>
                      ) : null}
                    </span>
                    {meta ? <span className="jv-ticker-meta">{meta}</span> : null}
                  </button>
                  {isOpen ? (
                    <div className="jv-ticker-detail" id={detailId}>
                      {item.summary ? (
                        <p className="jv-ticker-summary">{item.summary}</p>
                      ) : null}
                      {item.markdown ? (
                        <div className="jv-ticker-md">{markdownPreview(item.markdown)}</div>
                      ) : null}
                    </div>
                  ) : null}
                </li>
              );
            }

            const body = (
              <>
                <span className="jv-ticker-title">{item.title}</span>
                {meta ? <span className="jv-ticker-meta">{meta}</span> : null}
              </>
            );
            if (item.href) {
              return (
                <li key={item.id} className="jv-ticker-item">
                  <a className="jv-ticker-link" href={item.href} target="_blank" rel="noreferrer">
                    {body}
                  </a>
                </li>
              );
            }
            return (
              <li key={item.id} className="jv-ticker-item">
                {body}
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
