/**
 * PulseStrip — die drei Puls-Kacheln über den Worker-Karten: Slots (aktiv/Cap
 * + Queue), Heute fertig (done ✓ · blocked ◼) und die Token-Kachel. Werte
 * kommen als reine PulseSummary (derivePulse).
 *
 * V2 (2026-07-27): Token-Kachel ehrlich („— · Werte beim Closeout" ohne jede
 * Live-Stichprobe; Tages-Summe als Sub-Zeile).
 *
 * F5 (2026-07-28): durchgängige Scope-Ehrlichkeit. Jede Kachel trägt ein
 * Micro-Scope-Label, weil die Quellen unterschiedlich aggregieren:
 * - slotsUsed + tokenSum  → ALLE Boards (useAllBoardWorkers)
 * - slotsCap, queue, blocked, doneToday, tokensToday → aktuelles Board
 * Das Slots-Verhältnis ist deshalb KEIN Bruch mehr („4/3" las sich als
 * Cap-Verletzung): „4 aktiv · Cap 3 (Board)".
 *
 * Bronze (--fleet-puls) trägt nur die Slots-Zahl, wenn tatsächlich Worker
 * laufen (live), grün nur die semantische „done"-Zahl — DESIGN.md Regel 1+2.
 */
import type { ReactNode } from "react";
import { fmtTokens, type PulseSummary } from "../../lib/fleetHub";
import { de } from "../../i18n/de";

function PulseTile({
  label,
  value,
  sub,
  tone,
  scope,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "live" | "ok";
  /** Micro-Scope-Label in der Eyebrow-Zeile (z. B. „alle Boards" / „Board"). */
  scope?: string;
}) {
  const vcls =
    tone === "live"
      ? "fleet-ptile-v fleet-ptile-v-live"
      : tone === "ok"
        ? "fleet-ptile-v fleet-ptile-v-ok"
        : "fleet-ptile-v";
  return (
    <div className="fleet-ptile">
      <div className="fleet-ptile-k">
        {label}
        {scope ? <span className="fleet-ptile-scope">{scope}</span> : null}
      </div>
      <div className={vcls}>{value}</div>
      {sub ? <div className="fleet-ptile-sub">{sub}</div> : null}
    </div>
  );
}

export function PulseStrip({
  pulse,
  tokensToday = null,
  workersScopeAllBoards = true,
}: {
  pulse: PulseSummary;
  /** Token-Summe (ein+aus) abgeschlossener Runs heute — aus /runs/costs (aktuelles Board). */
  tokensToday?: number | null;
  /** T5a: false, wenn die Worker-Quelle (Fehler/Erstladung) aufs aktuelle Board
   * zurückgefallen ist — dann darf die Kachel nicht „alle Boards" behaupten. */
  workersScopeAllBoards?: boolean;
}) {
  // T5b: jeder Wert trägt seinen Scope direkt am Wert — die Slots-Kachel
  // mischt all-boards (aktiv) mit board-lokal (Cap, Queue).
  const workerScope = workersScopeAllBoards ? de.fleet.pulseAllBoardsSuffix : de.fleet.pulseQueueBoardSuffix;
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
    tokensToday != null && tokensToday > 0
      ? `${de.fleet.pulseTokensToday(fmtTokens(tokensToday))} ${de.fleet.pulseQueueBoardSuffix}`
      : null;

  return (
    <div className="fleet-pulse">
      <PulseTile
        label={de.fleet.pulseSlots}
        tone={pulse.slotsUsed > 0 ? "live" : undefined}
        value={
          <>
            {pulse.slotsUsed} {de.fleet.pulseActive}
            <small>
              {" "}
              {workerScope} · {de.fleet.pulseCapLabel} {pulse.slotsCap ?? "∞"} {de.fleet.pulseQueueBoardSuffix}
              {slotsSuffix}
            </small>
          </>
        }
      />
      <PulseTile
        label={de.fleet.pulseDoneToday}
        scope={de.fleet.pulseScopeBoard}
        tone="ok"
        value={
          <>
            {pulse.doneToday ?? "—"}
            <small> ✓{pulse.blocked > 0 ? ` · ${pulse.blocked} ◼` : ""}</small>
          </>
        }
      />
      <PulseTile
        label={de.fleet.pulseTokens}
        scope={workersScopeAllBoards ? de.fleet.pulseScopeAllBoards : de.fleet.pulseScopeBoard}
        value={tokenValue}
        sub={tokenSub}
      />
    </div>
  );
}
