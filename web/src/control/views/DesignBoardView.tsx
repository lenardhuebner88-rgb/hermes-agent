import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
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

const KINDS = ["bug", "wish", "mockup", "reference"] as const;

export function DesignBoardView(_props: { density?: string } = {}) {
  const [cards, setCards] = useState<CardSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<string>("bug");
  const [targetView, setTargetView] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    fetchJSON<CardSummary[]>("/api/design-board/cards")
      .then(setCards)
      .catch((e) => setError(String(e)));
  }, []);

  async function createCard() {
    if (!title.trim()) return;
    setBusy(true);
    try {
      const res = await fetchJSON<{ id: string }>("/api/design-board/cards", {
        method: "POST",
        body: JSON.stringify({
          kind,
          title: title.trim(),
          target: targetView.trim() ? { view: targetView.trim() } : null,
        }),
      });
      navigate(`/control/design-board/${res.id}`);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  return (
    <div className="min-h-full bg-surface-0 p-4">
      <div className="flex items-baseline justify-between gap-3">
        <SectionHeader label="Design Board" meta={`${cards.length} cards`} className="flex-1" />
        <button
          onClick={() => setShowForm((v) => !v)}
          className="shrink-0 rounded-card border border-line px-3 py-1 text-sm text-live"
        >
          {showForm ? "Abbrechen" : "＋ Neue Karte"}
        </button>
      </div>

      {showForm && (
        <div className="mt-3 hc-surface-card p-3">
          <div className="hc-type-label hc-dim">Neue Karte</div>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Titel — z. B. Header überlappt auf Tablet"
            className="mt-2 w-full rounded-card border border-line bg-surface-1 p-2 text-sm text-white"
          />
          <div className="mt-2 flex flex-wrap gap-2">
            {KINDS.map((k) => (
              <button
                key={k}
                onClick={() => setKind(k)}
                className={`rounded-card border border-line px-2 py-1 hc-type-label ${
                  kind === k ? "text-live" : "hc-dim"
                }`}
              >
                {k}
              </button>
            ))}
          </div>
          <input
            value={targetView}
            onChange={(e) => setTargetView(e.target.value)}
            placeholder="Ziel-View (optional) — z. B. FleetView"
            className="mt-2 w-full rounded-card border border-line bg-surface-1 p-2 text-sm text-white"
          />
          <button
            onClick={createCard}
            disabled={busy || !title.trim()}
            className="mt-2 rounded-card border border-line px-3 py-1 text-sm text-live"
          >
            Karte anlegen & Screenshot hinzufügen
          </button>
        </div>
      )}

      {error && (
        <div className="mt-4">
          <FleetEmptyState title="Failed to load" desc={error} />
        </div>
      )}
      {!error && cards.length === 0 && (
        <div className="mt-4">
          <FleetEmptyState title="No design cards yet" desc="Tippe ＋ Neue Karte, um zu starten." />
        </div>
      )}
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {cards.map((c) => (
          <Link key={c.id} to={`/control/design-board/${c.id}`} className="block">
            <FleetPanel eyebrow={c.kind} meta={c.status}>
              <div className="hc-mono text-sm font-semibold text-white">{c.title}</div>
              {c.target?.view && (
                <div className="mt-1 hc-type-label hc-dim">→ {c.target.view}</div>
              )}
              {c.linked_tasks.length > 0 && (
                <div className="mt-1 hc-type-label hc-soft">{c.linked_tasks.length} task(s)</div>
              )}
            </FleetPanel>
          </Link>
        ))}
      </div>
    </div>
  );
}
