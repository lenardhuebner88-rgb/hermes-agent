import { cn } from "@/lib/utils";
import { FO_REASON_LABELS } from "../../lib/foBacklog";
import type { FoReasonCode } from "../../lib/foBacklog";

export function ReasonChips({ codes, max = 4 }: { codes: FoReasonCode[]; max?: number }) {
  if (!codes.length) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {codes.slice(0, max).map((code) => {
        const negative = code.startsWith("penalty_") || code === "needs_grooming" || code === "drift" || code === "missing_acceptance" || code === "missing_next_action";
        return (
          <span
            key={code}
            className={cn(
              "rounded-sm px-1.5 py-0.5 text-[10px] font-medium",
              negative ? "bg-amber-500/10 text-amber-200" : "bg-cyan-500/10 text-cyan-200",
            )}
          >
            {FO_REASON_LABELS[code] ?? code}
          </span>
        );
      })}
    </div>
  );
}
