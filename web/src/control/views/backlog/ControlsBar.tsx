import { cn } from "@/lib/utils";
import { de } from "../../i18n/de";
import { Card, Eyebrow } from "../../components/primitives";
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
      <label htmlFor="fo-backlog-search">
        <Eyebrow>{de.backlog.searchLabel}</Eyebrow>
      </label>
      <input
        id="fo-backlog-search"
        type="search"
        aria-label={de.backlog.searchLabel}
        value={q}
        onChange={(e) => onQ(e.target.value)}
        placeholder={de.backlog.searchPlaceholder}
        className="mt-2 h-12 w-full rounded-card border border-line bg-surface-1 px-3 text-body text-ink placeholder:text-ink-3 focus:border-live focus:outline-none focus:ring-2 focus:ring-live/30"
      />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {(["", "high", "medium", "low"] as const).map((risk) => (
          <button key={risk || "all-risk"} type="button" onClick={() => onFilterRisk(risk)} className={cn("inline-flex min-h-12 items-center rounded-card border px-3 text-sec font-medium transition", filterRisk === risk ? "border-live bg-live/10 text-bronze-hi" : "border-line text-ink-2 hover:bg-surface-3 hover:text-ink")}>
            {risk || de.backlog.filterAll}
          </button>
        ))}
        <button type="button" onClick={() => onFilterStale(!filterStale)} aria-pressed={filterStale} className={cn("inline-flex min-h-12 items-center rounded-card border px-3 text-sec font-medium transition", filterStale ? "border-live bg-live/10 text-bronze-hi" : "border-line text-ink-2 hover:bg-surface-3 hover:text-ink")}>
          {de.backlog.filterStale}
        </button>
        {owners.length > 1 ? (
          <select aria-label="Nach Owner filtern" value={filterOwner} onChange={(e) => onFilterOwner(e.target.value)} className="h-12 rounded-card border border-line bg-surface-1 px-3 text-sec text-ink-2 focus:border-live focus:outline-none focus:ring-2 focus:ring-live/30">
            <option value="">{de.backlog.filterAll} Owner</option>
            {owners.map((owner) => <option key={owner} value={owner}>{owner}</option>)}
          </select>
        ) : null}
        <div className="ml-auto flex items-center gap-1.5">
          <Eyebrow>{de.backlog.sortLabel}</Eyebrow>
          {(["risk", "age", "status"] as FoSortKey[]).map((key) => (
            <button key={key} type="button" onClick={() => onSort(key)} className={cn("inline-flex min-h-12 items-center rounded-card border px-3 text-sec transition", sortKey === key ? "border-live bg-live/10 text-bronze-hi" : "border-line text-ink-2 hover:bg-surface-3 hover:text-ink")}>
              {key === "risk" ? de.backlog.sortRisk : key === "age" ? de.backlog.sortAge : de.backlog.sortStatus}
            </button>
          ))}
        </div>
      </div>
    </Card>
  );
}
