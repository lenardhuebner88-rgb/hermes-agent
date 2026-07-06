export type CardStatus = "open" | "in_progress" | "addressed" | "archived";

export const STATUS_LABELS: Record<CardStatus, string> = {
  open: "offen",
  in_progress: "in arbeit",
  addressed: "erledigt",
  archived: "archiviert",
};

export const STATUS_CLASS: Record<CardStatus, string> = {
  open: "text-ink-3",
  in_progress: "text-status-warn",
  addressed: "text-status-ok",
  archived: "text-brand",
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status as CardStatus] ?? status;
}

export function statusClass(status: string): string {
  return STATUS_CLASS[status as CardStatus] ?? "text-ink-3";
}

export function statusBadge(status: string | null | undefined) {
  const s = status ?? "open";
  return (
    <span className={`hc-type-label ${statusClass(s)}`}>
      {statusLabel(s)}
    </span>
  );
}
