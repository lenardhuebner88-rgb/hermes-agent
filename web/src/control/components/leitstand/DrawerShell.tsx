import { useEffect, type ComponentType, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Eyebrow } from "../primitives";

/**
 * DrawerShell — the shared frame every Leitstand detail drawer sits in: a
 * body-portalled bottom-sheet (mobile) / right drawer (sm+), with a backdrop,
 * Escape-to-close and scroll-lock, a header (optional icon + eyebrow + title +
 * close button, plus a free `headerExtra` slot) and a scrollable body with an
 * optional sticky footer.
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
  widthClassName = "sm:w-[min(760px,calc(100vw-2rem))]",
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
    // Mobile: bottom-sheet (items-end); desktop (sm+): right drawer (justify-end).
    <div
      className="fixed inset-0 z-50 flex items-end justify-end bg-black/50 backdrop-blur-sm sm:items-stretch sm:p-3"
      role="presentation"
      onClick={onClose}
    >
      <div
        className={cn(
          "hc-surface-card flex max-h-[92dvh] w-full flex-col overflow-hidden rounded-t-2xl shadow-2xl sm:h-full sm:max-h-full sm:rounded-2xl",
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
