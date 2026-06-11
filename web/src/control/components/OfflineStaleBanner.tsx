import { useEffect, useState } from "react";
import { WifiOff } from "lucide-react";
import type { SystemHealthResponse } from "../lib/types";

export function OfflineStaleBanner({ health }: {
  health: {
    data: SystemHealthResponse | null;
    error: string | null;
    isStale?: boolean;
    lastUpdated: number | null;
  };
}) {
  const [, forceTick] = useState(0);
  const visible = Boolean(health.error || health.isStale);

  useEffect(() => {
    if (!visible) return;
    const timer = window.setInterval(() => forceTick((tick) => tick + 1), 1000);
    return () => window.clearInterval(timer);
  }, [visible]);

  if (!visible) return null;
  const age = health.lastUpdated == null ? "noch nie" : `vor ${Math.max(0, Math.floor(Date.now() / 1000) - health.lastUpdated)}s`;
  const label = health.error ? "Health-Poll fehlgeschlagen" : "Health-Daten sind stale";

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
