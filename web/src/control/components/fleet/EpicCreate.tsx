/**
 * EpicCreate — "Epic anlegen" im Flow-Board-Kopf. Ein ruhiger Sekundär-Button,
 * der ein kleines Sheet öffnet (Titel + optionaler Body) und via POST /epics
 * ein echtes, dauerhaftes Epic anlegt (Phase-1-Schreibpfad). Kein optimistisches
 * UI — nach Erfolg lädt der Aufrufer die Epic-Liste neu.
 */
import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, Layers, Loader2, X } from "lucide-react";
import { de } from "../../i18n/de";
import { useEpicActions } from "../../hooks/useControlData";
import { Overlay } from "../Overlay";
import { hasFinePointer } from "../../lib/pointer";

function EpicCreateSheet({ onClose, onCreated }: { onClose: () => void; onCreated?: () => void }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [done, setDone] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const { busyKey, error, createEpic, clearError } = useEpicActions();

  useEffect(() => { if (hasFinePointer()) inputRef.current?.focus(); }, []);

  const busy = busyKey != null;
  const submit = async () => {
    const res = await createEpic(title.trim(), body);
    if (res.ok) {
      setDone(true);
      onCreated?.();
      window.setTimeout(onClose, 650);
    }
  };

  return (
    <Overlay onClose={onClose} ariaLabel={de.flow.epicCreate.sheetTitle}>
      <div className="flex items-center justify-between gap-2">
        <h2 className="hc-type-label text-white">{de.flow.epicCreate.sheetTitle}</h2>
        <button type="button" onClick={onClose} aria-label={de.flow.epicCreate.cancel} className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-[var(--hc-border)] hc-soft hover:border-[var(--hc-border-strong)]"><X className="h-4 w-4" /></button>
      </div>

      <input
        ref={inputRef}
        value={title}
        onChange={(e) => { setTitle(e.target.value); if (error) clearError(); }}
        onKeyDown={(e) => { if (e.key === "Enter" && title.trim() && !busy) void submit(); }}
        placeholder={de.flow.epicCreate.titlePlaceholder}
        className="mt-3 min-h-11 w-full rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] px-3 text-base text-white outline-none placeholder:hc-dim focus:border-[var(--hc-accent-border)]"
      />
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder={de.flow.epicCreate.bodyPlaceholder}
        rows={3}
        className="mt-2 w-full rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] px-3 py-2 text-base text-white outline-none placeholder:hc-dim focus:border-[var(--hc-accent-border)]"
      />

      {error ? <p className="mt-2.5 flex items-start gap-1.5 text-[0.75rem] text-red-300"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{error}</p> : null}

      <div className="mt-4 flex items-center justify-end gap-2">
        <button type="button" onClick={onClose} className="inline-flex min-h-11 items-center rounded-full border border-[var(--hc-border-strong)] px-4 text-sm hc-soft sm:min-h-9">{de.flow.epicCreate.cancel}</button>
        <button
          type="button"
          disabled={busy || !title.trim() || done}
          onClick={() => void submit()}
          className="inline-flex min-h-11 items-center gap-1.5 rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-4 text-sm font-medium text-[var(--hc-accent-text)] transition hover:brightness-110 disabled:opacity-40 sm:min-h-9"
        >
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : done ? <Check className="h-4 w-4" /> : <Layers className="h-4 w-4" />}
          {busy ? de.flow.epicCreate.submitting : done ? de.flow.epicCreate.done : de.flow.epicCreate.submit}
        </button>
      </div>
    </Overlay>
  );
}

export function EpicCreate({ onCreated }: { onCreated?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 rounded-full border border-indigo-400/30 bg-indigo-400/10 px-3.5 py-1.5 text-sm font-medium text-indigo-200 transition hover:bg-indigo-400/20"
      >
        <Layers className="h-4 w-4" />
        <span className="hidden sm:inline">{de.flow.epicCreate.button}</span>
      </button>
      {open ? <EpicCreateSheet onClose={() => setOpen(false)} onCreated={onCreated} /> : null}
    </>
  );
}
