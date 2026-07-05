import type { ComponentType, ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Led } from "../atoms";
import { Eyebrow } from "../primitives";
import type { DotKind } from "../../lib/tones";

/**
 * KpiTile — the ONE canonical value+label tile of the Leitstand: an eyebrow
 * label (with an optional status dot or icon), a big mono value with optional
 * suffix, and an optional delta line. Generalises the Fleet `FleetPod` and the
 * inline StatsMasthead KPIs into a single neutral, token-based primitive.
 *
 * The surface stays neutral; colour is carried only by the delta (status trio),
 * so the tone keeps meaning instead of becoming decoration (DESIGN.md rule 1+2).
 */
export function KpiTile({
  label,
  value,
  suffix,
  delta,
  deltaTone = "neutral",
  dot,
  icon: Icon,
  className,
}: {
  label: ReactNode;
  value: ReactNode;
  suffix?: ReactNode;
  delta?: ReactNode;
  deltaTone?: "up" | "down" | "neutral";
  dot?: DotKind;
  icon?: ComponentType<{ className?: string }>;
  className?: string;
}) {
  return (
    <div className={cn("min-w-0 rounded-card border border-line bg-surface-2 px-3 py-2.5", className)}>
      <div className="flex min-w-0 items-center gap-2">
        {dot ? <Led kind={dot} size={7} /> : null}
        {Icon ? <Icon className="h-3.5 w-3.5 shrink-0 hc-dim" /> : null}
        <Eyebrow>{label}</Eyebrow>
      </div>
      <div className="mt-1.5 truncate hc-mono text-lg font-semibold tabular-nums text-ink">
        {value}
        {suffix != null ? <small className="hc-dim"> {suffix}</small> : null}
      </div>
      {delta != null ? (
        <div
          className={cn(
            "mt-0.5 hc-type-label",
            deltaTone === "up" ? "text-status-ok" : deltaTone === "down" ? "text-status-alert" : "hc-dim",
          )}
        >
          {delta}
        </div>
      ) : null}
    </div>
  );
}
