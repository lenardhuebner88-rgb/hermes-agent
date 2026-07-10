import { Copy, FileText, TriangleAlert } from "lucide-react";
import { Link } from "react-router-dom";
import type { PlanSpecDetailResponse } from "../../lib/schemas";
import type { PlanSpecRecord } from "../../lib/types";
import { Eyebrow, SkeletonCard } from "../../components/primitives";
import { DrawerShell, SignalChip, signalToneFromLegacy } from "../../components/leitstand";
import { planSpecKanbanTone, planSpecKanbanLabel } from "./planSpecKanban";

function middleEllipsis(value: string, edge = 32): string {
  if (value.length <= edge * 2 + 3) return value;
  return `${value.slice(0, edge)}…${value.slice(-edge)}`;
}

function copyText(value: string): void {
  if (typeof navigator === "undefined" || !navigator.clipboard) return;
  void navigator.clipboard.writeText(value).catch(() => undefined);
}

export function PlanSpecDetailDrawer({ item, detail, loading, error, onClose }: {
  item: PlanSpecRecord;
  detail: PlanSpecDetailResponse | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
}) {
  return (
    <DrawerShell
      eyebrow="PlanSpec"
      title="PlanSpec-Details"
      icon={FileText}
      onClose={onClose}
      ariaLabel="PlanSpec Details"
      closeLabel="PlanSpec schließen"
      widthClassName="tab:w-[min(760px,calc(100vw-2rem))]"
    >
      <PlanSpecDetailContent item={item} detail={detail} loading={loading} error={error} />
    </DrawerShell>
  );
}

export function PlanSpecDetailContent({ item, detail, loading, error }: {
  item: PlanSpecRecord;
  detail: PlanSpecDetailResponse | null;
  loading: boolean;
  error: string | null;
}) {
  const displayPath = middleEllipsis(item.path);

  return (
    <div className="min-w-0">
      <div className="mb-4 min-w-0">
        <Eyebrow>PlanSpec</Eyebrow>
        <h3 className="mt-1 break-words font-display text-emph font-semibold text-ink" title={item.topic}>{item.topic}</h3>
        <div className="mt-3 flex min-w-0 items-center gap-2 rounded-card border border-line bg-surface-2 px-2 py-1.5">
          <code title={item.path} aria-label={`PlanSpec-Pfad ${item.path}`} className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap font-data text-micro text-ink-3">{displayPath}</code>
          <button
            type="button"
            onClick={() => copyText(item.path)}
            className="flex min-h-12 min-w-12 shrink-0 items-center justify-center rounded-card border border-line text-ink-2 hover:bg-surface-1 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-bronze"
            aria-label="PlanSpec-Pfad kopieren"
            title="PlanSpec-Pfad kopieren"
          >
            <Copy className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
      </div>
      <div className="flex flex-wrap gap-1.5">
        <SignalChip tone={signalToneFromLegacy(planSpecKanbanTone(item.kanban_state))} label={planSpecKanbanLabel(item)} />
        <span className="px-1 py-0.5 font-display text-micro uppercase tracking-[0.08em] text-ink-2">{detail?.freigabe || item.freigabe || "ohne Freigabe"}</span>
        <span className="px-1 py-0.5 font-display text-micro uppercase tracking-[0.08em] text-ink-2">{detail?.live_test_depth || item.live_test_depth || "smoke"}</span>
        {item.kanban_root_task_id ? (
          <Link to={`/control/fleet?root=${encodeURIComponent(item.kanban_root_task_id)}`} className="px-1 py-0.5 font-display text-micro font-semibold uppercase tracking-[0.08em] text-bronze underline decoration-bronze/50 underline-offset-4 hover:text-bronze-hi">
            Root {item.kanban_root_task_id} → Kette
          </Link>
        ) : null}
      </div>
      {loading ? <SkeletonCard rows={4} /> : null}
      {error ? <div role="alert" className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{error}</div> : null}
      {detail ? (
        <div className="mt-4 grid gap-4">
          <section className="rounded-card border border-line bg-surface-2 p-3">
            <Eyebrow>Ziel</Eyebrow>
            <p className="mt-2 whitespace-pre-wrap text-sec leading-relaxed text-ink-2">{detail.goal || item.topic}</p>
          </section>
          <section className="rounded-card border border-line bg-surface-2 p-3">
            <Eyebrow>Acceptance Criteria</Eyebrow>
            <ul className="mt-2 grid gap-2">
              {detail.acceptance_criteria.length ? detail.acceptance_criteria.map((ac, idx) => (
                <li key={`${ac.id ?? idx}`} className="rounded-card border border-line-soft bg-surface-1 px-3 py-2 text-sec text-ink-2">
                  {ac.id ? <span className="mb-1.5 inline-flex rounded-card border border-line bg-surface-2 px-2 py-0.5 font-data text-micro text-ink-2">{String(ac.id)}</span> : null}
                  <p className="whitespace-pre-wrap break-words leading-relaxed">{String(ac.statement ?? "")}</p>
                </li>
              )) : <li className="text-sec text-ink-2">Keine Kriterien im Detail-Payload.</li>}
            </ul>
          </section>
          {detail.anti_scope.length ? (
            <section className="rounded-card border border-line bg-surface-2 p-3">
              <Eyebrow>Nicht im Scope</Eyebrow>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-sec text-ink-2">{detail.anti_scope.map((x) => <li key={x}>{x}</li>)}</ul>
            </section>
          ) : null}
          {/* Dashboard-Prose-Plan: kein YAML-Frontmatter, kein AC/Anti-Scope —
              der Volltext-Button muss trotzdem den geschriebenen Markdown zeigen. */}
          {detail.prose_plan && detail.full_text ? (
            <section className="rounded-card border border-line bg-surface-2 p-3">
              <Eyebrow>Volltext</Eyebrow>
              <pre className="mt-2 whitespace-pre-wrap break-words text-sec leading-relaxed text-ink-2">{detail.full_text}</pre>
            </section>
          ) : null}
          <section className="rounded-card border border-line bg-surface-2 p-3">
            <Eyebrow>Subtask-Kette</Eyebrow>
            <ol className="mt-2 grid gap-2">
              {detail.subtasks.map((task, idx) => (
                <li key={`${task.id}:${idx}`} className="rounded-card border border-line-soft bg-surface-1 px-3 py-2">
                  <div className="flex flex-wrap items-center gap-2 text-sec text-ink"><span className="font-data text-micro text-ink-3">{idx + 1}</span><strong>{task.title || task.id}</strong></div>
                  <p className="mt-1 font-display text-micro uppercase tracking-[0.06em] text-ink-3">{task.id} · {task.lane || "ohne Lane"}{task.deps.length ? ` · deps: ${task.deps.join(", ")}` : ""}</p>
                </li>
              ))}
            </ol>
          </section>
        </div>
      ) : null}
    </div>
  );
}
