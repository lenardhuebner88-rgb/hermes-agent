import { useEffect, useMemo, useState } from "react";
import { Check, ClipboardCopy } from "lucide-react";

import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import { useBacklog, useBacklogDetail } from "../hooks/useControlData";
import { BacklogDetailDrawer } from "../components/BacklogDetailDrawer";
import { FoBacklogCard } from "../components/FoBacklogCard";
import { StatusPill, ToneCallout } from "../components/atoms";
import {
  buildFoCommissionPrompt,
  computeNextFoTaskId,
  filterFoItems,
  sortFoItems,
} from "../lib/foBacklog";
import type { FoSortKey } from "../lib/foBacklog";
import type { Density } from "../hooks/useDensity";
import type { BacklogItem } from "../lib/schemas";
import type { ToneName } from "../lib/types";

type Status = BacklogItem["status"];

const ACTIVE_COLUMNS: Array<{ key: Exclude<Status, "done">; label: string; tone: ToneName }> = [
  { key: "now", label: de.backlog.colNow, tone: "sky" },
  { key: "next", label: de.backlog.colNext, tone: "indigo" },
  { key: "in_progress", label: de.backlog.colInProgress, tone: "violet" },
  { key: "blocked", label: de.backlog.colBlocked, tone: "red" },
  { key: "later", label: de.backlog.colLater, tone: "zinc" },
];

const STREAM_STATUSES: Array<{ key: Exclude<Status, "done">; label: string; tone: ToneName }> = [
  { key: "now", label: de.backlog.colNow, tone: "sky" },
  { key: "next", label: de.backlog.colNext, tone: "indigo" },
  { key: "in_progress", label: de.backlog.colInProgress, tone: "violet" },
  { key: "blocked", label: de.backlog.colBlocked, tone: "red" },
  { key: "later", label: de.backlog.colLater, tone: "zinc" },
];

function clockLabel(nowSec: number): string {
  return new Date(nowSec * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}

function CommissionBanner({
  nextId,
  nextTitle,
  prompt,
}: {
  nextId: string;
  nextTitle: string;
  prompt: string | undefined;
}) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    if (!prompt) return;
    try {
      await navigator.clipboard.writeText(prompt);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard blocked */
    }
  };

  return (
    <div className="hc-card flex flex-col gap-3 border-cyan-400/25 bg-cyan-500/5 p-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">{de.backlog.nextTask}</p>
        <p className="mt-0.5 truncate text-sm font-medium text-white">{nextTitle}</p>
        <p className="mt-0.5 text-[11px] hc-mono hc-dim">{nextId}</p>
      </div>
      <button
        type="button"
        onClick={copy}
        disabled={!prompt}
        className={cn(
          "flex shrink-0 items-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition",
          copied
            ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
            : !prompt
              ? "cursor-wait border-white/10 text-zinc-500"
              : "border-cyan-500/30 bg-cyan-500/10 text-cyan-200 hover:bg-cyan-500/20",
        )}
        title={de.backlog.commissionHint}
      >
        {copied ? <Check className="h-4 w-4" /> : <ClipboardCopy className="h-4 w-4" />}
        {copied ? de.backlog.commissionCopied : de.backlog.commission}
      </button>
    </div>
  );
}

