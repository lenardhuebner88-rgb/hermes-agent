import { WifiOff } from "lucide-react";
import type { SystemHealthResponse } from "../lib/types";
import { useClientNowSeconds } from "../lib/clock";
import { freshness } from "../lib/derive";
import { de } from "../i18n/de";

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
  const ageFreshness = freshness(health.lastUpdated, health.pollIntervalMs ?? 5000, clientNow);
  const ageStale = ageFreshness.stale && health.lastUpdated != null;
  const visible = Boolean(health.error || health.isStale || ageStale);

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
