/**
 * Pulse sparkline — a small inline bar-sparkline for a session lane / fleet
 * card. There is NO real per-session activity history available from the
 * backend (the overview endpoint only ever gives us a single `state` +
 * `activity` timestamp, not a time series), so this is a HEURISTIC v1: bar
 * heights are seeded deterministically from the fleet state (not
 * Math.random() — that would break SSR/test determinism) so a `laeuft`
 * session reads as lively, `idle`/`dead` reads as flat/quiet. It is NOT real
 * telemetry.
 */
import { cn } from "@/lib/utils";
import type { AgentTerminalOverviewState } from "@/lib/api";

// Deterministic bar-height sets per state (px, 7 bars) — the shape roughly
// mirrors the mockup's hand-authored heartbeat pulse.
const SPARK_BARS: Record<AgentTerminalOverviewState, number[]> = {
  laeuft: [9, 14, 6, 12, 16, 8, 13],
  frage: [5, 7, 4, 6, 5, 4, 6],
  wartet: [6, 6, 6, 6, 6, 6, 6],
  idle: [4, 5, 4, 6, 4, 5, 4],
  dead: [3, 3, 3, 3, 3, 3, 3],
};

export function Sparkline({ state, className }: { state: AgentTerminalOverviewState; className?: string }) {
  const bars = SPARK_BARS[state] ?? SPARK_BARS.idle;
  const live = state === "laeuft";
  return (
    <div className={cn("flex h-4 items-end gap-[2px]", className)} aria-hidden="true" data-fleet-state={state}>
      {bars.map((height, index) => (
        <span
          key={index}
          style={{ height: `${height}px` }}
          className={cn(
            "w-[3px] rounded-[1px]",
            live ? "bg-live motion-safe:animate-pulse" : "bg-ink-3/60",
          )}
        />
      ))}
    </div>
  );
}
