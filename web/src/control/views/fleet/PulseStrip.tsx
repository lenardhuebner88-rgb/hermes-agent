/**
 * PulseStrip — die drei Puls-Kacheln über den Worker-Karten: Slots (belegt/Cap
 * + Queue), Heute fertig (done ✓ · blocked ◼) und die Token-Kachel. Werte
 * kommen als reine PulseSummary (derivePulse).
 *
 * V2 (2026-07-27):
 * - Scope-Ehrlichkeit: Slots/Tokens aggregieren über ALLE Boards, Queue/Blocked
 *   nur über das aktuelle — die Queue trägt deshalb das Suffix „(Board)".
 * - Token-Kachel ehrlich: ohne jede Live-Stichprobe zeigt sie „—" + den
 *   Hinweis „Werte beim Closeout" statt einer falschen 0; die Tages-Summe
 *   (aus /runs/costs, abgeschlossene Runs) reitet als Sub-Zeile mit.
 *
 * Bronze (--fleet-puls) trägt nur die Slots-Zahl, wenn tatsächlich Worker
 * laufen (live), grün nur die semantische „done"-Zahl — DESIGN.md Regel 1+2.
 */
import type { ReactNode } from "react";
import { fmtTokens, type PulseSummary } from "../../lib/fleetHub";
import { de } from "../../i18n/de";

function PulseTile({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "live" | "ok" }) {
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
      {sub ? <div className="fleet-ptile-sub">{sub}</div> : null}
    </div>
  );
}

export function PulseStrip({
  pulse,
  tokensToday = null,
}: {
  pulse: PulseSummary;
  /** Token-Summe (ein+aus) abgeschlossener Runs heute — aus /runs/costs. */
  tokensToday?: number | null;
}) {
  const slotsSuffix =
    pulse.queue > 0 ? (
      <small>
        {" "}
        +{pulse.queue} {de.fleet.pulseQueue} {de.fleet.pulseQueueBoardSuffix}
      </small>
    ) : pulse.slotsUsed === 0 ? (
      <small> {de.fleet.pulseFree}</small>
    ) : null;

  const tokenValue =
    pulse.tokenSum == null ? (
      <>
        —<small> {de.fleet.pulseTokensCloseout}</small>
      </>
    ) : (
      fmtTokens(pulse.tokenSum)
    );
  const tokenSub =
    tokensToday != null && tokensToday > 0 ? de.fleet.pulseTokensToday(fmtTokens(tokensToday)) : null;

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
      <PulseTile label={de.fleet.pulseTokens} value={tokenValue} sub={tokenSub} />
    </div>
  );
}
