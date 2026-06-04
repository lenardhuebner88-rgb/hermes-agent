import { useEffect, useMemo, useState } from "react";
import { Check, ClipboardCopy, Columns3, List } from "lucide-react";

import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import { useOrchestrationBacklog, useOrchestrationBacklogDetail } from "../hooks/useControlData";
import { BacklogDetailDrawer } from "../components/BacklogDetailDrawer";
import { BacklogCard } from "../components/BacklogCard";
import { StatusPill, ToneCallout } from "../components/atoms";
import {
  ageLabel,
  buildCommissionPrompt,
  computeNextTaskId,
  deriveQueueSignals,
  filterItems,
  isKnownStatus,
  nextActionForItem,
  projectFromRoot,
  readiness,
  sortItems,
} from "../lib/orchestration";
import type { Readiness, SortKey } from "../lib/orchestration";
import type { Density } from "../hooks/useDensity";
import type { OrchestrationBacklogResponse, OrchestrationDetail, OrchestrationItem } from "../lib/schemas";
import type { ToneName } from "../lib/types";

const ACTIVE_COLUMNS: Array<{ key: string; label: string; tone: ToneName }> = [
  { key: "doing", label: de.orchestrator.colDoing, tone: "violet" },
  { key: "review", label: de.orchestrator.colReview, tone: "amber" },
  { key: "todo", label: de.orchestrator.colTodo, tone: "sky" },
  { key: "backlog", label: de.orchestrator.colBacklog, tone: "zinc" },
  { key: "__drift", label: de.orchestrator.statusDrift, tone: "red" },
];

type ViewMode = "queue" | "board";
type DetailChip = { label: string; tone?: ToneName };

const PRIORITY_TONE: Record<string, ToneName> = { high: "red", medium: "amber", low: "zinc" };
const STATUS_TONE: Record<string, ToneName> = { doing: "violet", review: "amber", todo: "sky", backlog: "zinc", done: "emerald" };
const EMPTY_ITEMS: OrchestrationItem[] = [];

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

function statusTone(status: string): ToneName {
  if (!isKnownStatus(status)) return "red";
  return STATUS_TONE[status] ?? "zinc";
}

function priorityTone(priority: string): ToneName {
  return PRIORITY_TONE[priority] ?? "rose";
}

function proofLabel(item: OrchestrationItem): string {
  return item.lastProof?.trim() || de.orchestrator.proofMissing;
}

function ownerLabel(item: OrchestrationItem): string {
  return item.owner?.trim() || de.orchestrator.ownerMissing;
}

function sourceLabel(item: OrchestrationItem): string {
  return item.source?.trim() || projectFromRoot(item.root) || de.orchestrator.sourceFallback;
}

function sourcePath(id: string): string {
  return `~/orchestration/backlog/${id}.md`;
}

function buildOperatorBrief(
  item: OrchestrationItem | undefined,
  detail: OrchestrationDetail | undefined,
  nextAction: string,
  responseRef: string,
): string | undefined {
  if (!item && !detail) return undefined;
  const id = item?.id ?? detail?.id ?? "";
  const title = detail?.title || item?.title || id;
  const status = detail?.status || item?.status || "";
  const priority = detail?.priority || item?.priority || "";
  const owner = detail?.owner || item?.owner || de.orchestrator.ownerMissing;
  const source = detail?.source || item?.source || sourceLabel(item ?? ({ root: detail?.root ?? "" } as OrchestrationItem));
  const proof = detail?.lastProof || item?.lastProof || de.orchestrator.proofMissing;
  return [
    "Hermes Orchestrator Backlog Brief",
    `Task: ${title} (${id})`,
    `Status: ${status}`,
    `Priority/Risk: ${priority || "n/a"}`,
    `Owner: ${owner || de.orchestrator.ownerMissing}`,
    `Source: ${source || de.orchestrator.sourceFallback}`,
    `Last Proof: ${proof}`,
    `Next Action: ${nextAction}`,
    `Spec: ${sourcePath(id)}`,
    responseRef ? `Ref: ${responseRef}` : "",
  ].filter(Boolean).join("\n");
}

