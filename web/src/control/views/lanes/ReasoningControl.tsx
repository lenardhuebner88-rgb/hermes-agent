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
          className="inline-flex min-h-8 cursor-not-allowed items-center rounded-card border border-line bg-surface-1 px-2 font-data text-micro uppercase tracking-wide text-ink-3 opacity-60"
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

  // Joined segments (mockup AB1/AB4): one bordered strip, hairline dividers, no
  // wrap — dense mono STD·MIN·LOW·MED·HIGH. WCAG floor stays met (min-h-8 =
  // 32px ≥ 24px; the segment is a secondary in-row control, not a primary CTA).
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className="inline-flex items-stretch overflow-hidden rounded-card border border-line bg-surface-1"
    >
      {options.map((opt, index) => {
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
              "min-h-8 px-2 font-data text-micro uppercase tracking-wide transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-40",
              index > 0 && "border-l border-line-soft",
              on
                ? "bg-live/15 font-semibold text-bronze-hi"
                : "text-ink-3 hover:bg-surface-3 hover:text-ink",
            )}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
