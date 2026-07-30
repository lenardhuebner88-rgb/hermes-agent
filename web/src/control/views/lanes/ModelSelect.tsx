import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  choiceForModel,
  isModelReachable,
  type EditorRow,
  type LaneModelOption,
} from "./api";
import { providerDot } from "./providerColors";

// ModelSelect — filtered model picker for a matrix row. Default view shows the
// curated „sinnvoll & erreichbar" set (isModelReachable) grouped by provider
// with a stable identity dot; the „Alle (N)" toggle reveals the remaining
// catalog greyed under the same groups. Unknown/missing fields fail soft — an
// un-flagged model still lists (older payloads). Bronze marks the open trigger
// focus / selected row only; the dots are provider identity, never status.
//
// Keyboard contract (A11y pass): the trigger keeps focus and drives the open
// listbox via aria-activedescendant — ArrowUp/Down (opens when closed), Home/
// End, Enter/Space select, Escape closes, Type-ahead is intentionally absent
// (grouped list). The dropdown flips upward when the space below the trigger
// is too small (last matrix rows clipped before).

function groupModels(list: LaneModelOption[]): Array<[string, LaneModelOption[]]> {
  const map = new Map<string, LaneModelOption[]>();
  for (const model of list) {
    const key = model.group || model.provider || "API-Modelle";
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(model);
  }
  return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
}

interface FlatOption {
  choice: string;
  domId: string;
}

