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
        "inline-flex min-h-12 items-center gap-1.5 rounded-card border font-medium transition disabled:cursor-default",
        variant === "full" ? "w-full justify-center px-4 text-sec" : "px-3 text-sec",
        done
          ? "border-line bg-surface-2 text-ink-2"
          : err
            ? "border-live/40 bg-live/10 text-bronze-hi hover:bg-live/15"
            : "border-live/40 bg-live/10 text-bronze-hi hover:bg-live/15",
        className,
      )}
    >
      {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : done ? <Check className="h-3.5 w-3.5" /> : err ? <RotateCcw className="h-3.5 w-3.5" /> : <ArrowRight className="h-3.5 w-3.5" />}
      {busy ? de.fleet.commissionBusy : done ? de.fleet.commissionDone : err ? de.fleet.commissionRetry : variant === "full" ? de.fleet.commissionDrawer : de.fleet.commissionLabel}
    </button>
  );
}
