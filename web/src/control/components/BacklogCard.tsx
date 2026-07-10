import { cn } from "@/lib/utils";
import { Eyebrow } from "./primitives";
import { SignalLabel, type SignalTone } from "./leitstand";
import { ageLabel, depState, projectFromRoot, readiness } from "../lib/orchestration";
import { de } from "../i18n/de";
import type { OrchestrationItem } from "../lib/schemas";

const PRIORITY_TONE: Record<string, SignalTone> = { high: "alert", medium: "warn", low: "neutral" };

type ExtItem = OrchestrationItem & { root?: string; excerpt?: string };

export function BacklogCard({
  item,
  allItems,
  nowSec,
  onOpen,
  isNext = false,
}: {
  item: ExtItem;
  allItems: ReadonlyArray<OrchestrationItem>;
  nowSec: number;
  onOpen: (id: string) => void;
  isNext?: boolean;
}) {
  const r = readiness(item, allItems);
  const project = projectFromRoot(item.root);
  const showProject = project !== "Orchestration";

  return (
    <button
      type="button"
      className={cn(
        "min-h-12 w-full cursor-pointer rounded-card border bg-surface-2 p-3 text-left transition hover:bg-surface-3 focus:outline-none focus:ring-2 focus:ring-live/60",
        isNext
          ? "border-live/40 ring-1 ring-live/20 hover:border-live/60"
          : "border-line hover:border-live/40",
      )}
      onClick={() => onOpen(item.id)}
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          {isNext && (
            <Eyebrow className="mb-1 text-live">{de.orchestrator.nextBadge}</Eyebrow>
          )}
          <p className="text-sec font-medium leading-snug text-ink">{item.title}</p>
          {item.excerpt ? (
            <p className="mt-0.5 line-clamp-2 text-micro leading-snug text-ink-2">{item.excerpt}</p>
          ) : null}
        </div>
        <span className="shrink-0 font-data text-micro text-ink-3">{item.id}</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {showProject ? <span className="text-micro text-ink-2">{project}</span> : null}
        {r.state === "ready" ? <SignalLabel tone="ok" label={de.orchestrator.ready} /> : null}
        {r.state === "blocked" ? (
          <SignalLabel tone="alert" label={`${de.orchestrator.blockedBy} ${r.blockedBy.slice(0, 2).join(", ")}`} />
        ) : null}
        <SignalLabel tone={PRIORITY_TONE[item.priority] ?? "neutral"} label={item.priority} />
        {item.planGate ? <SignalLabel tone="neutral" label={de.orchestrator.planGate} /> : null}
        {item.dependsOn?.length ? (
          <SignalLabel
            tone={(item.dependsOn ?? []).some((depId) => depState(depId, allItems) !== "done") ? "alert" : "ok"}
            label={de.orchestrator.dependsOn(item.dependsOn.length)}
          />
        ) : null}
        <span className="ml-auto font-data text-micro tabular-nums text-ink-2">{ageLabel(item.created, nowSec)}</span>
      </div>
    </button>
  );
}
