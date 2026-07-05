import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchJSON } from "@/lib/api";
import { SectionHeader, FleetEmptyState } from "@/control/components/leitstand";
import { PinOverlay, type Pin } from "./PinOverlay";

type Facet = { id: string; status: string; assignee: string | null; terminal: boolean };
type Entry = {
  id: string; author: string; kind: string; note: string;
  asset: string | null; html: string | null; pins: Pin[]; created_at: number;
};
type CardDetailData = {
  id: string; kind: string; title: string; status: string;
  target: { view?: string } | null; linked_tasks: string[];
  entries: Entry[]; task_facets: Facet[]; derived_status: string | null;
};

function assetUrl(cardId: string, asset: string): string {
  return `/api/design-board/cards/${cardId}/assets/${asset.split("/").pop()}`;
}

export function CardDetail(_props: { density?: string } = {}) {
  const { cardId = "" } = useParams<{ cardId: string }>();
  const [card, setCard] = useState<CardDetailData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draftAsset, setDraftAsset] = useState<string | null>(null);
  const [draftPins, setDraftPins] = useState<Pin[]>([]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    fetchJSON<CardDetailData>(`/api/design-board/cards/${cardId}`)
      .then(setCard)
      .catch((e) => setError(String(e)));
  }, [cardId]);

  useEffect(() => { load(); }, [load]);

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetchJSON<{ name: string }>(
        `/api/design-board/cards/${cardId}/images`, { method: "POST", body: fd });
      setDraftAsset(`assets/${res.name}`);
      setDraftPins([]);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  async function submitEntry() {
    if (!draftAsset) return;
    setBusy(true);
    try {
      await fetchJSON(`/api/design-board/cards/${cardId}/entries`, {
        method: "POST",
        body: JSON.stringify({
          author: "piet", kind: "screenshot", note,
          pins: draftPins, asset_name: draftAsset.split("/").pop(),
        }),
      });
      setDraftAsset(null); setDraftPins([]); setNote("");
      if (fileRef.current) fileRef.current.value = "";
      load();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  if (error) return <div className="p-4"><FleetEmptyState title="Failed to load" desc={error} /></div>;
  if (!card) return <div className="p-4 hc-type-label hc-dim">Loading…</div>;

  return (
    <div className="min-h-full bg-surface-0 p-4">
      <SectionHeader label={card.title} meta={card.derived_status ?? card.status} />
      {card.target?.view && (
        <div className="mt-1 hc-type-label hc-dim">→ {card.target.view}</div>
      )}

      {card.task_facets.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {card.task_facets.map((f) => (
            <span key={f.id} data-testid={`facet-${f.id}`}
              className={`rounded-card border border-line px-2 py-1 hc-type-label ${f.terminal ? "text-status-ok" : "text-status-warn"}`}>
              {f.id} · {f.status}{f.assignee ? ` · ${f.assignee}` : ""}
            </span>
          ))}
        </div>
      )}

      <div className="mt-4 space-y-4">
        {card.entries.map((entry) => (
          <div key={entry.id} className="hc-surface-card p-3">
            <div className="hc-type-label hc-dim">{entry.author} · {entry.kind}</div>
            {entry.note && <div className="mt-1 text-sm text-white">{entry.note}</div>}
            {entry.asset && (
              <div className="mt-2">
                <PinOverlay src={assetUrl(card.id, entry.asset)} pins={entry.pins} editable={false} />
              </div>
            )}
            {entry.html && <MockupToggle cardId={card.id} html={entry.html} png={entry.asset} />}
          </div>
        ))}
      </div>

      <div className="mt-6 hc-surface-card p-3">
        <div className="hc-type-label hc-dim">Add a screenshot</div>
        <input ref={fileRef} type="file" accept="image/*" onChange={onFile}
          className="mt-2 text-sm text-white" disabled={busy} />
        {draftAsset && (
          <div className="mt-3">
            <PinOverlay src={assetUrl(card.id, draftAsset)} pins={draftPins} editable
              onAddPin={(p) => setDraftPins((prev) => [
                ...prev, { id: `p${prev.length + 1}`, x: p.x, y: p.y, note: "" },
              ])} />
            <div className="mt-1 hc-type-label hc-soft">{draftPins.length} pin(s) — click the image to add</div>
            <textarea value={note} onChange={(e) => setNote(e.target.value)}
              placeholder="Describe the issue…"
              className="mt-2 w-full rounded-card border border-line bg-surface-1 p-2 text-sm text-white" />
            <button onClick={submitEntry} disabled={busy}
              className="mt-2 rounded-card border border-line px-3 py-1 text-sm text-live">
              Add entry
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function MockupToggle(props: { cardId: string; html: string; png: string | null }) {
  const [live, setLive] = useState(false);
  return (
    <div className="mt-2">
      <button onClick={() => setLive((v) => !v)}
        className="mb-2 rounded-card border border-line px-2 py-1 hc-type-label text-live">
        {live ? "Show PNG" : "Show live HTML"}
      </button>
      {live ? (
        <iframe title="mockup" sandbox="allow-same-origin"
          src={`/api/design-board/cards/${props.cardId}/assets/${props.html.split("/").pop()}`}
          className="h-96 w-full rounded-card border border-line bg-surface-1" />
      ) : props.png ? (
        <img alt="mockup"
          src={`/api/design-board/cards/${props.cardId}/assets/${props.png.split("/").pop()}`}
          className="max-w-full rounded-card border border-line" />
      ) : null}
    </div>
  );
}
