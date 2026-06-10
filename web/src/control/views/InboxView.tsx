import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { cn } from "@/lib/utils";
import { useDecisionInbox } from "../hooks/useControlData";
import type { InboxItem, InboxSurface } from "../lib/decisionInbox";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import type { ToneName } from "../lib/types";
import { severitySpine } from "../lib/tones";
import { StatusPill, ToneCallout } from "../components/atoms";
import {
  Card,
  Skeleton,
  SkeletonCard,
  Stagger,
  StaggerItem,
  Stat,
  Text,
} from "../components/primitives";

const SURFACE_META: Record<InboxSurface, { label: string; tone: ToneName }> = {
  autoresearch: { label: de.inbox.surfaceAutoresearch, tone: "cyan" },
  family: { label: de.inbox.surfaceFamily, tone: "violet" },
  orchestrator: { label: de.inbox.surfaceOrchestrator, tone: "sky" },
  kanban: { label: de.inbox.surfaceKanban, tone: "amber" },
};

// worstTone → the hero's mood (gradient accent + the big number's colour).
const HERO_ACCENT: Record<ToneName, string> = {
  red: "var(--hc-red)", rose: "var(--hc-red)", amber: "var(--hc-amber)",
  emerald: "var(--hc-emerald)", cyan: "var(--hc-cyan)", sky: "var(--hc-cyan)",
  indigo: "var(--hc-cyan)", violet: "var(--hc-accent)", zinc: "var(--hc-zinc)",
};

export function InboxView({ density }: { density: Density }) {
  const navigate = useNavigate();
  const { items, summary, worstTone, loading, sourceErrors } = useDecisionInbox();
  const [filter, setFilter] = useState<InboxSurface | null>(null);

  const visible = filter ? items.filter((item) => item.surface === filter) : items;
  const calm = summary.total === 0;
  // While the very first load is still in flight we don't yet know the count —
  // don't flash a reassuring "0 / alles ruhig" that might be wrong a tick later.
  const settling = loading && items.length === 0;
  const heroTone: ToneName = settling ? "zinc" : calm ? "emerald" : worstTone;
  const accent = HERO_ACCENT[heroTone];

  const chips: Array<{ id: InboxSurface | null; label: string; count: number }> = [
    { id: null, label: de.inbox.filterAll, count: summary.total },
    { id: "autoresearch", label: SURFACE_META.autoresearch.label, count: summary.autoresearch },
    { id: "family", label: SURFACE_META.family.label, count: summary.family },
    { id: "orchestrator", label: SURFACE_META.orchestrator.label, count: summary.orchestrator },
    { id: "kanban", label: SURFACE_META.kanban.label, count: summary.kanban },
  ];

  return (
    <div className={cn("space-y-4", density === "compact" && "space-y-3")}>
      {/* Hero — the spine. The one number, oversized and tone-driven. The Stat
          `accent` renders the headline count as aurora-gradient display type;
          the .hc-hero shell carries the tone-driven gradient + edge. */}
      <section
        className="hc-hero p-5 sm:p-6"
        style={{ "--hc-hero-accent": accent } as React.CSSProperties}
      >
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          {/* Clamp the hero column at 390px so the display number + copy never
              overflow on phones (display type is large; ellipsis the title). */}
          <div className="min-w-0 max-w-[390px]">
            <Stat
              label={de.inbox.eyebrow}
              value={
                settling ? (
                  <Skeleton className="h-[1em] w-16 rounded-md" />
                ) : (
                  summary.total
                )
              }
              hint={settling ? de.inbox.loading : de.inbox.subtitle(summary.total)}
              accent
            />
            <Text as="h2" variant="title" className="mt-2 truncate text-[var(--hc-text)]">
              {de.inbox.title}
            </Text>
          </div>
          <StatusPill
            tone={calm ? "emerald" : worstTone === "red" || worstTone === "rose" ? "red" : "amber"}
            label={settling ? de.inbox.loading : calm ? de.inbox.calm : de.inbox.attention}
            dot={settling ? "idle" : calm ? "live" : worstTone === "red" || worstTone === "rose" ? "error" : "warn"}
            size="md"
          />
        </div>
      </section>

      {sourceErrors.length ? <ToneCallout tone="amber">{sourceErrors.join(" · ")}</ToneCallout> : null}

      {/* Surface filter — counts come straight from the deduped summary. */}
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

      {settling ? (
        // Settling — placeholder cards while the first inbox payload arrives,
        // instead of flashing the "alles ruhig" empty-state.
        <div className="space-y-2" aria-busy="true">
          <SkeletonCard rows={2} />
          <SkeletonCard rows={2} />
          <SkeletonCard rows={2} />
        </div>
      ) : visible.length === 0 ? (
        <section className="rounded-lg border border-emerald-500/20 bg-emerald-500/[.07] p-6 text-center">
          <p className="text-sm font-medium text-emerald-100">{de.inbox.empty}</p>
          <p className="mt-1 text-xs text-emerald-200/80">{de.inbox.emptyHint}</p>
        </section>
      ) : (
        <Stagger className="space-y-2">
          {visible.map((item) => (
            <StaggerItem key={item.key}>
              <InboxRow item={item} onOpen={() => navigate(item.target)} />
            </StaggerItem>
          ))}
        </Stagger>
      )}
    </div>
  );
}

function InboxRow({ item, onOpen }: { item: InboxItem; onOpen: () => void }) {
  const surface = SURFACE_META[item.surface];
  return (
    <Card
      surface="card"
      interactive
      onClick={onOpen}
      // The row was a <button>; Card renders a div, so re-add button semantics
      // + keyboard activation (Enter/Space) to preserve navigation behavior.
      role="button"
      tabIndex={0}
      ariaLabel={`${surface.label}: ${item.title}`}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
      // Keep the f5 severity spine — the pre-attentive left bar tells the eye
      // how serious a decision is before it's read.
      className={cn("hc-decision flex w-full items-start justify-between gap-3 px-3.5 py-3 text-left", severitySpine[item.tone])}
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
    </Card>
  );
}
