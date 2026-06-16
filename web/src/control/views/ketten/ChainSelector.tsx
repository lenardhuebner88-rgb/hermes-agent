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
  return (
    <div className="relative">
      <label htmlFor="chain-select" className="sr-only">
        {de.ketten.chooseChain}
      </label>
      <select
        id="chain-select"
        value={selectedRootId ?? ""}
        onChange={(e) => onSelect(e.target.value)}
        disabled={disabled || chains.length === 0}
        className={cn(
          "h-10 w-full appearance-none rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel-card)] px-3 pr-9 text-sm text-[var(--hc-text)] outline-none transition focus:border-[var(--hc-accent-border)] focus:ring-1 focus:ring-[var(--hc-accent-border)]",
          disabled && "opacity-60",
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
      <ChevronDown className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--hc-text-dim)]" />
      {selected ? (
        <p className="mt-1.5 truncate text-xs text-[var(--hc-text-dim)]">
          {selected.rootId} · {selected.total} Tasks · {selected.doneCount} done
          {selected.runningCount > 0 ? ` · ${selected.runningCount} läuft` : ""}
          {selected.blockedCount > 0 ? ` · ${selected.blockedCount} blockiert` : ""}
        </p>
      ) : null}
    </div>
  );
}
