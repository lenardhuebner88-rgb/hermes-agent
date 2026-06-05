import { useMemo, useState } from "react";
import { Inbox as InboxIcon, ChevronRight } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { cn } from "@/lib/utils";
import {
  useBacklog,
  useHermesRecentResults,
  useHermesWorkers,
  useMetricsLite,
  useOrchestrationBacklog,
  useProposals,
  useSystemHealth,
} from "../hooks/useControlData";
import { buildAgentOpsSnapshot } from "../lib/agentOps";
import { buildDecisionInbox, inboxSummary, type InboxItem, type InboxSurface } from "../lib/decisionInbox";
import { nowSec } from "../lib/derive";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import type { ToneName } from "../lib/types";
import { StatusPill, ToneCallout } from "../components/atoms";

const SURFACE_META: Record<InboxSurface, { label: string; tone: ToneName }> = {
  autoresearch: { label: de.inbox.surfaceAutoresearch, tone: "cyan" },
  family: { label: de.inbox.surfaceFamily, tone: "violet" },
  orchestrator: { label: de.inbox.surfaceOrchestrator, tone: "sky" },
};

function rowTone(tone: ToneName): string {
  return {
    red: "border-red-500/30 bg-red-500/[.06]",
    amber: "border-amber-500/30 bg-amber-500/[.06]",
    cyan: "border-cyan-500/25 bg-cyan-500/[.05]",
    sky: "border-sky-500/25 bg-sky-500/[.05]",
    violet: "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]",
    emerald: "border-emerald-500/25 bg-emerald-500/[.05]",
    indigo: "border-indigo-400/25 bg-indigo-400/[.05]",
    rose: "border-rose-500/25 bg-rose-500/[.05]",
    zinc: "border-white/10 bg-white/[.025]",
  }[tone];
}

export function InboxView({ density }: { density: Density }) {
  const navigate = useNavigate();
  const proposals = useProposals();
  const backlog = useBacklog();
  const workers = useHermesWorkers();
  const results = useHermesRecentResults();
  const health = useSystemHealth();
  const metrics = useMetricsLite();
  const orchestration = useOrchestrationBacklog();
  const now = nowSec();
  const [filter, setFilter] = useState<InboxSurface | null>(null);

  const snapshot = useMemo(
    () =>
      buildAgentOpsSnapshot({
        workers: workers.data?.workers ?? [],
        results: results.data?.results ?? [],
        proposals: proposals.proposals,
        orchestrationItems: orchestration.data?.items ?? [],
        contractHealth: orchestration.data?.contract_health,
        systemHealth: health.data,
        metrics: metrics.data,
        nowSec: orchestration.data?.checked_at ?? now,
      }),
    [workers.data, results.data, proposals.proposals, orchestration.data, health.data, metrics.data, now],
  );

  const items = useMemo(
    () =>
      buildDecisionInbox({
        proposals: proposals.proposals,
        foItems: backlog.data?.items ?? [],
        foNowSec: backlog.data?.checked_at ?? now,
        interventions: snapshot.interventions,
      }),
    [proposals.proposals, backlog.data, snapshot.interventions, now],
  );

  const summary = useMemo(() => inboxSummary(items), [items]);
  const visible = filter ? items.filter((item) => item.surface === filter) : items;
  const loading = proposals.loading || backlog.loading || orchestration.loading;

  const sourceErrors = [
    proposals.error ? `Autoresearch: ${proposals.error}` : "",
    backlog.error ? `Family: ${backlog.error}` : "",
    orchestration.error ? `Orchestrator: ${orchestration.error}` : "",
  ].filter(Boolean);

  const chips: Array<{ id: InboxSurface | null; label: string; count: number }> = [
    { id: null, label: de.inbox.filterAll, count: summary.total },
    { id: "autoresearch", label: SURFACE_META.autoresearch.label, count: summary.autoresearch },
    { id: "family", label: SURFACE_META.family.label, count: summary.family },
    { id: "orchestrator", label: SURFACE_META.orchestrator.label, count: summary.orchestrator },
  ];

  return (
    <div className={cn("space-y-4", density === "compact" && "space-y-3")}>
      <section className="hc-card flex flex-col gap-3 p-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <p className="hc-eyebrow">{de.inbox.eyebrow}</p>
          <h2 className="mt-1 text-xl font-semibold text-white">{de.inbox.title}</h2>
          <p className="mt-1 text-sm hc-soft">
            {loading && !items.length ? de.inbox.loading : de.inbox.subtitle(summary.total)}
          </p>
        </div>
        <InboxIcon className="hidden h-6 w-6 hc-dim sm:block" />
      </section>

      {sourceErrors.length ? <ToneCallout tone="amber">{sourceErrors.join(" · ")}</ToneCallout> : null}

      <div className="flex flex-wrap items-center gap-2" role="group" aria-label={de.inbox.filterAll}>
        {chips.map((chip) => (
          <button
            key={chip.id ?? "all"}
            type="button"
            aria-pressed={filter === chip.id}
            onClick={() => setFilter(chip.id)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70",
              filter === chip.id ? "border-cyan-400/50 bg-cyan-500/15 text-cyan-200" : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
            )}
          >
            {chip.label}
            <span className="hc-mono text-[11px] opacity-80">{chip.count}</span>
          </button>
        ))}
      </div>

      {visible.length === 0 ? (
        <section className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 p-6 text-center">
          <p className="text-sm font-medium text-emerald-100">{de.inbox.empty}</p>
          <p className="mt-1 text-xs text-emerald-200/80">{de.inbox.emptyHint}</p>
        </section>
      ) : (
        <div className="space-y-2">
          {visible.map((item) => (
            <InboxRow key={item.key} item={item} onOpen={() => navigate(item.target)} />
          ))}
        </div>
      )}
    </div>
  );
}

function InboxRow({ item, onOpen }: { item: InboxItem; onOpen: () => void }) {
  const surface = SURFACE_META[item.surface];
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        "flex w-full items-start justify-between gap-3 rounded-lg border px-3 py-3 text-left transition hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70",
        rowTone(item.tone),
      )}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill tone={surface.tone} label={surface.label} />
          <span className="truncate text-sm font-semibold text-white">{item.title}</span>
        </div>
        <p className="mt-1 line-clamp-2 text-xs hc-soft">{item.why}</p>
        <p className="mt-1 text-xs font-medium text-zinc-200">{de.inbox.do}: {item.nextAction}</p>
      </div>
      <ChevronRight className="mt-1 h-4 w-4 shrink-0 hc-dim" />
    </button>
  );
}
