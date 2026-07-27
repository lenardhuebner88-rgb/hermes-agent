import { useRef } from "react";
import { cn } from "@/lib/utils";
import { t } from "./strings";

// FastControl — Std·Fast radio pair for a profile's fast mode (hermes:
// `agent.service_tier` → priority/speed request overrides at spawn; claude-cli:
// the `claude_fast_mode` spawn bool). Mirrors ReasoningControl's honesty rule:
// a model the spawn gate cannot transport renders the control disabled with an
// explaining hint, never fake-enabled (a persisted but silently dropped toggle
// is the failure mode this avoids). Same radiogroup contract as
// ReasoningControl: role/aria-checked, roving tabindex, arrows move+select.

export function FastControl({
  value,
  supported,
  disabled,
  ariaLabel,
  hint,
  onChange,
}: {
  /** Staged value; null = "Std" (normal/untouched on persist). */
  value: "fast" | null;
  /** Spawn-gate truth for the row's effective model; false → disabled + hint. */
  supported: boolean;
  disabled?: boolean;
  ariaLabel: string;
  /** Honest explanation for an unsupported model; defaults to the generic text. */
  hint?: string | null;
  onChange: (value: "fast" | null) => void;
}) {
  const groupRef = useRef<HTMLDivElement>(null);

  if (!supported) {
    const noFast = hint ?? t.fastUnsupported;
    return (
      <div className="min-w-0">
        <div
          className="inline-flex min-h-8 cursor-not-allowed items-center rounded-card border border-line bg-surface-1 px-2 font-data text-micro uppercase tracking-wide text-ink-3"
          aria-disabled="true"
          title={noFast}
        >
          {t.fastStd}
        </div>
        <p className="mt-1 text-micro text-ink-2">{noFast}</p>
      </div>
    );
  }

  const options: Array<{ value: "fast" | null; label: string; full: string }> = [
    { value: null, label: t.fastStd, full: t.fastStdFull },
    { value: "fast", label: t.fastFast, full: t.fastFastFull },
  ];

  const moveSelection = (dir: 1 | -1) => {
    const current = options.findIndex((opt) => opt.value === value);
    const next = (current + dir + options.length) % options.length;
    onChange(options[next]!.value);
    const radios = groupRef.current?.querySelectorAll<HTMLButtonElement>('[role="radio"]');
    radios?.[next]?.focus();
  };

  // Joined segments, same geometry as ReasoningControl: 44px touch target on
  // phone/tablet (<56rem matrix panel width, min-h-11), the dense 32px (min-h-8) on desktop.
  return (
    <div
      ref={groupRef}
      role="radiogroup"
      aria-label={ariaLabel}
      onKeyDown={(e) => {
        if (disabled) return;
        if (e.key === "ArrowRight" || e.key === "ArrowDown") {
          e.preventDefault();
          moveSelection(1);
        } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
          e.preventDefault();
          moveSelection(-1);
        }
      }}
      className="inline-flex items-stretch overflow-hidden rounded-card border border-line bg-surface-1"
    >
      {options.map((opt, index) => {
        const on = value === opt.value;
        return (
          <button
            key={opt.full}
            type="button"
            role="radio"
            aria-checked={on}
            tabIndex={on ? 0 : -1}
            disabled={disabled}
            title={opt.full}
            onClick={() => onChange(opt.value)}
            className={cn(
              "min-h-11 px-2 font-data text-micro uppercase tracking-wide transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-40 @min-[56rem]:min-h-8",
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
