import { cn } from "@/lib/utils";
import { StatusPill } from "./atoms";
import { de } from "../i18n/de";
import type { BacklogItem } from "../lib/schemas";
import type { ToneName } from "../lib/types";

const OWNER_TONE: Record<string, ToneName> = {
  claude: "violet",
  hermes: "cyan",
  piet: "emerald",
  unassigned: "zinc",
};
const RISK_TONE: Record<string, ToneName> = { low: "zinc", medium: "amber", high: "red" };

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
        "cursor-pointer rounded-lg border p-3 outline-none transition-colors",
        "hover:bg-white/[.04] focus-visible:ring-2 focus-visible:ring-sky-400/70",
        item.stale
          ? "border-red-500/40 bg-red-500/10"
          : isNext
            ? "border-cyan-400/40 bg-cyan-500/5 ring-1 ring-cyan-400/20"
            : "border-white/10",
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
              <span className="shrink-0 rounded-full bg-cyan-500/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-cyan-300">
                {de.backlog.nextBadge}
              </span>
            )}
            <p className="truncate text-sm font-medium leading-snug text-white">{item.title}</p>
          </div>
          {item.excerpt && (
            <p className="mt-0.5 line-clamp-2 text-xs hc-dim">{item.excerpt}</p>
          )}
        </div>
        <span className="hc-mono shrink-0 text-[11px] hc-dim">{item.id}</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <StatusPill tone={OWNER_TONE[item.owner] ?? "zinc"} label={item.owner} />
        {item.area ? <StatusPill tone="cyan" label={item.area} /> : null}
        <StatusPill tone={RISK_TONE[item.risk] ?? "zinc"} label={item.risk} />
        {item.stale && <StatusPill tone="red" label={de.backlog.staleBadge} />}
        <span className={cn("ml-auto text-[11px]", item.stale ? "text-red-300" : "hc-soft")}>
          {relLabel(item.updated, nowSec)}
        </span>
      </div>
    </div>
  );
}
