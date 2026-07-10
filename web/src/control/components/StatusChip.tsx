import type { ComponentType } from "react";
import { cn } from "@/lib/utils";
import type { ToneName } from "../lib/types";

/**
 * StatusChip — der getönte KPI-Chip des Leitstands: LED-Punkt + Icon + Archivo-
 * Eyebrow-Label über einem Plex-Mono-Wert mit optionalem Hinweis. Die EINE
 * geteilte Fassung des zuvor in PressureView UND OpsRadarView wörtlich
 * kopierten Bausteins (S1-Fusion: System-View). Formsprache nach DESIGN.md:
 * Status ist nie farb-only — der Punkt trägt IMMER neben dem Label; neutral
 * bleibt chromlos (keine Wäsche), warn/alert bekommen nur eine dezente
 * 15%-Tönung auf der Fläche statt einer knalligen Volltonpille. Der Wert
 * selbst bleibt neutral, damit die Farbe Bedeutung behält statt Dekoration zu
 * werden (DESIGN.md, Regel 1 + 2).
 */
export function StatusChip({ icon: Icon, label, value, hint, tone = "zinc" }: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: string;
  hint?: string;
  tone?: ToneName;
}) {
  const tint = chipTint(tone);
  return (
    <div className={cn("min-h-16 min-w-0 rounded-lg border px-2.5 py-2 sm:min-h-20 sm:px-3", tint.className)} style={tint.style}>
      <div className="flex min-w-0 items-center gap-2">
        <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: dotColor(tone) }} />
        <Icon className="h-3.5 w-3.5 shrink-0 hc-dim" />
        <span className="truncate font-display uppercase tracking-[0.08em] text-micro font-semibold text-ink-3">{label}</span>
      </div>
      <p className="mt-1 truncate font-data tabular-nums text-sec font-semibold text-ink sm:mt-2">{value}</p>
      {hint ? <p className="mt-0.5 line-clamp-1 hc-type-label hc-soft">{hint}</p> : null}
    </div>
  );
}

/** Ton → LED-Punktfarbe. Status ist nie farb-only (immer + Label), also trägt
 *  JEDER Ton einen Punkt — neutral bekommt die tertiäre Ink-Farbe statt einer
 *  Statusfarbe statt still zu bleiben. */
function dotColor(tone: ToneName): string {
  switch (tone) {
    case "red":
    case "rose":
      return "var(--color-status-alert)";
    case "amber":
      return "var(--color-status-warn)";
    case "emerald":
      return "var(--color-status-ok)";
    case "cyan":
      return "var(--color-live)";
    default:
      return "var(--color-ink-3)";
  }
}

/** Ton → Rahmen + Fläche. Neutral bleibt die normale Karten-Fläche ohne Tönung
 *  ("keine Pille"); warn/alert bekommen eine dezente 15%-Wäsche auf der
 *  Fläche statt einer knalligen Volltonpille; alle nicht-neutralen Töne
 *  tragen inzwischen dieselbe Token-abgeleitete Tönung (DESIGN.md-Doktrin:
 *  "no pill unless already present"). Deckt die Töne ab, die Pressure (red/amber/emerald/zinc) und Ops
 *  Radar (rose/cyan zusätzlich) je an den Chip reichen. */
function chipTint(tone: ToneName): { className: string; style?: React.CSSProperties } {
  switch (tone) {
    case "red":
    case "rose":
      return {
        className: "border-status-alert/25",
        style: { backgroundColor: "color-mix(in srgb, var(--color-status-alert) 15%, var(--color-surface-2))" },
      };
    case "amber":
      return {
        className: "border-status-warn/25",
        style: { backgroundColor: "color-mix(in srgb, var(--color-status-warn) 15%, var(--color-surface-2))" },
      };
    case "emerald":
      return {
        className: "border-status-ok/25",
        style: { backgroundColor: "color-mix(in srgb, var(--color-status-ok) 15%, var(--color-surface-2))" },
      };
    case "cyan":
      return {
        className: "border-live/25",
        style: { backgroundColor: "color-mix(in srgb, var(--color-live) 12%, var(--color-surface-2))" },
      };
    default:
      return { className: "border-line bg-surface-2" };
  }
}
