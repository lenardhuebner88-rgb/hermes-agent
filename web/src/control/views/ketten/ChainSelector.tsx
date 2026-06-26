import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { de } from "../../i18n/de";
import { buildChainOptionLabel } from "./chainSelectorUtils";
import type { ChainModel } from "../../lib/fleet";
import type { BoardTask } from "../../lib/types";

export interface ChainSelectorProps {
  chains: ChainModel<BoardTask>[];
  /** Completed chains to show in a separate optgroup (optional). */
  doneChains?: ChainModel<BoardTask>[];
  selectedRootId: string | null;
  onSelect: (rootId: string) => void;
  disabled?: boolean;
}

export function ChainSelector({ chains, doneChains, selectedRootId, onSelect, disabled }: ChainSelectorProps) {
  const selected = chains.find((c) => c.rootId === selectedRootId)
    ?? doneChains?.find((c) => c.rootId === selectedRootId);

  // Build the visible trigger label: the selected chain's title or a placeholder.
  const triggerLabel = selected?.root?.title ?? selected?.rootId ?? de.ketten.noChains;

  // B3: mono-meta line uses "fertig" not "done"
  const metaLine = selected
    ? `${selected.rootId.slice(0, 12)} · ${selected.total} ${selected.total === 1 ? "Task" : "Tasks"} · ${selected.doneCount} fertig`
    : null;

  return (
    // B1: on mobile the row stacks (flex-col) so the select trigger gets the FULL
    // card width and the chain name stays readable; the mono-meta drops to its own
    // line below. From sm: up it's the original side-by-side row.
    <div className="flex min-w-0 flex-col items-stretch gap-2 sm:flex-row sm:items-center sm:gap-[14px]">
      {/* Trigger — a 46px-tall styled button with a hidden native <select> layered on top
          for accessibility. The select is w-full / h-full and opacity-0 so the browser's
          native popup fires on click, while the styled trigger shows through. */}
      <div
        className="relative min-w-0 flex-1 rounded-[12px] focus-within:ring-1 focus-within:ring-[var(--hc-accent-border)]"
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

        {/* Accessible native select — invisible but interactive.
            B1: w-full ensures the native popup shows the full option text on mobile. */}
        <label htmlFor="chain-select" className="sr-only">
          {de.ketten.chooseChain}
        </label>
        <select
          id="chain-select"
          value={selectedRootId ?? ""}
          onChange={(e) => onSelect(e.target.value)}
          disabled={disabled || (chains.length === 0 && (doneChains?.length ?? 0) === 0)}
          className={cn(
            "absolute inset-0 h-full w-full cursor-pointer appearance-none rounded-[12px] opacity-0",
            disabled && "pointer-events-none",
          )}
        >
          {chains.length === 0 && (doneChains?.length ?? 0) === 0 ? (
            <option value="">{de.ketten.noChains}</option>
          ) : null}

          {/* Active chains (no group label when there are no done chains — keeps it clean) */}
          {chains.length > 0 && (doneChains?.length ?? 0) > 0 ? (
            <optgroup label="Aktiv">
              {chains.map((chain) => (
                <option key={chain.rootId} value={chain.rootId}>
                  {buildChainOptionLabel(chain)}
                </option>
              ))}
            </optgroup>
          ) : (
            chains.map((chain) => (
              <option key={chain.rootId} value={chain.rootId}>
                {buildChainOptionLabel(chain)}
              </option>
            ))
          )}

          {/* B7: completed chains as a separate optgroup so just-finished chains remain inspectable */}
          {(doneChains?.length ?? 0) > 0 ? (
            <optgroup label="Abgeschlossen">
              {doneChains!.map((chain) => (
                <option key={chain.rootId} value={chain.rootId}>
                  {buildChainOptionLabel(chain)}
                </option>
              ))}
            </optgroup>
          ) : null}
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
