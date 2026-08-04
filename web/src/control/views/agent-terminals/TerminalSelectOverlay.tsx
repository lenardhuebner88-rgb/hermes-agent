/**
 * Frozen terminal history snapshot as native, searchable, selectable text.
 *
 * Reading a live attach means scrolling a fullscreen TUI through tmux copy-mode
 * — a chain that has broken more than once. This overlay sidesteps it: one
 * server-side snapshot rendered as plain text, so the browser's own scrolling,
 * selection and long-press handles do the work. (xterm.js 6 has no touch
 * selection at all — its SelectionService is mouse-only.)
 *
 * The text prop is frozen by the parent at open time — no live re-render while
 * open, so a selection cannot be destroyed by polling or socket traffic.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDownToLine, ArrowUpToLine, Search, X } from "lucide-react";

import { copyTextToClipboard } from "@/lib/clipboard";
import { cn } from "@/lib/utils";
import { Overlay } from "../../components/Overlay";
import { Eyebrow } from "../../components/primitives";
import { buildHistoryView } from "./terminalHelpers";

/** Cap DOM size: only the last N buffer lines enter the snapshot. */
export const TERMINAL_SNAPSHOT_MAX_LINES = 2000;

/** Minimal xterm buffer shape needed for snapshot extraction (real Terminal + test fakes). */
export type TerminalBufferLike = {
  buffer: {
    active: {
      length: number;
      /** xterm buffer type: "normal" | "alternate" (optional on fakes). */
      type?: string;
      getLine: (
        index: number,
      ) =>
        | {
            translateToString: (trimRight?: boolean) => string;
            /** True when this physical row continues the previous logical line. */
            isWrapped?: boolean;
          }
        | undefined
        | null;
    };
  };
};

/**
 * Extract printable buffer text from an xterm Terminal (or a test double).
 * Uses buffer.active line API, trims trailing empty lines, caps at the last
 * `maxLines` lines. Continuation rows marked `isWrapped` are concatenated onto
 * the previous logical line (no artificial newline).
 */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for TerminalPane + tests
export function extractTerminalBufferText(
  term: TerminalBufferLike | null | undefined,
  maxLines: number = TERMINAL_SNAPSHOT_MAX_LINES,
): string {
  const active = term?.buffer?.active;
  if (!active || active.length <= 0) return "";

  const length = active.length;
  const start = Math.max(0, length - Math.max(1, maxLines));
  const lines: string[] = [];
  for (let i = start; i < length; i += 1) {
    const line = active.getLine(i);
    const text = line ? line.translateToString(true) : "";
    // isWrapped on the current row means it continues the previous logical line.
    // Fakes without the field keep the old per-row newline behaviour.
    if (line?.isWrapped && lines.length > 0) {
      lines[lines.length - 1] += text;
    } else {
      lines.push(text);
    }
  }
  while (lines.length > 0 && lines[lines.length - 1].trim() === "") {
    lines.pop();
  }
  return lines.join("\n");
}

type OverlayCopyState = "idle" | "copied" | "error";

const OVERLAY_COPY_STATUS_MS = 2000;

