import { Stat } from "../../components/primitives";
import { foHealthStripCounts } from "../../lib/foBacklog";
import type { BacklogContractHealth, BacklogItem } from "../../lib/schemas";
import type { ToneName } from "../../lib/types";

export function FoHealthStrip({ items, contractHealth }: { items: BacklogItem[]; contractHealth?: BacklogContractHealth }) {
  const counts = foHealthStripCounts(items, contractHealth);
  const cells: Array<{ label: string; value: number; tone: ToneName }> = [
    { label: "Now", value: counts.now, tone: "sky" },
    { label: "Next Ready", value: counts.nextReady, tone: "indigo" },
    { label: "Blocked", value: counts.blocked, tone: "red" },
    { label: "Unowned", value: counts.unowned, tone: "amber" },
    { label: "Stale", value: counts.stale, tone: "red" },
    { label: "High Risk", value: counts.highRisk, tone: "red" },
    { label: "Contract Drift", value: counts.contractDrift, tone: counts.contractDrift ? "amber" : "zinc" },
    { label: "Missing Acceptance", value: counts.missingAcceptance, tone: counts.missingAcceptance ? "amber" : "zinc" },
  ];
  return (
    <section className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8" aria-label="FO Contract Health">
      {cells.map((cell) => (
        <Stat key={cell.label} label={cell.label} value={cell.value} tone={cell.tone} />
      ))}
    </section>
  );
}
