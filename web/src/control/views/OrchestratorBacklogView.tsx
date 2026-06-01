import { useEffect, useMemo, useState } from "react";

import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import { useOrchestrationBacklog, useOrchestrationBacklogDetail } from "../hooks/useControlData";
import { BacklogDetailDrawer } from "../components/BacklogDetailDrawer";
import { StatusPill, ToneCallout } from "../components/atoms";
import { readiness } from "../lib/orchestration";
import type { Readiness } from "../lib/orchestration";
import type { Density } from "../hooks/useDensity";
import type { OrchestrationItem } from "../lib/schemas";
import type { ToneName } from "../lib/types";

type Status = OrchestrationItem["status"];

// Active columns in lifecycle order — what's running first, then what's waiting
// on review, then the queue, then the unscheduled pile. `done` is a collapsed
// strip below so the active picture stays calm (mirrors the FO board).
const ACTIVE_COLUMNS: Array<{ key: Exclude<Status, "done">; label: string; tone: ToneName }> = [
  { key: "doing", label: de.orchestrator.colDoing, tone: "violet" },
  { key: "review", label: de.orchestrator.colReview, tone: "amber" },
  { key: "todo", label: de.orchestrator.colTodo, tone: "sky" },
  { key: "backlog", label: de.orchestrator.colBacklog, tone: "zinc" },
];

const PRIORITY_TONE: Record<string, ToneName> = { high: "red", medium: "amber", low: "zinc" };

type DetailChip = { label: string; tone?: ToneName };

function readinessChip(value: Readiness): DetailChip | null {
  if (value.state === "ready") return { tone: "emerald", label: de.orchestrator.ready };
  if (value.state === "blocked") {
    return { tone: "red", label: `${de.orchestrator.blockedBy} ${value.blockedBy.join(", ")}` };
  }
  return null;
}

function relLabel(created: string, nowSec: number): string {
  if (!created) return "—";
  const t = Date.parse(`${created}T00:00:00Z`);
  if (Number.isNaN(t)) return created;
  const days = Math.floor((nowSec * 1000 - t) / 86_400_000);
  if (days <= 0) return "heute";
  if (days === 1) return "gestern";
  if (days < 7) return `vor ${days} T`;
  if (days < 30) return `vor ${Math.floor(days / 7)} Wo`;
  return `vor ${Math.floor(days / 30)} Mon`;
}

function clockLabel(nowSec: number): string {
  return new Date(nowSec * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}

function ItemCard({
  item,
  allItems,
  nowSec,
  onOpen,
}: {
  item: OrchestrationItem;
  allItems: OrchestrationItem[];
  nowSec: number;
  onOpen: (id: string) => void;
}) {
  const readyChip = readinessChip(readiness(item, allItems));

  return (
    <div
      role="button"
      tabIndex={0}
      className="cursor-pointer rounded-lg border border-white/10 p-3 transition hover:border-white/20 hover:bg-white/[.03] focus:outline-none focus:ring-2 focus:ring-cyan-400/60"
      onClick={() => onOpen(item.id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen(item.id);
        }
      }}
    >
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium leading-snug text-white">{item.title}</p>
        <span className="hc-mono shrink-0 text-[11px] hc-dim">{item.id}</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {readyChip ? <StatusPill tone={readyChip.tone ?? "zinc"} label={readyChip.label} /> : null}
        <StatusPill tone={PRIORITY_TONE[item.priority] ?? "zinc"} label={item.priority} />
        {item.planGate ? <StatusPill tone="indigo" label={de.orchestrator.planGate} /> : null}
        {item.dependsOn.map((id) => (
          <StatusPill key={id} tone="cyan" label={id} />
        ))}
        <span className="ml-auto text-[11px] hc-soft">{relLabel(item.created, nowSec)}</span>
      </div>
    </div>
  );
}

export function OrchestratorBacklogView({ density }: { density: Density }) {
  const backlog = useOrchestrationBacklog();
  const { detailById, errorById, loadingId, fetch: fetchDetail } = useOrchestrationBacklogDetail();
  const [showAllDone, setShowAllDone] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const data = backlog.data;
  const nowSec = data?.checked_at ?? Math.floor(Date.now() / 1000);
  const gap = density === "compact" ? "gap-3" : "gap-4";

  useEffect(() => {
    if (openId) void fetchDetail(openId);
  }, [fetchDetail, openId]);

  const byStatus = useMemo(() => {
    const map: Record<string, OrchestrationItem[]> = {};
    for (const item of data?.items ?? []) (map[item.status] ??= []).push(item);
    return map;
  }, [data]);

  const doneItems = useMemo(() => {
    const arr = [...(byStatus.done ?? [])];
    // newest first by `created`, then id desc as a stable tiebreaker
    arr.sort((a, b) => (b.created.localeCompare(a.created)) || b.id.localeCompare(a.id));
    return arr;
  }, [byStatus]);

  const counts = data?.counts;
  const activeTotal = counts ? counts.doing + counts.review + counts.todo + counts.backlog : 0;
  const allItems = data?.items ?? [];
  const selectedItem = openId ? allItems.find((item) => item.id === openId) : undefined;
  const detail = openId ? detailById[openId] : undefined;
  const detailReadiness = selectedItem ? readinessChip(readiness(selectedItem, allItems)) : null;
  const detailChips: DetailChip[] = [
    ...(detailReadiness ? [detailReadiness] : []),
    ...((selectedItem?.dependsOn ?? detail?.dependsOn ?? []).map((id) => ({ tone: "cyan" as const, label: id }))),
  ];
  const detailFields: Array<{ label: string; value: string }> = detail
    ? ([
        detail.priority ? { label: de.orchestrator.priority, value: detail.priority } : null,
        { label: de.orchestrator.planGate, value: detail.planGate ? de.orchestrator.yes : de.orchestrator.no },
        detail.gate ? { label: de.orchestrator.gate, value: detail.gate } : null,
        detail.root ? { label: de.orchestrator.root, value: detail.root } : null,
        detail.created ? { label: de.orchestrator.created, value: detail.created } : null,
      ] as Array<{ label: string; value: string } | null>).filter(
        (field): field is { label: string; value: string } => field !== null,
      )
    : [];

  return (
    <div className="space-y-5">
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

      <div className={cn("grid", gap)} style={{ gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))" }}>
        {ACTIVE_COLUMNS.map((col) => {
          const items = byStatus[col.key] ?? [];
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
                  <ItemCard key={item.id} item={item} allItems={allItems} nowSec={nowSec} onOpen={setOpenId} />
                ))
              )}
            </section>
          );
        })}
      </div>

      <section className="hc-card p-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <StatusPill tone="emerald" label={de.orchestrator.colDone} />
            <span className="hc-mono text-xs hc-dim">{doneItems.length}</span>
            <span className="hidden text-xs hc-dim sm:inline">· {de.orchestrator.doneRecentHint}</span>
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
              <ItemCard key={item.id} item={item} allItems={allItems} nowSec={nowSec} onOpen={setOpenId} />
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
          loading={loadingId === openId}
          error={errorById[openId] || detail?.error}
          onClose={() => setOpenId(null)}
        />
      ) : null}
    </div>
  );
}
