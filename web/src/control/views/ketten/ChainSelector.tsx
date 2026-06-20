import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { de } from "../../i18n/de";
import type { ChainModel } from "../../lib/fleet";
import type { BoardTask } from "../../lib/types";

export interface ChainSelectorProps {
  chains: ChainModel<BoardTask>[];
  selectedRootId: string | null;
  onSelect: (rootId: string) => void;
  disabled?: boolean;
}

export function ChainSelector({ chains, selectedRootId, onSelect, disabled }: ChainSelectorProps) {
  const selected = chains.find((c) => c.rootId === selectedRootId);

  // Build the visible trigger label: the selected chain's title or a placeholder.
  const triggerLabel = selected?.root?.title ?? selected?.rootId ?? de.ketten.noChains;

  // Mono-meta line: task-id short · total tasks · done count.
  const metaLine = selected
    ? `${selected.rootId.slice(0, 12)} · ${selected.total} ${selected.total === 1 ? "Task" : "Tasks"} · ${selected.doneCount} done`
    : null;

  return (
    <div className="flex items-center gap-[14px]">
      {/* Trigger — a 46px-tall styled button with a hidden native <select> layered on top
          for accessibility. The select is w-full / h-full and opacity-0 so the browser's
          native popup fires on click, while the styled trigger shows through. */}
      <div
        className="relative flex-1 rounded-[12px] focus-within:ring-1 focus-within:ring-[var(--hc-accent-border)]"
        style={{ height: 46 }}
      >
        {/* Visual trigger surface */}
        <div
          aria-hidden="true"
          className={cn(
            "pointer-events-none absolute inset-0 flex items-center justify-between gap-2 overflow-hidden rounded-[12px] border border-[var(--hc-border)] bg-[var(--hc-panel-card)] px-4",
            disabled && "opacity-60",
          )}
          style={{ boxShadow: "inset 0 1px 0 rgba(255,255,255,.9)" }}
        >
          <span className="min-w-0 flex-1 truncate text-[14px] font-medium leading-none text-[var(--hc-text)]">
            {triggerLabel}
          </span>
          <ChevronDown className="h-4 w-4 shrink-0 text-[var(--hc-text-dim)]" />
        </div>

        {/* Accessible native select — invisible but interactive */}
        <label htmlFor="chain-select" className="sr-only">
          {de.ketten.chooseChain}
        </label>
        <select
          id="chain-select"
          value={selectedRootId ?? ""}
          onChange={(e) => onSelect(e.target.value)}
          disabled={disabled || chains.length === 0}
          className={cn(
            "absolute inset-0 h-full w-full cursor-pointer appearance-none rounded-[12px] opacity-0",
            disabled && "pointer-events-none",
          )}
        >
          {chains.length === 0 ? (
            <option value="">{de.ketten.noChains}</option>
          ) : null}
          {chains.map((chain) => {
            const title = chain.root?.title ?? chain.rootId;
            const status = chain.root?.status ?? "todo";
            return (
              <option key={chain.rootId} value={chain.rootId}>
                {title} · {chain.runningCount > 0 ? `${chain.runningCount} läuft` : ""}
                {chain.blockedCount > 0 ? `${chain.blockedCount} blockiert` : ""}
                {chain.runningCount === 0 && chain.blockedCount === 0 ? status : ""}
                {" · "}{chain.total} Tasks
              </option>
            );
          })}
        </select>
      </div>

      {/* Mono-meta: short task-id · task count · done count */}
      {metaLine ? (
        <span className="hc-mono shrink-0 text-[12px] tabular-nums text-[var(--hc-text-soft)]">
          {metaLine}
        </span>
      ) : null}
    </div>
  );
}
