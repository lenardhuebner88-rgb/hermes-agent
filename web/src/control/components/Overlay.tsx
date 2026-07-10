/**
 * Overlay — gemeinsamer Portal-Wrapper für Sheets/Modals im Control-Dashboard.
 *
 * Warum Portal: Overlays, die inline in einer View rendern, erben jeden
 * Stacking-Context ihrer Ancestors (`isolation`, `transform`, `backdrop-blur`…).
 * Konkret hat `.hc-hero { isolation: isolate }` die Flow-Sheets eingefangen —
 * ihr z-50 zählte nur innerhalb des Heros, die Bottom-Nav (z-40) und alles
 * spätere DOM malte über das offene Sheet (Submit unerreichbar auf Mobile,
 * Audit 2026-06-11: docs/design/control-mobile-audit-2026-06-11/spec.md).
 * `createPortal(document.body)` löst das strukturell für jedes Overlay.
 *
 * Außerdem zentral hier: Escape-zu-Schließen, Body-Scroll-Lock solange offen,
 * Backdrop-Dismiss, ein Fokus-Trap (erster fokussierbarer Eintrag beim Öffnen,
 * Tab bleibt im Dialog, Fokus-Restore beim Schließen), und die Tier-Präsenz:
 * unterhalb `tab` (600px) ein bottom-sheet mit `max-h`/innerem Scroll +
 * safe-area-Padding, ab `tab` ein rechtes side-sheet (SHELL-SPEC.md W2-c).
 */
import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/utils";
import { useFocusTrap } from "../hooks/useFocusTrap";

export function Overlay({
  onClose,
  ariaLabel,
  children,
  closeDisabled = false,
  maxWidthClassName = "max-w-md",
}: {
  onClose: () => void;
  ariaLabel: string;
  children: ReactNode;
  closeDisabled?: boolean;
  maxWidthClassName?: string;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(dialogRef, true);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !closeDisabled) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [closeDisabled, onClose]);

  // Scroll-Lock: solange ein Overlay offen ist, scrollt der Hintergrund nicht mit.
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = prev; };
  }, []);

  return createPortal(
    // data-control + display:contents: das Portal hängt an document.body, also
    // AUSSERHALB des [data-control]-Scopes, der alle --hc-*-Tokens (+ Daylight-
    // Remaps) trägt — ohne Scope rendert das Sheet transparent/farblos.
    // display:contents, weil [data-control] selbst min-height/background setzt
    // und als echte Box das Layout kaputt machen würde; so vererbt es nur.
    <div data-control className="contents">
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/55 p-0 tab:items-stretch tab:justify-end" onClick={() => { if (!closeDisabled) onClose(); }} role="presentation">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        onClick={(e) => e.stopPropagation()}
        className={cn(
          "hc-surface-card hc-side-sheet-in max-h-[85dvh] w-full overflow-y-auto overscroll-contain rounded-b-none rounded-t-2xl p-4 pb-[calc(1rem+env(safe-area-inset-bottom,0px))]",
          "tab:h-dvh tab:max-h-dvh tab:w-[min(420px,60vw)] tab:max-w-none tab:rounded-none tab:border-l tab:border-line tab:pb-4",
          maxWidthClassName,
        )}
      >
        {children}
      </div>
    </div>
    </div>,
    document.body,
  );
}
