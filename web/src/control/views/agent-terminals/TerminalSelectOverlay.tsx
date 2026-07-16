/**
 * Frozen terminal buffer snapshot as native selectable text.
 *
 * xterm.js 6 has no touch selection (mouse-only SelectionService), so mobile
 * operators cannot mark text for the toolbar copy button. This overlay takes a
 * one-shot buffer snapshot and renders it in a monospace <pre> where Android
 * long-press selection handles work. The text prop is frozen by the parent at
 * open time — no live re-render while open, so a selection cannot be destroyed
 * by polling or socket traffic.
 */
import { useEffect, useState } from "react";
import { X } from "lucide-react";

import { copyTextToClipboard } from "@/lib/clipboard";
import { cn } from "@/lib/utils";
import { Overlay } from "../../components/Overlay";
import { Eyebrow } from "../../components/primitives";

/** Cap DOM size: only the last N buffer lines enter the snapshot. */
export const TERMINAL_SNAPSHOT_MAX_LINES = 2000;

/** Minimal xterm buffer shape needed for snapshot extraction (real Terminal + test fakes). */
export type TerminalBufferLike = {
  buffer: {
    active: {
      length: number;
      getLine: (
        index: number,
      ) => { translateToString: (trimRight?: boolean) => string } | undefined | null;
    };
  };
};

/**
 * Extract printable buffer text from an xterm Terminal (or a test double).
 * Uses buffer.active line API, trims trailing empty lines, caps at the last
 * `maxLines` lines.
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
    lines.push(line ? line.translateToString(true) : "");
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
}: {
  /** Frozen snapshot — parent must not update this while the overlay is open. */
  text: string;
  onClose: () => void;
}) {
  const [copyState, setCopyState] = useState<OverlayCopyState>("idle");

  useEffect(() => {
    if (copyState === "idle") return;
    const timer = window.setTimeout(() => setCopyState("idle"), OVERLAY_COPY_STATUS_MS);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  const copyAll = async () => {
    if (!text) {
      setCopyState("error");
      return;
    }
    const ok = await copyTextToClipboard(text);
    setCopyState(ok ? "copied" : "error");
  };

  return (
    <Overlay onClose={onClose} ariaLabel="Terminal-Text auswählen" maxWidthClassName="max-w-2xl">
      <div className="flex flex-col gap-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <Eyebrow>Auswahl</Eyebrow>
            <h2 className="text-sm font-semibold text-ink">Text auswählen</h2>
            <p className="mt-0.5 text-[11px] text-ink-3">
              Snapshot — lang drücken zum Markieren, oder alles kopieren.
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

        <div className="max-h-[min(50dvh,28rem)] overflow-y-auto overscroll-contain rounded-card border border-line bg-surface-2 p-3">
          <pre className="select-text whitespace-pre-wrap break-words font-mono text-[12px] leading-5 text-ink">
            {text || "(kein Buffer-Text)"}
          </pre>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void copyAll()}
            className="inline-flex min-h-11 flex-1 items-center justify-center gap-1.5 rounded-card border border-live/50 bg-live/15 px-3 py-2 text-sm font-medium text-live hover:bg-live/25 sm:flex-none"
          >
            Alles kopieren
          </button>
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
