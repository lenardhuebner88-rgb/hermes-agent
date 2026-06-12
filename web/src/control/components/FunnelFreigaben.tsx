import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Lightbulb, Pencil, Rocket, RotateCcw, Save, Trash2, X } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { fmtClock } from "../lib/derive";
import { FleetPanel } from "./fleet/atoms";
import { ToneCallout } from "./atoms";
import { Markdown } from "./Markdown";
import { Overlay } from "./Overlay";
import { funnelDraftEditRequest } from "./funnelDraftEditRequest";

// Demand-Funnel Freigabe-Queue: fertige Drafts aus dem Wunsch-Trichter
// (family / discord-idee / fo-gap-audit), die auf den Operator-Klick warten.
// Freigeben legt den Build-Task als verlinktes Kind an (ready, Dispatcher
// übernimmt) — danach verschwindet der Draft hier und die Kette übernimmt
// das Flow-Board. Zwei-Schritt-Confirm nach TriageStrip-Muster.
const t = {
  eyebrow: "Funnel-Freigaben",
  meta: "fertige Drafts aus dem Wunsch-Trichter · Freigeben = Build-Task starten",
  empty: "Keine Drafts warten auf Freigabe.",
  approve: "Freigeben → bauen",
  approveHint: "legt den Build-Task an (ready) — der Worker setzt den Draft um, Gates wie immer",
  dismiss: "Verwerfen",
  dismissHint: "archiviert den Draft (mit Notiz) — es wird nichts gebaut",
  confirm: "Bestätigen",
  cancel: "Abbrechen",
  showDraft: "Draft ansehen",
  hideDraft: "Draft einklappen",
  edit: "Bearbeiten / Feedback",
  editedBadge: "Operator-Edit gespeichert",
  editTitle: "Plan-Spec bearbeiten",
  editTextLabel: "Plan-Spec",
  operatorNoteLabel: "Mein Input / Änderungswunsch",
  save: "Speichern",
  revise: "Überarbeiten lassen",
  buildFinal: "Finale Version bauen",
  close: "Schließen",
  saving: "Speichere …",
  saved: (id: string) => `${id} gespeichert — die Operator-Version ist jetzt die Build-Grundlage.`,
  revisionRequested: (id: string) => `Revision angefordert — neuer Spec-Task ${id} ist eingereiht.`,
  noDraft: "Kein Draft-Text gefunden — Referenzen stehen im Ursprungs-Task.",
  done: (id: string) => `Freigegeben — Build-Task ${id} ist eingereiht.`,
  dismissed: (id: string) => `${id} verworfen und archiviert.`,
};

const SOURCE_LABEL: Record<string, string> = {
  family: "Familie",
  "discord-idee": "Discord-Idee",
  "fo-gap-audit": "Daten-Audit",
};

export interface FunnelDraft {
  id: string;
  title: string;
  created_by: string;
  assignee: string | null;
  completed_at: number;
  draft_excerpt: string | null;
  draft_text?: string | null;
  operator_edited?: boolean;
  revision_of?: string | null;
}

interface DraftsResponse {
  drafts: FunnelDraft[];
}

