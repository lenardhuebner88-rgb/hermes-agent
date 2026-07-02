/**
 * S1: Ketten-Liste statt 236er-Dropdown. Replaces the flat native `<select>`
 * (ChainSelector, retired) with a searchable/filterable grouped list —
 * "Läuft jetzt" → "Wartet/Gehalten" → "Fertig" (fixed order, see chainList.ts)
 * — so "welche Kette läuft gerade?" is answerable at a glance instead of
 * scrolling a 236-entry, mostly-"Fertig" dropdown.
 *
 * All grouping/sort/filter/search maths lives in ../../lib/chainList.ts
 * (pure, colocated-tested) — this file is presentation only.
 */
import { useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import { de } from "../../i18n/de";
import { Eyebrow } from "../../components/primitives";
import { Led, StatusPill } from "../../components/atoms";
import type { DotKind } from "../../lib/tones";
import { taskStatusLabel } from "../../lib/tones";
import {
  buildChainListEntries,
  chainListCounts,
  filterChainListEntries,
  paginateChainListEntries,
  CHAIN_LIST_GROUP_ORDER,
  CHAIN_LIST_DONE_PAGE_SIZE,
  type ChainListEntry,
  type ChainListFilter,
  type ChainListGroup,
} from "../../lib/chainList";
import type { ChainModel } from "../../lib/fleet";
import type { BoardTask, ToneName } from "../../lib/types";

const GROUP_LABEL: Record<ChainListGroup, string> = {
  running: de.ketten.listGroupRunning,
  waiting: de.ketten.listGroupWaiting,
  done: de.ketten.listGroupDone,
};

/** Status pill for a chain row — mirrors FlowView's TaskCard pill precedence
 *  (blocked > review > running > done > plain status). */
function chainStatusPill(chain: ChainModel<BoardTask>, group: ChainListGroup): { tone: ToneName; label: string; dot: DotKind } {
  if (chain.blockedCount > 0) return { tone: "red", label: `${chain.blockedCount} blockiert`, dot: "error" };
  if (chain.reviewCount > 0) return { tone: "amber", label: `${chain.reviewCount} in Prüfung`, dot: "warn" };
  if (group === "running") return { tone: "cyan", label: `${chain.runningCount} läuft`, dot: "live" };
  if (group === "done") return { tone: "emerald", label: de.ketten.listGroupDone, dot: "ready" };
  const status = chain.root?.status ?? "todo";
  return { tone: "sky", label: taskStatusLabel[status] ?? status, dot: "idle" };
}

export interface ChainListPanelProps {
  chains: ChainModel<BoardTask>[];
  doneChains: ChainModel<BoardTask>[];
  selectedRootId: string | null;
  onSelect: (rootId: string) => void;
  disabled?: boolean;
}

export function ChainListPanel({ chains, doneChains, selectedRootId, onSelect, disabled }: ChainListPanelProps) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<ChainListFilter>("all");
  const [doneVisible, setDoneVisible] = useState(CHAIN_LIST_DONE_PAGE_SIZE);

  const entries = useMemo(() => buildChainListEntries({ active: chains, done: doneChains }), [chains, doneChains]);
  const counts = useMemo(() => chainListCounts(entries), [entries]);
  const filtered = useMemo(() => filterChainListEntries(entries, { filter, search }), [entries, filter, search]);

  const byGroup = useMemo(() => {
    const map: Record<ChainListGroup, ChainListEntry<BoardTask>[]> = { running: [], waiting: [], done: [] };
    for (const entry of filtered) map[entry.group].push(entry);
    return map;
  }, [filtered]);

  const donePage = useMemo(() => paginateChainListEntries(byGroup.done, doneVisible), [byGroup.done, doneVisible]);

  function handleFilter(next: ChainListFilter) {
    setFilter(next);
    setDoneVisible(CHAIN_LIST_DONE_PAGE_SIZE);
  }
  function handleSearch(next: string) {
    setSearch(next);
    setDoneVisible(CHAIN_LIST_DONE_PAGE_SIZE);
  }

  if (entries.length === 0) {
    return <p className="hc-type-label hc-dim">{de.ketten.noChains}</p>;
  }

  const filterOptions: { key: ChainListFilter; label: string; count: number }[] = [
    { key: "all", label: de.ketten.listFilterAll, count: entries.length },
    { key: "running", label: de.ketten.listGroupRunning, count: counts.running },
    { key: "waiting", label: de.ketten.listGroupWaiting, count: counts.waiting },
    { key: "done", label: de.ketten.listGroupDone, count: counts.done },
  ];

  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5 sm:gap-3", disabled && "pointer-events-none opacity-60")}>
      <label className="min-w-0" htmlFor="ketten-list-search">
        <span className="sr-only">{de.ketten.listSearchPlaceholder}</span>
        <input
          id="ketten-list-search"
          type="search"
          value={search}
          disabled={disabled}
          onChange={(e) => handleSearch(e.target.value)}
          placeholder={de.ketten.listSearchPlaceholder}
          className="min-h-8 w-full rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-3 text-sm outline-none placeholder:text-[var(--hc-text-dim)] focus:border-[var(--hc-accent-border)] sm:min-h-10"
        />
      </label>

      {/* Mobil eine Zeile mit horizontalem Scroll statt Umbruch — der Chip-Wrap
          fraß nach dem min-w-0-Fix die S5b-Dichtegewinne wieder auf (ui-verifier
          2026-07-02: erste Karte 330px statt <300px). Ab sm wie gehabt wrappen. */}
      <div className="flex items-center gap-1 overflow-x-auto pb-0.5 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:flex-wrap sm:overflow-x-visible sm:gap-1.5">
        {filterOptions.map((opt) => (
          <button
            key={opt.key}
            type="button"
            disabled={disabled}
            aria-pressed={filter === opt.key}
            onClick={() => handleFilter(opt.key)}
            className={cn(
              "inline-flex min-h-7 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2 text-xs transition sm:min-h-8 sm:px-2.5",
              filter === opt.key
                ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]"
                : "border-[var(--hc-border)] hc-soft hover:border-[var(--hc-border-strong)]",
            )}
          >
            {opt.label}
            <span className="hc-mono hc-type-label opacity-70">{opt.count}</span>
          </button>
        ))}
      </div>

      {filtered.length === 0 ? (
        <p className="hc-type-label hc-dim">{de.ketten.listEmptySearch}</p>
      ) : (
        <div className="flex min-w-0 flex-col gap-3 sm:gap-4">
          {CHAIN_LIST_GROUP_ORDER.filter((group) => byGroup[group].length > 0).map((group) => {
            const groupEntries = group === "done" ? donePage.visible : byGroup[group];
            return (
              <section key={group} className="min-w-0">
                <div className="flex items-center gap-2">
                  <Eyebrow>{GROUP_LABEL[group]}</Eyebrow>
                  <span className="hc-mono rounded-full border border-[var(--hc-border)] px-1.5 hc-type-label hc-soft">{byGroup[group].length}</span>
                </div>
                <div className="mt-1 flex min-w-0 flex-col gap-1.5 sm:mt-2">
                  {groupEntries.map((entry) => (
                    <ChainRow
                      key={entry.chain.rootId}
                      entry={entry}
                      selected={entry.chain.rootId === selectedRootId}
                      disabled={disabled}
                      onSelect={() => onSelect(entry.chain.rootId)}
                    />
                  ))}
                </div>
                {group === "done" && donePage.hasMore ? (
                  <button
                    type="button"
                    disabled={disabled}
                    onClick={() => setDoneVisible((v) => v + CHAIN_LIST_DONE_PAGE_SIZE)}
                    className="hc-mono mt-2 inline-flex min-h-8 items-center rounded-full border border-[var(--hc-border)] px-2.5 text-xs hc-soft transition hover:border-[var(--hc-border-strong)]"
                  >
                    {de.ketten.listLoadMore(Math.min(CHAIN_LIST_DONE_PAGE_SIZE, donePage.remaining))}
                  </button>
                ) : null}
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ChainRow({ entry, selected, disabled, onSelect }: {
  entry: ChainListEntry<BoardTask>;
  selected: boolean;
  disabled?: boolean;
  onSelect: () => void;
}) {
  const { chain, title, group } = entry;
  const pill = chainStatusPill(chain, group);
  return (
    <article
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-pressed={selected}
      aria-disabled={disabled}
      onClick={disabled ? undefined : onSelect}
      onKeyDown={(e) => {
        if (disabled) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      className={cn(
        "flex min-w-0 max-w-full cursor-pointer items-center gap-2 overflow-hidden rounded-[12px] border px-2.5 py-2 transition sm:gap-3 sm:px-3 sm:py-2.5",
        selected
          ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]"
          : "border-[var(--hc-border)] bg-[var(--hc-panel-card)] hover:border-[var(--hc-border-strong)]",
      )}
    >
      {group === "running" ? <Led kind="live" /> : null}
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-[var(--hc-text)]">{title}</p>
        <p className="hc-mono mt-0.5 truncate text-[11px] text-[var(--hc-text-soft)]">
          {chain.total} {chain.total === 1 ? "Task" : "Tasks"} · {chain.doneCount} fertig
        </p>
      </div>
      <span className="shrink-0">
        <StatusPill tone={pill.tone} label={pill.label} dot={pill.dot} />
      </span>
    </article>
  );
}
