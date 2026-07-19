import { WifiOff } from "lucide-react";
import type { SystemHealthResponse } from "../lib/types";
import { useClientNowSeconds, useVisibleSinceSeconds } from "../lib/clock";
import { freshness } from "../lib/derive";
import { de } from "../i18n/de";
import { ATTEMPT_DEADLINE_MS, getAttemptState } from "../hooks/pollingStore";

/** Seconds the tab must stay visible before age-stale alone shows the banner. */
export const REFOCUS_GRACE_S = 12;

/**
 * Age-only staleness (no fetch error, no in-flight attempt) is a soft signal:
 * on mobile, Android throttles WebView timers even while the page looks
 * visible, so a 15s poll routinely drifts past the freshness threshold with
 * nothing actually broken — the store self-heals on the next tick/resume
 * (Operator-Entscheid 2026-07-17: dieser Fall soll nicht mehr alarmieren).
 * Only a genuinely dead poll — minutes without success — earns the banner.
 * Fetch errors and explicit isStale remain immediate below.
 */
export const AGE_ALARM_S = 300;

/** Poll key used by useSystemHealth — attempt state is read non-notifying. */
const HEALTH_POLL_KEY = "health-status";

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

  // Legal in-flight refresh: store is actively fetching and still inside the
  // attempt deadline. Suppress age-stale only for that window so mobile resume
  // (stagger + slow health GET) does not flash "pausiert oder veraltet".
  // Read on the banner's own clock tick — getAttemptState never notifies.
  const attempt = getAttemptState(HEALTH_POLL_KEY);
  const legalPendingRefresh =
    attempt.refreshing &&
    attempt.attemptStartedAt != null &&
    (clientNow - attempt.attemptStartedAt) * 1000 < ATTEMPT_DEADLINE_MS;

  // Mobile app-switch freezes timers; on return age is already past threshold.
  // Suppress age-stale only until refocus grace elapses so the next poll can land.
  // Fetch errors and explicit isStale stay immediate (not grace-gated).
  const ageStaleVisible =
    ageStale &&
    !legalPendingRefresh &&
    health.lastUpdated != null &&
    clientNow - health.lastUpdated >= AGE_ALARM_S &&
    visibleSince != null &&
    clientNow - visibleSince >= REFOCUS_GRACE_S;
  const visible = Boolean(health.error || health.isStale || ageStaleVisible);

  if (!visible) return null;
  const age = health.lastUpdated == null ? "noch nie" : `vor ${Math.max(0, clientNow - health.lastUpdated)}s`;
  const label = health.error ? de.staleBanner.fetchError : ageStale ? de.staleBanner.pausedOrStale : de.staleBanner.stale;

  return (
    <div
      className="sticky top-0 z-50 border-b border-amber-500/30 bg-amber-500/10 px-4 py-2 text-sm text-amber-100 backdrop-blur"
      data-offline-banner=""
    >
      <div className="mx-auto flex max-w-6xl items-center gap-2">
        <WifiOff className="h-4 w-4 shrink-0" />
        <span className="font-medium">{label}</span>
        <span className="hc-soft">Zuletzt aktuell {age}.</span>
      </div>
    </div>
  );
}
