import { useMemo, useState } from "react";

import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import { useBacklog } from "../hooks/useControlData";
import { StatusPill, ToneCallout } from "../components/atoms";
import type { Density } from "../hooks/useDensity";
import type { BacklogItem } from "../lib/schemas";
import type { ToneName } from "../lib/types";

type Status = BacklogItem["status"];

// Active columns, ordered by what the operator cares about first: what's running,
// what's stuck, then the queue, then deferred. `done` is handled separately (a
// collapsed strip) so the active picture stays calm — the user's "aktiv-fokussiert".
const ACTIVE_COLUMNS: Array<{ key: Exclude<Status, "done">; label: string; tone: ToneName }> = [
  { key: "in_progress", label: de.backlog.colInProgress, tone: "violet" },
  { key: "blocked", label: de.backlog.colBlocked, tone: "red" },
  { key: "now", label: de.backlog.colNow, tone: "sky" },
  { key: "next", label: de.backlog.colNext, tone: "indigo" },
  { key: "later", label: de.backlog.colLater, tone: "zinc" },
];

const OWNER_TONE: Record<string, ToneName> = {
  claude: "violet",
  hermes: "cyan",
  codex: "amber",
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

function clockLabel(nowSec: number): string {
  return new Date(nowSec * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}

function ItemCard({ item, nowSec }: { item: BacklogItem; nowSec: number }) {
  return (
    <div className={cn("rounded-lg border p-3", item.stale ? "border-red-500/40 bg-red-500/10" : "border-white/10")}>
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium leading-snug text-white">{item.title}</p>
        <span className="hc-mono shrink-0 text-[11px] hc-dim">{item.id}</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <StatusPill tone={OWNER_TONE[item.owner] ?? "zinc"} label={item.owner} />
        {item.area ? <StatusPill tone="cyan" label={item.area} /> : null}
        <StatusPill tone={RISK_TONE[item.risk] ?? "zinc"} label={item.risk} />
        <span className={cn("ml-auto text-[11px]", item.stale ? "text-red-300" : "hc-soft")}>
          {item.stale ? `${de.backlog.staleBadge} · ` : ""}
          {relLabel(item.updated, nowSec)}
        </span>
      </div>
    </div>
  );
}

export function BacklogView({ density }: { density: Density }) {
  const backlog = useBacklog();
  const [showAllDone, setShowAllDone] = useState(false);
  const data = backlog.data;
  const nowSec = data?.checked_at ?? Math.floor(Date.now() / 1000);
  const gap = density === "compact" ? "gap-3" : "gap-4";

  const byStatus = useMemo(() => {
    const map: Record<string, BacklogItem[]> = {};
    for (const item of data?.items ?? []) (map[item.status] ??= []).push(item);
    return map;
  }, [data]);

  const doneItems = useMemo(() => {
    const arr = [...(byStatus.done ?? [])];
    // newest first by `updated`, then id desc as a stable tiebreaker
    arr.sort((a, b) => (b.updated.localeCompare(a.updated)) || b.id.localeCompare(a.id));
    return arr;
  }, [byStatus]);

  const counts = data?.counts;
  const activeTotal = counts
    ? counts.now + counts.next + counts.in_progress + counts.blocked + counts.later
    : 0;

  return (
    <div className="space-y-5">
      <section className="hc-card flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="hc-eyebrow">{de.backlog.eyebrow}</p>
          <h2 className="mt-1 text-xl font-semibold text-white">
            {de.backlog.title} · {activeTotal} aktiv
          </h2>
          <p className="mt-1 text-xs hc-soft">{de.backlog.subtitle}</p>
        </div>
        <div className="text-right text-xs hc-soft">
          <div>{backlog.loading && !data ? "lädt …" : de.backlog.updatedAt(clockLabel(nowSec))}</div>
          {counts ? (
            <div className="mt-1 hc-dim">
              {counts.done} erledigt · {data?.source.count ?? 0} gesamt
            </div>
          ) : null}
        </div>
      </section>

      {backlog.error ? <ToneCallout tone="red">{de.backlog.error}</ToneCallout> : null}
      {data?.error ? <ToneCallout tone="amber">{de.backlog.sourceMissing}</ToneCallout> : null}

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
                <p className="py-3 text-center text-xs hc-dim">{de.backlog.emptyColumn}</p>
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
            <StatusPill tone="emerald" label={de.backlog.colDone} />
            <span className="hc-mono text-xs hc-dim">{doneItems.length}</span>
            <span className="hidden text-xs hc-dim sm:inline">· {de.backlog.doneRecentHint}</span>
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
              <ItemCard key={item.id} item={item} nowSec={nowSec} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
