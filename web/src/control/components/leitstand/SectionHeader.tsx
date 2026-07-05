import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Eyebrow } from "../primitives";

/**
 * SectionHeader — the ONE canonical section label of the Leitstand: an
 * uppercase-mono micro-eyebrow on the left, a quiet right-aligned meta value on
 * the right, sitting (by default) on a top hairline. DESIGN.md rule 5 + 7.
 *
 * Replaces the per-view "GroupLabel" pattern (SystemView.tsx) and the
 * FleetPanel header row — one idiom S2–S4 share instead of re-inventing.
 * `rule={false}` drops the top border for headers already inside a card.
 */
export function SectionHeader({ label, meta, rule = true, className }: {
  label: ReactNode;
  meta?: ReactNode;
  rule?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex items-baseline justify-between gap-3",
        rule && "border-t border-[var(--hc-border)] pt-4",
        className,
      )}
    >
      <Eyebrow>{label}</Eyebrow>
      {meta ? <span className="hc-type-label hc-dim truncate text-right">{meta}</span> : null}
    </div>
  );
}