export function ModelSelect({
  row,
  models,
  disabled,
  onChange,
}: {
  row: EditorRow;
  models: LaneModelOption[];
  disabled?: boolean;
  onChange: (choice: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [activeIdx, setActiveIdx] = useState(-1);
  const [flipUp, setFlipUp] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  // useId: unique per mounted row, no ref access during render (lint).
  const uid = `ms-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;

  const selected = row.model
    ? models.find((m) => m.id === row.model && m.runtime === row.worker_runtime) ??
      models.find((m) => m.id === row.model) ??
      null
    : null;

  const curated = models.filter(isModelReachable);
  const rest = models.filter((m) => !isModelReachable(m));
  // Pin the currently selected model into the curated view: in credential-less
  // environments an active row can point at a non-"sinnvoll" model — the open
  // dropdown must always contain the selection (documented S2 follow-up).
  const selectedPinned =
    selected && !showAll && !curated.some((m) => m.id === selected.id && m.runtime === selected.runtime)
      ? [...curated, selected]
      : curated;
  const groups = groupModels(showAll ? models : selectedPinned);
  const restIds = new Set(rest.map((m) => `${m.runtime}|${m.id}`));

  // Flat option list in render order (leading "Standard" + grouped models) —
  // the keyboard cursor walks this list.
  const flat = useMemo<FlatOption[]>(() => {
    const out: FlatOption[] = [{ choice: "", domId: `${uid}-std` }];
    for (const [, items] of groups) {
      for (const model of items) {
        out.push({
          choice: choiceForModel(model),
          domId: `${uid}-${out.length}`,
        });
      }
    }
    return out;
    // groups re-derives from models/showAll/selected each render; the memo key
    // must track all three (identity-stable upstream).
  }, [models, showAll, selected]); // eslint-disable-line react-hooks/exhaustive-deps

  const close = (refocus = false) => {
    setOpen(false);
    setActiveIdx(-1);
    if (refocus) triggerRef.current?.focus();
  };

  const openList = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect && typeof window !== "undefined") {
      // Flip up when <340px below and more room above (last rows of the matrix).
      setFlipUp(window.innerHeight - rect.bottom < 340 && rect.top > 340);
    }
    const current = flat.findIndex((o) =>
      o.choice === "" ? row.choice === "" : o.choice === row.choice,
    );
    setActiveIdx(current >= 0 ? current : 0);
    setOpen(true);
  };

  const choose = (idx: number) => {
    const opt = flat[idx];
    if (!opt) return;
    onChange(opt.choice);
    close(true);
  };

  // Keep the active option visible while arrow-walking (jsdom has no
  // scrollIntoView — guard, the browser path is the real one).
  useEffect(() => {
    if (!open || activeIdx < 0) return;
    const el = listRef.current?.querySelector(`[data-idx="${activeIdx}"]`);
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ block: "nearest" });
    }
  }, [open, activeIdx]);

  const onTriggerKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        if (!open) openList();
        else setActiveIdx((i) => Math.min(i + 1, flat.length - 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        if (!open) openList();
        else setActiveIdx((i) => Math.max(i - 1, 0));
        break;
      case "Home":
        if (open) {
          e.preventDefault();
          setActiveIdx(0);
        }
        break;
      case "End":
        if (open) {
          e.preventDefault();
          setActiveIdx(flat.length - 1);
        }
        break;
      case "Enter":
      case " ":
        if (open) {
          e.preventDefault();
          choose(activeIdx);
        }
        break;
      case "Escape":
        if (open) {
          e.preventDefault();
          e.stopPropagation();
          close(true);
        }
        break;
      case "Tab":
        if (open) close();
        break;
    }
  };

  let renderIdx = 0;

  return (
    <div className={cn("relative min-w-0", open && "z-40")}>
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? `${uid}-list` : undefined}
        aria-activedescendant={open && activeIdx >= 0 ? flat[activeIdx]?.domId : undefined}
        aria-label={`Modell für ${row.profile}`}
        onClick={() => (open ? close() : openList())}
        onKeyDown={onTriggerKeyDown}
        className={cn(
          "flex min-h-11 w-full items-center gap-2 rounded-card border border-line bg-surface-2 px-2.5 py-1.5 text-left text-sec text-ink transition-colors duration-150 @min-[56rem]:min-h-9",
          "hover:border-live/60 focus-visible:border-live focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50",
        )}
      >
        {selected ? (
          <>
            <span className={cn("pdot", providerDot(selected.provider, selected.id))} aria-hidden />
            <span className="min-w-0 flex-1 truncate font-data text-micro">{selected.label}</span>
          </>
        ) : (
          <span className="min-w-0 flex-1 truncate font-data text-micro text-ink-2">
            Standard ({row.defaultLabel})
          </span>
        )}
        <ChevronDown className={cn("h-4 w-4 shrink-0 text-ink-3 transition-transform duration-150", open && !flipUp && "rotate-180", open && flipUp && "rotate-0")} />
      </button>

      {open ? (
        <>
          {/* fixed backdrop = outside-click close (SSR-safe, no document listener) */}
          <div className="fixed inset-0 z-30" aria-hidden onClick={() => close(true)} />
          <div
            ref={listRef}
            id={`${uid}-list`}
            role="listbox"
            aria-label={`Modell für ${row.profile}`}
            className={cn(
              "absolute left-0 z-40 max-h-80 w-full min-w-[17rem] overflow-y-auto rounded-card border border-line bg-surface-2 p-1 shadow-floating",
              flipUp ? "bottom-full mb-1" : "top-full mt-1",
            )}
          >
            {(() => {
              const idx = renderIdx++;
              const on = row.choice === "";
              return (
                <button
                  key="std"
                  id={flat[idx]?.domId}
                  data-idx={idx}
                  type="button"
                  role="option"
                  aria-selected={on}
                  onMouseEnter={() => setActiveIdx(idx)}
                  onClick={() => choose(idx)}
                  className={cn(
                    "flex min-h-11 w-full items-center gap-2 rounded-[5px] px-2.5 text-left text-sec",
                    on ? "bg-surface-3 text-ink" : "text-ink-2 hover:bg-surface-3 hover:text-ink",
                    activeIdx === idx && "bg-surface-3 text-ink",
                  )}
                >
                  Standard ({row.defaultLabel})
                </button>
              );
            })()}

            {groups.map(([group, items]) => (
              <div key={group} className="mt-1">
                <div className="flex items-center gap-1.5 px-2.5 pb-0.5 pt-1.5 text-micro text-ink-3">
                  <span className={cn("pdot", providerDot(items[0]?.provider, items[0]?.id))} aria-hidden />
                  <span className="truncate uppercase tracking-wide">{group}</span>
                </div>
                {items.map((model) => {
                  const idx = renderIdx++;
                  const choice = choiceForModel(model);
                  const on = row.choice === choice || (selected?.id === model.id && selected?.runtime === model.runtime);
                  const greyed = showAll && restIds.has(`${model.runtime}|${model.id}`);
                  return (
                    <button
                      key={`${model.runtime}|${model.provider ?? ""}|${model.id}`}
                      id={flat[idx]?.domId}
                      data-idx={idx}
                      type="button"
                      role="option"
                      aria-selected={on}
                      onMouseEnter={() => setActiveIdx(idx)}
                      onClick={() => choose(idx)}
                      className={cn(
                        "flex min-h-11 w-full items-center gap-2 rounded-[5px] px-2.5 text-left text-sec",
                        on ? "bg-surface-3 text-ink" : "text-ink hover:bg-surface-3",
                        activeIdx === idx && "bg-surface-3",
                        greyed && "text-ink-3",
                      )}
                    >
                      <span className={cn("pdot", providerDot(model.provider, model.id))} aria-hidden />
                      <span className="min-w-0 flex-1 truncate font-data text-micro">{model.label}</span>
                      {greyed ? <span className="shrink-0 text-micro text-ink-3">nicht erreichbar</span> : null}
                    </button>
                  );
                })}
              </div>
            ))}

            {rest.length > 0 ? (
              <div className="mt-1 border-t border-line-soft pt-1">
                <button
                  type="button"
                  tabIndex={-1}
                  onClick={() => setShowAll((v) => !v)}
                  className="flex min-h-11 w-full items-center px-2.5 text-left text-micro text-live hover:text-bronze-hi"
                >
                  {showAll ? "Nur sinnvolle & erreichbare" : `Alle (${models.length})`}
                </button>
              </div>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}
