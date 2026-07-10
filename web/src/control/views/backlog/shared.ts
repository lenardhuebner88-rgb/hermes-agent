import { de } from "../../i18n/de";
import {
  nextActionForFoItem,
} from "../../lib/foBacklog";
import type { FoQuickView } from "../../lib/foBacklog";
import type { BacklogDetail, BacklogItem } from "../../lib/schemas";
import type { ToneName } from "../../lib/types";

export type Status = "now" | "next" | "in_progress" | "blocked" | "later" | "done";
export type ViewMode = "queue" | "board";

export const QUICK_VIEWS: Array<{ id: FoQuickView; label: string }> = [
  { id: "all", label: "Alle" },
  { id: "ready", label: "Commission-ready" },
  { id: "groom", label: "Grooming nötig" },
  { id: "stale", label: "Stale" },
  { id: "unowned", label: "Ohne Owner" },
];

export const VIEW_STORAGE_KEY = "fo-backlog-view-v1";

export const ACTIVE_COLUMNS: Array<{ key: Exclude<Status, "done">; label: string; tone: ToneName }> = [
  { key: "now", label: de.backlog.colNow, tone: "sky" },
  { key: "next", label: de.backlog.colNext, tone: "indigo" },
  { key: "in_progress", label: de.backlog.colInProgress, tone: "violet" },
  { key: "blocked", label: de.backlog.colBlocked, tone: "red" },
  { key: "later", label: de.backlog.colLater, tone: "zinc" },
];

export const STATUS_TONE: Record<string, ToneName> = {
  now: "sky",
  next: "indigo",
  in_progress: "violet",
  blocked: "red",
  later: "zinc",
  done: "emerald",
};

export const RISK_TONE: Record<string, ToneName> = { high: "red", medium: "amber", low: "zinc" };

export function clockLabel(nowSec: number): string {
  return new Date(nowSec * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}

export function relLabel(updated: string, nowSec: number): string {
  if (!updated) return "-";
  const t = Date.parse(`${updated.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(t)) return updated;
  const days = Math.floor((nowSec * 1000 - t) / 86_400_000);
  if (days <= 0) return "heute";
  if (days === 1) return "gestern";
  if (days < 7) return `vor ${days} T`;
  if (days < 30) return `vor ${Math.floor(days / 7)} Wo`;
  return `vor ${Math.floor(days / 30)} Mon`;
}

export function sourceRef(item: BacklogItem): string {
  return item.source_path || `backlog/items/${item.id}.md`;
}

export function operatorBrief(item: BacklogItem, detail?: BacklogDetail): string {
  return [
    `FO Backlog ${item.id}: ${item.title}`,
    `Status/Risk/Owner: ${item.status} / ${item.risk || "-"} / ${item.owner || "-"}`,
    `Area: ${item.area || "-"}`,
    `Next Action: ${nextActionForFoItem(item, detail)}`,
    `Source: ${detail?.source_path || sourceRef(item)}`,
    detail?.source_ref ? `Ref: ${detail.source_ref}` : null,
  ].filter(Boolean).join("\n");
}
