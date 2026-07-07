import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchJSON } from "@/lib/api";
import { SectionHeader, FleetEmptyState } from "@/control/components/leitstand";
import { de } from "@/control/i18n/de";
import { PinOverlay, type Pin } from "./PinOverlay";
import { STATUS_LABELS, statusBadge, statusLabel } from "./status";

type Facet = { id: string; status: string; assignee: string | null; terminal: boolean };
type Entry = {
  id: string; author: string; kind: string; note: string;
  asset: string | null; html: string | null; pins: Pin[]; created_at: number;
};
type CardDetailData = {
  id: string; kind: string; title: string; status: string;
  target: { view?: string } | null; linked_tasks: string[];
  entries: Entry[]; task_facets: Facet[]; derived_status: string | null;
  kanban_ok: boolean;
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
  const [commentDraft, setCommentDraft] = useState("");
  const [statusDraft, setStatusDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [mockupBusy, setMockupBusy] = useState(false);
  const [mockupError, setMockupError] = useState<string | null>(null);
  const [promoteBusy, setPromoteBusy] = useState(false);
  const [promoteError, setPromoteError] = useState<string | null>(null);
  const [promotedTaskId, setPromotedTaskId] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const mockupRef = useRef<HTMLInputElement>(null);

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

  async function onMockupFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setMockupBusy(true);
    setMockupError(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      await fetchJSON(`/api/design-board/cards/${cardId}/mockups`, {
        method: "POST", body: fd,
      });
      if (mockupRef.current) mockupRef.current.value = "";
      load();
    } catch (err) {
      setMockupError(mockupErrorMessage(String(err)));
    } finally {
      setBusy(false);
      setMockupBusy(false);
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

  async function submitComment() {
    if (!commentDraft.trim()) return;
    setBusy(true);
    try {
      await fetchJSON(`/api/design-board/cards/${cardId}/entries`, {
        method: "POST",
        body: JSON.stringify({
          author: "piet", kind: "comment", note: commentDraft.trim(), pins: [],
        }),
      });
      setCommentDraft("");
      load();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  async function promote() {
    setPromoteBusy(true);
    setPromoteError(null);
    try {
      const res = await fetchJSON<{ task_id: string; card: CardDetailData }>(
        `/api/design-board/cards/${cardId}/promote`, { method: "POST" },
      );
      setPromotedTaskId(res.task_id);
      setCard(res.card);
    } catch (err) {
      const msg = String(err);
      if (msg.includes("409")) setPromoteError(de.designBoard.promoteAlreadyLinked);
      else if (msg.includes("503")) setPromoteError(de.designBoard.promoteUnavailable);
      else setPromoteError(msg);
    } finally {
      setPromoteBusy(false);
    }
  }

  async function updateStatus(newStatus: string) {
    setBusy(true);
    try {
      await fetchJSON(`/api/design-board/cards/${cardId}`, {
        method: "PATCH",
        body: JSON.stringify({ status: newStatus }),
      });
      setStatusDraft(null);
      load();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  if (error) return <div className="p-4"><FleetEmptyState title="Laden fehlgeschlagen" desc={error} /></div>;
  if (!card) return <div className="p-4 hc-type-label hc-dim">Laden…</div>;

  const beforeScreenshot = card.entries.find((entry) =>
    entry.kind === "screenshot" && entry.asset && entry.author !== "system",
  );
  const afterScreenshot = [...card.entries].reverse().find((entry) =>
    entry.kind === "screenshot" && entry.asset && entry.author === "system",
  );

  return (
    <div className="min-h-full bg-surface-0 p-4">
      <SectionHeader label={card.title} meta={statusBadge(card.derived_status ?? card.status)} />
      {card.target?.view && (
        <div className="mt-1 hc-type-label hc-dim">→ {card.target.view}</div>
      )}
      {card.derived_status && card.derived_status !== card.status && (
        <div className="mt-1 hc-type-label hc-soft">
          Kartenstatus: {statusLabel(card.status)}
        </div>
      )}
      {!card.kanban_ok && (
        <div className="mt-2 rounded-card border border-status-warn/20 bg-status-warn/10 p-2 text-xs text-status-warn">
          {de.designBoard.kanbanUnavailable}
        </div>
      )}

      {card.linked_tasks.length === 0 && card.status !== "archived" && (
        <div className="mt-3 flex items-center gap-2">
          <button
            onClick={() => void promote()}
            disabled={promoteBusy}
            className="rounded-card border border-live px-3 py-1 text-sm text-live disabled:opacity-45"
          >
            {promoteBusy ? de.designBoard.promoting : de.designBoard.promote}
          </button>
          {promotedTaskId && (
            <span className="hc-type-label text-status-ok">{de.designBoard.promoted(promotedTaskId)}</span>
          )}
          {promoteError && (
            <span className="hc-type-label text-status-warn">{promoteError}</span>
          )}
        </div>
      )}

      <div className="mt-3 flex items-center gap-2">
        <span className="hc-type-label hc-dim">Status:</span>
        {statusDraft === null ? (
          <button
            data-testid="status-edit"
            onClick={() => setStatusDraft(card.status)}
            disabled={busy}
            className="rounded-card border border-line px-2 py-1 hc-type-label text-live disabled:opacity-45"
          >
            {statusLabel(card.status)} ✎
          </button>
        ) : (
          <>
            <select
              aria-label="Status"
              value={statusDraft}
              onChange={(e) => setStatusDraft(e.target.value)}
              disabled={busy}
              className="rounded-card border border-line bg-surface-1 px-2 py-1 text-sm text-white"
            >
              {Object.entries(STATUS_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
            <button
              onClick={() => void updateStatus(statusDraft)}
              disabled={busy || statusDraft === card.status}
              className="rounded-card border border-live px-2 py-1 hc-type-label text-live disabled:opacity-45"
            >
              Speichern
            </button>
            <button
              onClick={() => setStatusDraft(null)}
              disabled={busy}
              className="rounded-card border border-line px-2 py-1 hc-type-label text-ink-3"
            >
              Abbrechen
            </button>
          </>
        )}
      </div>
      {card.derived_status && (
        <div className="mt-1 hc-type-label hc-soft">
          Abgeleitet aus verknüpften Aufgaben: {statusLabel(card.derived_status)}
        </div>
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

      {beforeScreenshot && afterScreenshot && (
        <div className="mt-4 grid gap-3 md:grid-cols-2" aria-label="Vorher-Nachher Screenshots">
          <div className="hc-surface-card p-3">
            <div className="hc-type-label hc-dim">Vorher · Operator-Screenshot</div>
            <PinOverlay src={assetUrl(card.id, beforeScreenshot.asset!)} pins={beforeScreenshot.pins} editable={false} />
          </div>
          <div className="hc-surface-card p-3">
            <div className="hc-type-label hc-dim">Nachher · System-Screenshot</div>
            <PinOverlay src={assetUrl(card.id, afterScreenshot.asset!)} pins={afterScreenshot.pins} editable={false} />
          </div>
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
        <div className="hc-type-label hc-dim">Kommentar hinzufügen</div>
        <textarea
          value={commentDraft}
          onChange={(e) => setCommentDraft(e.target.value)}
          placeholder="Notiz zur Karte…"
          disabled={busy}
          className="mt-2 w-full rounded-card border border-line bg-surface-1 p-2 text-sm text-white placeholder:text-ink-3"
        />
        <button
          onClick={() => void submitComment()}
          disabled={busy || !commentDraft.trim()}
          className="mt-2 rounded-card border border-live px-3 py-1 text-sm text-live disabled:opacity-45"
        >
          Kommentar speichern
        </button>
      </div>

      <div className="mt-6 hc-surface-card p-3">
        <div className="hc-type-label hc-dim">Screenshot hinzufügen</div>
        <input ref={fileRef} type="file" accept="image/*" onChange={onFile}
          className="mt-2 text-sm text-white" disabled={busy} />
        {draftAsset && (
          <div className="mt-3">
            <PinOverlay src={assetUrl(card.id, draftAsset)} pins={draftPins} editable
              onAddPin={(p) => setDraftPins((prev) => [
                ...prev, { id: `p${prev.length + 1}`, x: p.x, y: p.y, note: "" },
              ])} />
            <div className="mt-1 hc-type-label hc-soft">{draftPins.length} Markierung(en) — ins Bild klicken, um hinzuzufügen</div>
            {draftPins.length > 0 && (
              <div className="mt-2 space-y-1">
                {draftPins.map((p, i) => (
                  <div key={p.id} className="flex items-center gap-2">
                    <span className="hc-type-label hc-dim">#{i + 1}</span>
                    <input
                      data-testid={`pin-note-${p.id}`}
                      value={p.note}
                      onChange={(e) => {
                        const v = e.target.value;
                        setDraftPins((prev) =>
                          prev.map((pp) => (pp.id === p.id ? { ...pp, note: v } : pp)),
                        );
                      }}
                      placeholder={de.designBoard.pinNotePlaceholder}
                      className="flex-1 rounded-card border border-line bg-surface-1 px-2 py-1 text-xs text-white placeholder:text-ink-3"
                    />
                  </div>
                ))}
              </div>
            )}
            <textarea value={note} onChange={(e) => setNote(e.target.value)}
              placeholder="Beschreibe das Problem…"
              className="mt-2 w-full rounded-card border border-line bg-surface-1 p-2 text-sm text-white" />
            <button onClick={submitEntry} disabled={busy}
              className="mt-2 rounded-card border border-line px-3 py-1 text-sm text-live">
              Eintrag speichern
            </button>
          </div>
        )}
      </div>

      <div className="mt-6 hc-surface-card p-3">
        <div className="hc-type-label hc-dim">{de.designBoard.mockupUploadLabel}</div>
        <input ref={mockupRef} type="file" accept=".html,.htm,text/html"
          data-testid="mockup-upload" onChange={onMockupFile}
          className="mt-2 text-sm text-white" disabled={busy} />
        <div className="mt-1 hc-type-label hc-soft">{de.designBoard.mockupUploadHint}</div>
        {mockupBusy && <div className="mt-1 hc-type-label hc-dim">{de.designBoard.mockupUploading}</div>}
        {mockupError && (
          <div className="mt-2 rounded-card border border-status-warn/20 bg-status-warn/10 p-2 text-xs text-status-warn">
            {mockupError}
          </div>
        )}
      </div>
    </div>
  );
}

function mockupErrorMessage(raw: string): string {
  if (raw.includes("413") || raw.includes("file_too_large")) return de.designBoard.mockupTooLarge;
  if (raw.includes("render_unavailable")) return de.designBoard.mockupRenderUnavailable;
  if (raw.includes("render_failed") || raw.includes("render_timeout") || raw.includes("502") || raw.includes("504"))
    return de.designBoard.mockupRenderFailed;
  return raw;
}

function MockupToggle(props: { cardId: string; html: string; png: string | null }) {
  const [live, setLive] = useState(false);
  return (
    <div className="mt-2">
      <button onClick={() => setLive((v) => !v)}
        className="mb-2 rounded-card border border-line px-2 py-1 hc-type-label text-live">
        {live ? "PNG anzeigen" : "Live-HTML anzeigen"}
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
