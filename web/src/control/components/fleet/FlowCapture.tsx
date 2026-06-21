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
import { createPortal } from "react-dom";
import { AlertTriangle, Check, Loader2, Plus, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { de } from "../../i18n/de";
import { useCaptureTask } from "../../hooks/useControlData";
import { usesFlowCaptureEndpoint, type CaptureMethod, type CaptureLevers } from "../../lib/fleet";
import type { ReviewTier } from "../../lib/types";
import { Overlay } from "../Overlay";
import { hasFinePointer } from "../../lib/pointer";

// Mirror of FlowView's FlowPlanPanel labels so the capture-step levers read
// identically to the "Kette starten"-Panel the operator already knows.
const REVIEW_TIER_LABEL: Record<ReviewTier, string> = { standard: "Standard", review: "Review", critical: "Kritisch" };

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

function GateToggle({ gate, onChange }: { gate: boolean; onChange: (g: boolean) => void }) {
  return (
    <div className="mt-3 rounded-lg border border-[var(--hc-border)] p-2.5">
      <p className="hc-type-label text-white">{de.flow.capture.gateLabel}</p>
      <div className="mt-2 flex gap-2" role="radiogroup" aria-label={de.flow.capture.gateLabel}>
        {[
          { val: false, title: de.flow.capture.gateAuto, hint: de.flow.capture.gateAutoHint },
          { val: true, title: de.flow.capture.gateGate, hint: de.flow.capture.gateGateHint },
        ].map((opt) => (
          <button
            key={String(opt.val)}
            type="button"
            role="radio"
            aria-checked={gate === opt.val}
            onClick={() => onChange(opt.val)}
            className={cn(
              "flex-1 rounded-md border px-2.5 py-1.5 text-left transition",
              gate === opt.val ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]" : "border-[var(--hc-border)] hover:border-[var(--hc-border-strong)]",
            )}
          >
            <span className="block text-sm font-medium text-white">{opt.title}</span>
            <span className="mt-0.5 block text-[0.7rem] hc-dim">{opt.hint}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// Phase-C levers (Review-Tier + Scout-Vorlauf) at the capture step, mirroring
// FlowPlanPanel. Shown only for gate/park (see CaptureSheet.showLevers). Tier is
// always selectable here; Scout is meaningful only for a gated CHAIN — a single
// parked task has no build-children to precede — so it disables (+ hint) on park.
function TierScoutControls({ reviewTier, onTierChange, injectScout, onScoutChange, scoutEnabled }: {
  reviewTier: ReviewTier | "";
  onTierChange: (tier: ReviewTier | "") => void;
  injectScout: boolean;
  onScoutChange: (on: boolean) => void;
  scoutEnabled: boolean;
}) {
  return (
    <div className="mt-3 rounded-lg border border-[var(--hc-border)] p-2.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="inline-flex items-center gap-1">
          <span className="hc-type-label hc-soft">{de.flow.capture.reviewTierLabel}</span>
          {(["standard", "review", "critical"] as ReviewTier[]).map((tier) => (
            <button
              key={tier}
              type="button"
              aria-pressed={reviewTier === tier}
              onClick={() => onTierChange(reviewTier === tier ? "" : tier)}
              className={cn(
                "inline-flex min-h-8 items-center rounded-full border px-2.5 hc-type-label transition",
                reviewTier === tier
                  ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]"
                  : "border-[var(--hc-border)] hc-soft hover:border-[var(--hc-border-strong)]",
              )}
            >
              {REVIEW_TIER_LABEL[tier]}
            </button>
          ))}
        </div>
        <label className={cn("inline-flex items-center gap-1.5 hc-type-label hc-soft", !scoutEnabled && "opacity-40")}>
          <input
            type="checkbox"
            checked={scoutEnabled && injectScout}
            disabled={!scoutEnabled}
            onChange={(e) => onScoutChange(e.target.checked)}
            className="h-3.5 w-3.5 accent-[var(--hc-accent)]"
          />
          {de.flow.capture.scoutLabel}
        </label>
      </div>
      <p className="mt-1.5 hc-type-label hc-dim">{de.flow.capture.reviewTierHint}</p>
      <p className="mt-1 hc-type-label hc-dim">{scoutEnabled ? de.flow.capture.scoutHint : de.flow.capture.scoutParkHint}</p>
    </div>
  );
}

function CaptureSheet({ onClose, onCreated }: { onClose: () => void; onCreated?: (taskId: string) => void }) {
  const [title, setTitle] = useState("");
  const [method, setMethod] = useState<CaptureMethod>("lean");
  const [gate, setGate] = useState(false);
  const [reviewTier, setReviewTier] = useState<ReviewTier | "">("");
  const [injectScout, setInjectScout] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const { state, error, capture, reset } = useCaptureTask(onCreated);

  useEffect(() => { if (hasFinePointer()) inputRef.current?.focus(); }, []);

  const busy = state === "busy";
  // park is operator-held already; the gate switch only applies to the two
  // decomposing methods. Park always behaves as "auto" (effective gate=false).
  const effectiveGate = method === "park" ? false : gate;
  // The levers surface exactly where the operator looked for them: a gated chain
  // or a parked task. Auto (lean+auto) stays untouched — no autonomous-decompose
  // intent carrier. Scout is enabled only for a real gated chain.
  const showLevers = method === "park" || gate;
  const scoutEnabled = method !== "park" && gate;
  // The backend-driven path plans synchronously (LLM) — show a "planning" label.
  const planning = busy && usesFlowCaptureEndpoint(method, effectiveGate);
  const doneLabel = method === "park" ? de.flow.capture.donePark : method === "document" ? de.flow.capture.doneDocument : de.flow.capture.doneLean;
  const submit = async () => {
    // Only carry levers the current mode actually applies → a lever-less capture
    // is byte-identical to today (scout dropped on park, both dropped on auto).
    const levers: CaptureLevers = {
      reviewTier: showLevers ? reviewTier : "",
      injectScout: scoutEnabled && injectScout,
    };
    const res = await capture(title, method, effectiveGate, levers);
    if (res.ok) {
      // brief "done" flash, then close so the operator sees the new card land
      window.setTimeout(onClose, 650);
    }
  };

  return (
    <Overlay onClose={onClose} ariaLabel={de.flow.capture.sheetTitle}>
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

      <div className="mt-3 space-y-2" role="radiogroup" aria-label={de.flow.capture.methodLabel}>
        <ModeOption active={method === "lean"} onSelect={() => setMethod("lean")} title={de.flow.capture.methodLean} hint={de.flow.capture.methodLeanHint} />
        <ModeOption active={method === "document"} onSelect={() => setMethod("document")} title={de.flow.capture.methodDocument} hint={de.flow.capture.methodDocumentHint} />
        <ModeOption active={method === "park"} onSelect={() => setMethod("park")} title={de.flow.capture.methodPark} hint={de.flow.capture.methodParkHint} />
      </div>

      {method !== "park" ? <GateToggle gate={gate} onChange={setGate} /> : null}

      {showLevers ? (
        <TierScoutControls
          reviewTier={reviewTier}
          onTierChange={setReviewTier}
          injectScout={injectScout}
          onScoutChange={setInjectScout}
          scoutEnabled={scoutEnabled}
        />
      ) : null}

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
          {busy ? (planning ? de.flow.capture.planning : de.flow.capture.submitting) : state === "done" ? doneLabel : de.flow.capture.submit}
        </button>
      </div>
    </Overlay>
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
      {/* Mobile: a sticky FAB, lifted above the bottom tab nav (+ safe-area).
          Portal: inline säße der FAB im Hero-Stacking-Context (.hc-hero hat
          isolation:isolate) und sein z-40 wäre gegen Nav/Overlays Glückssache. */}
      {createPortal(
        // data-control-Wrapper (display:contents): außerhalb des Token-Scopes
        // (Portal an body) wären die --hc-accent-*-Farben des FABs unaufgelöst;
        // direkt am Button würde [data-control] min-height/background setzen.
        <div data-control className="contents">
          <button
            type="button"
            onClick={() => setOpen(true)}
            aria-label={de.flow.capture.fabAria}
            className="fixed bottom-[calc(5rem+env(safe-area-inset-bottom,0px))] right-4 z-40 inline-flex h-14 w-14 items-center justify-center rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)] shadow-lg shadow-black/40 backdrop-blur transition active:scale-95 sm:hidden"
          >
            <Plus className="h-6 w-6" />
          </button>
        </div>,
        document.body,
      )}
      {open ? <CaptureSheet onClose={() => setOpen(false)} onCreated={created} /> : null}
    </>
  );
}