export function FunnelFreigaben() {
  const [data, setData] = useState<DraftsResponse | null>(null);
  const [pending, setPending] = useState<{ id: string; kind: "approve" | "dismiss" } | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [editingDraft, setEditingDraft] = useState<FunnelDraft | null>(null);
  const [editText, setEditText] = useState("");
  const [operatorNote, setOperatorNote] = useState("");
  const [modalBusy, setModalBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await fetchJSON<DraftsResponse>("/api/plugins/kanban/funnel/drafts?days=30"));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    const initialLoad = window.setTimeout(() => void load(), 0);
    const id = window.setInterval(() => void load(), 30000);
    return () => {
      window.clearTimeout(initialLoad);
      window.clearInterval(id);
    };
  }, [load]);

  const act = useCallback(async (draft: FunnelDraft, kind: "approve" | "dismiss") => {
    setBusy(true);
    setError(null);
    try {
      if (kind === "approve") {
        const res = await fetchJSON<{ task: { id: string } }>(
          `/api/plugins/kanban/funnel/drafts/${encodeURIComponent(draft.id)}/approve`,
          { method: "POST" },
        );
        setNotice(t.done(res.task.id));
      } else {
        await fetchJSON(`/api/plugins/kanban/funnel/drafts/${encodeURIComponent(draft.id)}/dismiss`, { method: "POST" });
        setNotice(t.dismissed(draft.id));
      }
      void load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setPending(null);
    }
  }, [load]);

  const openEdit = useCallback((draft: FunnelDraft) => {
    setEditingDraft(draft);
    setEditText(draft.draft_text ?? draft.draft_excerpt ?? "");
    setOperatorNote("");
    setError(null);
  }, []);

  const saveEdit = useCallback(async (draft: FunnelDraft, text: string, note: string) => {
    return fetchJSON<{ draft: FunnelDraft }>(...funnelDraftEditRequest(draft.id, text, note));
  }, []);

  const handleSaveEdit = useCallback(async () => {
    if (!editingDraft) return;
    setModalBusy(true);
    setError(null);
    try {
      await saveEdit(editingDraft, editText, operatorNote);
      setNotice(t.saved(editingDraft.id));
      setEditingDraft(null);
      void load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setModalBusy(false);
    }
  }, [editText, editingDraft, load, operatorNote, saveEdit]);

  const handleRevise = useCallback(async () => {
    if (!editingDraft) return;
    setModalBusy(true);
    setError(null);
    try {
      const res = await fetchJSON<{ task: { id: string } }>(
        `/api/plugins/kanban/funnel/drafts/${encodeURIComponent(editingDraft.id)}/revise`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ draft_text: editText, operator_note: operatorNote }),
        },
      );
      setNotice(t.revisionRequested(res.task.id));
      setEditingDraft(null);
      void load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setModalBusy(false);
    }
  }, [editText, editingDraft, load, operatorNote]);

  const handleBuildFinal = useCallback(async () => {
    if (!editingDraft) return;
    setModalBusy(true);
    setError(null);
    try {
      await saveEdit(editingDraft, editText, operatorNote);
      const res = await fetchJSON<{ task: { id: string } }>(
        `/api/plugins/kanban/funnel/drafts/${encodeURIComponent(editingDraft.id)}/approve`,
        { method: "POST" },
      );
      setNotice(t.done(res.task.id));
      setEditingDraft(null);
      void load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setModalBusy(false);
    }
  }, [editText, editingDraft, load, operatorNote, saveEdit]);

  // Leere Queue = kein Panel (kein Rauschen für den Nicht-Nutzer).
  if (data !== null && data.drafts.length === 0 && !error && !notice) return null;

  return (
    <FleetPanel eyebrow={t.eyebrow} meta={t.meta}>
      {error ? <ToneCallout tone="red">{error}</ToneCallout> : null}
      {notice ? <ToneCallout tone="emerald">{notice}</ToneCallout> : null}
      {data === null ? <p className="text-sm hc-dim">…</p> : data.drafts.length === 0 ? (
        <p className="text-sm hc-dim">{t.empty}</p>
      ) : (
        <FreigabenList
          drafts={data.drafts}
          pending={pending}
          openId={openId}
          busy={busy}
          onAct={(d, kind) => void act(d, kind)}
          onPending={setPending}
          onToggleOpen={(id) => setOpenId(openId === id ? null : id)}
          onEdit={openEdit}
        />
      )}
      {editingDraft ? (
        <DraftEditDialog
          draft={editingDraft}
          editText={editText}
          operatorNote={operatorNote}
          error={error}
          busy={modalBusy}
          onEditTextChange={setEditText}
          onOperatorNoteChange={setOperatorNote}
          onClose={() => { if (!modalBusy) setEditingDraft(null); }}
          onSave={() => void handleSaveEdit()}
          onRevise={() => void handleRevise()}
          onBuild={() => void handleBuildFinal()}
        />
      ) : null}
    </FleetPanel>
  );
}

