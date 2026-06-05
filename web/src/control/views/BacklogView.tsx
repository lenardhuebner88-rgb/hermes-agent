import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ClipboardCopy, ExternalLink, Keyboard, LayoutGrid, List, X } from "lucide-react";

import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import { useBacklog, useBacklogDetail } from "../hooks/useControlData";
import { FoBacklogCard } from "../components/FoBacklogCard";
import { StatusPill, ToneCallout } from "../components/atoms";
import {
  buildFoCommissionPrompt,
  computeNextFoTaskId,
  FO_REASON_LABELS,
  filterFoItems,
  foHealthStripCounts,
  matchesFoQuickView,
  nextActionForFoItem,
  ownerLoadSummary,
  qualityFlagsForFoItem,
  queueStateForFoItem,
  rankFoItems,
  rankedQueueWithReasons,
  reasonCodesForFoItem,
  sortFoItems,
  staleSignalForFoItem,
} from "../lib/foBacklog";
import type { FoQuickView, FoReasonCode, FoSortKey } from "../lib/foBacklog";
import type { Density } from "../hooks/useDensity";
import type { BacklogContractHealth, BacklogDetail, BacklogItem } from "../lib/schemas";
import type { ToneName } from "../lib/types";

const QUICK_VIEWS: Array<{ id: FoQuickView; label: string }> = [
  { id: "all", label: "Alle" },
  { id: "ready", label: "Commission-ready" },
  { id: "groom", label: "Grooming nötig" },
  { id: "stale", label: "Stale" },
  { id: "unowned", label: "Ohne Owner" },
];

const VIEW_STORAGE_KEY = "fo-backlog-view-v1";

export function ReasonChips({ codes, max = 4 }: { codes: FoReasonCode[]; max?: number }) {
  if (!codes.length) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {codes.slice(0, max).map((code) => {
        const negative = code.startsWith("penalty_") || code === "needs_grooming" || code === "drift" || code === "missing_acceptance" || code === "missing_next_action";
        return (
          <span
            key={code}
            className={cn(
              "rounded-sm px-1.5 py-0.5 text-[10px] font-medium",
              negative ? "bg-amber-500/10 text-amber-200" : "bg-cyan-500/10 text-cyan-200",
            )}
          >
            {FO_REASON_LABELS[code] ?? code}
          </span>
        );
      })}
    </div>
  );
}

type Status = "now" | "next" | "in_progress" | "blocked" | "later" | "done";
type ViewMode = "queue" | "board";

const ACTIVE_COLUMNS: Array<{ key: Exclude<Status, "done">; label: string; tone: ToneName }> = [
  { key: "now", label: de.backlog.colNow, tone: "sky" },
  { key: "next", label: de.backlog.colNext, tone: "indigo" },
  { key: "in_progress", label: de.backlog.colInProgress, tone: "violet" },
  { key: "blocked", label: de.backlog.colBlocked, tone: "red" },
  { key: "later", label: de.backlog.colLater, tone: "zinc" },
];

const STATUS_TONE: Record<string, ToneName> = {
  now: "sky",
  next: "indigo",
  in_progress: "violet",
  blocked: "red",
  later: "zinc",
  done: "emerald",
};
const RISK_TONE: Record<string, ToneName> = { high: "red", medium: "amber", low: "zinc" };
const OWNER_TONE: Record<string, ToneName> = { claude: "violet", hermes: "cyan", piet: "emerald", codex: "sky", unassigned: "zinc" };
const EMPTY_ITEMS: BacklogItem[] = [];

