import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE_SELECTOR = "button,[href],input,select,textarea,[tabindex]:not([tabindex='-1'])";

function focusableIn(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter((el) => {
    if (el.hasAttribute("disabled")) return false;
    // Walk the local layout tree explicitly instead of relying only on
    // offsetParent: jsdom has no layout, and fixed-position controls can have
    // no offset parent despite being visible. Hidden/display:none ancestors
    // and visibility:hidden make a descendant ineligible for the trap.
    for (let current: HTMLElement | null = el; current; current = current.parentElement) {
      if (current.hidden) return false;
      const style = window.getComputedStyle(current);
      if (style.display === "none" || style.visibility === "hidden") return false;
      if (current === container) break;
    }
    return true;
  });
}

/**
 * useFocusTrap — the minimal focus contract every portalled dialog (Overlay,
 * DrawerShell) owes the keyboard: focus the first focusable element on open,
 * trap Tab/Shift+Tab inside the container while it's open, and restore focus
 * to whatever had it before opening once the dialog unmounts (only if that
 * element is still attached to the document — it may have been removed by
 * the same state change that closed the dialog).
 */
export function useFocusTrap(containerRef: RefObject<HTMLElement | null>, active: boolean): void {
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!active) return;
    previouslyFocused.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const container = containerRef.current;
    const first = container ? focusableIn(container)[0] : undefined;
    first?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || !containerRef.current) return;
      const items = focusableIn(containerRef.current);
      if (items.length === 0) return;
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      if (event.shiftKey && document.activeElement === firstEl) {
        event.preventDefault();
        lastEl.focus();
      } else if (!event.shiftKey && document.activeElement === lastEl) {
        event.preventDefault();
        firstEl.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);

    return () => {
      window.removeEventListener("keydown", onKeyDown);
      const toRestore = previouslyFocused.current;
      if (toRestore && document.contains(toRestore)) toRestore.focus();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);
}