function ContractDriftCallout({ data }: { data: OrchestrationBacklogResponse }) {
  const health = data.contract_health;
  const unknown = health.unknown_statuses.map((entry) => `${entry.status || "(leer)"}:${entry.count}`).join(", ");
  const parts = [
    `${de.orchestrator.sourceCount}: ${health.source_count}`,
    `${de.orchestrator.countGap}: ${Math.max(0, health.source_count - health.counted_sum)}`,
    unknown ? `${de.orchestrator.unknownStatuses}: ${unknown}` : "",
    health.invalid_priority_count ? `${de.orchestrator.invalidPriority}: ${health.invalid_priority_count}` : "",
    health.missing_dep_count ? `${de.orchestrator.missingDeps}: ${health.missing_dep_count}` : "",
  ].filter(Boolean);
  return <ToneCallout tone="amber">{parts.join(" · ")}</ToneCallout>;
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
        <p className="text-[11px] font-semibold uppercase tracking-wider text-cyan-400">{de.orchestrator.nextTask}</p>
        <p className="mt-0.5 truncate text-sm font-medium text-white">{nextTitle}</p>
        <p className="mt-0.5 text-[11px] hc-mono hc-dim">{nextId}</p>
      </div>
      <button
        type="button"
        onClick={copy}
        disabled={!prompt}
        className={cn(
          "flex shrink-0 items-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition focus:outline-none focus:ring-2 focus:ring-cyan-400/60",
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

function SignalStrip({ signals }: { signals: ReturnType<typeof deriveQueueSignals> }) {
  const tiles: Array<{ label: string; value: number; tone: ToneName }> = [
    { label: de.orchestrator.readyStrip, value: signals.ready, tone: "emerald" },
    { label: de.orchestrator.blockedStrip, value: signals.blocked, tone: "red" },
    { label: de.orchestrator.unownedStrip, value: signals.unowned, tone: "amber" },
    { label: de.orchestrator.staleProofStrip, value: signals.staleProof, tone: "rose" },
    { label: de.orchestrator.highRiskStrip, value: signals.highRisk, tone: "red" },
    { label: de.orchestrator.contractDrift, value: signals.contractDrift, tone: signals.contractDrift ? "red" : "zinc" },
  ];
  return (
    <section className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
      {tiles.map((tile) => (
        <div key={tile.label} className={cn("rounded-lg border px-3 py-2", tile.tone === "emerald" && "border-emerald-500/20 bg-emerald-500/10", tile.tone === "red" && "border-red-500/20 bg-red-500/10", tile.tone === "amber" && "border-amber-500/20 bg-amber-500/10", tile.tone === "rose" && "border-rose-500/20 bg-rose-500/10", tile.tone === "zinc" && "border-white/10 bg-white/[.03]")}>
          <p className="text-[10px] font-semibold uppercase tracking-wide hc-dim">{tile.label}</p>
          <p className="mt-1 hc-mono text-lg font-semibold text-white">{tile.value}</p>
        </div>
      ))}
    </section>
  );
}

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
  viewMode,
  onViewMode,
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
  viewMode: ViewMode;
  onViewMode: (v: ViewMode) => void;
}) {
  return (
    <div className="hc-card flex flex-col gap-3 p-3">
      <input
        type="search"
        value={q}
        onChange={(e) => onQ(e.target.value)}
        placeholder={de.orchestrator.searchPlaceholder}
        className="w-full rounded-lg border border-white/10 bg-white/[.04] px-3 py-2 text-sm text-white placeholder:text-zinc-500 focus:border-cyan-400/50 focus:outline-none focus:ring-2 focus:ring-cyan-400/30"
      />
      <div className="flex flex-wrap items-center gap-2">
        {(["", "high", "medium", "low"] as const).map((p) => (
          <button
            key={p || "all-prio"}
            type="button"
            onClick={() => onFilterPriority(p)}
            className={cn(
              "rounded-full border px-2.5 py-1 text-xs font-medium transition focus:outline-none focus:ring-2 focus:ring-cyan-400/50",
              filterPriority === p
                ? "border-cyan-400/50 bg-cyan-500/15 text-cyan-200"
                : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
            )}
          >
            {p ? p : de.orchestrator.filterAll}
          </button>
        ))}

        <button
          type="button"
          onClick={() => onFilterPlanGate(filterPlanGate === "true" ? "" : "true")}
          className={cn(
            "rounded-full border px-2.5 py-1 text-xs font-medium transition focus:outline-none focus:ring-2 focus:ring-cyan-400/50",
            filterPlanGate === "true"
              ? "border-indigo-400/50 bg-indigo-400/15 text-indigo-200"
              : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
          )}
        >
          {de.orchestrator.filterPlanGate}
        </button>

        {(["ready", "blocked"] as const).map((rs) => (
          <button
            key={rs}
            type="button"
            onClick={() => onFilterReadiness(filterReadiness === rs ? "" : rs)}
            className={cn(
              "rounded-full border px-2.5 py-1 text-xs font-medium transition focus:outline-none focus:ring-2 focus:ring-cyan-400/50",
              filterReadiness === rs
                ? rs === "ready"
                  ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-200"
                  : "border-red-500/50 bg-red-500/15 text-red-200"
                : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
            )}
          >
            {rs === "ready" ? de.orchestrator.filterReady : de.orchestrator.filterBlocked}
          </button>
        ))}

        {projects.length > 1 ? (
          <select
            value={filterProject}
            onChange={(e) => onFilterProject(e.target.value)}
            className="rounded-full border border-white/10 bg-white/[.04] px-2.5 py-1 text-xs text-zinc-200 focus:outline-none focus:ring-2 focus:ring-cyan-400/50"
          >
            <option value="">{de.orchestrator.filterAll} Projekt</option>
            {projects.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        ) : null}

        <div className="ml-auto flex flex-wrap items-center gap-1.5">
          <span className="text-xs hc-dim">{de.orchestrator.sortLabel}:</span>
          {(["priority", "age", "readiness"] as SortKey[]).map((sk) => (
            <button
              key={sk}
              type="button"
              onClick={() => onSort(sk)}
              className={cn(
                "rounded-full border px-2 py-0.5 text-xs transition focus:outline-none focus:ring-2 focus:ring-cyan-400/50",
                sortKey === sk
                  ? "border-cyan-400/40 bg-cyan-500/10 text-cyan-200"
                  : "border-white/10 text-zinc-400 hover:text-zinc-200",
              )}
            >
              {sk === "priority" ? de.orchestrator.sortPriority : sk === "age" ? de.orchestrator.sortAge : de.orchestrator.sortReadiness}
            </button>
          ))}
          <div className="ml-1 inline-flex rounded-lg border border-white/10 bg-black/20 p-0.5" aria-label="Ansicht">
            <button
              type="button"
              onClick={() => onViewMode("queue")}
              className={cn("inline-flex h-8 items-center gap-1 rounded-md px-2 text-xs focus:outline-none focus:ring-2 focus:ring-cyan-400/50", viewMode === "queue" ? "bg-cyan-500/15 text-cyan-200" : "hc-soft hover:bg-white/5")}
              title={de.orchestrator.queueView}
            >
              <List className="h-3.5 w-3.5" />
              {de.orchestrator.queueView}
            </button>
            <button
              type="button"
              onClick={() => onViewMode("board")}
              className={cn("inline-flex h-8 items-center gap-1 rounded-md px-2 text-xs focus:outline-none focus:ring-2 focus:ring-cyan-400/50", viewMode === "board" ? "bg-cyan-500/15 text-cyan-200" : "hc-soft hover:bg-white/5")}
              title={de.orchestrator.boardView}
            >
              <Columns3 className="h-3.5 w-3.5" />
              {de.orchestrator.boardView}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function OrchestratorQueueTable({
  items,
  allItems,
  nowSec,
  nextTaskId,
  onOpen,
}: {
  items: ReadonlyArray<OrchestrationItem>;
  allItems: ReadonlyArray<OrchestrationItem>;
  nowSec: number;
  nextTaskId: string | null;
  onOpen: (id: string) => void;
}) {
  if (items.length === 0) {
    return <p className="py-4 text-center text-sm hc-dim">{de.orchestrator.empty}</p>;
  }

  return (
    <section className="hc-card overflow-hidden">
      <div className="border-b border-[var(--hc-border)] px-3 py-2">
        <h3 className="text-sm font-semibold text-white">{de.orchestrator.queueTitle}</h3>
      </div>
      <div className="hidden grid-cols-[minmax(220px,2fr)_96px_112px_112px_84px_140px_150px_minmax(160px,1.2fr)] gap-3 border-b border-[var(--hc-border)] px-3 py-2 text-[10px] font-semibold uppercase tracking-wide hc-dim md:grid">
        <span>{de.orchestrator.colTitle}</span>
        <span>{de.orchestrator.colStatus}</span>
        <span>{de.orchestrator.colRiskPriority}</span>
        <span>{de.orchestrator.colOwner}</span>
        <span>{de.orchestrator.colAge}</span>
        <span>{de.orchestrator.colSource}</span>
        <span>{de.orchestrator.colLastProof}</span>
        <span>{de.orchestrator.colNextAction}</span>
      </div>
      <div className="divide-y divide-[var(--hc-border)]">
        {items.map((item) => {
          const nextAction = nextActionForItem(item, allItems);
          const isNext = item.id === nextTaskId;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onOpen(item.id)}
              className={cn(
                "grid w-full grid-cols-1 gap-2 px-3 py-3 text-left transition hover:bg-white/[.03] focus:outline-none focus:ring-2 focus:ring-inset focus:ring-cyan-400/60 md:grid-cols-[minmax(220px,2fr)_96px_112px_112px_84px_140px_150px_minmax(160px,1.2fr)] md:items-center md:gap-3",
                isNext && "bg-cyan-500/5",
              )}
            >
              <div className="min-w-0">
                {isNext ? (
                  <span className="mb-1 inline-block rounded bg-cyan-400/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-cyan-300">
                    {de.orchestrator.nextBadge}
                  </span>
                ) : null}
                <p className="truncate text-sm font-medium text-white">{item.title}</p>
                <p className="mt-0.5 truncate text-[11px] hc-mono hc-dim">{item.id}</p>
              </div>
              <div>
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colStatus}</span>
                <StatusPill tone={statusTone(item.status)} label={item.status || de.orchestrator.statusDrift} />
              </div>
              <div>
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colRiskPriority}</span>
                <StatusPill tone={priorityTone(item.priority)} label={item.priority || "n/a"} />
              </div>
              <div className={cn("truncate text-sm", item.owner ? "text-zinc-100" : "text-amber-200")}>
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colOwner}</span>
                {ownerLabel(item)}
              </div>
              <div className="text-sm hc-soft">
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colAge}</span>
                {ageLabel(item.created, nowSec)}
              </div>
              <div className="truncate text-sm hc-soft">
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colSource}</span>
                {sourceLabel(item)}
              </div>
              <div className={cn("truncate text-sm", item.lastProof ? "text-zinc-100" : "hc-dim")}>
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colLastProof}</span>
                {proofLabel(item)}
              </div>
              <div className="min-w-0 text-sm text-white">
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colNextAction}</span>
                <span className="line-clamp-2">{nextAction}</span>
              </div>
            </button>
          );
        })}
      </div>
    </section>
  );
}

