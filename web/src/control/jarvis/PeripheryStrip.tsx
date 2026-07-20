/**
 * PeripheryStrip — S5-Design („JARVIS OS"): der Maschinenraum in einer
 * schlanken Zeile über dem Gespräch. Zeigt den deduplizierten Wächter-Digest
 * (Tages-Zähler ✓/👁/⚠ + letzter Stand mit Uhrzeit) — die Wächter-Karten
 * fluten den Chat nicht mehr, Abschlüsse bleiben trotzdem immer sichtbar.
 * Tap öffnet den bestehenden Aktivitaet-Drawer der Shell (volles Log, S3.10).
 * Leerzustand: ohne Watcher-Events rendert der Strip nichts.
 */
import type { KeyboardEvent } from "react";

import { de } from "../i18n/de";
import type { WatcherDigest, WatcherState } from "./watcherDigest";

const t = de.jarvis;

/** Kurz-Icons je Watcher-Zustand (kompakte Mono-/Emoji-Mischung des HUD). */
const STATE_ICON: Record<WatcherState, string> = {
  completed: "✓",
  attention: "👁",
  blocked: "⚠",
  gave_up: "✗",
  receipt: "▣",
  session: "⏏",
  info: "•",
};

/** Titel-Kurzform der Peripherie (~60 Zeichen, hart gekappt mit Ellipsis). */
function shortTitle(title: string): string {
  return title.length > 60 ? `${title.slice(0, 59)}…` : title;
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function PeripheryStrip({
  digest,
  inboxCount = 0,
  onOpenLog,
}: {
  digest: WatcherDigest;
  inboxCount?: number;
  /** Tap → Aktivitaet-Drawer der Shell (volles Log bleibt erreichbar). */
  onOpenLog: () => void;
}) {
  if (digest.latest.length === 0 && inboxCount <= 0) return null; // S6: Inbox-Badge bleibt erreichbar.
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpenLog();
    }
  };
  const last = digest.lastEvent;
  return (
    <div
      className="jv-periphery"
      role="button"
      tabIndex={0}
      aria-label={t.peripheryAria}
      onClick={onOpenLog}
      onKeyDown={onKeyDown}
    >
      <span className="jv-periph-counts">
        ✓ {digest.completedToday} · 👁 {digest.attentionOpen} · ⚠ {digest.blockedOpen}
        {inboxCount > 0 ? (
          <span className="jv-periph-inbox"> · {t.peripheryInbox(inboxCount)}</span>
        ) : null}
      </span>
      {last ? (
        <span className="jv-periph-last">
          {t.peripheryLast} {STATE_ICON[last.state]} {shortTitle(last.title)},{" "}
          {formatTime(last.ts)}
        </span>
      ) : null}
    </div>
  );
}
