/**
 * KiLageTicker — 1-zeiliger KI-LAGE-Ticker in der rechten Chat-Säule (G2).
 *
 * Zeigt „KI-LAGE · <neuester Titel>"; Tap/Klick klappt ein Akkordeon mit den
 * letzten 5 Einträgen auf. Daten vorerst aus usePaFeed (wie das frühere
 * Float-Panel); bei Fehler/leer Fallback auf JARVIS_NEWS_ITEMS (Degraded-Mode:
 * Feature dunkel statt Crash). Items mit href sind klickbar.
 */
import { useMemo, useState } from "react";

import type { PaFeedItem } from "@/lib/api";
import { fmtRelativeTime, nowSec } from "../lib/derive";
import { JARVIS_NEWS_ITEMS } from "./mockContent";
import { usePaFeed } from "./usePaFeed";

export interface KiLageTickerItem {
  id: string;
  title: string;
  /** Unix-Sekunden (Feed); relativ gerendert. */
  ts?: number;
  /** Statische Mock-Quelle, wenn kein ts. */
  source?: string;
  href?: string | null;
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

export function KiLageTicker() {
  const feed = usePaFeed();
  const [open, setOpen] = useState(false);
  const now = nowSec();

  const items = useMemo(() => {
    const raw = feed.data?.items ?? [];
    if (raw.length > 0) return feedToItems(raw);
    // Fehler ODER leerer Feed → Mock-Fallback (Feature dunkel, kein Crash).
    return mockFallbackItems();
  }, [feed.data]);

  const latest = items[0];
  const latestLabel = latest ? `KI-LAGE · ${latest.title}` : "KI-LAGE";

  const metaFor = (item: KiLageTickerItem): string => {
    if (item.ts != null) return fmtRelativeTime(item.ts, now);
    return item.source ?? "";
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