// Pure Listen-Darstellung — separat exportiert für den statischen Render-Test.
export function FreigabenList({
  drafts,
  pending,
  openId,
  busy,
  onAct,
  onPending,
  onToggleOpen,
  onEdit,
}: {
  drafts: FunnelDraft[];
  pending: { id: string; kind: "approve" | "dismiss" } | null;
  openId: string | null;
  busy: boolean;
  onAct: (d: FunnelDraft, kind: "approve" | "dismiss") => void;
  onPending: (p: { id: string; kind: "approve" | "dismiss" } | null) => void;
  onToggleOpen: (id: string) => void;
  onEdit: (d: FunnelDraft) => void;
}) {
  return (
    <ul className="space-y-1.5">
      {drafts.map((d) => {
            const isPending = pending?.id === d.id ? pending : null;
            const isOpen = openId === d.id;
            return (
              <li key={d.id} className="rounded-md border border-[var(--hc-accent-border)] px-3 py-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <Lightbulb className="h-3.5 w-3.5 shrink-0 text-amber-200" />
                  <span className="min-w-0 flex-1 basis-56 truncate text-[0.85rem] font-medium text-white">{d.title}</span>
                  <span className="hc-mono shrink-0 rounded-full border border-white/15 px-2 py-0.5 text-[0.68rem] hc-soft">
                    {SOURCE_LABEL[d.created_by] ?? d.created_by}
                  </span>
                  {d.operator_edited ? (
                    <span className="hc-mono shrink-0 rounded-full border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 text-[0.68rem] text-amber-100">
                      {t.editedBadge}
                    </span>
                  ) : null}
                  <span className="hc-mono shrink-0 text-[0.72rem] hc-dim">{fmtClock(d.completed_at)}</span>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {isPending ? (
                    <>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => onAct(d, isPending.kind)}
                        className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 py-1 text-[0.78rem] font-medium text-[var(--hc-accent-text)] disabled:opacity-50"
                      >
                        {isPending.kind === "approve" ? <Rocket className="h-3.5 w-3.5" /> : <Trash2 className="h-3.5 w-3.5" />}
                        {isPending.kind === "approve" ? t.approve : t.dismiss} · {t.confirm}
                      </button>
                      <button type="button" disabled={busy} onClick={() => onPending(null)} className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-3 py-1 text-[0.78rem] hc-soft">{t.cancel}</button>
                      <span className="text-[0.72rem] hc-dim">{isPending.kind === "approve" ? t.approveHint : t.dismissHint}</span>
                    </>
                  ) : (
                    <>
                      <button type="button" disabled={busy} onClick={() => onPending({ id: d.id, kind: "approve" })} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-emerald-500/30 px-3 py-1 text-[0.78rem] text-emerald-200 hover:bg-emerald-500/10">
                        <Rocket className="h-3.5 w-3.5" />{t.approve}
                      </button>
                      <button type="button" disabled={busy} onClick={() => onPending({ id: d.id, kind: "dismiss" })} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-red-500/25 px-3 py-1 text-[0.78rem] text-red-200 hover:bg-red-500/10">
                        <Trash2 className="h-3.5 w-3.5" />{t.dismiss}
                      </button>
                    </>
                  )}
                  <button
                    type="button"
                    onClick={() => onEdit(d)}
                    className="inline-flex min-h-9 items-center gap-1 rounded-md border border-amber-400/25 px-3 py-1 text-[0.78rem] text-amber-100 hover:bg-amber-400/10"
                  >
                    <Pencil className="h-3.5 w-3.5" />
                    {t.edit}
                  </button>
                  <button
                    type="button"
                    onClick={() => onToggleOpen(d.id)}
                    className="inline-flex min-h-9 items-center gap-1 rounded-md border border-white/10 px-3 py-1 text-[0.78rem] hc-soft hover:bg-white/5"
                  >
                    {isOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                    {isOpen ? t.hideDraft : t.showDraft}
                  </button>
                </div>
                {isOpen ? (
                  <div className="mt-2 rounded-md border border-white/10 bg-black/20 p-3 text-[0.8rem]">
                    {d.draft_text || d.draft_excerpt ? <Markdown body={d.draft_text ?? d.draft_excerpt ?? ""} /> : <p className="hc-dim">{t.noDraft}</p>}
                  </div>
                ) : null}
              </li>
            );
      })}
    </ul>
  );
}

