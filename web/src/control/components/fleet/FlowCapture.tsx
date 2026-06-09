/**
 * FlowCapture — the "+ Aufgabe" quick-capture for the Flow board. Renders a
 * header button on desktop and a sticky FAB on mobile (thumb-reachable above the
 * bottom tab nav); both open the same sheet. The sheet lets the operator pick an
 * honest mode before a REAL Kanban task is created (lib/fleet.captureRequest):
 *   • An Orchestrator (Triage, Default) → lands in Triage; the in-gateway
 *     orchestrator triages the raw prompt into subtasks / routes it.
 *   • Parken (selbst dispatchen) → lands GEPARKT in Plan, no worker auto-starts;
 *     the operator clicks Dispatch.
 * No optimistic illusion — on success the new card appears on the live board.
 */
import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, Loader2, Plus, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { de } from "../../i18n/de";
import { useCaptureTask } from "../../hooks/useControlData";
import type { CaptureMode } from "../../lib/fleet";

function ModeOption({ active, onSelect, title, hint }: { active: boolean; onSelect: () => void; title: string; hint: string }) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      onClick={onSelect}
      className={cn(
        "flex w-full items-start gap-2.5 rounded-lg border p-2.5 text-left transition",
        active ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]" : "border-[var(--hc-border)] hover:border-[var(--hc-border-strong)]",
      )}
    >
      <span className={cn("mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border", active ? "border-[var(--hc-accent-border)]" : "border-[var(--hc-border-strong)]")}>
        {active ? <span className="h-2 w-2 rounded-full bg-[var(--hc-accent-text)]" /> : null}
      </span>
      <span className="min-w-0">
        <span className="block text-sm font-medium text-white">{title}</span>
        <span className="mt-0.5 block text-[0.72rem] hc-dim">{hint}</span>
      </span>
    </button>
  );
}

function CaptureSheet({ onClose, onCreated }: { onClose: () => void; onCreated?: (taskId: string) => void }) {
  const [title, setTitle] = useState("");
  const [mode, setMode] = useState<CaptureMode>("orchestrate");
  const inputRef = useRef<HTMLInputElement>(null);
  const { state, error, capture, reset } = useCaptureTask(onCreated);

  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const busy = state === "busy";
  const submit = async () => {
    const res = await capture(title, mode);
    if (res.ok) {
      // brief "done" flash, then close so the operator sees the new card land
      window.setTimeout(onClose, 650);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/55 p-0 sm:items-center sm:p-4" onClick={onClose} role="presentation">
      <div
        role="dialog"
        aria-modal="true"
        aria-label={de.flow.capture.sheetTitle}
        onClick={(e) => e.stopPropagation()}
        className="hc-surface-card w-full max-w-md rounded-b-none rounded-t-2xl p-4 sm:rounded-2xl"
      >
        <div className="flex items-center justify-between gap-2">
          <h2 className="hc-type-label text-white">{de.flow.capture.sheetTitle}</h2>
          <button type="button" onClick={onClose} aria-label={de.flow.capture.cancel} className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-[var(--hc-border)] hc-soft hover:border-[var(--hc-border-strong)]"><X className="h-4 w-4" /></button>
        </div>

        <input
          ref={inputRef}
          value={title}
          onChange={(e) => { setTitle(e.target.value); if (state === "error") reset(); }}
          onKeyDown={(e) => { if (e.key === "Enter" && title.trim() && !busy) void submit(); }}
          placeholder={de.flow.capture.titlePlaceholder}
          className="mt-3 min-h-11 w-full rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] px-3 text-base text-white outline-none placeholder:hc-dim focus:border-[var(--hc-accent-border)]"
        />

        <div className="mt-3 space-y-2" role="radiogroup" aria-label={de.flow.capture.modeLabel}>
          <ModeOption active={mode === "orchestrate"} onSelect={() => setMode("orchestrate")} title={de.flow.capture.modeOrchestrate} hint={de.flow.capture.modeOrchestrateHint} />
          <ModeOption active={mode === "park"} onSelect={() => setMode("park")} title={de.flow.capture.modePark} hint={de.flow.capture.modeParkHint} />
        </div>

        {error ? <p className="mt-2.5 flex items-start gap-1.5 text-[0.75rem] text-red-300"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{error}</p> : null}

        <div className="mt-4 flex items-center justify-end gap-2">
          <button type="button" onClick={onClose} className="inline-flex min-h-11 items-center rounded-full border border-[var(--hc-border-strong)] px-4 text-sm hc-soft sm:min-h-9">{de.flow.capture.cancel}</button>
          <button
            type="button"
            disabled={busy || !title.trim() || state === "done"}
            onClick={() => void submit()}
            className="inline-flex min-h-11 items-center gap-1.5 rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-4 text-sm font-medium text-[var(--hc-accent-text)] transition hover:brightness-110 disabled:opacity-40 sm:min-h-9"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : state === "done" ? <Check className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
            {busy ? de.flow.capture.submitting : state === "done" ? (mode === "park" ? de.flow.capture.donePark : de.flow.capture.doneOrchestrate) : de.flow.capture.submit}
          </button>
        </div>
      </div>
    </div>
  );
}

export function FlowCapture({ onCreated }: { onCreated?: (taskId: string) => void }) {
  const [open, setOpen] = useState(false);
  const created: ((id: string) => void) | undefined = onCreated;
  return (
    <>
      {/* Desktop: a calm header button. */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="hidden items-center gap-1.5 rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3.5 py-1.5 text-sm font-medium text-[var(--hc-accent-text)] transition hover:brightness-110 sm:inline-flex"
      >
        <Plus className="h-4 w-4" />{de.flow.capture.button}
      </button>
      {/* Mobile: a sticky FAB, lifted above the bottom tab nav. */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={de.flow.capture.fabAria}
        className="fixed bottom-20 right-4 z-40 inline-flex h-14 w-14 items-center justify-center rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)] shadow-lg shadow-black/40 backdrop-blur transition active:scale-95 sm:hidden"
      >
        <Plus className="h-6 w-6" />
      </button>
      {open ? <CaptureSheet onClose={() => setOpen(false)} onCreated={created} /> : null}
    </>
  );
}
