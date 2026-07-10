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
  const displayPath = middleEllipsis(item.path);
  return (
    <DrawerShell
      eyebrow="PlanSpec"
      title={<span title={item.topic}>{item.topic}</span>}
      icon={FileText}
      onClose={onClose}
      ariaLabel="PlanSpec Details"
      closeLabel="PlanSpec schließen"
      widthClassName="tab:w-[min(760px,calc(100vw-2rem))]"
      headerExtra={
        <div className="mt-2 flex min-w-0 items-center gap-2 rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1.5">
          <code title={item.path} aria-label={`PlanSpec-Pfad ${item.path}`} className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap hc-mono text-[0.72rem] hc-dim">{displayPath}</code>
          <button type="button" onClick={() => copyText(item.path)} className="shrink-0 rounded-md border border-[var(--hc-border)] p-1.5 hc-soft hover:bg-white/5" aria-label="PlanSpec-Pfad kopieren" title="PlanSpec-Pfad kopieren">
            <Copy className="h-3.5 w-3.5" />
          </button>
        </div>
      }
    >
      <div className="flex flex-wrap gap-1.5">
        <SignalChip tone={signalToneFromLegacy(planSpecKanbanTone(item.kanban_state))} label={planSpecKanbanLabel(item)} />
        <span className="rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-type-label hc-soft">{detail?.freigabe || item.freigabe || "ohne Freigabe"}</span>
        <span className="rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-type-label hc-soft">{detail?.live_test_depth || item.live_test_depth || "smoke"}</span>
        {item.kanban_root_task_id ? (
          <Link to={`/control/fleet?root=${encodeURIComponent(item.kanban_root_task_id)}`} className="rounded-full border border-live/30 bg-live/10 px-2 py-0.5 hc-type-label text-live hover:brightness-110">
            Root {item.kanban_root_task_id} → Kette
          </Link>
        ) : null}
      </div>
      {loading ? <SkeletonCard rows={4} /> : null}
      {error ? <div role="alert" className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{error}</div> : null}
      {detail ? (
        <div className="mt-4 grid gap-4">
          <section className="rounded-xl border border-[var(--hc-border)] bg-[var(--hc-panel-2)] p-3">
            <Eyebrow>Ziel</Eyebrow>
            <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed hc-soft">{detail.goal || item.topic}</p>
          </section>
          <section className="rounded-xl border border-[var(--hc-border)] bg-[var(--hc-panel-2)] p-3">
            <Eyebrow>Acceptance Criteria</Eyebrow>
            <ul className="mt-2 grid gap-2">
              {detail.acceptance_criteria.length ? detail.acceptance_criteria.map((ac, idx) => (
                <li key={`${ac.id ?? idx}`} className="rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel-card)] px-3 py-2 text-sm hc-soft">
                  {ac.id ? <span className="mb-1.5 inline-flex rounded-full border border-line bg-white/5 px-2 py-0.5 font-data text-[0.7rem] text-ink-2">{String(ac.id)}</span> : null}
                  <p className="whitespace-pre-wrap break-words leading-relaxed">{String(ac.statement ?? "")}</p>
                </li>
              )) : <li className="text-sm hc-dim">Keine Kriterien im Detail-Payload.</li>}
            </ul>
          </section>
          {detail.anti_scope.length ? (
            <section className="rounded-xl border border-[var(--hc-border)] bg-[var(--hc-panel-2)] p-3">
              <Eyebrow>Nicht im Scope</Eyebrow>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-sm hc-soft">{detail.anti_scope.map((x) => <li key={x}>{x}</li>)}</ul>
            </section>
          ) : null}
          {/* Dashboard-Prose-Plan: kein YAML-Frontmatter, kein AC/Anti-Scope —
              der Volltext-Button muss trotzdem den geschriebenen Markdown zeigen. */}
          {detail.prose_plan && detail.full_text ? (
            <section className="rounded-xl border border-[var(--hc-border)] bg-[var(--hc-panel-2)] p-3">
              <Eyebrow>Volltext</Eyebrow>
              <pre className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed hc-soft">{detail.full_text}</pre>
            </section>
          ) : null}
          <section className="rounded-xl border border-[var(--hc-border)] bg-[var(--hc-panel-2)] p-3">
            <Eyebrow>Subtask-Kette</Eyebrow>
            <ol className="mt-2 grid gap-2">
              {detail.subtasks.map((task, idx) => (
                <li key={`${task.id}:${idx}`} className="rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel-card)] px-3 py-2">
                  <div className="flex flex-wrap items-center gap-2 text-sm text-[var(--hc-text)]"><span className="hc-mono text-[0.7rem] hc-dim">{idx + 1}</span><strong>{task.title || task.id}</strong></div>
                  <p className="mt-1 hc-type-label hc-dim">{task.id} · {task.lane || "ohne Lane"}{task.deps.length ? ` · deps: ${task.deps.join(", ")}` : ""}</p>
                </li>
              ))}
            </ol>
          </section>
        </div>
      ) : null}
    </DrawerShell>
  );
}
