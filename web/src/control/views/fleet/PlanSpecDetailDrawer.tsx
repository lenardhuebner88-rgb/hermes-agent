import { Copy, FileText, TriangleAlert } from "lucide-react";
import { useState, type KeyboardEvent, type ReactNode } from "react";
import type { PlanSpecDetailResponse } from "../../lib/schemas";
import type { PlanSpecRecord } from "../../lib/types";
import { Eyebrow, SkeletonCard } from "../../components/primitives";
import { DrawerShell, SignalChip } from "../../components/leitstand";
import {
  PLAN_ACTION_STATE_TONE,
  planSpecActionState,
  planSpecKanbanLabel,
  planSpecFindingIsStatusEcho,
  planSpecIsClosed,
  planSpecVisibleFindingCount,
} from "./planSpecKanban";

function middleEllipsis(value: string, edge = 32): string {
  if (value.length <= edge * 2 + 3) return value;
  return `${value.slice(0, edge)}…${value.slice(-edge)}`;
}

function copyText(value: string): void {
  if (typeof navigator === "undefined" || !navigator.clipboard) return;
  void navigator.clipboard.writeText(value).catch(() => undefined);
}

function planIsOnBoard(item: PlanSpecRecord): boolean {
  if (!item.kanban_root_task_id) return false;
  if (item.action_state) {
    return item.action_state === "handed_off"
      || item.action_state === "running"
      || item.action_state === "blocked";
  }
  if (item.kanban_state === "running" || item.kanban_state === "blocked") return true;
  if (item.kanban_state !== "queued") return false;
  return !(item.freigabe === "operator" && item.kanban_root_status === "scheduled");
}

const NEXT_ACTION_LABELS: Record<string, string> = {
  edit: "Plan überarbeiten",
  review_findings: "Befunde klären",
  preview_ingest: "Übergabe vorprüfen",
  release: "Kette freigeben",
  open_task: "Root-Task öffnen",
  open_result: "Abschluss prüfen",
  none: "Keine Aktion",
};

export function PlanSpecDetailDrawer({ item, detail, loading, error, stale = false, onClose, actions, onOpenChain }: {
  item: PlanSpecRecord;
  detail: PlanSpecDetailResponse | null;
  loading: boolean;
  error: string | null;
  stale?: boolean;
  onClose: () => void;
  actions?: ReactNode;
  onOpenChain?: (rootId: string) => void;
}) {
  return (
    <DrawerShell
      eyebrow="PlanSpec"
      title={item.topic || item.filename}
      icon={FileText}
      onClose={onClose}
      ariaLabel="PlanSpec Details"
      closeLabel="PlanSpec schließen"
      widthClassName="tab:w-[min(620px,calc(100vw-2rem))]"
      headerExtra={(
        <div className="mt-2 grid min-w-0 gap-2">
          <div className="flex flex-wrap items-center gap-2">
            {/* Ton = derselbe wie in der Listenzeile (I4); das Label bleibt
                das präzisere Dispositionswort (ausgeliefert/obsolet/…). */}
            <SignalChip tone={PLAN_ACTION_STATE_TONE[planSpecActionState(item)]} label={planSpecKanbanLabel(item)} />
            <span className="font-display text-micro uppercase tracking-[0.08em] text-ink-2">
              Freigabe: {detail?.freigabe || item.freigabe || "keine"}
            </span>
            {/* Die Live-Test-Tiefe stand nur in der Desktop-Variante; unter lg
                fehlte damit ein entscheidungsrelevanter Wert im Kopf. */}
            <span className="font-display text-micro uppercase tracking-[0.08em] text-ink-2">
              Live-Test: {detail?.live_test_depth || item.live_test_depth || "smoke"}
            </span>
            {item.kanban_root_task_id ? (
              <span className="font-data text-micro text-ink-3">Root {middleEllipsis(item.kanban_root_task_id, 10)}</span>
            ) : null}
          </div>
          <div className="flex min-w-0 items-center gap-1.5">
            <code title={item.path} className="min-w-0 flex-1 truncate font-data text-micro text-ink-3">{item.path}</code>
            <button
              type="button"
              onClick={() => copyText(item.path)}
              className="flex min-h-10 min-w-10 shrink-0 items-center justify-center rounded-card border border-line text-ink-2 hover:bg-surface-1 hover:text-ink"
              aria-label="PlanSpec-Pfad kopieren"
            >
              <Copy className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        </div>
      )}
    >
      <PlanSpecDetailContent
        item={item}
        detail={detail}
        loading={loading}
        error={error}
        stale={stale}
        actions={actions}
        onOpenChain={onOpenChain}
        showHeader={false}
      />
    </DrawerShell>
  );
}

