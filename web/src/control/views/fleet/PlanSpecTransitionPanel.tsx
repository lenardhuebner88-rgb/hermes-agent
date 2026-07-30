import { useEffect, useRef, useState } from "react";

import { fetchJSON } from "@/lib/api";
import { previewPlanSpecTransition } from "../../hooks/planSpecsLanes";
import { de } from "../../i18n/de";
import { PlanSpecDetailResponseSchema, parseOrThrow } from "../../lib/schemas";
import type { PlanSpecDetailSubtask, PlanSpecTransitionPreview } from "../../lib/schemas";
import { planSpecHasParkedSignedChain } from "../../lib/fleetHub";
import type { PlanSpecRecord } from "./shared";

export function PlanSpecTransitionPanel({
  item,
  boardSlug,
  readOnly,
  onChanged,
  onOpenTask,
}: {
  item: PlanSpecRecord;
  boardSlug: string | null;
  readOnly: boolean;
  onChanged: () => void | Promise<void>;
  onOpenTask?: (taskId: string) => void;
}) {
  const [preview, setPreview] = useState<PlanSpecTransitionPreview | null>(null);
  const [subtasks, setSubtasks] = useState<PlanSpecDetailSubtask[]>([]);
  const [busy, setBusy] = useState<"idle" | "preview" | "ingest" | "release">("idle");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [armed, setArmed] = useState(false);
  const requestRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const rootId = item.kanban_root_task_id;
  const parkedSigned = planSpecHasParkedSignedChain(item);
  const canPreview = !rootId
    && item.valid
    && (!item.next_action || item.next_action === "preview_ingest");
  const canRelease = Boolean(
    rootId
      && item.kanban_root_status === "scheduled"
      && (!item.next_action || item.next_action === "release"),
  );

  useEffect(() => {
    requestRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    return () => abortRef.current?.abort();
  }, [item.path, boardSlug]);

  async function loadPreview() {
    const request = ++requestRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy("preview");
    setError(null);
    setDone(null);
    try {
      const [nextPreview, detail] = await Promise.all([
        previewPlanSpecTransition(item.path, boardSlug, controller.signal),
        fetchJSON<unknown>(
          `/api/plugins/kanban/planspecs/detail?path=${encodeURIComponent(item.path)}`,
          { signal: controller.signal },
        )
          .then((raw) => parseOrThrow(PlanSpecDetailResponseSchema, raw, "planspecs/detail"))
          .catch(() => null),
      ]);
      if (request !== requestRef.current) return;
      setPreview(nextPreview);
      setSubtasks(detail?.subtasks ?? []);
    } catch (cause) {
      if (controller.signal.aborted || request !== requestRef.current) return;
      setPreview(null);
      setSubtasks([]);
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (request === requestRef.current) setBusy("idle");
    }
  }

  async function ingest() {
    if (!preview?.can_ingest) return;
    setBusy("ingest");
    setError(null);
    try {
      const query = boardSlug ? `?board=${encodeURIComponent(boardSlug)}` : "";
      await fetchJSON(`/api/plugins/kanban/planspecs/ingest${query}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: item.path, source_digest: preview.source_digest }),
      });
      setDone("Als gehaltene Kette ans Board übergeben.");
      setPreview(null);
      setSubtasks([]);
      await onChanged();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      if (message.includes("geändert") || message.includes("source_changed")) {
        setPreview(null);
        setSubtasks([]);
        setError("Plan wurde geändert · bitte erneut prüfen.");
      } else {
        setError(message);
      }
    } finally {
      setBusy("idle");
    }
  }

  async function release() {
    if (!rootId) return;
    if (parkedSigned && !armed) {
      setArmed(true);
      return;
    }
    setBusy("release");
    setError(null);
    try {
      const query = boardSlug ? `?board=${encodeURIComponent(boardSlug)}` : "";
      if (parkedSigned) {
        await fetchJSON(`/api/plugins/kanban/tasks/${encodeURIComponent(rootId)}/flow-release${query}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ release_level: "live" }),
        });
      } else {
        await fetchJSON(`/api/plugins/kanban/planspecs/approve${query}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ root_task_id: rootId }),
        });
      }
      setDone(parkedSigned ? "Kette gestartet." : "Kette freigegeben.");
      setArmed(false);
      await onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy("idle");
    }
  }

  if (readOnly) {
    return <p className="fleet-plan-hint">Fremd-Board · Übergabe und Freigabe sind deaktiviert.</p>;
  }

  return (
    <section className="fleet-plan-transition" aria-label="Plan-Übergabe">
      <div className="fleet-plan-transition-actions">
        {canPreview ? (
          <button type="button" onClick={() => void loadPreview()} disabled={busy !== "idle"}>
            {busy === "preview" ? "Prüft …" : preview ? "Erneut prüfen" : "Übergabe prüfen"}
          </button>
        ) : null}
        {canRelease ? (
          <button type="button" onClick={() => void release()} disabled={busy !== "idle"}>
            {busy === "release"
              ? "Freigabe läuft …"
              : parkedSigned && armed
                ? de.fleet.planKetteStartenConfirm
                : parkedSigned ? de.fleet.planKetteStarten : "Kette freigeben"}
          </button>
        ) : null}
        {rootId && onOpenTask ? (
          <button type="button" className="is-secondary" onClick={() => onOpenTask(rootId)}>
            Root öffnen
          </button>
        ) : null}
      </div>

      {preview ? (
        <div className="fleet-plan-preview" aria-live="polite">
          <div className="fleet-plan-preview-effect">
            <strong>{preview.root_cards_created} Root · {preview.child_tasks_created} Tasks auf {preview.target_board}</strong>
            <span>{preview.starts_worker ? "Worker startet unmittelbar." : "Kein Worker startet ohne separate Freigabe."}</span>
          </div>
          {/* Die primäre Aktion steht direkt hinter der Wirkungszeile. Am Ende
              der Vorschau lag sie nach „Übergabe prüfen" gemessen bei y=1021
              (Viewport 1000) und war per Pointer nicht erreichbar — Klick ohne
              Wirkung und ohne Rückmeldung. Details folgen darunter. */}
          <button
            type="button"
            className="fleet-plan-ingest"
            onClick={() => void ingest()}
            disabled={!preview.can_ingest || busy !== "idle"}
          >
            {busy === "ingest" ? "Übergibt …" : "Als gehaltene Kette übergeben"}
          </button>
          {!preview.can_ingest ? (
            <p className="fleet-plan-hint">
              {preview.blocking_findings.length
                ? `Übergabe gesperrt · ${preview.blocking_findings.length} Blocker unten.`
                : preview.lanes_resolvable
                  ? "Übergabe gesperrt · die Vorprüfung lässt diesen Plan nicht zu."
                  : "Übergabe gesperrt · mindestens eine Lane ist nicht auflösbar."}
            </p>
          ) : null}
          {subtasks.length ? (
            <ul className="fleet-plan-preview-tasks">
              {subtasks.slice(0, 10).map((task, idx) => (
                <li key={`${task.id}:${idx}`}>
                  {task.title || task.id} · {task.lane || "ohne Lane"}
                  {task.review_tier ? ` · Review ${task.review_tier}` : ""}
                </li>
              ))}
              {subtasks.length > 10 ? <li>… und {subtasks.length - 10} weitere</li> : null}
            </ul>
          ) : null}
          <dl className="fleet-plan-preview-guards">
            <div><dt>Lanes</dt><dd>{preview.lanes_resolvable ? "auflösbar" : "nicht auflösbar"}</dd></div>
            <div><dt>Prüfung</dt><dd>{preview.blocking_findings.length ? `${preview.blocking_findings.length} Blocker` : preview.warnings.length ? `${preview.warnings.length} Hinweis(e)` : "sauber"}</dd></div>
            <div><dt>Freigabe</dt><dd>gehalten</dd></div>
          </dl>
          {preview.blocking_findings.length ? (
            <ul className="fleet-plan-findings">
              {preview.blocking_findings.map((finding) => <li key={finding}>{finding}</li>)}
            </ul>
          ) : null}
          {preview.warnings.length ? (
            <ul className="fleet-plan-warnings">
              {preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}
            </ul>
          ) : null}
          <details className="fleet-plan-preview-tech">
            <summary>Technische Parameter</summary>
            <dl>
              <div><dt>Workspace</dt><dd>{preview.workspace_mode}</dd></div>
              <div><dt>Live-Test</dt><dd>{preview.live_test_depth}</dd></div>
              <div><dt>Review-Floor</dt><dd>{preview.review_floor}</dd></div>
              <div><dt>Auto-Scouts</dt><dd>{preview.auto_scout_tasks}</dd></div>
            </dl>
          </details>
          <p className="fleet-plan-hint">Deterministische Vorprüfung. Live-Konflikte und kritische Prüfungen werden beim Übergang erneut validiert.</p>
        </div>
      ) : null}
      {!canPreview && !canRelease && !rootId ? (
        <p className="fleet-plan-hint">{item.action_reason || "Für diesen Plan ist keine Übergabeaktion verfügbar."}</p>
      ) : null}
      {error ? <p role="alert" className="fleet-plan-msg fleet-plan-msg-error">{error}</p> : null}
      {done ? <p role="status" className="fleet-plan-msg fleet-plan-msg-success">{done}</p> : null}
    </section>
  );
}
