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

const SURFACE_META: Record<InboxSurface, { label: string; tone: ToneName }> = {
  autoresearch: { label: de.inbox.surfaceAutoresearch, tone: "cyan" },
  family: { label: de.inbox.surfaceFamily, tone: "violet" },
  orchestrator: { label: de.inbox.surfaceOrchestrator, tone: "sky" },
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
  ];

  return (
    <div className={cn("space-y-4", density === "compact" && "space-y-3")}>
      {/* Hero — the spine. The one number, oversized and tone-driven. */}
      <section
        className="hc-hero p-5 sm:p-6"
        style={{ "--hc-hero-accent": accent } as React.CSSProperties}
      >
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-4 sm:gap-5">
            <div
              className="hc-count text-5xl sm:text-6xl"
              style={{ color: accent }}
              aria-hidden
            >
              {settling ? "·" : summary.total}
            </div>
            <div className="min-w-0">
              <p className="hc-eyebrow">{de.inbox.eyebrow}</p>
              <h2 className="mt-1 text-xl font-semibold tracking-tight text-white sm:text-2xl">{de.inbox.title}</h2>
              <p className="mt-1.5 text-sm hc-soft">
                {settling ? de.inbox.loading : de.inbox.subtitle(summary.total)}
              </p>
            </div>
          </div>
          <StatusPill
            tone={calm ? "emerald" : worstTone === "red" || worstTone === "rose" ? "red" : "amber"}
            label={calm ? de.inbox.calm : de.inbox.attention}
            dot={calm ? "live" : worstTone === "red" || worstTone === "rose" ? "error" : "warn"}
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

      {visible.length === 0 ? (
        <section className="rounded-lg border border-emerald-500/20 bg-emerald-500/[.07] p-6 text-center">
          <p className="text-sm font-medium text-emerald-100">{de.inbox.empty}</p>
          <p className="mt-1 text-xs text-emerald-200/80">{de.inbox.emptyHint}</p>
        </section>
      ) : (
        <div className="space-y-2">
          {visible.map((item, idx) => (
            <InboxRow key={item.key} item={item} index={idx} onOpen={() => navigate(item.target)} />
          ))}
        </div>
      )}
    </div>
  );
}

function InboxRow({ item, index, onOpen }: { item: InboxItem; index: number; onOpen: () => void }) {
  const surface = SURFACE_META[item.surface];
  return (
    <button
      type="button"
      onClick={onOpen}
      style={{ animationDelay: `${Math.min(index, 8) * 40}ms` }}
      className={cn("hc-decision hc-rise flex w-full items-start justify-between gap-3 px-3.5 py-3 text-left", severitySpine[item.tone])}
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
