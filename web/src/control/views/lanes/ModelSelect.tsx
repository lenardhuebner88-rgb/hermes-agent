import { useState } from "react";
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

function groupModels(list: LaneModelOption[]): Array<[string, LaneModelOption[]]> {
  const map = new Map<string, LaneModelOption[]>();
  for (const model of list) {
    const key = model.group || model.provider || "API-Modelle";
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(model);
  }
  return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
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

  const close = () => setOpen(false);

  return (
    <div className={cn("relative min-w-0", open && "z-40")}>
      <button
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Modell für ${row.profile}`}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "flex min-h-11 w-full items-center gap-2 rounded-card border border-line bg-surface-2 px-2.5 py-1.5 text-left text-sec text-ink transition-colors duration-150 min-[52rem]:min-h-9",
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
        <ChevronDown className={cn("h-4 w-4 shrink-0 text-ink-3 transition-transform duration-150", open && "rotate-180")} />
      </button>

      {open ? (
        <>
          {/* fixed backdrop = outside-click close (SSR-safe, no document listener) */}
          <div className="fixed inset-0 z-30" aria-hidden onClick={close} />
          <div
            role="listbox"
            aria-label={`Modell für ${row.profile}`}
            onKeyDown={(e) => {
              if (e.key === "Escape") close();
            }}
            className="absolute left-0 top-full z-40 mt-1 max-h-80 w-full min-w-[17rem] overflow-y-auto rounded-card border border-line bg-surface-2 p-1 shadow-2xl"
          >
            <button
              type="button"
              role="option"
              aria-selected={row.choice === ""}
              onClick={() => {
                onChange("");
                close();
              }}
              className={cn(
                "flex min-h-11 w-full items-center gap-2 rounded-[5px] px-2.5 text-left text-sec",
                row.choice === "" ? "bg-surface-3 text-ink" : "text-ink-2 hover:bg-surface-3 hover:text-ink",
              )}
            >
              Standard ({row.defaultLabel})
            </button>

            {groups.map(([group, items]) => (
              <div key={group} className="mt-1">
                <div className="flex items-center gap-1.5 px-2.5 pb-0.5 pt-1.5 text-micro text-ink-3">
                  <span className={cn("pdot", providerDot(items[0]?.provider, items[0]?.id))} aria-hidden />
                  <span className="truncate uppercase tracking-wide">{group}</span>
                </div>
                {items.map((model) => {
                  const choice = choiceForModel(model);
                  const on = row.choice === choice || (selected?.id === model.id && selected?.runtime === model.runtime);
                  const greyed = showAll && restIds.has(`${model.runtime}|${model.id}`);
                  return (
                    <button
                      key={`${model.runtime}|${model.provider ?? ""}|${model.id}`}
                      type="button"
                      role="option"
                      aria-selected={on}
                      onClick={() => {
                        onChange(choice);
                        close();
                      }}
                      className={cn(
                        "flex min-h-11 w-full items-center gap-2 rounded-[5px] px-2.5 text-left text-sec",
                        on ? "bg-surface-3 text-ink" : "text-ink hover:bg-surface-3",
                        greyed && "text-ink-3 opacity-60",
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
