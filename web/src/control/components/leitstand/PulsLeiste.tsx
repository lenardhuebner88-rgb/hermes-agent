import type { ReactNode } from "react";
import { AlertTriangle, Command } from "lucide-react";
import { cn } from "@/lib/utils";
import { fmtUsd } from "../../lib/fleetHub";
import { healthLed, healthLabel } from "../../lib/health";
import type { HealthStatus, ToneName } from "../../lib/types";
import { de } from "../../i18n/de";

export interface PulsLeisteGateway {
  status: HealthStatus | "unknown";
  stale: boolean;
  title: string;
}

interface PulsLeisteProps {
  /** Route masthead label (e.g. `navLabel(active)`). */
  label: string;
  subtitle?: string;
  /** Laufende Worker (running-Filter, s. fleetHub.deriveKpi). `null` = keine Quelle. */
  workers: number | null;
  /** Entscheidungs-Inbox-Gesamtzahl (deduped). `null` = keine Quelle. */
  fragen: number | null;
  /** Schwerster Ton der offenen Fragen — färbt LED + Icon, wenn `fragen > 0`. */
  fragenTone?: ToneName;
  /** Kosten heute in USD. `null` = keine Quelle → "—" (nie 0-Fake). */
  kostenUsd: number | null;
  /** true, wenn `kostenUsd` aus dem Äquivalenzwert stammt (kein `actual_cost_usd`) —
   *  markiert den Instrument-Wert mit "äquiv." (gleiche Ehrlichkeits-Regel wie
   *  Fleet's HeuteTab, s. fleetHub.costDisplayValue). */
  kostenIsEquivalent?: boolean;
  gateway: PulsLeisteGateway;
  /** Right-side extra slot (NotificationBridge/StatusDots/⌘K-Button etc.). */
  children?: ReactNode;
  /** Eingebauter ⌘K-Trigger für Aufrufer ohne eigenen Command-Button (nur <tab
   *  sichtbar — ab `tab` trägt die Rail ihr eigenes ⌘K, wie das bisherige
   *  Masthead-CommandButton-Muster). */
  onOpenCommand?: () => void;
}

/**
 * PulsLeiste — das eine geteilte Instrumenten-Band, das jede Route trägt
 * (DESIGN.md "Puls-Leiste contract" / SHELL-SPEC.md W2-b). Links der
 * Routen-Masthead, rechts vier Live-Instrumente in fester Reihenfolge —
 * Worker · Inbox · Kosten · Gateway — dieselbe Muskelgedächtnis-Geometrie
 * auf jeder View. Rein präsentational: KEIN eigenes Daten-Fetching, alle
 * Werte kommen von den bestehenden Hooks des Aufrufers.
 */
export function PulsLeiste({ label, subtitle, workers, fragen, fragenTone = "amber", kostenUsd, kostenIsEquivalent, gateway, children, onOpenCommand }: PulsLeisteProps) {
  const workerActive = typeof workers === "number" && workers > 0;
  const fragenActive = typeof fragen === "number" && fragen > 0;

  return (
    <div
      data-testid="puls-leiste"
      className="flex items-center justify-between gap-3 border-b border-line bg-surface-1 px-4 py-3 sm:px-6 tab:h-14 lg:h-16 lg:px-8"
    >
      {/* Route-Identität darf nie verschwinden: KEIN `min-w-0` hier — die
          rechte Instrumenten-/Utility-Seite ist die, die bei knappem Platz
          (Medium-Tier + der bestehenden StatusDots-Pille) intern scrollt,
          statt das Label auf 0 zusammenzuquetschen. */}
      <div className="flex flex-col justify-center gap-0.5">
        <p className="truncate font-display text-sec font-semibold uppercase tracking-[0.08em] text-ink tab:text-emph">{label}</p>
        {subtitle ? <p className="truncate text-micro text-ink-3">{subtitle}</p> : null}
      </div>
      <div className="flex min-w-0 items-center gap-4 overflow-x-auto">
        <div className="hidden items-center gap-6 tab:flex">
          <Instrument label={de.pulsLeiste.worker} value={workers ?? "—"} valueMuted={!workerActive}>
            <span className={cn("hc-led h-1.5 w-1.5 rounded-full", workerActive ? "hc-led-live" : "hc-led-idle")} />
          </Instrument>
          <Instrument label={de.pulsLeiste.inbox} value={fragen ?? "—"} valueMuted={!fragenActive}>
            {fragenActive ? (
              <>
                <span className={cn("hc-led h-1.5 w-1.5 rounded-full", fragenLedClass(fragenTone))} />
                <AlertTriangle aria-hidden className={cn("h-3 w-3", fragenIconClass(fragenTone))} />
              </>
            ) : null}
          </Instrument>
          <Instrument
            label={de.pulsLeiste.costs}
            value={
              <>
                {fmtUsd(kostenUsd)}
                {kostenIsEquivalent && kostenUsd != null ? <span className="text-micro text-ink-3"> äquiv.</span> : null}
              </>
            }
          />
          <Instrument label={de.pulsLeiste.gateway} value={healthLabel(gateway.status, gateway.stale)} title={gateway.title}>
            <span className={cn("hc-led h-1.5 w-1.5 rounded-full", healthLed(gateway.status, gateway.stale))} />
          </Instrument>
        </div>
        {onOpenCommand ? (
          <button
            type="button"
            title="Command Palette"
            aria-label="Command Palette"
            onClick={onOpenCommand}
            className="hc-hit inline-flex items-center gap-2 rounded-card border border-line px-3 text-sm text-ink-2 hover:bg-surface-2 hover:text-ink tab:hidden"
          >
            <Command className="h-4 w-4" />⌘K
          </button>
        ) : null}
        {children}
      </div>
    </div>
  );
}

// Inbox ist "nie color-only": LED + AlertTriangle-Icon teilen sich denselben
// Zwei-Stufen-Ton (roter Alarm bei red/rose, sonst warn) — dieselbe
// Vereinfachung wie ControlShell.tabBadge für den Postfach-Badge.
function fragenLedClass(tone: ToneName): string {
  return tone === "red" || tone === "rose" ? "hc-led-error" : "hc-led-warn";
}

function fragenIconClass(tone: ToneName): string {
  return tone === "red" || tone === "rose" ? "text-status-alert" : "text-status-warn";
}

/** Ein Mikro-Instrument: 11px Archivo-Caps-Label (optional mit LED/Icon davor)
 *  über einem font-data/tabular Wert. */
function Instrument({ label, value, valueMuted, title, children }: {
  label: string;
  value: ReactNode;
  valueMuted?: boolean;
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div title={title} className="flex flex-col items-end gap-1">
      <span className="flex items-center gap-1.5 font-display text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-3">
        {children}
        {label}
      </span>
      <span className={cn("font-data text-sm font-medium tabular-nums text-ink", valueMuted && "text-ink-3")}>{value}</span>
    </div>
  );
}
