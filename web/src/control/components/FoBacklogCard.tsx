import { cn } from "@/lib/utils";
import { Eyebrow } from "./primitives";
import { SignalLabel, type SignalTone } from "./leitstand";
import { de } from "../i18n/de";
import type { BacklogItem } from "../lib/schemas";

const RISK_TONE: Record<string, SignalTone> = { low: "neutral", medium: "warn", high: "alert" };

function relLabel(updated: string, nowSec: number): string {
  if (!updated) return "—";
  const t = Date.parse(`${updated}T00:00:00Z`);
  if (Number.isNaN(t)) return updated;
  const days = Math.floor((nowSec * 1000 - t) / 86_400_000);
  if (days <= 0) return "heute";
  if (days === 1) return "gestern";
  if (days < 7) return `vor ${days} T`;
  if (days < 30) return `vor ${Math.floor(days / 7)} Wo`;
  return `vor ${Math.floor(days / 30)} Mon`;
}

interface FoBacklogCardProps {
  item: BacklogItem;
  nowSec: number;
  isNext: boolean;
  onOpen: (id: string) => void;
}

export function FoBacklogCard({ item, nowSec, isNext, onOpen }: FoBacklogCardProps) {
  return (
    <div
      role="button"
      tabIndex={0}
      className={cn(
        "min-h-12 cursor-pointer rounded-card border bg-surface-2 p-3 outline-none transition-colors",
        "hover:bg-surface-3 focus-visible:ring-2 focus-visible:ring-live/70",
        item.stale
          ? "border-status-alert/40"
          : isNext
            ? "border-live/40 bg-live/5 ring-1 ring-live/20"
            : "border-line",
      )}
      onClick={() => onOpen(item.id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen(item.id);
        }
      }}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            {isNext && (
              <Eyebrow className="shrink-0 text-live">{de.backlog.nextBadge}</Eyebrow>
            )}
            <p className="truncate text-sec font-medium leading-snug text-ink">{item.title}</p>
          </div>
          {item.excerpt && (
            <p className="mt-0.5 line-clamp-2 text-sec text-ink-2">{item.excerpt}</p>
          )}
        </div>
        <span className="shrink-0 font-data text-micro text-ink-3">{item.id}</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <span className="text-micro text-ink-2">{item.owner}</span>
        {item.area ? <span className="text-micro text-ink-2">· {item.area}</span> : null}
        <SignalLabel tone={RISK_TONE[item.risk] ?? "neutral"} label={item.risk} />
        {item.stale && <SignalLabel tone="alert" label={de.backlog.staleBadge} />}
        <span className={cn("ml-auto font-data text-micro tabular-nums", item.stale ? "text-status-alert" : "text-ink-2")}>
          {relLabel(item.updated, nowSec)}
        </span>
      </div>
    </div>
  );
}
