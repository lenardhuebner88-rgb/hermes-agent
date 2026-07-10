import { useEffect, useState } from "react";

/**
 * Shared ≥1024-Fork für TwoPane-Aufrufer (Drawer <1024 vs Pane ≥1024).
 * Eine Quelle statt per-View-Kopien — muss mit TwoPane.css (min-width: 1024px)
 * synchron bleiben.
 */
export function useTwoPaneExpanded(): boolean {
  const [matches, setMatches] = useState(() => (
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(min-width: 1024px)").matches
      : false
  ));

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(min-width: 1024px)");
    const onChange = () => setMatches(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  return matches;
}
