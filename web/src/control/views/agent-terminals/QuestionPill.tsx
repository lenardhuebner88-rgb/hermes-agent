/**
 * Clickable chip: "N Fragen" with warn semantics. Hidden when count is 0.
 * Not bronze/live (status chip, not interaction accent — DESIGN.md).
 */
import { cn } from "@/lib/utils";

export function QuestionPill({
  count,
  onClick,
  className,
}: {
  count: number;
  onClick: () => void;
  className?: string;
}) {
  if (count <= 0) return null;
  const label = count === 1 ? "1 Frage" : `${count} Fragen`;
  return (
    <button
      type="button"
      data-testid="frage-pill"
      aria-label={label}
      title={label}
      onClick={onClick}
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 self-center rounded-full border border-status-warn/40 bg-status-warn/10 px-2.5 py-1 text-[11px] font-medium text-status-warn transition hover:bg-status-warn/15",
        className,
      )}
    >
      <span className="relative grid h-1.5 w-1.5 shrink-0 place-items-center" aria-hidden>
        <span className="absolute h-1.5 w-1.5 animate-ping rounded-full bg-status-warn/50" />
        <span className="h-1.5 w-1.5 rounded-full bg-status-warn" />
      </span>
      {label}
    </button>
  );
}
