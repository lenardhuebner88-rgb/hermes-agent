import { useCallback, useEffect, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { fetchJSON } from "@/lib/api";
import { FleetPanel, FleetEmptyState, FreshnessStrip, SignalLabel, ErrorNote, ViewHeader } from "@/control/components/leitstand";
import { Eyebrow } from "@/control/components/primitives";
import { de } from "@/control/i18n/de";
import { nowSec } from "@/control/lib/derive";
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

function targetPreview(view: string): string {
  const normalized = view.replace(/\s+/g, " ").trim();
  if (normalized.length <= 72) return normalized;
  return `${normalized.slice(0, 72)}…`;
}

export function DesignBoardView(_props: { density?: string } = {}) {
  const [cards, setCards] = useState<CardSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<string>("bug");
  const [targetView, setTargetView] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  const load = useCallback(async () => {
    try {
      setCards(await fetchJSON<CardSummary[]>("/api/design-board/cards"));
      setError(null);
      setLastUpdated(nowSec());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    // Erst-Load direkt (nicht per setTimeout): der POST im Anlege-Formular muss
    // die Mock-Reihenfolge des List-Loads nicht überholen können — ein späterer
    // Load mit Nicht-Array-Payload crashte `cards.some` (Gate 2026-07-30).
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async Load; kein synchrones setState im Effect-Body
    void load();
  }, [load]);

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
      <ViewHeader
        eyebrow="FEEDBACK · VARIANTEN"
        title="Design Board"
        description={`${cards.length} Karten`}
        actions={(
          <button
            onClick={() => setShowForm((v) => !v)}
            aria-expanded={showForm}
            className="min-h-12 shrink-0 rounded-card border border-line px-3 py-1 text-sec text-live transition-colors duration-150 ease-out hover:border-live hover:bg-live/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          >
            {showForm ? "Abbrechen" : "＋ Neue Karte"}
          </button>
        )}
      />
      <FreshnessStrip lastUpdated={lastUpdated} onRefresh={() => load()} />

      {showForm && (
        <div className="mt-3 rounded-panel border border-line bg-surface-2 p-3">
          <Eyebrow>Neue Karte</Eyebrow>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            aria-label="Titel"
            placeholder="Titel — z. B. Header überlappt auf Tablet"
            className="mt-2 min-h-12 w-full rounded-card border border-line bg-surface-2 p-2 text-body text-ink placeholder:text-ink-3 focus:border-live"
          />
          <div className="mt-2 flex flex-wrap gap-2">
            {KINDS.map((k) => (
              <button
                key={k}
                onClick={() => setKind(k)}
                aria-pressed={kind === k}
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
            aria-label="Ziel-View (optional)"
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
        <ErrorNote className="mt-4" message="Laden fehlgeschlagen" detail={error} onRetry={() => void load()} />
      )}
      {!error && cards.length === 0 && (
        <div className="mt-4">
          <FleetEmptyState
            title="Noch keine Design-Karten"
            desc="Der Arbeitsbereich ist noch unbestückt."
            action={(
              <button type="button" onClick={() => setShowForm(true)} className="inline-flex min-h-12 items-center rounded-card border border-live px-3 text-sec text-live hover:bg-live/10">
                Neue Karte anlegen
              </button>
            )}
          />
        </div>
      )}
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {cards.map((c) => (
          <Link key={c.id} to={`/control/design-board/${c.id}`} className="block min-w-0">
            <FleetPanel eyebrow={c.kind} meta={<SignalLabel tone={designStatusTone(c.derived_status ?? c.status)} label={statusLabel(c.derived_status ?? c.status)} />}>
              <div className="text-sec font-semibold text-ink">{c.title}</div>
              {c.target?.view && (
                <div className="mt-1 line-clamp-1 font-data text-micro text-ink-3" title={c.target.view}>
                  → {targetPreview(c.target.view)}
                </div>
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