export function TerminalSelectOverlay({
  text,
  onClose,
  onLoadMore,
  loadingMore = false,
  canLoadMore = false,
}: {
  /** Frozen snapshot — parent must not update this while the overlay is open. */
  text: string;
  onClose: () => void;
  /** Re-fetch the snapshot at a deeper scrollback bound. */
  onLoadMore?: () => void;
  loadingMore?: boolean;
  canLoadMore?: boolean;
}) {
  const [copyState, setCopyState] = useState<OverlayCopyState>("idle");
  const [query, setQuery] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (copyState === "idle") return;
    const timer = window.setTimeout(() => setCopyState("idle"), OVERLAY_COPY_STATUS_MS);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  const view = useMemo(() => buildHistoryView(text, query), [text, query]);

  // Open at the newest output — that is what the operator came to read. Deeper
  // reloads keep the position they were scrolled to, so `text` is not a dep.
  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, []);

  const jumpTo = (edge: "start" | "end") => {
    const node = scrollRef.current;
    if (!node) return;
    node.scrollTop = edge === "start" ? 0 : node.scrollHeight;
  };

  const copyAll = async () => {
    if (!text) {
      setCopyState("error");
      return;
    }
    // Copy what is on screen: a filtered view copies the hits, not the haystack.
    const payload = view.filtered ? view.lines.map((line) => line.text).join("\n") : text;
    const ok = await copyTextToClipboard(payload);
    setCopyState(ok ? "copied" : "error");
  };

  return (
    <Overlay onClose={onClose} ariaLabel="Terminal-Verlauf" maxWidthClassName="max-w-2xl">
      <div className="flex flex-col gap-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <Eyebrow>Verlauf</Eyebrow>
            <h2 className="text-sm font-semibold text-ink">Verlauf lesen</h2>
            <p className="mt-0.5 text-[11px] text-ink-3">
              {view.filtered
                ? `${view.lines.length} von ${view.total} Zeilen`
                : `${view.total} Zeilen — lang drücken zum Markieren.`}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Schließen"
            className="shrink-0 rounded-card border border-line p-1.5 text-ink-2 hover:bg-surface-3"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex items-center gap-2">
          <div className="relative min-w-0 flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-3" />
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Im Verlauf suchen"
              aria-label="Im Verlauf suchen"
              className="min-h-11 w-full rounded-card border border-line bg-surface-2 py-2 pl-8 pr-2 font-mono text-[12px] text-ink placeholder:text-ink-3 focus:border-live/50 focus:outline-none"
            />
          </div>
          <button
            type="button"
            onClick={() => jumpTo("start")}
            aria-label="Zum Anfang"
            className="grid min-h-11 w-11 shrink-0 place-items-center rounded-card border border-line bg-surface-2 text-ink-2 hover:bg-surface-3"
          >
            <ArrowUpToLine className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => jumpTo("end")}
            aria-label="Zum Ende"
            className="grid min-h-11 w-11 shrink-0 place-items-center rounded-card border border-line bg-surface-2 text-ink-2 hover:bg-surface-3"
          >
            <ArrowDownToLine className="h-4 w-4" />
          </button>
        </div>

        {copyState !== "idle" && (
          <span
            role="status"
            className={cn(
              "text-[11px]",
              copyState === "copied" ? "text-live" : "text-status-alert",
            )}
          >
            {copyState === "copied" ? "Kopiert" : "Kopieren fehlgeschlagen"}
          </span>
        )}

        <div
          ref={scrollRef}
          className="max-h-[min(68dvh,36rem)] overflow-y-auto overscroll-contain rounded-card border border-line bg-surface-2 p-3"
        >
          {view.lines.length === 0 ? (
            <p className="font-mono text-[12px] text-ink-3">
              {view.filtered ? "Keine Treffer" : "(kein Buffer-Text)"}
            </p>
          ) : (
            <pre className="select-text whitespace-pre-wrap break-words font-mono text-[12px] leading-5 text-ink">
              {view.filtered
                ? view.lines.map((line) => `${line.number}  ${line.text}`).join("\n")
                : text}
            </pre>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void copyAll()}
            className="inline-flex min-h-11 flex-1 items-center justify-center gap-1.5 rounded-card border border-live/50 bg-live/15 px-3 py-2 text-sm font-medium text-live hover:bg-live/25 sm:flex-none"
          >
            {view.filtered ? "Treffer kopieren" : "Alles kopieren"}
          </button>
          {canLoadMore && onLoadMore && (
            <button
              type="button"
              onClick={onLoadMore}
              disabled={loadingMore}
              className="inline-flex min-h-11 flex-1 items-center justify-center rounded-card border border-line bg-surface-2 px-3 py-2 text-sm text-ink-2 hover:bg-surface-3 disabled:opacity-40 sm:flex-none"
            >
              {loadingMore ? "Lädt …" : "Mehr laden"}
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            className="inline-flex min-h-11 flex-1 items-center justify-center rounded-card border border-line bg-surface-2 px-3 py-2 text-sm text-ink-2 hover:bg-surface-3 sm:flex-none"
          >
            Schließen
          </button>
        </div>
      </div>
    </Overlay>
  );
}