function ControlsBar({
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
    <div className="hc-card flex flex-col gap-3 p-3">
      <input
        type="search"
        value={q}
        onChange={(e) => onQ(e.target.value)}
        placeholder={de.backlog.searchPlaceholder}
        className="w-full rounded-lg border border-white/10 bg-white/[.04] px-3 py-2 text-sm text-white placeholder:text-zinc-500 focus:border-cyan-400/50 focus:outline-none"
      />
      <div className="flex flex-wrap items-center gap-2">
        {/* Risk filter */}
        {(["", "high", "medium", "low"] as const).map((r) => (
          <button
            key={r || "all-risk"}
            type="button"
            onClick={() => onFilterRisk(r)}
            className={cn(
              "rounded-full border px-2.5 py-1 text-xs font-medium transition",
              filterRisk === r
                ? "border-cyan-400/50 bg-cyan-500/15 text-cyan-200"
                : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
            )}
          >
            {r || de.backlog.filterAll}
          </button>
        ))}

        {/* Stale filter */}
        <button
          type="button"
          onClick={() => onFilterStale(!filterStale)}
          className={cn(
            "rounded-full border px-2.5 py-1 text-xs font-medium transition",
            filterStale
              ? "border-red-400/50 bg-red-500/15 text-red-200"
              : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
          )}
        >
          {de.backlog.filterStale}
        </button>

        {/* Owner filter */}
        {owners.length > 1 && (
          <select
            value={filterOwner}
            onChange={(e) => onFilterOwner(e.target.value)}
            className="rounded-full border border-white/10 bg-white/[.04] px-2.5 py-1 text-xs text-zinc-200 focus:outline-none"
          >
            <option value="">{de.backlog.filterAll} Owner</option>
            {owners.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        )}

        {/* Sort */}
        <div className="ml-auto flex items-center gap-1.5">
          <span className="text-xs hc-dim">{de.backlog.sortLabel}:</span>
          {(["risk", "age", "status"] as FoSortKey[]).map((sk) => (
            <button
              key={sk}
              type="button"
              onClick={() => onSort(sk)}
              className={cn(
                "rounded-full border px-2 py-0.5 text-xs transition",
                sortKey === sk
                  ? "border-cyan-400/40 bg-cyan-500/10 text-cyan-200"
                  : "border-white/10 text-zinc-400 hover:text-zinc-200",
              )}
            >
              {sk === "risk" ? de.backlog.sortRisk : sk === "age" ? de.backlog.sortAge : de.backlog.sortStatus}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

export function BacklogView({ density }: { density: Density }) {
  const backlog = useBacklog();
  const { detailById, errorById, loadingId, fetch: fetchDetail } = useBacklogDetail();
  const [showAllDone, setShowAllDone] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  // Filter/sort state
  const [q, setQ] = useState("");
  const [filterOwner, setFilterOwner] = useState("");
  const [filterRisk, setFilterRisk] = useState("");
  const [filterStale, setFilterStale] = useState(false);
  const [sortKey, setSortKey] = useState<FoSortKey>("status");

  const data = backlog.data;
  const nowSec = data?.checked_at ?? Math.floor(Date.now() / 1000);
  const gap = density === "compact" ? "gap-3" : "gap-4";
  const allItems = data?.items ?? [];

  useEffect(() => {
    if (openId) void fetchDetail(openId);
  }, [fetchDetail, openId]);

  // Compute next task + auto-fetch detail for commission prompt
  const nextTaskId = useMemo(() => computeNextFoTaskId(allItems), [allItems]);
  const nextTask = nextTaskId ? allItems.find((it) => it.id === nextTaskId) : null;

  useEffect(() => {
    if (nextTaskId && !detailById[nextTaskId]) void fetchDetail(nextTaskId);
  }, [nextTaskId, detailById, fetchDetail]);

  const nextDetail = nextTaskId ? detailById[nextTaskId] : undefined;
  const commissionPromptForNext = nextDetail ? buildFoCommissionPrompt(nextDetail) : undefined;

  // Distinct owners for filter dropdown
  const owners = useMemo(() => {
    const set = new Set<string>();
    for (const it of allItems) if (it.owner && it.owner !== "unassigned") set.add(it.owner);
    return [...set].sort();
  }, [allItems]);

  // Grouped by status (full, unfiltered)
  const byStatus = useMemo(() => {
    const map: Record<string, BacklogItem[]> = {};
    for (const item of allItems) (map[item.status] ??= []).push(item);
    return map;
  }, [allItems]);

  // Filtered + sorted active items
  const filteredActive = useMemo(() => {
    const active = allItems.filter((it) => it.status !== "done");
    const filtered = filterFoItems(active, q, {
      owner: filterOwner || undefined,
      risk: filterRisk || undefined,
      stale: filterStale || undefined,
    });
    return sortFoItems(filtered, sortKey);
  }, [allItems, q, filterOwner, filterRisk, filterStale, sortKey]);

  const filteredByStatus = useMemo(() => {
    const map: Record<string, BacklogItem[]> = {};
    for (const item of filteredActive) (map[item.status] ??= []).push(item);
    return map;
  }, [filteredActive]);

  const doneItems = useMemo(() => {
    const arr = [...(byStatus.done ?? [])];
    arr.sort((a, b) => b.updated.localeCompare(a.updated) || b.id.localeCompare(a.id));
    return arr;
  }, [byStatus]);

  const counts = data?.counts;
  const activeTotal = counts ? counts.now + counts.next + counts.in_progress + counts.blocked + counts.later : 0;

  const selectedItem = openId ? allItems.find((item) => item.id === openId) : undefined;
  const detail = openId ? detailById[openId] : undefined;

  const detailFields: Array<{ label: string; value: string }> = detail
    ? (
        [
          detail.owner ? { label: de.backlog.owner, value: detail.owner } : null,
          detail.risk ? { label: de.backlog.risk, value: detail.risk } : null,
          detail.area ? { label: de.backlog.area, value: detail.area } : null,
          detail.lane ? { label: de.backlog.lane, value: detail.lane } : null,
          detail.result ? { label: de.backlog.result, value: detail.result } : null,
          detail.updated ? { label: de.backlog.updatedLabel, value: detail.updated } : null,
        ] as Array<{ label: string; value: string } | null>
      ).filter((f): f is { label: string; value: string } => f !== null)
    : [];

  const commissionPromptForDrawer = detail ? buildFoCommissionPrompt(detail) : undefined;

  return (
    <div className="space-y-4">
      {/* Header */}
      <section className="hc-card flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="hc-eyebrow">{de.backlog.eyebrow}</p>
          <h2 className="mt-1 text-xl font-semibold text-white">
            {de.backlog.title} · {activeTotal} aktiv
          </h2>
          <p className="mt-1 text-xs hc-soft">{de.backlog.subtitle}</p>
        </div>
        <div className="text-right text-xs hc-soft">
          <div>{backlog.loading && !data ? de.backlog.loading : de.backlog.updatedAt(clockLabel(nowSec))}</div>
          {counts ? (
            <div className="mt-1 hc-dim">
              {counts.done} erledigt · {data?.source.count ?? 0} gesamt
            </div>
          ) : null}
        </div>
      </section>

      {backlog.error ? <ToneCallout tone="red">{de.backlog.error}</ToneCallout> : null}
      {data?.error ? <ToneCallout tone="amber">{de.backlog.sourceMissing}</ToneCallout> : null}

      {/* Commission banner */}
      {nextTask ? (
        <CommissionBanner
          nextId={nextTask.id}
          nextTitle={nextTask.title}
          prompt={commissionPromptForNext}
        />
      ) : allItems.length > 0 ? (
        <ToneCallout tone="zinc">{de.backlog.noNextTask}</ToneCallout>
      ) : null}

      {/* Controls */}
      {allItems.length > 0 && (
        <ControlsBar
          q={q} onQ={setQ}
          filterOwner={filterOwner} onFilterOwner={setFilterOwner}
          filterRisk={filterRisk} onFilterRisk={setFilterRisk}
          filterStale={filterStale} onFilterStale={setFilterStale}
          sortKey={sortKey} onSort={setSortKey}
          owners={owners}
        />
      )}

      {/* Desktop board (≥lg), hidden on mobile */}
      <div className={cn("hidden lg:grid", gap)} style={{ gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))" }}>
        {ACTIVE_COLUMNS.map((col) => {
          const items = filteredByStatus[col.key] ?? [];
          return (
            <section key={col.key} className="hc-card flex min-w-0 flex-col gap-2 p-3">
              <div className="flex items-center justify-between">
                <StatusPill tone={col.tone} label={col.label} />
                <span className="hc-mono text-xs hc-dim">{items.length}</span>
              </div>
              {items.length === 0 ? (
                <p className="py-3 text-center text-xs hc-dim">{de.backlog.emptyColumn}</p>
              ) : (
                items.map((item) => (
                  <FoBacklogCard
                    key={item.id}
                    item={item}
                    nowSec={nowSec}
                    isNext={item.id === nextTaskId}
                    onOpen={setOpenId}
                  />
                ))
              )}
            </section>
          );
        })}
      </div>

      {/* Mobile stream (<lg), skip empty groups */}
      <div className={cn("flex flex-col lg:hidden", gap)}>
        {STREAM_STATUSES.map((col) => {
          const items = filteredByStatus[col.key] ?? [];
          if (items.length === 0) return null;
          return (
            <section key={col.key} className="hc-card flex flex-col gap-2 p-3">
              <div className="flex items-center gap-2">
                <StatusPill tone={col.tone} label={col.label} />
                <span className="hc-mono text-xs hc-dim">{items.length}</span>
              </div>
              {items.map((item) => (
                <FoBacklogCard
                  key={item.id}
                  item={item}
                  nowSec={nowSec}
                  isNext={item.id === nextTaskId}
                  onOpen={setOpenId}
                />
              ))}
            </section>
          );
        })}
        {filteredActive.length === 0 && (
          <p className="py-4 text-center text-sm hc-dim">{de.backlog.empty}</p>
        )}
      </div>

      {/* Done section */}
      <section className="hc-card p-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <StatusPill tone="emerald" label={de.backlog.colDone} />
            <span className="hc-mono text-xs hc-dim">{doneItems.length}</span>
            <span className="hidden text-xs hc-dim sm:inline">· {de.backlog.doneRecentHint}</span>
            <span className="hidden text-xs hc-dim sm:inline">· {de.backlog.doneResultHint}</span>
          </div>
          {doneItems.length > 5 ? (
            <button
              type="button"
              onClick={() => setShowAllDone((v) => !v)}
              className="rounded-md border border-white/10 px-2 py-1 text-xs hc-soft hover:bg-white/5"
            >
              {showAllDone ? de.backlog.showRecent : de.backlog.showAll}
            </button>
          ) : null}
        </div>
        {doneItems.length === 0 ? (
          <p className="py-2 text-xs hc-dim">{de.backlog.empty}</p>
        ) : (
          <div className={cn("grid", gap)} style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))" }}>
            {(showAllDone ? doneItems : doneItems.slice(0, 5)).map((item) => (
              <FoBacklogCard key={item.id} item={item} nowSec={nowSec} isNext={false} onOpen={setOpenId} />
            ))}
          </div>
        )}
      </section>

      {/* Detail drawer */}
      {openId ? (
        <BacklogDetailDrawer
          title={selectedItem?.title ?? detail?.title ?? openId}
          id={openId}
          body={detail?.body ?? ""}
          chips={[
            ...(selectedItem?.stale ? [{ label: de.backlog.staleBadge, tone: "red" as const }] : []),
          ]}
          fields={detailFields}
          loading={loadingId === openId}
          error={errorById[openId] || detail?.error}
          commissionPrompt={commissionPromptForDrawer}
          onClose={() => setOpenId(null)}
        />
      ) : null}
    </div>
  );
}
