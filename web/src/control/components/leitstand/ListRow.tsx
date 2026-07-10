import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * ListRow — the ONE canonical result/list row of the Leitstand: a leading badge
 * cluster (status pill, role chip …), a two-line-clamp title, a quiet mono meta
 * line, an optional trailing control, and an optional expanded detail body.
 * Generalised from FleetResultCard's compact face so S2–S4 lists share one row
 * idiom instead of re-deriving the card chrome (DESIGN.md rule 9: card ⇄ drawer).
 */
export function ListRow({
  leading,
  title,
  meta,
  trailing,
  children,
  onClick,
  className,
}: {
  leading?: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  /** Right-aligned control on the badge row (e.g. a Details toggle). */
  trailing?: ReactNode;
  /** Expanded detail body, rendered below the meta line. */
  children?: ReactNode;
  onClick?: () => void;
  className?: string;
}) {
  return (
    <article
      className={cn("hc-surface-card p-3.5", onClick && "cursor-pointer", className)}
      onClick={onClick}
    >
      {leading != null || trailing != null ? (
        <div className="flex flex-wrap items-center gap-2">
          {leading}
          {trailing != null ? <div className="ml-auto">{trailing}</div> : null}
        </div>
      ) : null}
      <h3 className="mt-2.5 line-clamp-2 text-sec font-semibold leading-snug text-white">{title}</h3>
      {meta != null ? <p className="mt-1.5 font-data tabular-nums text-micro hc-dim">{meta}</p> : null}
      {children}
    </article>
  );
}
