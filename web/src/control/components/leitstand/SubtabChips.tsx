import { cn } from "@/lib/utils";

export interface SubtabItem {
  id: string;
  label: string;
  /** Optional superscript count (hidden when undefined / 0 by the caller). */
  count?: number;
  /** Optional warning dot after the label. */
  warn?: boolean;
}

/** Class skin for the chip strip. Defaults render a neutral, token-based
 *  Leitstand chip; a themed view (e.g. the dark Fleet scope) passes its own
 *  `fleet-chip` classes so the structure/behaviour is shared but the look is
 *  preserved with zero visual regress. */
export interface SubtabChipClasses {
  chip: string;
  chipActive: string;
  warnDot: string;
}

const DEFAULT_CLASSES: SubtabChipClasses = {
  chip: "inline-flex items-center gap-1 whitespace-nowrap rounded-card border border-line bg-surface-2 px-3 py-1.5 text-xs text-ink-2 transition hover:bg-surface-3",
  chipActive: "border-live bg-surface-3 text-ink",
  warnDot: "ml-0.5 inline-block h-1.5 w-1.5 rounded-full bg-status-warn",
};

/**
 * SubtabChips — the ONE canonical horizontal subtab / segment strip of the
 * Leitstand: a scrollable row of chips, each with an optional superscript count
 * and warning dot, one marked active. Extracted from the inlined `fleet-chip`
 * pattern in FleetView so S2–S4 stop re-inventing it.
 *
 * DESIGN.md rule 6 caveat: these chips ARE navigation controls (real buttons
 * with `aria-pressed`), not the status-only chips of rule 6 — the surrounding
 * view keeps the current state reachable by other means.
 */
export function SubtabChips<T extends string>({
  items,
  active,
  onSelect,
  ariaLabelPrefix = "Tab",
  warnSuffix = " — enthält Warnungen",
  warnDotLabel = "Warnung",
  className,
  classes = DEFAULT_CLASSES,
}: {
  items: ReadonlyArray<SubtabItem & { id: T }>;
  active: T;
  onSelect: (id: T) => void;
  ariaLabelPrefix?: string;
  warnSuffix?: string;
  warnDotLabel?: string;
  className?: string;
  classes?: SubtabChipClasses;
}) {
  return (
    <div className={cn("flex gap-1.5 overflow-x-auto scrollbar-none", className)}>
      {items.map((item) => {
        const on = active === item.id;
        return (
          <button
            key={item.id}
            type="button"
            className={cn(classes.chip, on && classes.chipActive)}
            onClick={() => onSelect(item.id)}
            aria-pressed={on}
            aria-label={`${ariaLabelPrefix} ${item.label}${item.warn ? warnSuffix : ""}`}
          >
            {item.label}
            {item.count != null ? <sup>{item.count}</sup> : null}
            {item.warn ? <span className={classes.warnDot} aria-label={warnDotLabel} /> : null}
          </button>
        );
      })}
    </div>
  );
}
