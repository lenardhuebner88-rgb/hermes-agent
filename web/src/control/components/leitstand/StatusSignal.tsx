import { cn } from "@/lib/utils";

/**
 * StatusSignal — die EINE geteilte Status-Wortmarke des Leitstands, in zwei
 * Formen (W4-Konsolidierung: ersetzt die per-View nachgebauten SignalLabel/
 * SignalChip-Klone aus der Backlog-/Orchestrator-Migration):
 *
 *  - `SignalLabel` — nackter LED-Punkt + Wort, inline (Karten-Metazeilen,
 *    Spalten-Header). Kein Chip-Körper: dort einsetzen, wo ein Rahmen nur
 *    Rauschen wäre.
 *  - `SignalChip` — Chip-Körper (Border + Tint + Punkt + Wort) für Tabellen-
 *    Statuszellen und Callout-nahe Stellen.
 *
 * Status ist nie farb-only: beide Formen tragen IMMER das Wort; der Punkt ist
 * aria-hidden. Bronze ist hier verboten (Status ≠ Interaktion, DESIGN.md).
 * `signalToneFromLegacy` übersetzt das alte ToneName-Vokabular (red/rose/
 * amber/emerald/…) beim Migrieren — neuer Code benennt Töne direkt.
 */
export type SignalTone = "ok" | "warn" | "alert" | "neutral";

// eslint-disable-next-line react-refresh/only-export-components -- migration helper is part of the canonical status primitive API
export function signalToneFromLegacy(tone: string | undefined): SignalTone {
  if (tone === "red" || tone === "rose") return "alert";
  if (tone === "amber") return "warn";
  if (tone === "emerald") return "ok";
  return "neutral";
}

const DOT: Record<SignalTone, string> = {
  ok: "bg-status-ok",
  warn: "bg-status-warn",
  alert: "bg-status-alert",
  neutral: "bg-ink-3",
};

const TEXT: Record<SignalTone, string> = {
  ok: "text-status-ok",
  warn: "text-status-warn",
  alert: "text-status-alert",
  neutral: "text-ink-2",
};

const CHIP: Record<SignalTone, string> = {
  ok: "border-status-ok/30 bg-status-ok/10 text-status-ok",
  warn: "border-status-warn/30 bg-status-warn/10 text-status-warn",
  alert: "border-status-alert/30 bg-status-alert/10 text-status-alert",
  neutral: "border-line bg-surface-2 text-ink-2",
};

export function SignalLabel({ tone, label, className }: { tone: SignalTone; label: string; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-micro font-medium", TEXT[tone], className)}>
      <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", DOT[tone])} />
      {label}
    </span>
  );
}

export function SignalChip({ tone, label, className }: { tone: SignalTone; label: string; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-micro font-medium", CHIP[tone], className)}>
      <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", DOT[tone])} />
      {label}
    </span>
  );
}
