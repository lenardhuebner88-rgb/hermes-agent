/**
 * CommissionButton — the one-click "copy this Family-Organizer backlog item into
 * the Fleet" button. Shows idle / busy / done / error states off the
 * useCommissionToFleet hook. `variant="full"` renders the wide drawer button;
 * "pill" renders the compact per-row chip.
 */
import { ArrowRight, Check, Loader2, RotateCcw } from "lucide-react";
import { cn } from "@/lib/utils";
import { de } from "../../i18n/de";
import type { CommissionState } from "../../hooks/useControlData";

export function CommissionButton({
  state,
  onClick,
  variant = "pill",
  className,
}: {
  state?: CommissionState;
  onClick: (event: React.MouseEvent) => void;
  variant?: "pill" | "full";
  className?: string;
}) {
  const busy = state === "busy";
  const done = state === "done";
  const err = state === "error";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy || done}
      title={de.fleet.commissionTitle}
      aria-label={de.fleet.commissionDrawer}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border font-medium transition disabled:cursor-default",
        variant === "full" ? "min-h-9 w-full justify-center px-4 text-sm" : "min-h-8 px-3 text-xs",
        done
          ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
          : err
            ? "border-red-500/40 text-red-200 hover:bg-red-500/10"
            : "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)] hover:brightness-110",
        className,
      )}
    >
      {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : done ? <Check className="h-3.5 w-3.5" /> : err ? <RotateCcw className="h-3.5 w-3.5" /> : <ArrowRight className="h-3.5 w-3.5" />}
      {busy ? de.fleet.commissionBusy : done ? de.fleet.commissionDone : err ? de.fleet.commissionRetry : variant === "full" ? de.fleet.commissionDrawer : de.fleet.commissionLabel}
    </button>
  );
}
