import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { fetchJSON } from "@/lib/api";
import { SectionHeader, FleetPanel, FleetEmptyState } from "@/control/components/leitstand";
import { de } from "@/control/i18n/de";
import { statusBadge } from "./designboard/status";

type CardSummary = {
  id: string;
  kind: string;
  title: string;
  target: { view?: string } | null;
  status: string;
  derived_status: string | null;
  linked_tasks: string[];
  updated_at: number;
  kanban_ok?: boolean;
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
        <SectionHeader label="Design Board" meta={`${cards.length} Karten`} className="flex-1" />
        <button
          onClick={() => setShowForm((v) => !v)}
          className="shrink-0 rounded-card border border-line px-3 py-1 text-sm text-live"
        >
          {showForm ? "Abbrechen" : "＋ Neue Karte"}
        </button>
      </div>

      {showForm && (
        <div className="mt-3 rounded-panel border border-line bg-surface-2 p-3">
          <div className="hc-type-label text-ink-3">Neue Karte</div>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Titel — z. B. Header überlappt auf Tablet"
            className="mt-2 w-full rounded-card border border-line bg-surface-3 p-2 text-sm text-ink placeholder:text-ink-3"
          />
          <div className="mt-2 flex flex-wrap gap-2">
            {KINDS.map((k) => (
              <button
                key={k}
                onClick={() => setKind(k)}
                className={`rounded-card border border-line px-2 py-1 hc-type-label ${
                  kind === k ? "text-live" : "text-ink-3"
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
            className="mt-2 w-full rounded-card border border-line bg-surface-3 p-2 text-sm text-ink placeholder:text-ink-3"
          />
          <button
            onClick={createCard}
            disabled={busy || !title.trim()}
            className="mt-2 rounded-card border border-live px-3 py-1 text-sm text-live disabled:opacity-45"
          >
            Karte anlegen & Screenshot hinzufügen
          </button>
        </div>
      )}

      {!error && cards.some((c) => c.kanban_ok === false) && (
        <div className="mt-3 rounded-card border border-status-warn/20 bg-status-warn/10 p-2 text-xs text-status-warn">
          {de.designBoard.kanbanUnavailable}
        </div>
      )}

      {error && (
        <div className="mt-4">
          <FleetEmptyState title="Laden fehlgeschlagen" desc={error} />
        </div>
      )}
      {!error && cards.length === 0 && (
        <div className="mt-4">
          <FleetEmptyState title="Noch keine Design-Karten" desc="Tippe ＋ Neue Karte, um zu starten." />
        </div>
      )}
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {cards.map((c) => (
          <Link key={c.id} to={`/control/design-board/${c.id}`} className="block">
            <FleetPanel eyebrow={c.kind} meta={statusBadge(c.derived_status ?? c.status)}>
              <div className="hc-mono text-sm font-semibold text-white">{c.title}</div>
              {c.target?.view && (
                <div className="mt-1 hc-type-label hc-dim">→ {c.target.view}</div>
              )}
              {c.linked_tasks.length > 0 && (
                <div className="mt-1 hc-type-label hc-soft">{c.linked_tasks.length} verknüpfte Aufgabe(n)</div>
              )}
            </FleetPanel>
          </Link>
        ))}
      </div>
    </div>
  );
}
