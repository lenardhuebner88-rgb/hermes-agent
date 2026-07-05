import type { ComponentType } from "react";
import { cn } from "@/lib/utils";
import type { ToneName } from "../lib/types";

/**
 * StatusChip — der getönte KPI-Chip des Leitstands: Icon + Eyebrow-Label über
 * einem mono-Wert mit optionalem Hinweis. Die EINE geteilte Fassung des zuvor
 * in PressureView UND OpsRadarView wörtlich kopierten Bausteins (S1-Fusion:
 * System-View). Der Ton trägt nur als Hairline-Wäsche (Status-Trio + Cyan für
 * live/interaktiv); der Wert selbst bleibt neutral (weiß), damit die Farbe
 * Bedeutung behält statt Dekoration zu werden (DESIGN.md, Regel 1 + 2).
 */
export function StatusChip({ icon: Icon, label, value, hint, tone = "zinc" }: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: string;
  hint?: string;
  tone?: ToneName;
}) {
  return (
    <div className={cn("min-h-16 min-w-0 rounded-lg border px-2.5 py-2 sm:min-h-20 sm:px-3", chipTint(tone))}>
      <div className="flex min-w-0 items-center gap-2">
        <Icon className="h-3.5 w-3.5 shrink-0 hc-dim" />
        <span className="truncate hc-type-label hc-dim">{label}</span>
      </div>
      <p className="mt-1 truncate hc-mono text-sm font-semibold text-white sm:mt-2">{value}</p>
      {hint ? <p className="mt-0.5 line-clamp-1 hc-type-label hc-soft">{hint}</p> : null}
    </div>
  );
}

/** Ton → Hairline-Border + transluzente Wäsche. Deckt die Töne ab, die Pressure
 *  (red/amber/emerald/zinc) und Ops Radar (rose/cyan zusätzlich) je an den Chip
 *  reichen; alles übrige fällt auf die neutrale Fläche zurück. */
function chipTint(tone: ToneName): string {
  switch (tone) {
    case "red":
      return "border-red-500/25 bg-red-500/10";
    case "rose":
      return "border-rose-500/25 bg-rose-500/10";
    case "amber":
      return "border-amber-500/25 bg-amber-500/10";
    case "emerald":
      return "border-emerald-500/25 bg-emerald-500/10";
    case "cyan":
      return "border-cyan-500/25 bg-cyan-500/10";
    default:
      return "border-white/10 bg-white/[.03]";
  }
}
