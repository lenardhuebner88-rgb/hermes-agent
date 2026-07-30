import { useEffect, useState } from "react";

/** Viewport-Schwelle des TwoPane-Splits: 840px = dokumentierte
 *  Medium/Expanded-Grenze (DESIGN.md UX contract). Muss mit TwoPane.css
 *  (@media min-width: 840px) synchron bleiben. */
const EXPANDED_QUERY = "(min-width: 840px)";

/**
 * Shared ≥840-Fork für TwoPane-Aufrufer (Drawer <840 vs Pane ≥840).
 * Eine Quelle statt per-View-Kopien — muss mit TwoPane.css (min-width: 840px)
 * synchron bleiben.
 */
export function useTwoPaneExpanded(): boolean {
  const [matches, setMatches] = useState(() => (
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(EXPANDED_QUERY).matches
      : false
  ));

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(EXPANDED_QUERY);
    const onChange = () => setMatches(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  return matches;
}