export function PlanSpecDetailContent({ item, detail, loading, error, stale = false, actions, onOpenChain, showHeader = true }: {
  item: PlanSpecRecord;
  detail: PlanSpecDetailResponse | null;
  loading: boolean;
  error: string | null;
  stale?: boolean;
  actions?: ReactNode;
  onOpenChain?: (rootId: string) => void;
  showHeader?: boolean;
}) {
  const [tab, setTab] = useState<"target" | "steps" | "check">("target");
  const displayPath = middleEllipsis(item.path);
  // Kriterien pro Schritt, flach für den Überblick — mit Schritt-ID, damit
  // sichtbar bleibt, wogegen abgenommen wird.
  const sliceCriteria = (detail?.subtasks ?? []).flatMap((task) =>
    (task.acceptance_criteria ?? []).map((statement) => ({
      stepId: task.id || task.title,
      statement: String(statement),
    })),
  );
  // `acceptance_criteria_total` fehlt bei einem älteren Server (Deploy-Skew).
  // Dann zählen wir, was tatsächlich ankam, statt eine 0 zu behaupten.
  const criteriaCount = detail
    ? detail.acceptance_criteria_total ?? detail.acceptance_criteria.length + sliceCriteria.length
    : 0;
  // Prüfbefunde ohne das Abschluss-Echo („closed status: …") — das ist die
  // Disposition, kein Befund, und steht schon im Zustandslabel.
  const visibleFindings = [
    ...(detail?.source_findings ?? []), ...item.errors, ...item.ingest_findings,
  ]
    .filter((finding, index, all) => all.indexOf(finding) === index)
    .filter((finding) => !planSpecFindingIsStatusEcho(finding));
  // Geschlossene Pläne haben keinen nächsten Schritt im Dashboard — die
  // Zeile „Abschluss prüfen" führte in eine Sackgasse (keine Aktion dahinter).
  const closed = planSpecIsClosed(item);
  const showNextAction = !closed
    || Boolean(item.next_action && !["none", "open_result"].includes(item.next_action));
  const tabs = [
    ["target", "Überblick"],
    ["steps", `Ablauf ${detail?.subtasks.length ?? 0}`],
    ["check", "Übergabe"],
  ] as const;

  function moveTab(event: KeyboardEvent<HTMLButtonElement>, current: typeof tab) {
    const index = tabs.findIndex(([id]) => id === current);
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? tabs.length - 1
        : event.key === "ArrowRight"
          ? (index + 1) % tabs.length
          : event.key === "ArrowLeft"
            ? (index - 1 + tabs.length) % tabs.length
            : -1;
    if (nextIndex < 0) return;
    event.preventDefault();
    const next = tabs[nextIndex][0];
    setTab(next);
    document.getElementById(`fleet-plan-tab-${next}`)?.focus();
  }

  return (
    <div className="fleet-plan-detail-scope min-w-0">
      {showHeader ? <div className="mb-4 min-w-0">
        <Eyebrow>PlanSpec</Eyebrow>
        <h3 className="fleet-plan-detail-title mt-1 break-words font-display text-emph font-semibold text-ink" title={item.topic}>{item.topic}</h3>
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
      </div> : null}
      {showHeader ? <div className="flex flex-wrap gap-1.5">
        <SignalChip tone={PLAN_ACTION_STATE_TONE[planSpecActionState(item)]} label={planSpecKanbanLabel(item)} />
        <span className="px-1 py-0.5 font-display text-micro uppercase tracking-[0.08em] text-ink-2">Freigabe: {detail?.freigabe || item.freigabe || "keine"}</span>
        <span className="px-1 py-0.5 font-display text-micro uppercase tracking-[0.08em] text-ink-2">Live-Test: {detail?.live_test_depth || item.live_test_depth || "smoke"}</span>
        {item.kanban_root_task_id ? (
          <span className="px-1 py-0.5 font-data text-micro text-ink-3">Root {item.kanban_root_task_id}</span>
        ) : null}
      </div> : null}
      <section className="fleet-plan-decision" aria-label="Operative Lage">
        <div>
          <span>Zustand</span>
          <strong>{planSpecKanbanLabel(item)}</strong>
        </div>
        <div>
          <span>Warum</span>
          <strong>{item.action_reason || "Keine zusätzliche Einordnung vorhanden."}</strong>
        </div>
        {showNextAction ? (
          <div>
            <span>Nächster Schritt</span>
            <strong>{NEXT_ACTION_LABELS[item.next_action ?? "none"] ?? item.next_action ?? "Keine Aktion"}</strong>
          </div>
        ) : null}
      </section>
      {planIsOnBoard(item) && onOpenChain ? (
        <button
          type="button"
          className="mt-3 inline-flex min-h-10 items-center rounded-card border border-line bg-surface-2 px-3 font-display text-micro font-semibold text-bronze hover:bg-surface-1 hover:text-bronze-hi focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-bronze"
          onClick={() => onOpenChain(item.kanban_root_task_id as string)}
        >
          In Ketten anzeigen
        </button>
      ) : null}
      {loading ? <SkeletonCard rows={4} /> : null}
      {error ? <div role="alert" className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{error}</div> : null}
      {stale && detail ? <p className="fleet-plan-hint" role="status">Letzter erfolgreicher Stand · die aktuelle Abfrage ist fehlgeschlagen.</p> : null}
      {!detail && !loading && !error ? <p className="fleet-plan-hint">Für diese Quelle liegen keine lesbaren Details vor.</p> : null}
      {detail ? (
        <div className="mt-4">
          <div className="fleet-detail-tabs" role="tablist" aria-label="PlanSpec-Detailbereiche">
            {tabs.map(([id, label]) => (
              <button
                key={id}
                id={`fleet-plan-tab-${id}`}
                type="button"
                role="tab"
                aria-selected={tab === id}
                aria-controls={`fleet-plan-panel-${id}`}
                tabIndex={tab === id ? 0 : -1}
                className={`fleet-detail-tab${tab === id ? " fleet-detail-tab-on" : ""}`}
                onClick={() => setTab(id)}
                onKeyDown={(event) => moveTab(event, id)}
              >
                {label}
              </button>
            ))}
          </div>

          {tab === "target" ? <div id="fleet-plan-panel-target" role="tabpanel" aria-labelledby="fleet-plan-tab-target" className="mt-4 grid gap-4">
            {visibleFindings.length ? (
              <section className="fleet-plan-findings-panel">
                <Eyebrow>Prüfbefunde</Eyebrow>
                <ul>
                  {visibleFindings.map((finding) => <li key={finding}>{finding}</li>)}
                </ul>
              </section>
            ) : null}
            <section className="fleet-detail-instrument">
              <Eyebrow>Ziel</Eyebrow>
              <p className="mt-2 whitespace-pre-wrap text-sec leading-relaxed text-ink-2">{detail.goal || item.topic}</p>
            </section>
            <section className="fleet-detail-instrument">
              <Eyebrow>Acceptance Criteria</Eyebrow>
              <ol className="fleet-plan-criteria">
                {detail.acceptance_criteria.map((ac, idx) => (
                  <li key={`plan:${ac.id ?? idx}`} className="text-sec text-ink-2">
                    <span>{ac.id ? String(ac.id) : String(idx + 1).padStart(2, "0")}</span>
                    <p className="whitespace-pre-wrap break-words leading-relaxed">{String(ac.statement ?? "")}</p>
                  </li>
                ))}
                {/* Canon planspec-taskgraph.md stellt Kriterien PRO SLICE; plan-weite
                    Einträge sind der Fallback. Nur die plan-weite Liste zu rendern
                    meldete „Keine Kriterien im Plan" auf Plänen mit 23 bzw. 5
                    authored Kriterien (gemessen 2026-07-30). */}
                {sliceCriteria.map(({ stepId, statement }, idx) => (
                  <li key={`slice:${stepId}:${idx}`} className="text-sec text-ink-2">
                    <span>{stepId}</span>
                    <p className="whitespace-pre-wrap break-words leading-relaxed">{statement}</p>
                  </li>
                ))}
                {criteriaCount === 0 ? (
                  <li className="text-sec text-ink-2">Keine Kriterien im Plan.</li>
                ) : null}
              </ol>
            </section>
            {detail.anti_scope.length ? (
              <details className="fleet-plan-disclosure">
                <summary>Nicht im Scope · {detail.anti_scope.length}</summary>
                <ul>{detail.anti_scope.map((x) => <li key={x}>{x}</li>)}</ul>
              </details>
            ) : null}
            {(detail.evidence_required ?? []).length ? (
              <details className="fleet-plan-disclosure">
                <summary>Erforderliche Evidenz · {(detail.evidence_required ?? []).length}</summary>
                <ul>{(detail.evidence_required ?? []).map((x) => <li key={x}>{x}</li>)}</ul>
              </details>
            ) : null}
            {detail.full_text ? (
              <details className="fleet-plan-disclosure">
                <summary>Quelltext anzeigen</summary>
                <div className="mt-2 whitespace-pre-wrap break-words text-sec leading-relaxed text-ink-2">{detail.full_text}</div>
              </details>
            ) : null}
          </div> : null}

          {tab === "steps" ? <section id="fleet-plan-panel-steps" role="tabpanel" aria-labelledby="fleet-plan-tab-steps" className="mt-4 fleet-detail-instrument">
            <Eyebrow>Subtask-Kette</Eyebrow>
            <ol className="fleet-plan-step-rail">
              {detail.subtasks.length ? detail.subtasks.map((task, idx) => (
                <li key={`${task.id}:${idx}`}>
                  <span className="fleet-plan-step-index">{String(idx + 1).padStart(2, "0")}</span>
                  <div>
                    <strong>{task.title || task.id}</strong>
                    <p>{task.id} · {task.lane || "ohne Lane"}{task.review_tier ? ` · Review ${task.review_tier}` : ""}{task.kind ? ` · ${task.kind}` : ""}</p>
                    {task.deps.length ? <p>Vorgänger: {task.deps.join(", ")}</p> : null}
                    {(task.body || task.instructions || task.scope_files?.length || task.acceptance_criteria?.length) ? (
                      <details className="fleet-plan-step-detail">
                        <summary>Arbeitsvertrag</summary>
                        {/* Die Kriterien öffneten diesen Block schon, wurden aber nie
                            gerendert — der Arbeitsvertrag zeigte Prosa und Pfade und
                            verschwieg genau das, wogegen abgenommen wird. */}
                        {task.acceptance_criteria?.length ? (
                          <ol className="fleet-plan-step-criteria">
                            {task.acceptance_criteria.map((statement, acIdx) => (
                              <li key={`${task.id}:ac:${acIdx}`} className="whitespace-pre-wrap">{String(statement)}</li>
                            ))}
                          </ol>
                        ) : null}
                        {task.body ? <p className="whitespace-pre-wrap">{task.body}</p> : null}
                        {task.instructions ? <p className="whitespace-pre-wrap">{task.instructions}</p> : null}
                        {task.scope_files?.length ? <p className="font-data text-micro">{task.scope_files.join(" · ")}</p> : null}
                      </details>
                    ) : null}
                  </div>
                </li>
              )) : <li className="text-sec text-ink-2">Keine ausführbaren Schritte vorhanden.</li>}
            </ol>
          </section> : null}

          {tab === "check" ? <div id="fleet-plan-panel-check" role="tabpanel" aria-labelledby="fleet-plan-tab-check" className="mt-4 grid gap-4">
            {/* Für geschlossene Pläne ist die Übergabe-Matrix irreführend —
                nichts davon landet noch auf einem Board. Stattdessen beantwortet
                der Tab hier die eigentliche Frage: wie ist es ausgegangen? */}
            {closed ? (
              (item.receipt || item.release_evidence || item.closed_by) ? (
                <section className="fleet-detail-instrument">
                  <Eyebrow>Abschlussbeleg</Eyebrow>
                  {item.closed_by ? <p className="mt-2 text-sec text-ink-2">Geschlossen von {item.closed_by}</p> : null}
                  {item.receipt ? <p className="mt-1 wrap-anywhere font-data text-micro text-ink-3">{item.receipt}</p> : null}
                  {item.release_evidence ? <p className="mt-1 wrap-anywhere text-sec text-ink-2">{item.release_evidence}</p> : null}
                </section>
              ) : null
            ) : (
              <dl className="fleet-plan-decision">
                <div><dt>Auswirkung</dt><dd>{detail.subtasks.length} Schritte auf {detail.target_board || item.target_board || "dem aktiven Board"}</dd></div>
                <div><dt>Schutz</dt><dd>{detail.freigabe === "operator" ? "Start erfordert Operator-Freigabe" : "Freigabe laut PlanSpec"}</dd></div>
                <div><dt>Prüfung</dt><dd>{planSpecVisibleFindingCount(item)} Befunde · Live-Test {detail.live_test_depth || "smoke"}</dd></div>
              </dl>
            )}
            {actions}
          </div> : null}
        </div>
      ) : null}
    </div>
  );
}
