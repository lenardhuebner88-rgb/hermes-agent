import { useEffect, useMemo, useState } from "react";
import { Check, ClipboardCopy } from "lucide-react";

import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import { useOrchestrationBacklog, useOrchestrationBacklogDetail } from "../hooks/useControlData";
import { BacklogDetailDrawer } from "../components/BacklogDetailDrawer";
import { BacklogCard } from "../components/BacklogCard";
import { StatusPill, ToneCallout } from "../components/atoms";
import {
  buildCommissionPrompt,
  computeNextTaskId,
  filterItems,
  projectFromRoot,
  readiness,
  sortItems,
} from "../lib/orchestration";
import type { Readiness, SortKey } from "../lib/orchestration";
import type { Density } from "../hooks/useDensity";
import type { OrchestrationItem } from "../lib/schemas";
import type { ToneName } from "../lib/types";

type Status = OrchestrationItem["status"];

const ACTIVE_COLUMNS: Array<{ key: Exclude<Status, "done">; label: string; tone: ToneName }> = [
  { key: "doing", label: de.orchestrator.colDoing, tone: "violet" },
  { key: "review", label: de.orchestrator.colReview, tone: "amber" },
  { key: "todo", label: de.orchestrator.colTodo, tone: "sky" },
  { key: "backlog", label: de.orchestrator.colBacklog, tone: "zinc" },
];

// Mobile stream: active statuses in priority order (skip empty active groups)
const STREAM_STATUSES: Array<{ key: Exclude<Status, "done">; label: string; tone: ToneName }> = [
  { key: "doing", label: de.orchestrator.colDoing, tone: "violet" },
  { key: "review", label: de.orchestrator.colReview, tone: "amber" },
  { key: "todo", label: de.orchestrator.colTodo, tone: "sky" },
  { key: "backlog", label: de.orchestrator.colBacklog, tone: "zinc" },
];

type DetailChip = { label: string; tone?: ToneName };

function readinessChip(value: Readiness): DetailChip | null {
  if (value.state === "ready") return { tone: "emerald", label: de.orchestrator.ready };
  if (value.state === "blocked") {
    return { tone: "red", label: `${de.orchestrator.blockedBy} ${value.blockedBy.join(", ")}` };
  }
  return null;
}

function clockLabel(nowSec: number): string {
  return new Date(nowSec * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}

// Commission-Banner component
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
        <p className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">{de.orchestrator.nextTask}</p>
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
        title={de.orchestrator.commissionHint}
      >
        {copied ? <Check className="h-4 w-4" /> : <ClipboardCopy className="h-4 w-4" />}
        {copied ? de.orchestrator.commissionCopied : de.orchestrator.commission}
      </button>
    </div>
  );
}

