import { useMemo, useState } from "react";

import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import { useOrchestrationBacklog } from "../hooks/useControlData";
import { StatusPill, ToneCallout } from "../components/atoms";
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

function ItemCard({ item, nowSec }: { item: OrchestrationItem; nowSec: number }) {
  return (
    <div className="rounded-lg border border-white/10 p-3">
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium leading-snug text-white">{item.title}</p>
        <span className="hc-mono shrink-0 text-[11px] hc-dim">{item.id}</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <StatusPill tone={PRIORITY_TONE[item.priority] ?? "zinc"} label={item.priority} />
        {item.planGate ? <StatusPill tone="indigo" label={de.orchestrator.planGate} /> : null}
        {item.dependsOn.length > 0 ? <StatusPill tone="cyan" label={de.orchestrator.dependsOn(item.dependsOn.length)} /> : null}
        <span className="ml-auto text-[11px] hc-soft">{relLabel(item.created, nowSec)}</span>
      </div>
    </div>
  );
}

export function OrchestratorBacklogView({ density }: { density: Density }) {
  const backlog = useOrchestrationBacklog();
  const [showAllDone, setShowAllDone] = useState(false);
  const data = backlog.data;
  const nowSec = data?.checked_at ?? Math.floor(Date.now() / 1000);
  const gap = density === "compact" ? "gap-3" : "gap-4";

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
          <div>{backlog.loading && !data ? "lädt …" : de.orchestrator.updatedAt(clockLabel(nowSec))}</div>
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
                items.map((item) => <ItemCard key={item.id} item={item} nowSec={nowSec} />)
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
              <ItemCard key={item.id} item={item} nowSec={nowSec} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