export function DraftEditDialog({
  draft,
  editText,
  operatorNote,
  error,
  busy,
  onEditTextChange,
  onOperatorNoteChange,
  onClose,
  onSave,
  onRevise,
  onBuild,
}: {
  draft: FunnelDraft;
  editText: string;
  operatorNote: string;
  error?: string | null;
  busy: boolean;
  onEditTextChange: (value: string) => void;
  onOperatorNoteChange: (value: string) => void;
  onClose: () => void;
  onSave: () => void;
  onRevise: () => void;
  onBuild: () => void;
}) {
  return (
    <Overlay ariaLabel={t.editTitle} closeDisabled={busy} maxWidthClassName="max-w-3xl" onClose={onClose}>
      <div className="-mx-4 -mt-4 overflow-hidden rounded-t-2xl sm:rounded-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-white/10 px-4 py-3">
          <div className="min-w-0">
            <p className="hc-mono text-[0.68rem] uppercase tracking-[0.18em] hc-dim">{SOURCE_LABEL[draft.created_by] ?? draft.created_by} · {draft.id}</p>
            <h3 id="funnel-draft-edit-title" className="mt-1 truncate text-base font-semibold text-white">{t.editTitle}</h3>
            <p className="mt-1 truncate text-[0.78rem] hc-dim">{draft.title}</p>
          </div>
          <button type="button" disabled={busy} onClick={onClose} className="inline-flex min-h-9 items-center gap-1 rounded-md border border-white/10 px-3 py-1 text-[0.78rem] hc-soft hover:bg-white/5 disabled:opacity-50">
            <X className="h-3.5 w-3.5" />{t.close}
          </button>
        </div>
        <div className="max-h-[70vh] space-y-3 overflow-y-auto px-4 py-3">
          {error ? <ToneCallout tone="red">{error}</ToneCallout> : null}
          <label className="block text-[0.78rem] font-medium text-white" htmlFor="funnel-edit-text">{t.editTextLabel}</label>
          <textarea
            id="funnel-edit-text"
            value={editText}
            onChange={(e) => onEditTextChange(e.target.value)}
            disabled={busy}
            className="min-h-[40vh] w-full resize-y rounded-md border border-white/10 bg-black/25 p-3 font-mono text-[0.78rem] leading-relaxed text-white outline-none focus:border-amber-300/50 disabled:opacity-60"
          />
          <label className="block text-[0.78rem] font-medium text-white" htmlFor="funnel-operator-note">{t.operatorNoteLabel}</label>
          <textarea
            id="funnel-operator-note"
            value={operatorNote}
            onChange={(e) => onOperatorNoteChange(e.target.value)}
            disabled={busy}
            className="min-h-24 w-full resize-y rounded-md border border-white/10 bg-black/25 p-3 text-[0.82rem] leading-relaxed text-white outline-none focus:border-amber-300/50 disabled:opacity-60"
            placeholder="Was fehlt noch? Was soll explizit in die Plan-Spec?"
          />
        </div>
        <div className="sticky bottom-0 flex flex-wrap items-center justify-end gap-2 border-t border-white/10 bg-[var(--hc-panel)] px-4 py-3">
          <button type="button" disabled={busy} onClick={onSave} className="inline-flex min-h-10 items-center gap-1.5 rounded-md border border-amber-400/30 px-3 py-1.5 text-[0.8rem] text-amber-100 hover:bg-amber-400/10 disabled:opacity-50">
            <Save className="h-3.5 w-3.5" />{busy ? t.saving : t.save}
          </button>
          <button type="button" disabled={busy} onClick={onRevise} className="inline-flex min-h-10 items-center gap-1.5 rounded-md border border-sky-400/30 px-3 py-1.5 text-[0.8rem] text-sky-100 hover:bg-sky-400/10 disabled:opacity-50">
            <RotateCcw className="h-3.5 w-3.5" />{t.revise}
          </button>
          <button type="button" disabled={busy} onClick={onBuild} className="inline-flex min-h-10 items-center gap-1.5 rounded-md border border-emerald-400/30 bg-emerald-400/10 px-3 py-1.5 text-[0.8rem] font-medium text-emerald-100 hover:bg-emerald-400/15 disabled:opacity-50">
            <Rocket className="h-3.5 w-3.5" />{t.buildFinal}
          </button>
        </div>
      </div>
    </Overlay>
  );
}
