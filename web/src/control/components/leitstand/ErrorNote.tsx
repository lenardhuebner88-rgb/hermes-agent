/**
 * ErrorNote — das EINE Fetch-Fehler-Banner des Leitstands. Ersetzt die ~15
 * kopierten TriangleAlert-Banner, die auseinandergelaufen waren (mal mit
 * role="alert", mal ohne, nie mit Retry), obwohl jeder usePolling-Hook ein
 * reload() liefert.
 *
 * Ruhiges Operator-Deutsch, Token-Farben only, immer role="alert". Der
 * Retry-Button ist bewusst neutral gehalten (kein Status-Farb-Button —
 * DESIGN.md Akzent-Doktrin 3: Statusfarbe ist nie die Affordanz; Bronze nur
 * am Hover, weil der Button interaktiv ist).
 *
 * NICHT für: Aktions-Fehler (POST schlug fehl — da gibt es nichts zu
 * "erneut laden"), Daten-Lagen-Hinweise (Job last_error, Gateway-down,
 * dünne Nenner) und Leer-Zustände (das ist FleetEmptyState).
 */
import { RefreshCw, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export type ErrorNoteTone = "alert" | "warn";

const TONE_CLASSES: Record<ErrorNoteTone, string> = {
  alert: "border-status-alert/30 bg-status-alert/10 text-status-alert",
  warn: "border-status-warn/30 bg-status-warn/10 text-status-warn",
};

export function ErrorNote({
  message,
  detail,
  onRetry,
  retryLabel = "Erneut laden",
  tone = "alert",
  className,
}: {
  /** Die ruhige Meldungszeile (was ist nicht lesbar). */
  message: ReactNode;
  /** Optional: technischer Grund unter der Meldung (z. B. error.message). */
  detail?: ReactNode;
  /** Retry-Affordanz — in der Regel `() => void reload()` des Hooks. */
  onRetry?: () => void;
  retryLabel?: string;
  /** "warn" wenn der Lese-Fehler degradiert statt tot ist (stale-while-error). */
  tone?: ErrorNoteTone;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn("flex items-start gap-2 rounded-card border px-3 py-2 text-sec", TONE_CLASSES[tone], className)}
    >
      <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
      <span className="min-w-0 flex-1">
        <span className="block">{message}</span>
        {detail != null ? <span className="mt-0.5 block break-words text-ink-2">{detail}</span> : null}
      </span>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex min-h-9 shrink-0 items-center gap-1.5 self-center rounded-card border border-line bg-surface-2 px-3 text-sec text-ink hover:border-live hover:text-live"
        >
          <RefreshCw aria-hidden className="size-3.5" />
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}