function clockLabel(nowSec: number): string {
  return new Date(nowSec * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}

function relLabel(updated: string, nowSec: number): string {
  if (!updated) return "-";
  const t = Date.parse(`${updated.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(t)) return updated;
  const days = Math.floor((nowSec * 1000 - t) / 86_400_000);
  if (days <= 0) return "heute";
  if (days === 1) return "gestern";
  if (days < 7) return `vor ${days} T`;
  if (days < 30) return `vor ${Math.floor(days / 7)} Wo`;
  return `vor ${Math.floor(days / 30)} Mon`;
}

function sourceRef(item: BacklogItem): string {
  return item.source_path || `backlog/items/${item.id}.md`;
}

function operatorBrief(item: BacklogItem, detail?: BacklogDetail): string {
  return [
    `FO Backlog ${item.id}: ${item.title}`,
    `Status/Risk/Owner: ${item.status} / ${item.risk || "-"} / ${item.owner || "-"}`,
    `Area: ${item.area || "-"}`,
    `Next Action: ${nextActionForFoItem(item, detail)}`,
    `Source: ${detail?.source_path || sourceRef(item)}`,
    detail?.source_ref ? `Ref: ${detail.source_ref}` : null,
  ].filter(Boolean).join("\n");
}

function CopyButton({ text, label, copiedLabel }: { text: string | undefined; label: string; copiedLabel: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked */
    }
  };
  return (
    <button
      type="button"
      onClick={copy}
      disabled={!text}
      className={cn(
        "inline-flex min-h-10 items-center justify-center gap-2 rounded-md border px-3 text-sm font-medium transition",
        copied
          ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
          : "border-cyan-500/30 bg-cyan-500/10 text-cyan-200 hover:bg-cyan-500/15 disabled:cursor-not-allowed disabled:border-white/10 disabled:text-zinc-500",
      )}
    >
      {copied ? <Check className="h-4 w-4" /> : <ClipboardCopy className="h-4 w-4" />}
      {copied ? copiedLabel : label}
    </button>
  );
}

function SectionLines({ title, lines, fallback }: { title: string; lines: string[] | undefined; fallback: string }) {
  const visible = lines?.filter((line) => line.trim() !== "") ?? [];
  return (
    <section className="border-t border-[var(--hc-border)] pt-3">
      <h3 className="text-[11px] font-semibold uppercase text-zinc-400">{title}</h3>
      {visible.length ? (
        <ul className="mt-2 space-y-1.5">
          {visible.map((line, index) => (
            <li key={`${title}-${index}-${line}`} className="break-words text-sm text-zinc-100">{line}</li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-sm hc-soft">{fallback}</p>
      )}
    </section>
  );
}

export function FoHealthStrip({ items, contractHealth }: { items: BacklogItem[]; contractHealth?: BacklogContractHealth }) {
  const counts = foHealthStripCounts(items, contractHealth);
  const cells: Array<{ label: string; value: number; tone: ToneName }> = [
    { label: "Now", value: counts.now, tone: "sky" },
    { label: "Next Ready", value: counts.nextReady, tone: "indigo" },
    { label: "Blocked", value: counts.blocked, tone: "red" },
    { label: "Unowned", value: counts.unowned, tone: "amber" },
    { label: "Stale", value: counts.stale, tone: "red" },
    { label: "High Risk", value: counts.highRisk, tone: "red" },
    { label: "Contract Drift", value: counts.contractDrift, tone: counts.contractDrift ? "amber" : "zinc" },
    { label: "Missing Acceptance", value: counts.missingAcceptance, tone: counts.missingAcceptance ? "amber" : "zinc" },
  ];
  return (
    <section className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8" aria-label="FO Contract Health">
      {cells.map((cell) => (
        <div key={cell.label} className="min-w-0 rounded-md border border-[var(--hc-border)] bg-white/[.025] px-3 py-2">
          <div className="truncate text-[11px] font-semibold uppercase text-zinc-400">{cell.label}</div>
          <div className={cn("mt-1 hc-mono text-lg font-semibold", cell.tone === "red" ? "text-red-200" : cell.tone === "amber" ? "text-amber-200" : cell.tone === "sky" ? "text-sky-200" : cell.tone === "indigo" ? "text-indigo-200" : "text-zinc-200")}>{cell.value}</div>
        </div>
      ))}
    </section>
  );
}

export function FoBacklogQueueTable({
  items,
  nowSec,
  nextTaskId,
  activeId = null,
  detailById = {},
  onOpen,
}: {
  items: BacklogItem[];
  nowSec: number;
  nextTaskId: string | null;
  activeId?: string | null;
  detailById?: Record<string, BacklogDetail | undefined>;
  onOpen: (id: string) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-md border border-[var(--hc-border)]">
      <table className="w-full table-fixed border-collapse text-left text-sm">
        <thead className="bg-white/[.035] text-[11px] uppercase text-zinc-400">
          <tr>
            <th className="w-[30%] px-3 py-2">Title</th>
            <th className="w-[9%] px-3 py-2">Status</th>
            <th className="hidden w-[8%] px-3 py-2 md:table-cell">Risk</th>
            <th className="hidden w-[10%] px-3 py-2 lg:table-cell">Owner</th>
            <th className="hidden w-[10%] px-3 py-2 xl:table-cell">Area</th>
            <th className="hidden w-[10%] px-3 py-2 md:table-cell">Age/Updated</th>
            <th className="hidden w-[10%] px-3 py-2 lg:table-cell">Stale/Proof</th>
            <th className="hidden w-[13%] px-3 py-2 xl:table-cell">Source/Id</th>
            <th className="w-[28%] px-3 py-2">Next Action</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const detail = detailById[item.id];
            const queueState = queueStateForFoItem(item);
            const flags = qualityFlagsForFoItem(item, detail);
            const stale = staleSignalForFoItem(item, nowSec);
            return (
              <tr
                key={item.id}
                data-fo-row={item.id}
                tabIndex={0}
                aria-current={item.id === activeId ? "true" : undefined}
                onClick={() => onOpen(item.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onOpen(item.id);
                  }
                }}
                className={cn(
                  "cursor-pointer border-t border-[var(--hc-border)] align-top outline-none hover:bg-white/[.035] focus-visible:bg-white/[.045]",
                  item.id === nextTaskId && "bg-cyan-500/[.06]",
                  item.id === activeId && "bg-white/[.05] ring-2 ring-inset ring-cyan-400/70",
                )}
              >
                <td className="px-3 py-2">
                  <div className="min-w-0">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="truncate font-medium text-white">{item.title}</span>
                      {item.id === nextTaskId ? <span className="shrink-0 rounded-sm bg-cyan-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-cyan-200">NEXT</span> : null}
                    </div>
                    {item.excerpt ? <p className="mt-1 line-clamp-2 text-xs hc-dim">{item.excerpt}</p> : null}
                    {flags.length ? (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {flags.slice(0, 3).map((flag) => <span key={`${item.id}-${flag.kind}`} className={cn("rounded-sm px-1.5 py-0.5 text-[10px]", flag.severity === "risk" ? "bg-amber-500/10 text-amber-200" : "bg-zinc-500/10 text-zinc-300")}>{flag.label}</span>)}
                      </div>
                    ) : null}
                  </div>
                </td>
                <td className="px-3 py-2"><StatusPill tone={queueState.state === "drift" ? "amber" : STATUS_TONE[queueState.state]} label={item.status || "missing"} /></td>
                <td className="hidden px-3 py-2 md:table-cell"><StatusPill tone={RISK_TONE[item.risk] ?? "amber"} label={item.risk || "missing"} /></td>
                <td className="hidden px-3 py-2 lg:table-cell"><StatusPill tone={OWNER_TONE[item.owner] ?? "amber"} label={item.owner || "missing"} /></td>
                <td className="hidden px-3 py-2 text-zinc-200 xl:table-cell">{item.area || "-"}</td>
                <td className="hidden px-3 py-2 md:table-cell"><span className="hc-mono text-xs hc-soft">{relLabel(item.updated, nowSec)}</span></td>
                <td className="hidden px-3 py-2 lg:table-cell"><span className={cn("text-xs", stale.state === "stale" ? "text-red-200" : stale.state === "missing_update" ? "text-amber-200" : "hc-soft")}>{stale.label}</span></td>
                <td className="hidden px-3 py-2 xl:table-cell"><span className="block truncate hc-mono text-[11px] text-zinc-400">{sourceRef(item)}</span><span className="hc-mono text-[11px] text-zinc-500">{item.id}</span></td>
                <td className="px-3 py-2"><p className="line-clamp-3 text-sm text-zinc-100">{nextActionForFoItem(item, detail)}</p></td>
              </tr>
            );
          })}
        </tbody>
      </table>
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
    <section className="rounded-md border border-[var(--hc-border)] bg-white/[.02] p-3">
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
    </section>
  );
}

function FoDetailDrawer({
  item,
  detail,
  loading,
  error,
  commissionPrompt,
  onClose,
}: {
  item: BacklogItem;
  detail?: BacklogDetail;
  loading: boolean;
  error?: string;
  commissionPrompt?: string;
  onClose: () => void;
}) {
  const brief = operatorBrief(item, detail);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50">
      <button type="button" className="absolute inset-0 bg-black/60" aria-label="Schließen" onClick={onClose} />
      <aside role="dialog" aria-modal="true" aria-labelledby="fo-detail-title" className="hc-card absolute right-0 top-0 flex h-full w-full max-w-xl flex-col rounded-none border-y-0 border-l border-[var(--hc-border)] shadow-2xl">
        <header className="flex items-start justify-between gap-3 border-b border-[var(--hc-border)] p-5">
          <div className="min-w-0">
            <h2 id="fo-detail-title" className="text-lg font-semibold text-white">{item.title}</h2>
            <p className="mt-1 truncate text-xs hc-mono hc-dim">{detail?.source_path || sourceRef(item)}</p>
          </div>
          <button type="button" className="grid h-9 w-9 shrink-0 place-items-center rounded-md border border-white/10 bg-white/[.03] text-zinc-200 hover:bg-white/[.07]" aria-label="Schließen" onClick={onClose}>
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
          {error ? <ToneCallout tone="red">{error}</ToneCallout> : null}
          {loading && !detail ? <ToneCallout tone="zinc">{de.backlog.loading}</ToneCallout> : null}

          <section className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {[["Status", item.status], ["Risk", item.risk], ["Owner", item.owner], ["Area", item.area]].map(([label, value]) => (
              <div key={label} className="rounded-md border border-[var(--hc-border)] bg-white/[.025] px-3 py-2">
                <div className="text-[10px] font-semibold uppercase text-zinc-500">{label}</div>
                <div className="mt-1 truncate text-sm text-zinc-100">{value || "-"}</div>
              </div>
            ))}
          </section>

          <section className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-2">
            <h3 className="text-[11px] font-semibold uppercase text-emerald-200">Next Action</h3>
            <p className="mt-1 text-sm text-white">{nextActionForFoItem(item, detail)}</p>
          </section>

          <SectionLines title="Decision / Why now" lines={detail?.decision} fallback={item.excerpt || "Keine explizite Entscheidung im Body gefunden."} />
          <SectionLines title="Acceptance Criteria" lines={detail?.acceptance_criteria} fallback="Keine Akzeptanzkriterien gefunden." />
          <SectionLines title="Current Evidence / Last Proof" lines={detail?.proofs} fallback={item.result || "Kein letzter Beleg gefunden."} />
          <SectionLines title="Blockers" lines={detail?.blockers} fallback="Keine Blocker im Body gefunden." />

          <section className="border-t border-[var(--hc-border)] pt-3">
            <h3 className="text-[11px] font-semibold uppercase text-zinc-400">Source path/ref</h3>
            <dl className="mt-2 space-y-2 text-sm">
              <div><dt className="text-[10px] uppercase text-zinc-500">Path</dt><dd className="break-words hc-mono text-zinc-100">{detail?.source_path || sourceRef(item)}</dd></div>
              <div><dt className="text-[10px] uppercase text-zinc-500">Ref</dt><dd className="break-words hc-mono text-zinc-100">{detail?.source_ref || "git:origin/main"}</dd></div>
            </dl>
          </section>

          <div className="grid gap-2 sm:grid-cols-2">
            <CopyButton text={brief} label="Copy operator brief" copiedLabel="Brief kopiert" />
            <CopyButton text={commissionPrompt} label="Copy implementation prompt" copiedLabel={de.backlog.commissionCopied} />
          </div>

          {detail?.links?.length ? (
            <section className="border-t border-[var(--hc-border)] pt-3">
              <h3 className="text-[11px] font-semibold uppercase text-zinc-400">Links</h3>
              <div className="mt-2 space-y-1">
                {detail.links.map((link) => (
                  <a key={`${link.label}-${link.href}`} href={link.href} target="_blank" rel="noreferrer" className="flex min-w-0 items-center gap-2 text-sm text-cyan-200 hover:text-cyan-100">
                    <ExternalLink className="h-3.5 w-3.5 shrink-0" />
                    <span className="truncate">{link.label}</span>
                  </a>
                ))}
              </div>
            </section>
          ) : null}
        </div>
      </aside>
    </div>
  );
}

type PersistedView = {
  viewMode?: ViewMode;
  filterRisk?: string;
  filterStale?: boolean;
  filterOwner?: string;
  sortKey?: FoSortKey;
  quickView?: FoQuickView;
};

function loadPersistedView(): PersistedView {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(VIEW_STORAGE_KEY);
    return raw ? (JSON.parse(raw) as PersistedView) : {};
  } catch {
    return {};
  }
}

export function BacklogView({ density }: { density: Density }) {
  const backlog = useBacklog();
  const { detailById, errorById, loadingId, fetch: fetchDetail } = useBacklogDetail();
  const [persisted] = useState(loadPersistedView);
  const [showAllDone, setShowAllDone] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>(persisted.viewMode ?? "queue");
  const [q, setQ] = useState("");
  const [filterOwner, setFilterOwner] = useState(persisted.filterOwner ?? "");
  const [filterRisk, setFilterRisk] = useState(persisted.filterRisk ?? "");
  const [filterStale, setFilterStale] = useState(persisted.filterStale ?? false);
  const [sortKey, setSortKey] = useState<FoSortKey>(persisted.sortKey ?? "status");
  const [quickView, setQuickView] = useState<FoQuickView>(persisted.quickView ?? "all");
  const [activeIndex, setActiveIndex] = useState(-1);
  const [showHelp, setShowHelp] = useState(false);
  const [fallbackNowSec] = useState(() => Math.floor(Date.now() / 1000));
  const queueRef = useRef<HTMLDivElement>(null);

  // Persist the operator's working view (not the transient search text).
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(
        VIEW_STORAGE_KEY,
        JSON.stringify({ viewMode, filterRisk, filterStale, filterOwner, sortKey, quickView }),
      );
    } catch {
      /* storage blocked / quota — view persistence is best-effort */
    }
  }, [viewMode, filterRisk, filterStale, filterOwner, sortKey, quickView]);

  const data = backlog.data;
  const allItems = data?.items ?? EMPTY_ITEMS;
  const nowSec = data?.checked_at ?? fallbackNowSec;
  const gap = density === "compact" ? "gap-3" : "gap-4";

  useEffect(() => {
    if (openId) void fetchDetail(openId);
  }, [fetchDetail, openId]);

  const nextTaskId = useMemo(() => computeNextFoTaskId(allItems), [allItems]);
  const nextTask = nextTaskId ? allItems.find((item) => item.id === nextTaskId) : null;

  useEffect(() => {
    if (nextTaskId && !detailById[nextTaskId]) void fetchDetail(nextTaskId);
  }, [nextTaskId, detailById, fetchDetail]);

  const owners = useMemo(() => {
    const set = new Set<string>();
    for (const item of allItems) if (item.owner && item.owner !== "unassigned") set.add(item.owner);
    return [...set].sort();
  }, [allItems]);

  const byStatus = useMemo(() => {
    const map: Record<string, BacklogItem[]> = {};
    for (const item of allItems) (map[item.status] ??= []).push(item);
    return map;
  }, [allItems]);

  const filteredActive = useMemo(() => {
    const active = allItems.filter((item) => item.status !== "done" && matchesFoQuickView(item, quickView));
    const filtered = filterFoItems(active, q, {
      owner: filterOwner || undefined,
      risk: filterRisk || undefined,
      stale: filterStale || undefined,
    });
    const sorted = sortFoItems(filtered, sortKey);
    return sortKey === "risk" ? rankFoItems(sorted, nowSec) : sorted;
  }, [allItems, quickView, q, filterOwner, filterRisk, filterStale, sortKey, nowSec]);

  // Ranked active candidates + reason codes, computed once and reused by the next-task
  // spotlight and the compare-top-candidates strip so they cannot disagree.
  const ranked = useMemo(() => rankedQueueWithReasons(allItems, nowSec), [allItems, nowSec]);
  const topCandidates = useMemo(() => ranked.slice(0, 3), [ranked]);

  // Prefetch detail for the top candidates so their commission prompts are ready.
  useEffect(() => {
    for (const candidate of topCandidates) {
      if (!detailById[candidate.item.id]) void fetchDetail(candidate.item.id);
    }
  }, [topCandidates, detailById, fetchDetail]);

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

  const ownerLoad = useMemo(() => ownerLoadSummary(allItems).slice(0, 4), [allItems]);
  const counts = data?.counts;
  const activeTotal = counts ? counts.now + counts.next + counts.in_progress + counts.blocked + counts.later : allItems.filter((item) => item.status !== "done").length;
  const selectedItem = openId ? allItems.find((item) => item.id === openId) : undefined;
  const detail = openId ? detailById[openId] : undefined;
  const commissionPrompt = detail ? buildFoCommissionPrompt(detail) : undefined;

  // Clamp the roving selection to the current filtered set at render time (no effect →
  // no cascading-render lint). Movements below also clamp, so it self-corrects.
  const clampedIndex = activeIndex >= filteredActive.length ? filteredActive.length - 1 : activeIndex;
  const activeId = clampedIndex >= 0 ? (filteredActive[clampedIndex]?.id ?? null) : null;

  // Bring the roving row into view when it changes.
  useEffect(() => {
    if (!activeId || !queueRef.current) return;
    queueRef.current.querySelector<HTMLElement>(`[data-fo-row="${activeId}"]`)?.scrollIntoView({ block: "nearest" });
  }, [activeId]);

  // Queue keyboard nav: j/k move, Enter opens, ? toggles help. Ignores typing in inputs
  // and yields to the drawer (which owns Escape while open).
  const onQueueKey = useCallback(
    (event: KeyboardEvent) => {
      if (viewMode !== "queue" || openId) return;
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName)) return;
      if (event.key === "?") {
        event.preventDefault();
        setShowHelp((value) => !value);
        return;
      }
      if (event.key === "Escape" && showHelp) {
        setShowHelp(false);
        return;
      }
      if (!filteredActive.length) return;
      const last = filteredActive.length - 1;
      if (event.key === "j" || event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((index) => Math.min(last, Math.min(index, last) + 1));
      } else if (event.key === "k" || event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((index) => Math.max(0, Math.min(index, last) - 1));
      } else if (event.key === "Enter" && clampedIndex >= 0) {
        event.preventDefault();
        setOpenId(filteredActive[clampedIndex].id);
      }
    },
    [viewMode, openId, showHelp, filteredActive, clampedIndex],
  );

  useEffect(() => {
    window.addEventListener("keydown", onQueueKey);
    return () => window.removeEventListener("keydown", onQueueKey);
  }, [onQueueKey]);

  return (
    <div className="space-y-4">
      <section className="rounded-md border border-[var(--hc-border)] bg-white/[.02] p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <p className="hc-eyebrow">{de.backlog.eyebrow}</p>
            <h2 className="mt-1 text-xl font-semibold text-white">{de.backlog.title} · {activeTotal} aktiv</h2>
            <p className="mt-1 text-xs hc-soft">{de.backlog.subtitle}</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="mr-2 text-xs hc-soft">{backlog.loading && !data ? de.backlog.loading : de.backlog.updatedAt(clockLabel(nowSec))}</div>
            <button type="button" onClick={() => setViewMode("queue")} className={cn("grid h-9 w-9 place-items-center rounded-md border", viewMode === "queue" ? "border-cyan-400/50 bg-cyan-500/15 text-cyan-200" : "border-white/10 text-zinc-400 hover:text-zinc-200")} title="Queue">
              <List className="h-4 w-4" />
            </button>
            <button type="button" onClick={() => setViewMode("board")} className={cn("grid h-9 w-9 place-items-center rounded-md border", viewMode === "board" ? "border-cyan-400/50 bg-cyan-500/15 text-cyan-200" : "border-white/10 text-zinc-400 hover:text-zinc-200")} title="Board">
              <LayoutGrid className="h-4 w-4" />
            </button>
          </div>
        </div>
      </section>

      {backlog.error ? <ToneCallout tone="red">{de.backlog.error}</ToneCallout> : null}
      {data?.error ? <ToneCallout tone="amber">{de.backlog.sourceMissing}</ToneCallout> : null}

      <FoHealthStrip items={allItems} contractHealth={data?.contract_health} />

      {nextTask ? (
        <section className="rounded-md border border-cyan-400/25 bg-cyan-500/5 p-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <p className="text-[11px] font-semibold uppercase text-cyan-300">{de.backlog.nextTask}</p>
              <p className="mt-0.5 truncate text-sm font-medium text-white">{nextTask.title}</p>
              <p className="mt-0.5 hc-mono text-[11px] hc-dim">{sourceRef(nextTask)}</p>
              <div className="mt-1.5">
                <ReasonChips codes={reasonCodesForFoItem(nextTask, nowSec)} />
              </div>
            </div>
            <CopyButton text={detailById[nextTask.id] ? buildFoCommissionPrompt(detailById[nextTask.id]) : undefined} label={de.backlog.commission} copiedLabel={de.backlog.commissionCopied} />
          </div>
        </section>
      ) : allItems.length > 0 ? (
        <ToneCallout tone="zinc">{de.backlog.noNextTask}</ToneCallout>
      ) : null}

      {topCandidates.length > 1 ? (
        <section className="rounded-md border border-[var(--hc-border)] bg-white/[.02] p-3" aria-label="Top-Kandidaten vergleichen">
          <p className="mb-2 text-[11px] font-semibold uppercase text-zinc-400">Top-Kandidaten vergleichen</p>
          <div className="grid gap-2 md:grid-cols-3">
            {topCandidates.map((candidate, index) => {
              const item = candidate.item;
              const candidateDetail = detailById[item.id];
              return (
                <article key={item.id} className="flex min-w-0 flex-col gap-1.5 rounded-md border border-[var(--hc-border)] bg-white/[.02] p-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="hc-mono text-[10px] text-zinc-500">#{index + 1} · {item.id}</span>
                    <StatusPill tone={STATUS_TONE[item.status] ?? "amber"} label={item.status || "?"} />
                  </div>
                  <button type="button" onClick={() => setOpenId(item.id)} className="truncate text-left text-sm font-medium text-white hover:text-cyan-200">
                    {item.title}
                  </button>
                  <p className="text-[11px] hc-dim">{item.risk || "?"} · {item.area || "?"} · {staleSignalForFoItem(item, nowSec).label}</p>
                  <ReasonChips codes={candidate.reasonCodes} max={3} />
                  <div className="mt-auto pt-1">
                    <CopyButton text={candidateDetail ? buildFoCommissionPrompt(candidateDetail) : undefined} label={de.backlog.commission} copiedLabel={de.backlog.commissionCopied} />
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      ) : null}

      {ownerLoad.length ? (
        <section className="grid gap-2 md:grid-cols-2 xl:grid-cols-4" aria-label="Owner load summary">
          {ownerLoad.map((owner) => (
            <div key={owner.owner} className="rounded-md border border-[var(--hc-border)] bg-white/[.02] px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm font-medium text-white">{owner.owner}</span>
                <span className="hc-mono text-xs hc-soft">{owner.total}</span>
              </div>
              <p className="mt-1 text-xs hc-dim">High {owner.highRisk} · Stale {owner.stale} · Unready {owner.unready}</p>
            </div>
          ))}
        </section>
      ) : null}

      {allItems.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Gespeicherte Ansichten">
          {QUICK_VIEWS.map((view) => (
            <button
              key={view.id}
              type="button"
              aria-pressed={quickView === view.id}
              onClick={() => { setQuickView(view.id); setActiveIndex(-1); }}
              className={cn(
                "rounded-md border px-2.5 py-1 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70",
                quickView === view.id ? "border-cyan-400/50 bg-cyan-500/15 text-cyan-200" : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
              )}
            >
              {view.label}
            </button>
          ))}
          <button
            type="button"
            aria-pressed={showHelp}
            onClick={() => setShowHelp((value) => !value)}
            title="Tastenkürzel"
            className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-white/10 px-2.5 py-1 text-xs text-zinc-400 transition hover:text-zinc-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70"
          >
            <Keyboard className="h-3.5 w-3.5" /> Tasten
          </button>
        </div>
      ) : null}

      {showHelp ? (
        <section className="rounded-md border border-cyan-400/25 bg-cyan-500/5 px-3 py-2 text-xs text-zinc-200" aria-label="Tastenkürzel">
          <span className="font-semibold text-cyan-200">Tastatur:</span>{" "}
          <kbd className="hc-mono">j</kbd>/<kbd className="hc-mono">k</kbd> bewegen ·{" "}
          <kbd className="hc-mono">Enter</kbd> öffnen ·{" "}
          <kbd className="hc-mono">Esc</kbd> schließen ·{" "}
          <kbd className="hc-mono">?</kbd> Hilfe
        </section>
      ) : null}

      {allItems.length > 0 ? (
        <ControlsBar
          q={q}
          onQ={setQ}
          filterOwner={filterOwner}
          onFilterOwner={setFilterOwner}
          filterRisk={filterRisk}
          onFilterRisk={setFilterRisk}
          filterStale={filterStale}
          onFilterStale={setFilterStale}
          sortKey={sortKey}
          onSort={setSortKey}
          owners={owners}
        />
      ) : null}

      {viewMode === "queue" ? (
        filteredActive.length ? (
          <div ref={queueRef}>
            <FoBacklogQueueTable items={filteredActive} nowSec={nowSec} nextTaskId={nextTaskId} activeId={activeId} detailById={detailById} onOpen={setOpenId} />
          </div>
        ) : (
          <p className="py-4 text-center text-sm hc-dim">{de.backlog.empty}</p>
        )
      ) : (
        <div className={cn("grid", gap)} style={{ gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
          {ACTIVE_COLUMNS.map((col) => {
            const items = filteredByStatus[col.key] ?? [];
            return (
              <section key={col.key} className="min-w-0 rounded-md border border-[var(--hc-border)] bg-white/[.02] p-3">
                <div className="mb-2 flex items-center justify-between">
                  <StatusPill tone={col.tone} label={col.label} />
                  <span className="hc-mono text-xs hc-dim">{items.length}</span>
                </div>
                <div className="space-y-2">
                  {items.length ? items.map((item) => <FoBacklogCard key={item.id} item={item} nowSec={nowSec} isNext={item.id === nextTaskId} onOpen={setOpenId} />) : <p className="py-3 text-center text-xs hc-dim">{de.backlog.emptyColumn}</p>}
                </div>
              </section>
            );
          })}
        </div>
      )}

      <section className="rounded-md border border-[var(--hc-border)] bg-white/[.02] p-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <StatusPill tone="emerald" label={de.backlog.colDone} />
            <span className="hc-mono text-xs hc-dim">{doneItems.length}</span>
          </div>
          {doneItems.length > 5 ? (
            <button type="button" onClick={() => setShowAllDone((value) => !value)} className="rounded-md border border-white/10 px-2 py-1 text-xs hc-soft hover:bg-white/5">
              {showAllDone ? de.backlog.showRecent : de.backlog.showAll}
            </button>
          ) : null}
        </div>
        {doneItems.length ? (
          <FoBacklogQueueTable items={showAllDone ? doneItems : doneItems.slice(0, 5)} nowSec={nowSec} nextTaskId={null} detailById={detailById} onOpen={setOpenId} />
        ) : (
          <p className="py-2 text-xs hc-dim">{de.backlog.empty}</p>
        )}
      </section>

      {selectedItem ? (
        <FoDetailDrawer
          item={selectedItem}
          detail={detail}
          loading={loadingId === selectedItem.id}
          error={errorById[selectedItem.id] || detail?.error}
          commissionPrompt={commissionPrompt}
          onClose={() => setOpenId(null)}
        />
      ) : null}
    </div>
  );
}
