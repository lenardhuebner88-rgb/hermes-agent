/**
 * ViewHeader — der EINE Seitenkopf für Daten-Routen des Leitstands: Eyebrow
 * (Archivo-Caps, micro, ink-3) über Titel (font-display, h1, ink) über einer
 * optionalen ruhigen Beschreibungszeile (sec, ink-2, mit Maß), plus einem
 * rechts ausgerichteten `actions`-Slot für Status-Chips oder die eine
 * Seiten-Aktion. Ein ruhiger Rhythmus (mt-2), keine Karte, kein Banner.
 *
 * Ersetzt die auseinandergelaufenen Eigenbau-Köpfe (hc-type-display-tight in
 * Scorecard/Statistik, hc-type-display in Hebel, Eyebrow+h2-Basteleien in
 * Diktat/Schmiede/Projekte, SectionHeader-als-Seitenkopf im DesignBoard).
 *
 * NICHT für:
 *  - Routen, deren Masthead-Label in der PulsLeiste bereits ausreicht (z. B.
 *    Crons — dort würde Titel = Masthead doppeln). Ein Eyebrow-Band genügt.
 *  - Die Jarvis-Zone (/control/projekte) — eigene Token-Quelle (A4).
 *  - Hero-Routen (Research/Strategist/Backlog-HeroPanel) — der Hero IST der
 *    Kopf; ein ViewHeader darüber wäre eine zweite Titelseite.
 *  - Skin-Routen, deren lokale Farbwelt nicht aus den Sheet-Tokens komponiert
 *    (Loops-Nachtschicht) — ein Token-Kopf sticht dort farblich heraus.
 */
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function ViewHeader({
  eyebrow,
  title,
  description,
  actions,
  className,
}: {
  /** Archivo-Caps-Zeile über dem Titel (z. B. "QUALITÄT · 7 TAGE"). */
  eyebrow: ReactNode;
  /** Der Seitentitel in der Display-Stimme (h1-Step). */
  title: ReactNode;
  /** Optional: eine ruhige Erklärzeile (Body-Minimum ink-2, max 65ch). */
  description?: ReactNode;
  /** Optional: rechte Seite — Status-Chip(s) oder die eine Seiten-Aktion. */
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header className={cn("flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between", className)}>
      <div className="min-w-0">
        <p className="font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3">{eyebrow}</p>
        <h1 className="mt-2 font-display text-h1 font-semibold tracking-tight text-ink">{title}</h1>
        {description != null ? <p className="mt-2 max-w-[65ch] text-sec text-ink-2">{description}</p> : null}
      </div>
      {actions != null ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}
