import { cn } from "@/lib/utils";

// ReasoningControl — segment buttons for a model's transportable reasoning
// efforts (S1 `reasoning_support`). Bronze marks the SELECTED segment (bronze =
// interactive, DESIGN.md rule 1). When the model has no Reasoning-Knopf
// (support empty — honest for grok/qwen/alibaba, no transport branch) the whole
// control renders disabled with an explaining hint instead of a fake-enabled
// selector (PlanSpec risk #3: Reasoning-Ehrlichkeit).

const SHORT: Record<string, string> = {
  minimal: "min",
  low: "low",
  medium: "med",
  high: "high",
};

const STD = "Std";

export function ReasoningControl({
  value,
  support,
  disabled,
  ariaLabel,
  onChange,
}: {
  /** Staged value; null = "Std" (leave config untouched). */
  value: string | null;
  /** Transportable values; empty → disabled control + hint. */
  support: string[];
  disabled?: boolean;
  ariaLabel: string;
  onChange: (value: string | null) => void;
}) {
  if (support.length === 0) {
    return (
      <div className="min-w-0">
        <div
          className="inline-flex min-h-10 cursor-not-allowed items-center rounded-card border border-line bg-surface-1 px-2.5 text-micro text-ink-3 opacity-60"
          aria-disabled="true"
          title="Modell hat keinen Reasoning-Knopf"
        >
          {STD}
        </div>
        <p className="mt-1 text-micro text-ink-3">Modell hat keinen Reasoning-Knopf</p>
      </div>
    );
  }

  const options: Array<{ value: string | null; label: string; full: string }> = [
    { value: null, label: STD, full: "Standard (Profil-Config)" },
    ...support.map((s) => ({ value: s, label: SHORT[s] ?? s, full: s })),
  ];

  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className="inline-flex flex-wrap items-center gap-1 rounded-card border border-line bg-surface-1 p-1"
    >
      {options.map((opt) => {
        const on = value === opt.value;
        return (
          <button
            key={opt.full}
            type="button"
            disabled={disabled}
            aria-pressed={on}
            title={opt.full}
            onClick={() => onChange(opt.value)}
            className={cn(
              "min-h-10 rounded-[5px] px-2.5 text-micro font-medium transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-40",
              on
                ? "border border-live bg-live/15 text-bronze-hi"
                : "border border-transparent text-ink-2 hover:bg-surface-3 hover:text-ink",
            )}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
