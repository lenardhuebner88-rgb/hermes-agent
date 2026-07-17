/**
 * Clickable chip: "N Fragen" with warn semantics. Hidden when count is 0.
 * Not bronze/live (status chip, not interaction accent — DESIGN.md).
 * Store is authoritative (I3): sole question surface; optional live age label.
 */
import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";

function formatStandingAge(ts: string, nowMs: number): string {
  const ms = Date.parse(ts);
  if (!Number.isFinite(ms)) return "steht seit kurzem";
  const sec = Math.max(0, Math.round((nowMs - ms) / 1000));
  if (sec < 60) return `steht seit ${sec}s`;
  const min = Math.round(sec / 60);
  if (min < 60) return `steht seit ${min} min`;
  const h = Math.round(min / 60);
  return `steht seit ${h} h`;
}

export function QuestionPill({
  count,
  standingSinceTs,
  onClick,
  className,
}: {
  count: number;
  /** ISO ts of the oldest (or focused) open question for live age display. */
  standingSinceTs?: string | null;
  onClick: () => void;
  className?: string;
}) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!standingSinceTs) return;
    const id = window.setInterval(() => setNowMs(Date.now()), 15_000);
    return () => window.clearInterval(id);
  }, [standingSinceTs]);

  if (count <= 0) return null;
  const label = count === 1 ? "1 Frage" : `${count} Fragen`;
  const age =
    standingSinceTs && count > 0
      ? formatStandingAge(standingSinceTs, nowMs)
      : null;
  const title = age ? `${label} · ${age}` : label;
  return (
    <button
      type="button"
      data-testid="frage-pill"
      aria-label={title}
      title={title}
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
      <span>{label}</span>
      {age ? <span className="text-[10px] font-normal opacity-80">{age}</span> : null}
    </button>
  );
}
