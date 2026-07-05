import { useEffect, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { SectionHeader, FleetPanel, FleetEmptyState } from "@/control/components/leitstand";

type CardSummary = {
  id: string;
  kind: string;
  title: string;
  target: { view?: string } | null;
  status: string;
  linked_tasks: string[];
  updated_at: number;
};

export function DesignBoardView(_props: { density?: string } = {}) {
  const [cards, setCards] = useState<CardSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchJSON<CardSummary[]>("/api/design-board/cards")
      .then(setCards)
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <div className="min-h-full bg-surface-0 p-4">
      <SectionHeader label="Design Board" meta={`${cards.length} cards`} />
      {error && (
        <div className="mt-4">
          <FleetEmptyState title="Failed to load" desc={error} />
        </div>
      )}
      {!error && cards.length === 0 && (
        <div className="mt-4">
          <FleetEmptyState title="No design cards yet" desc="Drop a screenshot to start a card." />
        </div>
      )}
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {cards.map((c) => (
          <FleetPanel key={c.id} eyebrow={c.kind} meta={c.status}>
            <div className="hc-mono text-sm font-semibold text-white">{c.title}</div>
            {c.target?.view && (
              <div className="mt-1 hc-type-label hc-dim">→ {c.target.view}</div>
            )}
            {c.linked_tasks.length > 0 && (
              <div className="mt-1 hc-type-label hc-soft">{c.linked_tasks.length} task(s)</div>
            )}
          </FleetPanel>
        ))}
      </div>
    </div>
  );
}