export function OrchestratorBacklogView({ density }: { density: Density }) {
  const backlog = useOrchestrationBacklog();
  const { detailById, errorById, loadingId, fetch: fetchDetail } = useOrchestrationBacklogDetail();
  const [showAllDone, setShowAllDone] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("queue");

  const [q, setQ] = useState("");
  const [filterPriority, setFilterPriority] = useState("");
  const [filterProject, setFilterProject] = useState("");
  const [filterPlanGate, setFilterPlanGate] = useState("");
  const [filterReadiness, setFilterReadiness] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("priority");
  const [fallbackNowSec] = useState(() => Math.floor(Date.now() / 1000));

  const data = backlog.data;
  const nowSec = data?.checked_at ?? fallbackNowSec;
  const gap = density === "compact" ? "gap-3" : "gap-4";
  const allItems = data?.items ?? EMPTY_ITEMS;
  const responseRef = data?.source.ref ?? "";

  useEffect(() => {
    if (openId) void fetchDetail(openId);
  }, [fetchDetail, openId]);

  const nextTaskId = useMemo(() => computeNextTaskId(allItems), [allItems]);
  const nextTask = nextTaskId ? allItems.find((it) => it.id === nextTaskId) : null;

  useEffect(() => {
    if (nextTaskId && !detailById[nextTaskId]) void fetchDetail(nextTaskId);
  }, [nextTaskId, detailById, fetchDetail]);

  const nextDetail = nextTaskId ? detailById[nextTaskId] : undefined;
  const commissionPromptForNext = nextDetail ? buildCommissionPrompt(nextDetail) : undefined;

  const projects = useMemo(() => {
    const set = new Set<string>();
    for (const it of allItems) {
      const p = projectFromRoot(it.root);
      if (p !== "Orchestration") set.add(p);
    }
    return [...set].sort();
  }, [allItems]);

  const byStatus = useMemo(() => {
    const map: Record<string, OrchestrationItem[]> = {};
    for (const item of allItems) (map[item.status] ??= []).push(item);
    return map;
  }, [allItems]);

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

  const signals = useMemo(() => deriveQueueSignals(allItems, data?.contract_health, nowSec), [allItems, data?.contract_health, nowSec]);
  const activeTotal = allItems.filter((item) => item.status !== "done").length;
  const selectedItem = openId ? allItems.find((item) => item.id === openId) : undefined;
  const detail = openId ? detailById[openId] : undefined;
  const drawerNextAction = selectedItem ? nextActionForItem(selectedItem, allItems) : "";

  const detailReadiness = selectedItem ? readinessChip(readiness(selectedItem, allItems)) : null;
  const detailChips: DetailChip[] = [
    ...(detailReadiness ? [detailReadiness] : []),
    ...(selectedItem && !isKnownStatus(selectedItem.status) ? [{ label: `${de.orchestrator.statusDrift}: ${selectedItem.status}`, tone: "red" as const }] : []),
    ...((selectedItem?.dependsOn ?? detail?.dependsOn ?? []).length ? [{ label: de.orchestrator.dependsOn((selectedItem?.dependsOn ?? detail?.dependsOn ?? []).length), tone: "cyan" as const }] : []),
  ];
  const detailFields: Array<{ label: string; value: string }> = detail
    ? (
        [
          detail.status ? { label: de.orchestrator.colStatus, value: detail.status } : null,
          detail.priority ? { label: de.orchestrator.priority, value: detail.priority } : null,
          detail.owner ? { label: de.orchestrator.colOwner, value: detail.owner } : null,
          { label: de.orchestrator.planGate, value: detail.planGate ? de.orchestrator.yes : de.orchestrator.no },
          detail.gate ? { label: de.orchestrator.gate, value: detail.gate } : null,
          detail.created ? { label: de.orchestrator.created, value: detail.created } : null,
        ] as Array<{ label: string; value: string } | null>
      ).filter((f): f is { label: string; value: string } => f !== null)
    : [];

  const sourceRef = openId
    ? [
        { label: de.orchestrator.colSource, value: detail?.source || selectedItem?.source || sourceLabel(selectedItem ?? ({ root: detail?.root ?? "" } as OrchestrationItem)) },
        { label: "Ref", value: responseRef },
        { label: de.orchestrator.detailSpec, value: sourcePath(openId) },
        { label: de.orchestrator.root, value: detail?.root || selectedItem?.root || "" },
      ]
    : [];
  const proofTimeline = detail?.proofs?.length ? detail.proofs : [detail?.lastProof || selectedItem?.lastProof || ""].filter(Boolean);
  const commissionPromptForDrawer = detail ? buildCommissionPrompt(detail) : undefined;
  const operatorBriefForDrawer = buildOperatorBrief(selectedItem, detail, drawerNextAction, responseRef);
  const hasContractDrift = Boolean(data && signals.contractDrift > 0);

  return (
    <div className="space-y-4">
      <section className="hc-card flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="hc-eyebrow">{de.orchestrator.eyebrow}</p>
          <h2 className="mt-1 text-xl font-semibold text-white">
            {de.orchestrator.title} · {activeTotal} aktiv
          </h2>
          <p className="mt-1 text-xs hc-soft">{de.orchestrator.subtitle}</p>
        </div>
        <div className="text-left text-xs hc-soft sm:text-right">
          <div>{backlog.loading && !data ? de.orchestrator.loading : de.orchestrator.updatedAt(clockLabel(nowSec))}</div>
          {data ? (
            <div className="mt-1 hc-dim">
              {data.contract_health.source_count} Quellen · {data.contract_health.counted_sum} gezählt · {data.source.ref}
            </div>
          ) : null}
        </div>
      </section>

      {backlog.error ? <ToneCallout tone="red">{de.orchestrator.error}</ToneCallout> : null}
      {data?.error ? <ToneCallout tone="amber">{de.orchestrator.sourceMissing}</ToneCallout> : null}
      {hasContractDrift && data ? <ContractDriftCallout data={data} /> : null}

      {allItems.length > 0 ? <SignalStrip signals={signals} /> : null}

      {nextTask ? (
        <CommissionBanner
          nextId={nextTask.id}
          nextTitle={nextTask.title}
          prompt={commissionPromptForNext}
        />
      ) : allItems.length > 0 ? (
        <ToneCallout tone="zinc">{de.orchestrator.noNextTask}</ToneCallout>
      ) : null}

      {allItems.length > 0 ? (
        <ControlsBar
          q={q} onQ={setQ}
          filterPriority={filterPriority} onFilterPriority={setFilterPriority}
          filterProject={filterProject} onFilterProject={setFilterProject}
          filterPlanGate={filterPlanGate} onFilterPlanGate={setFilterPlanGate}
          filterReadiness={filterReadiness} onFilterReadiness={setFilterReadiness}
          sortKey={sortKey} onSort={setSortKey}
          projects={projects}
          viewMode={viewMode}
          onViewMode={setViewMode}
        />
      ) : null}

      <OrchestratorQueueTable
        items={filteredActive}
        allItems={allItems}
        nowSec={nowSec}
        nextTaskId={nextTaskId}
        onOpen={setOpenId}
      />

      {viewMode === "board" ? (
        <div className={cn("grid", gap)} style={{ gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))" }}>
          {ACTIVE_COLUMNS.map((col) => {
            const items = col.key === "__drift"
              ? filteredActive.filter((item) => item.status !== "done" && !isKnownStatus(item.status))
              : filteredByStatus[col.key] ?? [];
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
      ) : null}

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
              className="rounded-md border border-white/10 px-2 py-1 text-xs hc-soft hover:bg-white/5 focus:outline-none focus:ring-2 focus:ring-cyan-400/60"
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

      {openId ? (
        <BacklogDetailDrawer
          title={selectedItem?.title ?? detail?.title ?? openId}
          id={openId}
          body={detail?.body ?? ""}
          chips={detailChips}
          fields={detailFields}
          proofTimeline={proofTimeline}
          nextAction={drawerNextAction}
          sourceRef={sourceRef}
          links={detail?.links}
          loading={loadingId === openId}
          error={errorById[openId] || detail?.error}
          commissionPrompt={commissionPromptForDrawer}
          operatorBrief={operatorBriefForDrawer}
          onClose={() => setOpenId(null)}
        />
      ) : null}
    </div>
  );
}
