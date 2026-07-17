import { WifiOff } from "lucide-react";
import type { SystemHealthResponse } from "../lib/types";
import { useClientNowSeconds, useVisibleSinceSeconds } from "../lib/clock";
import { freshness } from "../lib/derive";
import { de } from "../i18n/de";

/** Seconds the tab must stay visible before age-stale alone shows the banner. */
export const REFOCUS_GRACE_S = 12;

export function OfflineStaleBanner({ health }: {
  health: {
    data: SystemHealthResponse | null;
    error: string | null;
    isStale?: boolean;
    pollIntervalMs?: number;
    lastUpdated: number | null;
  };
}) {
  // Geteilte 10s-Uhr statt eigenem 1s-forceTick: Date.now() im Render ist
  // impure (react-hooks/purity), und der Banner braucht keine Sekunden-Auflösung.
  const clientNow = useClientNowSeconds();
  const visibleSince = useVisibleSinceSeconds();
  const ageFreshness = freshness(health.lastUpdated, health.pollIntervalMs ?? 5000, clientNow);
  const ageStale = ageFreshness.stale && health.lastUpdated != null;
  // Mobile app-switch freezes timers; on return age is already past threshold.
  // Suppress age-stale only until refocus grace elapses so the next poll can land.
  // Fetch errors and explicit isStale stay immediate (not grace-gated).
  const ageStaleVisible =
    ageStale &&
    visibleSince != null &&
    clientNow - visibleSince >= REFOCUS_GRACE_S;
  const visible = Boolean(health.error || health.isStale || ageStaleVisible);

  if (!visible) return null;
  const age = health.lastUpdated == null ? "noch nie" : `vor ${Math.max(0, clientNow - health.lastUpdated)}s`;
  const label = health.error ? de.staleBanner.fetchError : ageStale ? de.staleBanner.pausedOrStale : de.staleBanner.stale;

  return (
    <div className="sticky top-0 z-50 border-b border-amber-500/30 bg-amber-500/10 px-4 py-2 text-sm text-amber-100 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center gap-2">
        <WifiOff className="h-4 w-4 shrink-0" />
        <span className="font-medium">{label}</span>
        <span className="hc-soft">Zuletzt aktuell {age}.</span>
      </div>
    </div>
  );
}
