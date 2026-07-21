/**
 * JarvisShellView — die Jarvis-Zone auf /control/projekte.
 *
 * G2 (Produktreife E1): Desktop-Grid statt Floats — Graph-Zone links
 * (`minmax(0,1fr)`), rechte Chat-Säule 380px (Orb · Wartet · Thread ·
 * KiLageTicker · Composer). G6 mobil (≤759px): Graph-first + Chat als
 * Bottom-Sheet (closed | half | full). Alte Float-Panels (Brain/Filter/
 * KI-LAGE/Sparklines), HUD-Toggle und Strip-Band entfallen; der
 * Aktivitaet-/Sessions-Drawer bleibt als Overlay und öffnet weiter über
 * JARVIS_OPEN_AKTIVITAET_EVENT.
 * G3: statt des Interims-ProjektePanels sitzt der eingeklappte ProjekteChip
 * oben links unter der TopBar (Popover nur Alarme + „alle zeigen" → Klassik).
 *
 * Styles kommen ausschließlich aus ../jarvis.css (unter `.jv` gescopet,
 * lazy mit diesem Chunk geladen) — die einzige Route mit Ratchet-Ausnahme,
 * siehe DESIGN.md „Jarvis-Zone".
 */
import { useCallback, useEffect, useRef, useState } from "react";

import "../jarvis.css";
import { AktivitaetPanel } from "./AktivitaetPanel";
import { JARVIS_OPEN_AKTIVITAET_EVENT, JarvisChat } from "./JarvisChat";
import { JarvisGraph } from "./JarvisGraph";
import { JarvisTopBar } from "./JarvisTopBar";
import { KiLageTicker } from "./KiLageTicker";
import { ProjekteChip } from "./ProjekteChip";
import { SessionsPanel } from "./SessionsPanel";
import { SystemVitals } from "./SystemVitals";
import { useOfflineBannerHeight } from "./useOfflineBannerHeight";
import { WartetPanel } from "./WartetPanel";

/** Welcher Drawer offen ist (höchstens einer gleichzeitig).
 *  `?aktivitaet=open` / `?sessions=open` öffnen initial (Deep-Link). */
type ShellPanel = "aktivitaet" | "sessions";

/** G6 Mobile-Sheet: zu / halb / voll. Desktop rendert die Mechanik nicht. */
export type MobileSheetState = "closed" | "half" | "full";

const MOBILE_MQ = "(max-width: 759px)";

function initialOpenPanel(): ShellPanel | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  if (params.get("aktivitaet") === "open") return "aktivitaet";
  if (params.get("sessions") === "open") return "sessions";
  return null;
}

/** matchMedia-Guard: SSR-/jsdom-sicher, Default false (Desktop-Pfad). */
function useMobileJarvisViewport(): boolean {
  const [mobile, setMobile] = useState(false);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(MOBILE_MQ);
    const apply = () => setMobile(mq.matches);
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);
  return mobile;
}

