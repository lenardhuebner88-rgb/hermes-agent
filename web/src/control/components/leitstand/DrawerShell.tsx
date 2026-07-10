import { useEffect, useRef, type ComponentType, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Eyebrow } from "../primitives";
import { useFocusTrap } from "../../hooks/useFocusTrap";

/**
 * DrawerShell — the shared frame every Leitstand detail drawer sits in: a
 * body-portalled bottom-sheet (Compact, <600px) / right side-sheet (from
 * `tab`, 600px, SHELL-SPEC.md W2-c), with a backdrop, Escape-to-close,
 * scroll-lock, a focus trap (first focusable on open, Tab stays inside,
 * focus restores to whatever had it on close), a header (optional icon +
 * eyebrow + title + close button, plus a free `headerExtra` slot) and a
 * scrollable body with an optional sticky footer.
 *
 * Derived from PlanSpecDetailDrawer + NodeDetailDrawer so detail views share one
 * shell (DESIGN.md rule 9: a card expands into a drawer). Portalled to
 * document.body inside a `data-control` wrapper so the --hc-* tokens resolve and
 * the z-50 layer sits above all view chrome (FAB / bell) — see the
 * PlanSpecDetailDrawer stacking-context note. SSR-safe: without a DOM it renders
 * inline (renderToStaticMarkup in tests).
 */
export function DrawerShell({
  eyebrow,
  title,
  icon: Icon,
  onClose,
  ariaLabel,
  closeLabel = "Schließen",
  headerExtra,
  footer,
  children,
  widthClassName = "tab:w-[min(420px,60vw)]",
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  icon?: ComponentType<{ className?: string }>;
  onClose: () => void;
  ariaLabel: string;
  closeLabel?: string;
  /** Extra header content under the title (path/copy row, chips …). */
  headerExtra?: ReactNode;
  /** Sticky footer (actions). */
  footer?: ReactNode;
  children: ReactNode;
  widthClassName?: string;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(dialogRef, true);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  const content = (
    // Mobile (<tab): bottom-sheet (items-end). From `tab` (600px): flush right
    // side-sheet (items-stretch, no gap/rounding — border-l is the only seam).
    <div
      className="fixed inset-0 z-50 flex items-end justify-end bg-black/50 backdrop-blur-sm tab:items-stretch"
      role="presentation"
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        className={cn(
          "hc-surface-card hc-side-sheet-in flex max-h-[92dvh] w-full flex-col overflow-hidden rounded-t-2xl shadow-2xl tab:h-full tab:max-h-full tab:rounded-none tab:border-l tab:border-line tab:shadow-none",
          widthClassName,
        )}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-3 border-b border-[var(--hc-border)] p-4">
          {Icon ? <Icon className="mt-1 h-5 w-5 shrink-0 text-[var(--hc-accent-text)]" /> : null}
          <div className="min-w-0 flex-1">
            {eyebrow != null ? <Eyebrow>{eyebrow}</Eyebrow> : null}
            <h2 className="mt-1 line-clamp-3 break-words text-lg font-semibold leading-snug text-[var(--hc-text)]">
              {title}
            </h2>
            {headerExtra}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-full border border-[var(--hc-border)] p-2 hc-soft hover:bg-white/5"
            aria-label={closeLabel}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4 pb-8">{children}</div>
        {footer != null ? <div className="border-t border-[var(--hc-border)] p-3">{footer}</div> : null}
      </div>
    </div>
  );

  if (typeof document === "undefined") return content;
  // data-control (display:contents): outside the [data-control] scope the --hc-*
  // tokens would be unresolved — same technique as Overlay / PlanSpecDetailDrawer.
  return createPortal(<div data-control className="contents">{content}</div>, document.body);
}
