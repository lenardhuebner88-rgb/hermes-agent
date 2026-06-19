import { useEffect } from "react";
import { createPortal } from "react-dom";
import { FileText, X } from "lucide-react";
import { Link } from "react-router-dom";
import type { PlanSpecDetailResponse } from "../../lib/schemas";
import type { PlanSpecRecord } from "../../lib/types";
import { StatusPill, ToneCallout } from "../../components/atoms";
import { Eyebrow, SkeletonCard } from "../../components/primitives";
import { planSpecKanbanTone, planSpecKanbanLabel } from "./planSpecKanban";

export function PlanSpecDetailDrawer({ item, detail, loading, error, onClose }: {
  item: PlanSpecRecord;
  detail: PlanSpecDetailResponse | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
}) {
  // Portal an document.body + Escape/Scroll-Lock: inline gerendert säße der Drawer
  // im Stacking-Context von FlowView/RouteTransition — sein z-50 zählte nur dort,
  // und der body-Level Capture-FAB (z-40) malte darüber (Screenshot-Audit
  // 2026-06-19). Am body steht z-50 wieder über allem Chrome.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = prev; };
  }, [onClose]);
  const content = (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/50 p-3 backdrop-blur-sm" role="presentation" onClick={onClose}>
      {/* hc-surface-card = solider Daylight-Fill (--hc-panel-card) + Border +
          Elevation. Früher lag hier der Token hc-surface (ohne -card) als Fill —
          den gibt es NICHT, also war das var() ohne Fallback transparent und das
          Panel durchsichtig/unlesbar. (Klassen-Literal hier bewusst vermeiden —
          Tailwind v4 scannt auch Kommentare und würde sonst tote CSS erzeugen.) */}
      <div className="hc-surface-card flex h-full w-full max-w-2xl flex-col overflow-hidden rounded-2xl shadow-2xl" role="dialog" aria-modal="true" aria-label="PlanSpec Details" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start gap-3 border-b border-[var(--hc-border)] p-4">
          <FileText className="mt-1 h-5 w-5 shrink-0 text-[var(--hc-accent-text)]" />
          <div className="min-w-0 flex-1">
            <Eyebrow>PlanSpec</Eyebrow>
            {/* Topics sind oft ganze Sätze — als Titel auf 3 Zeilen clampen
                (der Volltext steht ungekürzt in der „Ziel"-Sektion unten). */}
            <h2 title={item.topic} className="mt-1 line-clamp-3 break-words text-lg font-semibold leading-snug text-white">{item.topic}</h2>
            <p className="mt-1 break-all hc-mono text-[0.72rem] hc-dim">{item.path}</p>
          </div>
          <button type="button" onClick={onClose} className="rounded-full border border-[var(--hc-border)] p-2 hc-soft hover:bg-white/5" aria-label="PlanSpec schließen">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="flex flex-wrap gap-1.5">
            <StatusPill tone={planSpecKanbanTone(item.kanban_state)} label={planSpecKanbanLabel(item)} />
            <span className="rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-type-label hc-soft">{detail?.freigabe || item.freigabe || "ohne Freigabe"}</span>
            <span className="rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-type-label hc-soft">{detail?.live_test_depth || item.live_test_depth || "smoke"}</span>
            {item.kanban_root_task_id ? (
              <Link to={`/control/ketten?root=${encodeURIComponent(item.kanban_root_task_id)}`} className="rounded-full border border-cyan-400/30 bg-cyan-400/10 px-2 py-0.5 hc-type-label text-cyan-100 hover:brightness-110">
                Root {item.kanban_root_task_id} → Kette
              </Link>
            ) : null}
          </div>
          {loading ? <SkeletonCard rows={4} /> : null}
          {error ? <ToneCallout tone="amber">{error}</ToneCallout> : null}
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
                      {ac.id ? <span className="mr-2 hc-mono text-[0.7rem] text-cyan-100">{String(ac.id)}</span> : null}
                      {String(ac.statement ?? "")}
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
              <section className="rounded-xl border border-[var(--hc-border)] bg-[var(--hc-panel-2)] p-3">
                <Eyebrow>Subtask-Kette</Eyebrow>
                <ol className="mt-2 grid gap-2">
                  {detail.subtasks.map((task, idx) => (
                    <li key={`${task.id}:${idx}`} className="rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel-card)] px-3 py-2">
                      <div className="flex flex-wrap items-center gap-2 text-sm text-white"><span className="hc-mono text-[0.7rem] hc-dim">{idx + 1}</span><strong>{task.title || task.id}</strong></div>
                      <p className="mt-1 hc-type-label hc-dim">{task.id} · {task.lane || "ohne Lane"}{task.deps.length ? ` · deps: ${task.deps.join(", ")}` : ""}</p>
                    </li>
                  ))}
                </ol>
              </section>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
  // SSR-/Static-Render-sicher: ohne DOM (renderToStaticMarkup im Test) inline
  // rendern; im Browser an document.body portalen, damit das z-50 über FAB/Glocke
  // und allem View-Chrome liegt (sonst im Stacking-Context von FlowView gefangen).
  if (typeof document === "undefined") return content;
  // data-control (display:contents): außerhalb des [data-control]-Scopes wären
  // die --hc-*-Tokens unaufgelöst — gleiche Technik wie Overlay/FlowCapture.
  return createPortal(<div data-control className="contents">{content}</div>, document.body);
}
