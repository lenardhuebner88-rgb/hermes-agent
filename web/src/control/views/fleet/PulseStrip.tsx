/**
 * PulseStrip — die drei Puls-Kacheln über den Swimlanes: Slots (belegt/Cap +
 * Queue), Heute fertig (done ✓ · blocked ◼) und die Live-Token-Summe über alle
 * aktiven Worker. Werte kommen als reine PulseSummary (derivePulse).
 *
 * Cyan (--fleet-puls) trägt nur die Slots-Zahl, wenn tatsächlich Worker laufen
 * (live), grün nur die semantische „done"-Zahl — konform mit DESIGN.md Regel 1+2.
 */
import type { ReactNode } from "react";
import { fmtTokens, type PulseSummary } from "../../lib/fleetHub";
import { de } from "../../i18n/de";

function PulseTile({ label, value, tone }: { label: string; value: ReactNode; tone?: "live" | "ok" }) {
  const vcls =
    tone === "live"
      ? "fleet-ptile-v fleet-ptile-v-live"
      : tone === "ok"
        ? "fleet-ptile-v fleet-ptile-v-ok"
        : "fleet-ptile-v";
  return (
    <div className="fleet-ptile">
      <div className="fleet-ptile-k">{label}</div>
      <div className={vcls}>{value}</div>
    </div>
  );
}

export function PulseStrip({ pulse }: { pulse: PulseSummary }) {
  const slotsSuffix =
    pulse.queue > 0 ? (
      <small>
        {" "}
        +{pulse.queue} {de.fleet.pulseQueue}
      </small>
    ) : pulse.slotsUsed === 0 ? (
      <small> {de.fleet.pulseFree}</small>
    ) : null;

  return (
    <div className="fleet-pulse">
      <PulseTile
        label={de.fleet.pulseSlots}
        tone={pulse.slotsUsed > 0 ? "live" : undefined}
        value={
          <>
            {pulse.slotsUsed}/{pulse.slotsCap ?? "∞"}
            {slotsSuffix}
          </>
        }
      />
      <PulseTile
        label={de.fleet.pulseDoneToday}
        tone="ok"
        value={
          <>
            {pulse.doneToday ?? "—"}
            <small> ✓{pulse.blocked > 0 ? ` · ${pulse.blocked} ◼` : ""}</small>
          </>
        }
      />
      <PulseTile label={de.fleet.pulseTokens} value={fmtTokens(pulse.tokenSum)} />
    </div>
  );
}
