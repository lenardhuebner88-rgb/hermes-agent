/**
 * useOfflineBannerHeight — M3: die Höhe des OfflineStaleBanner in die
 * Stage-Höhe der Jarvis-Zone einrechnen.
 *
 * Der Banner (sticky, im normalen Fluss über der ControlShell) schiebt die
 * Zone nach unten; die Desktop-Stage rechnet aber mit `100dvh − Masthead`,
 * sodass die fixierte Frag-Leiste bei sichtbarem Banner um die Bannerhöhe
 * unten clippte. Der Hook misst das Banner ([data-offline-banner], direktes
 * Kind von [data-control]) und spiegelt seine Höhe als `--jv-banner-h` auf
 * das `.jv`-Wurzelelement; jarvis.css zieht die Variable in allen Höhen-
 * Kalkulationen ab. Banner weg → 0px (kein Layout-Unterschied zu vorher).
 *
 * Erscheinen/Verschwinden des Banners ist ein Conditional Render direkt
 * unter [data-control] → childList-Mutation dort genügt (kein Subtree);
 * Umbruch-Höhenänderungen fängt der ResizeObserver. Beide Observer fehlen in
 * jsdom → der Hook degradiert dort still (CSS-Default 0px).
 */
import { useEffect, type RefObject } from "react";

export function useOfflineBannerHeight(ref: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const host = ref.current;
    if (!host) return;
    if (typeof MutationObserver === "undefined" || typeof ResizeObserver === "undefined") {
      return;
    }
    const container = document.querySelector("[data-control]") ?? document.body;
    let resizeObserver: ResizeObserver | null = null;

    const apply = () => {
      const banner = document.querySelector("[data-offline-banner]");
      if (banner instanceof HTMLElement) {
        const setHeight = () =>
          host.style.setProperty(
            "--jv-banner-h",
            `${banner.getBoundingClientRect().height}px`,
          );
        setHeight();
        if (!resizeObserver) {
          resizeObserver = new ResizeObserver(setHeight);
          resizeObserver.observe(banner);
        }
      } else {
        host.style.setProperty("--jv-banner-h", "0px");
        resizeObserver?.disconnect();
        resizeObserver = null;
      }
    };

    apply();
    const observer = new MutationObserver(apply);
    observer.observe(container, { childList: true });
    return () => {
      observer.disconnect();
      resizeObserver?.disconnect();
      host.style.removeProperty("--jv-banner-h");
    };
  }, [ref]);
}
