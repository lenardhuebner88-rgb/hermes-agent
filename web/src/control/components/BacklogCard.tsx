import { cn } from "@/lib/utils";
import { StatusPill } from "./atoms";
import { ageLabel, depState, projectFromRoot, readiness } from "../lib/orchestration";
import { de } from "../i18n/de";
import type { ToneName } from "../lib/types";
import type { OrchestrationItem } from "../lib/schemas";

const PRIORITY_TONE: Record<string, ToneName> = { high: "red", medium: "amber", low: "zinc" };

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
        "w-full cursor-pointer rounded-lg border p-3 text-left transition hover:bg-white/[.03] focus:outline-none focus:ring-2 focus:ring-cyan-400/60",
        isNext
          ? "border-cyan-400/40 ring-1 ring-cyan-400/20 hover:border-cyan-400/60"
          : "border-white/10 hover:border-white/20",
      )}
      onClick={() => onOpen(item.id)}
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          {isNext && (
            <span className="mb-1 inline-block rounded bg-cyan-400/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-cyan-300">
              {de.orchestrator.nextBadge}
            </span>
          )}
          <p className="text-sm font-medium leading-snug text-white">{item.title}</p>
          {item.excerpt ? (
            <p className="mt-0.5 line-clamp-2 text-[11px] leading-snug hc-soft">{item.excerpt}</p>
          ) : null}
        </div>
        <span className="hc-mono shrink-0 text-[11px] hc-dim">{item.id}</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {showProject ? <StatusPill tone="violet" label={project} /> : null}
        {r.state === "ready" ? <StatusPill tone="emerald" label={de.orchestrator.ready} /> : null}
        {r.state === "blocked" ? (
          <StatusPill tone="red" label={`${de.orchestrator.blockedBy} ${r.blockedBy.slice(0, 2).join(", ")}`} />
        ) : null}
        <StatusPill tone={PRIORITY_TONE[item.priority] ?? "zinc"} label={item.priority} />
        {item.planGate ? <StatusPill tone="indigo" label={de.orchestrator.planGate} /> : null}
        {item.dependsOn?.length ? (
          <StatusPill
            tone={(item.dependsOn ?? []).some((depId) => depState(depId, allItems) !== "done") ? "red" : "emerald"}
            label={de.orchestrator.dependsOn(item.dependsOn.length)}
          />
        ) : null}
        <span className="ml-auto text-[11px] hc-soft">{ageLabel(item.created, nowSec)}</span>
      </div>
    </button>
  );
}
