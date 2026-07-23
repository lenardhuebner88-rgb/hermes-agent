import { cn } from "@/lib/utils";

// ScoreMeter — a neutral data meter for the compass fit score. The shared
// atoms MeterBar's only non-status fill is bronze, but bronze is reserved for
// interactive/live elements and is explicitly forbidden as a meter fill
// (DESIGN.md rule 1 + Brief: "ink-2-Füllstand — NIE bronze"). So the compass
// draws its own meter: ink-2 fill on a canvas track (painted in lanes.css under
// `.lp .meter`), value carried as mono text beside it — data, not affordance.
export function ScoreMeter({ score, className }: { score: number; className?: string }) {
  const pct = Math.max(0, Math.min(100, Math.round(score)));
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <div className="meter min-w-0 flex-1" aria-hidden>
        <span style={{ width: `${pct}%` }} />
      </div>
      <span className="w-9 shrink-0 text-right font-data text-micro tabular-nums text-ink">{pct}</span>
    </div>
  );
}
