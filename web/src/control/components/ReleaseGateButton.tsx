import { useState } from "react";
import { cn } from "@/lib/utils";
import type { useReleaseGateExecute } from "../hooks/useControlData";

/** Release-Gate: confirm-gated execution for a parked post-merge child.
 *  Zwei-Klick-Muster: erster Klick scharfschalten, zweiter Klick feuert
 *  POST .../release-gate. Theme-agnostisch — Default-Skin ist die Fleet
 *  `fleet-ta-btn`-Aktionsknopf-Idiom (siehe TaskActions.tsx); `className`
 *  hängt zusätzliche/überschreibende Klassen an, falls die Fläche das
 *  irgendwann anderswo braucht. Einziges Zuhause seit der Umzug aus dem
 *  /control-Postfach: Fleet → Risiko. */
export function ReleaseGateButton({ taskId, releaseGate, className }: {
  taskId: string;
  releaseGate: ReturnType<typeof useReleaseGateExecute>;
  className?: string;
}) {
  const [arming, setArming] = useState(false);
  const busy = releaseGate.busyId === taskId;
  const done = !!releaseGate.doneIds[taskId];
  const err = releaseGate.errorById[taskId];
  if (done) {
    return (
      <span
        className={cn("fleet-ta-btn", className)}
        style={{ color: "var(--fleet-gruen)", borderColor: "rgba(67,214,154,.35)" }}
      >
        Release-Gate grün
      </span>
    );
  }
  return (
    <span className="flex flex-col items-stretch gap-1">
      <button
        type="button"
        disabled={busy}
        onClick={(e) => {
          e.stopPropagation();
          if (!arming) { setArming(true); return; }
          setArming(false);
          void releaseGate.run(taskId);
        }}
        onBlur={() => setArming(false)}
        className={cn("fleet-ta-btn", className)}
        style={arming ? { color: "var(--fleet-rot)", borderColor: "rgba(255,93,115,.5)" } : undefined}
      >
        {busy ? "läuft…" : arming ? "Sicher? Erneut klicken" : "Release-Gate ausführen"}
      </button>
      {err ? <p className="fleet-ta-error">{err}</p> : null}
    </span>
  );
}
