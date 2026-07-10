import { KpiTile } from "../../components/leitstand";
import { foHealthStripCounts } from "../../lib/foBacklog";
import type { BacklogContractHealth, BacklogItem } from "../../lib/schemas";
import type { DotKind } from "../../lib/tones";

/** Kontrakt-Gesundheit als KpiTile-Zeile (W4-1-Nachzügler: war bei der
 *  Backlog-Migration nicht im Inventar; gleiche Grammatik wie die
 *  Orchestrator-SignalStrip — Zähler in Ruhe, Defekt-Zähler mit LED). */
export function FoHealthStrip({ items, contractHealth }: { items: BacklogItem[]; contractHealth?: BacklogContractHealth }) {
  const counts = foHealthStripCounts(items, contractHealth);
  const cells: Array<{ label: string; value: number; dot?: DotKind }> = [
    { label: "Now", value: counts.now },
    { label: "Next Ready", value: counts.nextReady, dot: "ready" },
    { label: "Blocked", value: counts.blocked, dot: "error" },
    { label: "Unowned", value: counts.unowned, dot: "warn" },
    { label: "Stale", value: counts.stale, dot: "error" },
    { label: "High Risk", value: counts.highRisk, dot: "error" },
    { label: "Contract Drift", value: counts.contractDrift, dot: counts.contractDrift ? "warn" : "idle" },
    { label: "Missing Acceptance", value: counts.missingAcceptance, dot: counts.missingAcceptance ? "warn" : "idle" },
  ];
  return (
    <section className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8" aria-label="FO Contract Health">
      {cells.map((cell) => (
        <KpiTile key={cell.label} label={cell.label} value={cell.value} dot={cell.dot} />
      ))}
    </section>
  );
}
