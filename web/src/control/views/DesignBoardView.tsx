import { useEffect, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { fetchJSON } from "@/lib/api";
import { SectionHeader, FleetPanel, FleetEmptyState, SignalLabel } from "@/control/components/leitstand";
import { Eyebrow } from "@/control/components/primitives";
import { de } from "@/control/i18n/de";
import { statusLabel } from "./designboard/status";

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

function designStatusTone(status: string): "ok" | "warn" | "neutral" {
  if (status === "addressed") return "ok";
  if (status === "in_progress") return "warn";
  return "neutral";
}

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
          className="min-h-12 shrink-0 rounded-card border border-line px-3 py-1 text-sec text-live hover:border-live hover:bg-live/10"
        >
          {showForm ? "Abbrechen" : "＋ Neue Karte"}
        </button>
      </div>

      {showForm && (
        <div className="mt-3 rounded-panel border border-line bg-surface-2 p-3">
          <Eyebrow>Neue Karte</Eyebrow>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Titel — z. B. Header überlappt auf Tablet"
            className="mt-2 min-h-12 w-full rounded-card border border-line bg-surface-2 p-2 text-body text-ink placeholder:text-ink-3 focus:border-live"
          />
          <div className="mt-2 flex flex-wrap gap-2">
            {KINDS.map((k) => (
              <button
                key={k}
                onClick={() => setKind(k)}
                className={`min-h-12 rounded-card border border-line px-2 py-1 text-micro ${
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
            className="mt-2 min-h-12 w-full rounded-card border border-line bg-surface-2 p-2 text-body text-ink placeholder:text-ink-3 focus:border-live"
          />
          <button
            onClick={createCard}
            disabled={busy || !title.trim()}
            className="mt-2 min-h-12 rounded-card border border-live px-3 py-1 text-sec text-live hover:bg-live/10 disabled:opacity-45"
          >
            Karte anlegen & Screenshot hinzufügen
          </button>
        </div>
      )}

      {!error && cards.some((c) => c.kanban_ok === false) && (
        <div className="mt-3 flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn">
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{de.designBoard.kanbanUnavailable}
        </div>
      )}

      {error && (
        <div role="alert" className="mt-4 flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert">
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
          <span><strong>Laden fehlgeschlagen</strong><br />{error}</span>
        </div>
      )}
      {!error && cards.length === 0 && (
        <div className="mt-4">
          <FleetEmptyState
            title="Noch keine Design-Karten"
            desc={(
              <span className="inline-flex flex-col items-start gap-2">
                <span>Der Arbeitsbereich ist noch unbestückt.</span>
                <button type="button" onClick={() => setShowForm(true)} className="inline-flex min-h-12 items-center rounded-card border border-live px-3 text-sec text-live hover:bg-live/10">
                  Neue Karte anlegen
                </button>
              </span>
            )}
          />
        </div>
      )}
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {cards.map((c) => (
          <Link key={c.id} to={`/control/design-board/${c.id}`} className="block">
            <FleetPanel eyebrow={c.kind} meta={<SignalLabel tone={designStatusTone(c.derived_status ?? c.status)} label={statusLabel(c.derived_status ?? c.status)} />}>
              <div className="text-sec font-semibold text-ink">{c.title}</div>
              {c.target?.view && (
                <div className="mt-1 font-data text-micro text-ink-3">→ {c.target.view}</div>
              )}
              {c.linked_tasks.length > 0 && (
                <div className="mt-1 text-micro text-ink-2"><span className="font-data tabular-nums">{c.linked_tasks.length}</span> verknüpfte Aufgabe(n)</div>
              )}
            </FleetPanel>
          </Link>
        ))}
      </div>
    </div>
  );
}
