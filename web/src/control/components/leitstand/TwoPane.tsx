import { useEffect, useId, useRef, useState, type FocusEvent, type ReactNode } from "react";
import { X } from "lucide-react";

import "./TwoPane.css";

export interface TwoPaneProps {
  list: ReactNode;
  detail?: ReactNode;
  detailLabel: string;
  onCloseDetail?: () => void;
  idleDetail?: ReactNode;
}

function useIsExpanded(): boolean {
  const [matches, setMatches] = useState(() => (
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(min-width: 1024px)").matches
      : false
  ));

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(min-width: 1024px)");
    const onChange = () => setMatches(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  return matches;
}

/**
 * Canonical expanded list-detail composition. The caller owns the viewport
 * fork: below 1024px it passes no detail and mounts its DrawerShell instead.
 */
export function TwoPane({
  list,
  detail,
  detailLabel,
  onCloseDetail,
  idleDetail,
}: TwoPaneProps) {
  const isExpanded = useIsExpanded();
  const detailId = useId();
  const listRef = useRef<HTMLDivElement>(null);
  const lastListFocusRef = useRef<HTMLElement | null>(null);
  const visibleDetail = isExpanded ? detail ?? idleDetail : undefined;
  const hasDetail = visibleDetail !== undefined;

  function rememberListFocus(event: FocusEvent<HTMLDivElement>) {
    if (event.target instanceof HTMLElement) lastListFocusRef.current = event.target;
  }

  function closeDetail() {
    onCloseDetail?.();
    const trigger = lastListFocusRef.current;
    if (trigger?.isConnected) trigger.focus();
    else listRef.current?.focus();
  }

  return (
    <div
      className={`leitstand-two-pane${hasDetail ? " leitstand-two-pane--split" : ""}`}
      data-layout={hasDetail ? "split" : "single"}
    >
      <div
        ref={listRef}
        className="min-h-0 min-w-0 overflow-y-auto"
        tabIndex={-1}
        onFocusCapture={rememberListFocus}
      >
        {list}
      </div>

      {hasDetail ? (
        <section
          id={detailId}
          className="min-h-0 min-w-0 overflow-hidden rounded-panel border border-line bg-surface-1"
          role="region"
          aria-label={detailLabel}
        >
          <header className="flex min-h-12 items-center gap-3 border-b border-line-soft px-4">
            <h2 className="min-w-0 flex-1 truncate font-display text-micro font-semibold uppercase tracking-[0.12em] text-ink-3">
              {detailLabel}
            </h2>
            {onCloseDetail ? (
              <button
                type="button"
                className="flex min-h-12 min-w-12 items-center justify-center rounded-card text-ink-2 transition-colors duration-150 hover:bg-surface-2 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-bronze"
                onClick={closeDetail}
                aria-label="Detail schließen"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            ) : null}
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">{visibleDetail}</div>
        </section>
      ) : null}
    </div>
  );
}
