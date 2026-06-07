import { cn } from "@/lib/utils";
import { de } from "../../i18n/de";
import { Card } from "../../components/primitives";
import type { FoSortKey } from "../../lib/foBacklog";

export function ControlsBar({
  q,
  onQ,
  filterOwner,
  onFilterOwner,
  filterRisk,
  onFilterRisk,
  filterStale,
  onFilterStale,
  sortKey,
  onSort,
  owners,
}: {
  q: string;
  onQ: (v: string) => void;
  filterOwner: string;
  onFilterOwner: (v: string) => void;
  filterRisk: string;
  onFilterRisk: (v: string) => void;
  filterStale: boolean;
  onFilterStale: (v: boolean) => void;
  sortKey: FoSortKey;
  onSort: (v: FoSortKey) => void;
  owners: string[];
}) {
  return (
    <Card surface="card" className="p-3">
      <input
        type="search"
        value={q}
        onChange={(e) => onQ(e.target.value)}
        placeholder={de.backlog.searchPlaceholder}
        className="w-full rounded-md border border-white/10 bg-white/[.04] px-3 py-2 text-sm text-white placeholder:text-zinc-500 focus:border-cyan-400/50 focus:outline-none"
      />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {(["", "high", "medium", "low"] as const).map((risk) => (
          <button key={risk || "all-risk"} type="button" onClick={() => onFilterRisk(risk)} className={cn("rounded-md border px-2.5 py-1 text-xs font-medium transition", filterRisk === risk ? "border-cyan-400/50 bg-cyan-500/15 text-cyan-200" : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200")}>
            {risk || de.backlog.filterAll}
          </button>
        ))}
        <button type="button" onClick={() => onFilterStale(!filterStale)} className={cn("rounded-md border px-2.5 py-1 text-xs font-medium transition", filterStale ? "border-red-400/50 bg-red-500/15 text-red-200" : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200")}>
          {de.backlog.filterStale}
        </button>
        {owners.length > 1 ? (
          <select value={filterOwner} onChange={(e) => onFilterOwner(e.target.value)} className="rounded-md border border-white/10 bg-white/[.04] px-2.5 py-1 text-xs text-zinc-200 focus:outline-none">
            <option value="">{de.backlog.filterAll} Owner</option>
            {owners.map((owner) => <option key={owner} value={owner}>{owner}</option>)}
          </select>
        ) : null}
        <div className="ml-auto flex items-center gap-1.5">
          <span className="text-xs hc-dim">{de.backlog.sortLabel}:</span>
          {(["risk", "age", "status"] as FoSortKey[]).map((key) => (
            <button key={key} type="button" onClick={() => onSort(key)} className={cn("rounded-md border px-2 py-1 text-xs transition", sortKey === key ? "border-cyan-400/40 bg-cyan-500/10 text-cyan-200" : "border-white/10 text-zinc-400 hover:text-zinc-200")}>
              {key === "risk" ? de.backlog.sortRisk : key === "age" ? de.backlog.sortAge : de.backlog.sortStatus}
            </button>
          ))}
        </div>
      </div>
    </Card>
  );
}
