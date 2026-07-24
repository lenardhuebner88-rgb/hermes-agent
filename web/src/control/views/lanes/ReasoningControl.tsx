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
  // claude-cli `claude --effort` carries two levels beyond the hermes trio (S1):
  // xhigh + max. The joined strip renders STD·LOW·MED·HIGH·XHI·MAX for those rows.
  xhigh: "xhi",
  max: "max",
};

const STD = "Std";

export function ReasoningControl({
  value,
  support,
  disabled,
  ariaLabel,
  hint,
  onChange,
}: {
  /** Staged value; null = "Std" (leave config untouched). */
  value: string | null;
  /** Transportable values; empty → disabled control + hint. */
  support: string[];
  disabled?: boolean;
  ariaLabel: string;
  /** Honest explanation for an empty support set; defaults to the generic
   *  "no Reasoning-Knopf" text (grok/qwen/alibaba — no transport branch).
   *  claude-cli rows are NOT empty anymore (S1 wired `claude_effort`), so they
   *  render the active 5-level strip and never use this hint. */
  hint?: string | null;
  onChange: (value: string | null) => void;
}) {
  if (support.length === 0) {
    const noKnopf = hint ?? "Modell hat keinen Reasoning-Knopf";
    return (
      <div className="min-w-0">
        <div
          className="inline-flex min-h-8 cursor-not-allowed items-center rounded-card border border-line bg-surface-1 px-2 font-data text-micro uppercase tracking-wide text-ink-3 opacity-60"
          aria-disabled="true"
          title={noKnopf}
        >
          {STD}
        </div>
        <p className="mt-1 text-micro text-ink-3">{noKnopf}</p>
      </div>
    );
  }

  const options: Array<{ value: string | null; label: string; full: string }> = [
    { value: null, label: STD, full: "Standard (Profil-Config)" },
    ...support.map((s) => ({ value: s, label: SHORT[s] ?? s, full: s })),
  ];

  // Joined segments (mockup AB1/AB4): one bordered strip, hairline dividers, no
  // wrap — dense mono STD·MIN·LOW·MED·HIGH (hermes) or STD·LOW·MED·HIGH·XHI·MAX
  // (claude-cli's 5-level claude_effort set, S1). Touch target: 44px on
  // phone/tablet (<52rem, min-h-11) per the ≥44px mobile rule, the dense 32px
  // (min-h-8) on desktop — both clear the WCAG 2.5.8 ≥24px floor.
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
              "min-h-11 px-2 font-data text-micro uppercase tracking-wide transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-40 min-[52rem]:min-h-8",
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
