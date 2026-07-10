import { de } from "../../i18n/de";
import {
  isKnownStatus,
  projectFromRoot,
} from "../../lib/orchestration";
import type { Readiness } from "../../lib/orchestration";
import type { OrchestrationDetail, OrchestrationItem } from "../../lib/schemas";
import type { SignalTone } from "../../components/leitstand";

export type ViewMode = "queue" | "board";
export type DetailChip = { label: string; tone?: SignalTone };

export const ACTIVE_COLUMNS: Array<{ key: string; label: string; tone: SignalTone }> = [
  { key: "doing", label: de.orchestrator.colDoing, tone: "ok" },
  { key: "review", label: de.orchestrator.colReview, tone: "warn" },
  { key: "todo", label: de.orchestrator.colTodo, tone: "neutral" },
  { key: "backlog", label: de.orchestrator.colBacklog, tone: "neutral" },
  { key: "__drift", label: de.orchestrator.statusDrift, tone: "alert" },
];

const PRIORITY_TONE: Record<string, SignalTone> = { high: "alert", medium: "warn", low: "neutral" };
const STATUS_TONE: Record<string, SignalTone> = { doing: "ok", review: "warn", todo: "neutral", backlog: "neutral", done: "ok" };

export function readinessChip(value: Readiness): DetailChip | null {
  if (value.state === "ready") return { tone: "ok", label: de.orchestrator.ready };
  if (value.state === "blocked") {
    return { tone: "alert", label: `${de.orchestrator.blockedBy} ${value.blockedBy.join(", ")}` };
  }
  return null;
}

export function clockLabel(nowSec: number): string {
  return new Date(nowSec * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}

export function statusTone(status: string): SignalTone {
  if (!isKnownStatus(status)) return "alert";
  return STATUS_TONE[status] ?? "neutral";
}

export function priorityTone(priority: string): SignalTone {
  return PRIORITY_TONE[priority] ?? "neutral";
}

export function proofLabel(item: OrchestrationItem): string {
  return item.lastProof?.trim() || de.orchestrator.proofMissing;
}

export function ownerLabel(item: OrchestrationItem): string {
  return item.owner?.trim() || de.orchestrator.ownerMissing;
}

export function sourceLabel(item: OrchestrationItem): string {
  return item.source?.trim() || projectFromRoot(item.root) || de.orchestrator.sourceFallback;
}

export function sourcePath(id: string): string {
  return `~/orchestration/backlog/${id}.md`;
}

export function buildOperatorBrief(
  item: OrchestrationItem | undefined,
  detail: OrchestrationDetail | undefined,
  nextAction: string,
  responseRef: string,
): string | undefined {
  if (!item && !detail) return undefined;
  const id = item?.id ?? detail?.id ?? "";
  const title = detail?.title || item?.title || id;
  const status = detail?.status || item?.status || "";
  const priority = detail?.priority || item?.priority || "";
  const owner = detail?.owner || item?.owner || de.orchestrator.ownerMissing;
  const source = detail?.source || item?.source || sourceLabel(item ?? ({ root: detail?.root ?? "" } as OrchestrationItem));
  const proof = detail?.lastProof || item?.lastProof || de.orchestrator.proofMissing;
  return [
    "Hermes Orchestrator Backlog Brief",
    `Task: ${title} (${id})`,
    `Status: ${status}`,
    `Priority/Risk: ${priority || "n/a"}`,
    `Owner: ${owner || de.orchestrator.ownerMissing}`,
    `Source: ${source || de.orchestrator.sourceFallback}`,
    `Last Proof: ${proof}`,
    `Next Action: ${nextAction}`,
    `Spec: ${sourcePath(id)}`,
    responseRef ? `Ref: ${responseRef}` : "",
  ].filter(Boolean).join("\n");
}