export function JarvisShellView() {
  const rootRef = useRef<HTMLDivElement | null>(null);
  useOfflineBannerHeight(rootRef);
  const isMobile = useMobileJarvisViewport();
  const [sheet, setSheet] = useState<MobileSheetState>("closed");
  const [openPanel, setOpenPanel] = useState<ShellPanel | null>(initialOpenPanel);
  const togglePanel = (panel: ShellPanel) =>
    setOpenPanel((current) => (current === panel ? null : panel));

  // Periphery-Zeile im Chat öffnet den Aktivitaet-Drawer (Window-Event,
  // keine Prop-Bohrung durch die Shell).
  useEffect(() => {
    const onOpenAktivitaet = () => setOpenPanel("aktivitaet");
    window.addEventListener(JARVIS_OPEN_AKTIVITAET_EVENT, onOpenAktivitaet);
    return () => window.removeEventListener(JARVIS_OPEN_AKTIVITAET_EVENT, onOpenAktivitaet);
  }, []);

  // G6: Composer-Fokus öffnet das Sheet (closed → half) und hält es offen
  // (kein Zuklappen unter der Tastatur).
  useEffect(() => {
    if (!isMobile) return;
    const root = rootRef.current;
    if (!root) return;
    const onFocusIn = (event: FocusEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (!target.closest(".jv-column")) return;
      // Input/Textarea im Sheet (Composer) oder sonstige Editables.
      if (
        target.matches("input, textarea, [contenteditable='true']") ||
        target.closest("input, textarea")
      ) {
        setSheet((current) => (current === "closed" ? "half" : current));
      }
    };
    root.addEventListener("focusin", onFocusIn);
    return () => root.removeEventListener("focusin", onFocusIn);
  }, [isMobile]);

  // G6: visualViewport → --jv-kb (Tastatur-Offset), S7-Verhalten nicht
  // verschlechtern: Composer bleibt über der Tastatur.
  useEffect(() => {
    if (!isMobile) return;
    const root = rootRef.current;
    const vv = window.visualViewport;
    if (!root || !vv) return;
    const sync = () => {
      const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      root.style.setProperty("--jv-kb", `${Math.round(kb)}px`);
    };
    sync();
    vv.addEventListener("resize", sync);
    vv.addEventListener("scroll", sync);
    return () => {
      vv.removeEventListener("resize", sync);
      vv.removeEventListener("scroll", sync);
      root.style.removeProperty("--jv-kb");
    };
  }, [isMobile]);

  // Griff/Leiste: closed ↔ half; aus full zurück auf half (Brief: closed↔half).
  const onHandleClick = useCallback(() => {
    setSheet((current) => {
      if (current === "closed") return "half";
      if (current === "half") return "closed";
      return "half"; // full → half
    });
  }, []);

  // Expand-Steuer bzw. weiterer Tap-Pfad: half ↔ full.
  const onExpandClick = useCallback(() => {
    setSheet((current) => {
      if (current === "full") return "half";
      if (current === "half") return "full";
      return "half"; // closed → half, dann expand zu full
    });
  }, []);

  return (
    <div
      className="jv"
      ref={rootRef}
      data-mobile-sheet={isMobile ? sheet : undefined}
    >
      <JarvisTopBar />
      <div className="jv-stage">
        <div className="jv-graphzone">
          <JarvisGraph />
          {/* G3: Projekte-Chip oben links unter der TopBar (Popover nur Alarme). */}
          <div className="jv-chipzone">
            <ProjekteChip />
          </div>
          {/* G4: System-Vitals-Pille unten links (echte Microsparks). */}
          <SystemVitals />
        </div>

        <div
          className={isMobile ? "jv-column jv-chatsheet" : "jv-column"}
          data-sheet={isMobile ? sheet : undefined}
          id="jv-chat-sheet"
        >
          {isMobile ? (
            <div className="jv-sheet-chrome">
              <button
                type="button"
                className="jv-sheet-handle"
                onClick={onHandleClick}
                aria-expanded={sheet !== "closed"}
                aria-controls="jv-chat-sheet"
                data-testid="jv-sheet-handle"
                aria-label={
                  sheet === "closed" ? "Chat öffnen" : "Chat einklappen"
                }
              >
                <span className="jv-sheet-grip" aria-hidden="true" />
                <span className="jv-sheet-peek">
                  <span className="jv-sheet-orbdot" aria-hidden="true" />
                  <span className="jv-sheet-peek-label">Chat · Jarvis</span>
                </span>
              </button>
              {sheet !== "closed" ? (
                <button
                  type="button"
                  className="jv-sheet-expand"
                  onClick={onExpandClick}
                  data-testid="jv-sheet-expand"
                  aria-label={
                    sheet === "full" ? "Chat verkleinern" : "Chat maximieren"
                  }
                >
                  {sheet === "full" ? "▾" : "▴"}
                </button>
              ) : null}
            </div>
          ) : null}

          <div className="jv-sheet-body">
            <JarvisChat
              aboveThread={
                <div className="jv-warte-slot">
                  <WartetPanel />
                </div>
              }
              belowThread={<KiLageTicker />}
            />
          </div>
        </div>

        {/* Drawer-Host: Strip-Band entfällt; Panel-Komponenten bleiben für den
            Overlay-Drawer (Periphery-Event / Deep-Link). Strips per CSS hidden.
            z-Index über dem Mobile-Sheet (G6). */}
        <div className="jv-drawers" aria-live="polite" data-testid="jv-drawers">
          <AktivitaetPanel
            open={openPanel === "aktivitaet"}
            onToggle={() => togglePanel("aktivitaet")}
          />
          <SessionsPanel
            open={openPanel === "sessions"}
            onToggle={() => togglePanel("sessions")}
          />
        </div>
      </div>
    </div>
  );
}