// Controls bar: search + filter chips + sort
function ControlsBar({
  q,
  onQ,
  filterPriority,
  onFilterPriority,
  filterProject,
  onFilterProject,
  filterPlanGate,
  onFilterPlanGate,
  filterReadiness,
  onFilterReadiness,
  sortKey,
  onSort,
  projects,
}: {
  q: string;
  onQ: (v: string) => void;
  filterPriority: string;
  onFilterPriority: (v: string) => void;
  filterProject: string;
  onFilterProject: (v: string) => void;
  filterPlanGate: string;
  onFilterPlanGate: (v: string) => void;
  filterReadiness: string;
  onFilterReadiness: (v: string) => void;
  sortKey: SortKey;
  onSort: (v: SortKey) => void;
  projects: string[];
}) {
  return (
    <div className="hc-card flex flex-col gap-3 p-3">
      <input
        type="search"
        value={q}
        onChange={(e) => onQ(e.target.value)}
        placeholder={de.orchestrator.searchPlaceholder}
        className="w-full rounded-lg border border-white/10 bg-white/[.04] px-3 py-2 text-sm text-white placeholder:text-zinc-500 focus:border-cyan-400/50 focus:outline-none"
      />
      <div className="flex flex-wrap items-center gap-2">
        {/* Priority filter */}
        {(["", "high", "medium", "low"] as const).map((p) => (
          <button
            key={p || "all-prio"}
            type="button"
            onClick={() => onFilterPriority(p)}
            className={cn(
              "rounded-full border px-2.5 py-1 text-xs font-medium transition",
              filterPriority === p
                ? "border-cyan-400/50 bg-cyan-500/15 text-cyan-200"
                : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
            )}
          >
            {p ? p : de.orchestrator.filterAll}
          </button>
        ))}

        {/* Plan-Gate filter */}
        <button
          type="button"
          onClick={() => onFilterPlanGate(filterPlanGate === "true" ? "" : "true")}
          className={cn(
            "rounded-full border px-2.5 py-1 text-xs font-medium transition",
            filterPlanGate === "true"
              ? "border-indigo-400/50 bg-indigo-400/15 text-indigo-200"
              : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
          )}
        >
          {de.orchestrator.filterPlanGate}
        </button>

        {/* Readiness filter */}
        {(["", "ready", "blocked"] as const).map((rs) => (
          <button
            key={rs || "all-r"}
            type="button"
            onClick={() => onFilterReadiness(rs)}
            className={cn(
              "rounded-full border px-2.5 py-1 text-xs font-medium transition",
              filterReadiness === rs && rs !== ""
                ? rs === "ready"
                  ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-200"
                  : "border-red-500/50 bg-red-500/15 text-red-200"
                : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
            )}
          >
            {rs === "ready" ? de.orchestrator.filterReady : rs === "blocked" ? de.orchestrator.filterBlocked : ""}
          </button>
        )).filter((_, i) => i > 0)}

        {/* Project filter */}
        {projects.length > 1 && (
          <select
            value={filterProject}
            onChange={(e) => onFilterProject(e.target.value)}
            className="rounded-full border border-white/10 bg-white/[.04] px-2.5 py-1 text-xs text-zinc-200 focus:outline-none"
          >
            <option value="">{de.orchestrator.filterAll} Projekt</option>
            {projects.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        )}

        {/* Sort */}
        <div className="ml-auto flex items-center gap-1.5">
          <span className="text-xs hc-dim">{de.orchestrator.sortLabel}:</span>
          {(["priority", "age", "readiness"] as SortKey[]).map((sk) => (
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
              {sk === "priority" ? de.orchestrator.sortPriority : sk === "age" ? de.orchestrator.sortAge : de.orchestrator.sortReadiness}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

export function OrchestratorBacklogView({ density }: { density: Density }) {
  const backlog = useOrchestrationBacklog();
  const { detailById, errorById, loadingId, fetch: fetchDetail } = useOrchestrationBacklogDetail();
  const [showAllDone, setShowAllDone] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  // Filter/sort state
  const [q, setQ] = useState("");
  const [filterPriority, setFilterPriority] = useState("");
  const [filterProject, setFilterProject] = useState("");
  const [filterPlanGate, setFilterPlanGate] = useState("");
  const [filterReadiness, setFilterReadiness] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("priority");

  const data = backlog.data;
  const nowSec = data?.checked_at ?? Math.floor(Date.now() / 1000);
  const gap = density === "compact" ? "gap-3" : "gap-4";
  const allItems = data?.items ?? [];

  // Load detail when drawer opens
  useEffect(() => {
    if (openId) void fetchDetail(openId);
  }, [fetchDetail, openId]);

  // Compute next task
  const nextTaskId = useMemo(() => computeNextTaskId(allItems), [allItems]);
  const nextTask = nextTaskId ? allItems.find((it) => it.id === nextTaskId) : null;

  // Auto-fetch detail for next task (for commission prompt)
  useEffect(() => {
    if (nextTaskId && !detailById[nextTaskId]) void fetchDetail(nextTaskId);
  }, [nextTaskId, detailById, fetchDetail]);

  const nextDetail = nextTaskId ? detailById[nextTaskId] : undefined;
  const commissionPromptForNext = nextDetail
    ? buildCommissionPrompt(nextDetail)
    : undefined;

  // All available projects for filter dropdown
  const projects = useMemo(() => {
    const set = new Set<string>();
    for (const it of allItems) {
      const p = projectFromRoot(it.root);
      if (p !== "Orchestration") set.add(p);
    }
    return [...set].sort();
  }, [allItems]);

  // Grouped by status (full, unfiltered)
  const byStatus = useMemo(() => {
    const map: Record<string, OrchestrationItem[]> = {};
    for (const item of allItems) (map[item.status] ??= []).push(item);
    return map;
  }, [allItems]);

  // Filtered + sorted for the active columns
  const filteredActive = useMemo(() => {
    const active = allItems.filter((it) => it.status !== "done");
    const filtered = filterItems(
      active,
      q,
      { priority: filterPriority, project: filterProject, planGate: filterPlanGate, readiness: filterReadiness },
      allItems,
    );
    return sortItems(filtered, sortKey, allItems);
  }, [allItems, q, filterPriority, filterProject, filterPlanGate, filterReadiness, sortKey]);

  const filteredByStatus = useMemo(() => {
    const map: Record<string, OrchestrationItem[]> = {};
    for (const item of filteredActive) (map[item.status] ??= []).push(item);
    return map;
  }, [filteredActive]);

  const doneItems = useMemo(() => {
    const arr = [...(byStatus.done ?? [])];
    arr.sort((a, b) => b.created.localeCompare(a.created) || b.id.localeCompare(a.id));
    return arr;
  }, [byStatus]);

  const counts = data?.counts;
  const activeTotal = counts ? counts.doing + counts.review + counts.todo + counts.backlog : 0;
  const selectedItem = openId ? allItems.find((item) => item.id === openId) : undefined;
  const detail = openId ? detailById[openId] : undefined;

  const detailReadiness = selectedItem ? readinessChip(readiness(selectedItem, allItems)) : null;
  const detailChips: DetailChip[] = [
    ...(detailReadiness ? [detailReadiness] : []),
    ...((selectedItem?.dependsOn ?? detail?.dependsOn ?? []).map((id) => ({ tone: "cyan" as const, label: id }))),
  ];
  const detailFields: Array<{ label: string; value: string }> = detail
    ? (
        [
          detail.priority ? { label: de.orchestrator.priority, value: detail.priority } : null,
          { label: de.orchestrator.planGate, value: detail.planGate ? de.orchestrator.yes : de.orchestrator.no },
          detail.gate ? { label: de.orchestrator.gate, value: detail.gate } : null,
          detail.root ? { label: de.orchestrator.root, value: detail.root } : null,
          detail.created ? { label: de.orchestrator.created, value: detail.created } : null,
        ] as Array<{ label: string; value: string } | null>
      ).filter((f): f is { label: string; value: string } => f !== null)
    : [];

  const commissionPromptForDrawer = detail ? buildCommissionPrompt(detail) : undefined;

  return (
    <div className="space-y-4">
      {/* Header card */}
      <section className="hc-card flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="hc-eyebrow">{de.orchestrator.eyebrow}</p>
          <h2 className="mt-1 text-xl font-semibold text-white">
            {de.orchestrator.title} · {activeTotal} aktiv
          </h2>
          <p className="mt-1 text-xs hc-soft">{de.orchestrator.subtitle}</p>
        </div>
        <div className="text-right text-xs hc-soft">
          <div>{backlog.loading && !data ? de.orchestrator.loading : de.orchestrator.updatedAt(clockLabel(nowSec))}</div>
          {counts ? (
            <div className="mt-1 hc-dim">
              {counts.done} erledigt · {data?.source.count ?? 0} gesamt
            </div>
          ) : null}
        </div>
      </section>

      {backlog.error ? <ToneCallout tone="red">{de.orchestrator.error}</ToneCallout> : null}
      {data?.error ? <ToneCallout tone="amber">{de.orchestrator.sourceMissing}</ToneCallout> : null}

      {/* Commission banner */}
      {nextTask ? (
        <CommissionBanner
          nextId={nextTask.id}
          nextTitle={nextTask.title}
          prompt={commissionPromptForNext}
        />
      ) : allItems.length > 0 ? (
        <ToneCallout tone="zinc">{de.orchestrator.noNextTask}</ToneCallout>
      ) : null}

      {/* Controls */}
      {allItems.length > 0 && (
        <ControlsBar
          q={q} onQ={setQ}
          filterPriority={filterPriority} onFilterPriority={setFilterPriority}
          filterProject={filterProject} onFilterProject={setFilterProject}
          filterPlanGate={filterPlanGate} onFilterPlanGate={setFilterPlanGate}
          filterReadiness={filterReadiness} onFilterReadiness={setFilterReadiness}
          sortKey={sortKey} onSort={setSortKey}
          projects={projects}
        />
      )}

      {/* Desktop: column board (≥lg), Mobile: stream (<lg) */}

      {/* DESKTOP board (hidden on mobile) */}
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
                <p className="py-3 text-center text-xs hc-dim">{de.orchestrator.emptyColumn}</p>
              ) : (
                items.map((item) => (
                  <BacklogCard
                    key={item.id}
                    item={item}
                    allItems={allItems}
                    nowSec={nowSec}
                    onOpen={setOpenId}
                    isNext={item.id === nextTaskId}
                  />
                ))
              )}
            </section>
          );
        })}
      </div>

      {/* MOBILE stream (hidden on desktop) */}
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
                <BacklogCard
                  key={item.id}
                  item={item}
                  allItems={allItems}
                  nowSec={nowSec}
                  onOpen={setOpenId}
                  isNext={item.id === nextTaskId}
                />
              ))}
            </section>
          );
        })}
        {filteredActive.length === 0 && (
          <p className="py-4 text-center text-sm hc-dim">{de.orchestrator.empty}</p>
        )}
      </div>

      {/* Done section */}
      <section className="hc-card p-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <StatusPill tone="emerald" label={de.orchestrator.colDone} />
            <span className="hc-mono text-xs hc-dim">{doneItems.length}</span>
            <span className="hidden text-xs hc-dim sm:inline">· {de.orchestrator.doneRecentHint}</span>
            <span className="hidden text-xs hc-dim sm:inline">· {de.orchestrator.doneReceiptHint}</span>
          </div>
          {doneItems.length > 5 ? (
            <button
              type="button"
              onClick={() => setShowAllDone((v) => !v)}
              className="rounded-md border border-white/10 px-2 py-1 text-xs hc-soft hover:bg-white/5"
            >
              {showAllDone ? de.orchestrator.showRecent : de.orchestrator.showAll}
            </button>
          ) : null}
        </div>
        {doneItems.length === 0 ? (
          <p className="py-2 text-xs hc-dim">{de.orchestrator.empty}</p>
        ) : (
          <div className={cn("grid", gap)} style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))" }}>
            {(showAllDone ? doneItems : doneItems.slice(0, 5)).map((item) => (
              <BacklogCard key={item.id} item={item} allItems={allItems} nowSec={nowSec} onOpen={setOpenId} />
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
          chips={detailChips}
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
