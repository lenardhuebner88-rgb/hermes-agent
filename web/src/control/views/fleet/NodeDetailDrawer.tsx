/**
 * Karten-Detail-Inhalt + mobiler Drawer mit Tabs (Übersicht/Aktivität/Log/Ergebnis).
 *
 * Aus FleetView.tsx extrahiert — reine Zerlegung, kein Verhalten geändert.
 */
import { useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import {
  fmtSeconds,
  fmtTokens,
  fmtUsd,
  chainTotalCostUsdWithSource,
  type CostDisplayValue,
  profileInitial,
  profileColorClass,
  premiumLaneMarker,
} from "../../lib/fleetHub";
import { de } from "../../i18n/de";
import { runStatusLabel } from "../../lib/tones";
import { useWorkerActivity, useHermesReviewVerdicts, useTaskBodyOnDemand, useTaskDeliverablesOnDemand, useLanesCatalog, extractDetail } from "../../hooks/useControlData";
import { DrawerShell } from "../../components/leitstand";
import { WorkerLogTail } from "../../components/WorkerCard";
import { Eyebrow } from "../../components/primitives";
import { fetchJSON, openAuthedApiFile } from "@/lib/api";
import { fmtUsdDisplay, type ChainNode } from "./shared";
import { elapsedSeconds } from "../../lib/derive";
import { FleetTaskActions } from "./TaskActions";
import { AnswerQuestion } from "./AnswerQuestion";
import { isOperatorQuestion } from "../../lib/fleet";
import { TaskReassignResponseSchema, parseOrThrow } from "../../lib/schemas";
import { FleetSourceFreshness } from "./FleetSourceFreshness";

// ─── Karten-Detail-Drawer ─────────────────────────────────────────────────────

type DetailTab = "uebersicht" | "aktivitaet" | "log" | "ergebnis";

interface NodeDetailDrawerProps {
  taskId: string;
  /** Nodes der aktuellen Kette — zur Berechnung der Ketten-Gesamtkosten im Ergebnis-Tab. */
  chainNodes: ChainNode[];
  now: number;
  onClose: () => void;
  /** Board nach einer Steuerungs-Aktion (Unblock/Retry/Cancel) neu laden. */
  onChanged?: () => void | Promise<void>;
}

export function NodeDetailDrawer({ taskId, chainNodes, now, onClose, onChanged }: NodeDetailDrawerProps) {
  return (
    <DrawerShell
      eyebrow="Fleet"
      title={`Task ${taskId}`}
      onClose={onClose}
      ariaLabel={`Task ${taskId} Details`}
      closeLabel={de.fleet.detailSchliessen}
      widthClassName="tab:w-[min(560px,calc(100vw-2rem))]"
    >
      <NodeDetailContent
        taskId={taskId}
        chainNodes={chainNodes}
        now={now}
        onClose={onClose}
        onChanged={onChanged}
      />
    </DrawerShell>
  );
}

export function NodeDetailContent({ taskId, chainNodes, now, onClose, onChanged }: NodeDetailDrawerProps) {
  const [tab, setTab] = useState<DetailTab>("uebersicht");
  const [copied, setCopied] = useState(false);

  // On-Demand-Daten (nur bei offenem Drawer)
  const taskBody = useTaskBodyOnDemand(taskId);
  const deliverablesResult = useTaskDeliverablesOnDemand(taskId);
  const activity = useWorkerActivity(taskId);
  const verdicts = useHermesReviewVerdicts();

  const task = taskBody.data?.task ?? null;
  const runs = taskBody.data?.runs ?? [];
  const links = taskBody.data?.links;
  const stageBlockReason = (() => {
    if (task?.status !== "todo" && task?.status !== "scheduled") return null;
    const parentIds = links?.parents ?? [];
    const states = new Map((links?.parent_states ?? []).map((parent) => [parent.id, parent]));
    const blockers = parentIds.flatMap((parentId) => {
      const parent = states.get(parentId);
      if (!parent) return [`${parentId} (Status unbekannt)`];
      return parent.status === "done" ? [] : [`${parent.title} (${parent.status}) ist nicht fertig`];
    });
    return blockers.length > 0
      ? `Starten nicht verfügbar — Vorgänger ${blockers.join(", ")}.`
      : null;
  })();
  // Ketten-Root für "Kette abbrechen": min-level Node, nur bei echter Mehr-Node-Kette.
  const chainRootId = chainNodes.length > 1
    ? ([...chainNodes].sort((a, b) => a.level - b.level)[0]?.id ?? null)
    : null;
  // Deliverables: eigener Endpoint /tasks/{id}/deliverables — degradiert sauber zu []
  const deliverables = deliverablesResult.data?.deliverables ?? [];
  const events = activity.data?.events ?? [];

  // Review-Verdict für diesen Task
  const taskVerdicts = (verdicts.data?.reviews ?? []).filter((r) => r.task_id === taskId).map((v) => ({
    task_id: v.task_id,
    reviewer_profile: v.reviewer_profile,
    review_run_state: v.review_run_state ?? "pending",
    verifier_verdict: v.verifier_verdict ?? null,
  }));

  // GET /tasks/:id intentionally returns attempt history oldest-first
  // (kanban_db.list_runs start order). The drawer needs the newest attempt,
  // so selecting index 0 silently showed stale blocked/review state after a
  // successful retry.
  const latestRun = runs.length > 0 ? runs[runs.length - 1] : null;
  const elapsedSec = latestRun?.runtime_seconds ?? (
    latestRun?.started_at != null
      ? (elapsedSeconds(latestRun.started_at, now) ?? Number.NaN)
      : null
  );

  function handleCopy() {
    void navigator.clipboard.writeText(taskId).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  }

  const TABS: Array<{ id: DetailTab; label: string }> = [
    { id: "uebersicht", label: de.fleet.detailTabUebersicht },
    { id: "aktivitaet", label: de.fleet.detailTabAktivitaet },
    { id: "log", label: de.fleet.detailTabLog },
    { id: "ergebnis", label: de.fleet.detailTabErgebnis },
  ];

  return (
      <div data-fleet-theme className="fleet-drawer-inner">
        {/* Kopf */}
        <div className="fleet-dr-head">
          <div
            className={`fleet-avatar fleet-avatar-gross ${profileColorClass(task?.assignee ?? "")}`}
            {...premiumLaneMarker(task?.assignee)}
          >
            {profileInitial(task?.assignee ?? "?")}
          </div>
          <div className="fleet-dr-title">
            <span className="text-sec">{task?.title || taskId}</span>
            <span>
              <button
                type="button"
                className="fleet-copy-id"
                onClick={handleCopy}
                title={de.fleet.detailKopieren}
                aria-label={de.fleet.detailKopieren}
              >
                {taskId}
                <span className="text-[9px] opacity-70">{copied ? " ✓" : " ⊕"}</span>
              </button>
            </span>
          </div>
        </div>

        <FleetSourceFreshness sources={[{
          label: "Task-Detail",
          error: taskBody.error,
          errorObj: taskBody.errorObj,
          isStale: taskBody.isStale,
          lastUpdated: taskBody.lastUpdated,
        }]} />

        {/* Tab-Leiste */}
        <div className="fleet-detail-tabs">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={`fleet-detail-tab${tab === t.id ? " fleet-detail-tab-on" : ""}`}
              onClick={() => setTab(t.id)}
              aria-pressed={tab === t.id}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Tab-Inhalte */}
        <div className="fleet-detail-body">
          {tab === "uebersicht" && (
            <UebersichtTab
              task={task}
              latestRun={latestRun}
              elapsedSec={elapsedSec}
              deliverables={deliverables}
            />
          )}
          {tab === "aktivitaet" && (
            <AktivitaetTab events={events} now={now} loading={activity.loading && !activity.data} />
          )}
          {tab === "log" && (
            <LogTab taskId={taskId} />
          )}
          {tab === "ergebnis" && (
            <ErgebnisTab
              verdicts={taskVerdicts}
              deliverables={deliverables}
              chainCost={chainTotalCostUsdWithSource(chainNodes)}
            />
          )}
        </div>

        {/* Steuerung — Unblock/Retry/Cancel Task/Cancel Kette (S3, tab-übergreifend) */}
        {task ? (
          <div className="fleet-dr-actions">
            <TaskReassignControl
              taskId={taskId}
              status={task.status ?? ""}
              currentProfile={task.assignee ?? null}
              onChanged={onChanged}
            />
            <FleetTaskActions
              taskId={taskId}
              status={task.status ?? ""}
              chainRootId={chainRootId}
              onChanged={onChanged}
              onCancelled={onClose}
              stageBlockReason={stageBlockReason}
            />
          </div>
        ) : null}
      </div>
  );
}

// The backend refuses a running reassign unless the caller explicitly opts
// into reclaim_first. This control deliberately never reclaims a live worker,
// so exposing it for running would be a deterministic 409 action.
const NON_REASSIGNABLE_STATUSES = new Set(["running", "done", "archived"]);

function TaskReassignControl({
  taskId,
  status,
  currentProfile,
  onChanged,
}: {
  taskId: string;
  status: string;
  currentProfile: string | null;
  onChanged?: () => void | Promise<void>;
}) {
  const lanesCatalog = useLanesCatalog();
  const [targetProfile, setTargetProfile] = useState("");
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const profiles = useMemo(() => (
    (lanesCatalog.data?.profiles ?? [])
      .map((profile) => profile.name.trim())
      .filter((name) => name.length > 0)
  ), [lanesCatalog.data?.profiles]);

  const normalizedStatus = status.trim().toLowerCase();
  const normalizedCurrentProfile = currentProfile?.trim() ?? "";
  const defaultProfile = profiles.includes(normalizedCurrentProfile)
    ? normalizedCurrentProfile
    : profiles[0] ?? "";
  const selectedProfile = profiles.includes(targetProfile) ? targetProfile : defaultProfile;

  if (NON_REASSIGNABLE_STATUSES.has(normalizedStatus) || profiles.length === 0) return null;

  const changedProfile = selectedProfile && selectedProfile !== normalizedCurrentProfile;
  const disabled = busy || !changedProfile;

  async function fire() {
    if (!selectedProfile) return;
    setBusy(true);
    setError("");
    setNote("");
    setArmed(false);
    try {
      const raw = await fetchJSON<unknown>(
        `/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}/reassign`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            profile: selectedProfile,
            reclaim_first: false,
            reason: "Fleet Cockpit: Profil geändert",
          }),
        },
      );
      const result = parseOrThrow(TaskReassignResponseSchema, raw, "Task-Reassign");
      setNote(de.fleet.reassignDone(result.assignee ?? "—"));
      await onChanged?.();
    } catch (e) {
      setError(extractDetail(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fleet-task-actions" aria-label={de.fleet.reassignTitle}>
      {armed ? (
        <div className="fleet-ta-confirm">
          <span className="fleet-ta-confirm-text">
            {de.fleet.reassignConfirm(selectedProfile)}
          </span>
          <button
            type="button"
            className="fleet-ta-btn"
            style={{ color: "var(--color-live)", borderColor: "color-mix(in oklab, var(--color-live) 45%, transparent)" }}
            disabled={busy}
            onClick={() => void fire()}
          >
            {busy ? de.fleet.actionBusy : de.fleet.actionConfirm}
          </button>
          <button type="button" className="fleet-ta-btn" disabled={busy} onClick={() => setArmed(false)}>
            {de.fleet.actionDismiss}
          </button>
        </div>
      ) : (
        <div className="fleet-ta-row items-center">
          <label
            htmlFor={`fleet-reassign-${taskId}`}
            className="font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3"
          >
            {de.fleet.reassignProfileLabel}
          </label>
          <select
            id={`fleet-reassign-${taskId}`}
            value={selectedProfile}
            onChange={(event) => {
              setTargetProfile(event.target.value);
              setError("");
              setNote("");
            }}
            disabled={busy || lanesCatalog.loading}
            className="min-h-8 flex-[1_1_150px] rounded-[9px] border border-line bg-surface-2 px-[9px] py-[7px] text-micro font-medium text-ink"
          >
            {profiles.map((profile) => (
              <option key={profile} value={profile}>{profile}</option>
            ))}
          </select>
          <button
            type="button"
            className="fleet-ta-btn"
            disabled={disabled}
            onClick={() => { setError(""); setNote(""); setArmed(true); }}
          >
            {de.fleet.reassignButton}
          </button>
        </div>
      )}
      {error ? <p className="fleet-ta-error">{error}</p> : null}
      {note ? <p className="fleet-ta-note">{note}</p> : null}
    </div>
  );
}

// ─── Detail-Drawer Tabs ───────────────────────────────────────────────────────

interface UebersichtTabProps {
  task: {
    id?: string;
    title?: string;
    body?: string | null;
    status?: string;
    assignee?: string | null;
    block_reason?: string | null;
    operator_question?: boolean;
    review_tier?: string | null;
    branch_name?: string | null;
    model_override?: string | null;
    workspace_kind?: string | null;
    workspace_path?: string | null;
    acceptance_criteria?: unknown;
  } | null;
  latestRun: {
    profile?: string | null;
    status?: string;
    started_at?: number | null;
    ended_at?: number | null;
    runtime_seconds?: number | null;
    input_tokens?: number | null;
    output_tokens?: number | null;
    cost_usd?: number | null;
  } | null;
  elapsedSec: number | null;
  deliverables: Array<{ filename: string; url: string; size: number }>;
}

export function UebersichtTab({ task, latestRun, elapsedSec, deliverables }: UebersichtTabProps) {
  if (!task) {
    return (
      <div className="fleet-empty" style={{ padding: "16px 4px" }}>
        <p className="fleet-empty-sub">Lade Details …</p>
      </div>
    );
  }

  // Acceptance-Criteria normalisieren
  const ac = task.acceptance_criteria;
  let acList: string[] = [];
  if (Array.isArray(ac)) {
    acList = ac.map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object" && "statement" in item) return String((item as { statement: unknown }).statement);
      return String(item);
    });
  } else if (typeof ac === "string" && ac.trim()) {
    acList = [ac];
  }

  return (
    <>
      {/* Status-Badge — LED + Label, nie farb-only (DESIGN.md Regel 2). */}
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2 py-[3px] text-micro",
            task.status === "running" ? "border-live/40 text-live"
              : task.status === "done" ? "border-status-ok/35 text-status-ok"
              : "border-line text-ink-2",
          )}
        >
          {task.status === "running" || task.status === "done" ? (
            <span
              aria-hidden
              className={cn("h-1.5 w-1.5 shrink-0 rounded-full", task.status === "running" ? "bg-live" : "bg-status-ok")}
            />
          ) : null}
          {task.status ?? "—"}
        </span>
        {task.review_tier ? (
          <span className="rounded-full border border-line px-2 py-[3px] text-micro text-ink-3">
            {task.review_tier}
          </span>
        ) : null}
      </div>

      {/* Block-Reason */}
      {task.block_reason ? (
        <div className="rounded-panel border border-status-warn/30 bg-status-warn/10 px-2.5 py-2 text-micro text-status-warn">
          {de.fleet.detailLabelBlockReason}: {task.block_reason}
        </div>
      ) : null}

      {/* Operator-Frage beantworten (S6) — nur wenn blockiert + operator_question */}
      {task.status === "blocked" && isOperatorQuestion(task.operator_question) ? (
        <AnswerQuestion taskId={task.id ?? ""} />
      ) : null}

      {/* KV-Grid */}
      <div className="fleet-grid2">
        <div className="fleet-kv">
          <div className="fleet-kv-k">{de.fleet.detailLabelAssignee}</div>
          <div className="fleet-kv-v">{task.assignee ?? "—"}</div>
        </div>
        <div className="fleet-kv">
          <div className="fleet-kv-k">{de.fleet.detailLabelModell}</div>
          <div className="fleet-kv-v" style={{ fontSize: 11 }}>{latestRun?.profile ?? task.model_override ?? "—"}</div>
        </div>
        <div className="fleet-kv">
          <div className="fleet-kv-k">{de.fleet.detailLabelBranch}</div>
          <div className="fleet-kv-v" style={{ fontSize: 11, overflow: "hidden", textOverflow: "ellipsis" }}>
            {task.branch_name ?? task.workspace_path ?? (task.workspace_kind ? `(${task.workspace_kind})` : "—")}
          </div>
        </div>
        <div className="fleet-kv">
          <div className="fleet-kv-k">{de.fleet.detailLabelLaufzeit}</div>
          <div className="fleet-kv-v">{elapsedSec != null ? fmtSeconds(elapsedSec) : "—"}</div>
        </div>
        {latestRun?.status ? (
          <div className="fleet-kv">
            <div className="fleet-kv-k">Laufstatus</div>
            <div className="fleet-kv-v" title={latestRun.status}>{runStatusLabel(latestRun.status)}</div>
          </div>
        ) : null}
        {(latestRun?.input_tokens != null || latestRun?.output_tokens != null) ? (
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.detailLabelTokens}</div>
            <div className="fleet-kv-v" style={{ fontSize: 11 }}>
              {fmtTokens(latestRun?.input_tokens)} → {fmtTokens(latestRun?.output_tokens)}
            </div>
          </div>
        ) : null}
        {latestRun?.cost_usd != null ? (
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.detailLabelKosten}</div>
            <div className="fleet-kv-v">{fmtUsd(latestRun.cost_usd)}</div>
          </div>
        ) : null}
      </div>

      {/* Task-Body */}
      {task.body ? (
        <div>
          <Eyebrow className="mb-1.5">{de.fleet.detailBodyLabel}</Eyebrow>
          <div className="max-h-40 overflow-y-auto wrap-anywhere whitespace-pre-wrap border-l-2 border-line pl-2.5 text-sec text-ink-2">
            {task.body}
          </div>
        </div>
      ) : null}

      {/* Acceptance-Criteria */}
      {acList.length > 0 ? (
        <div>
          <Eyebrow className="mb-1.5">{de.fleet.detailAcceptanceLabel}</Eyebrow>
          <ul className="m-0 flex flex-col gap-1 pl-4">
            {acList.slice(0, 8).map((item, i) => (
              <li key={i} className="text-sec text-ink-2">
                {item}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Deliverables-Liste — Auth-geschützte Endpoints: openAuthedApiFile statt raw href */}
      {deliverables.length > 0 ? (
        <div>
          <Eyebrow className="mb-1.5">{de.fleet.detailDeliverables}</Eyebrow>
          {deliverables.map((d) => (
            <button
              key={d.url || d.filename}
              type="button"
              onClick={() => void openAuthedApiFile(d.url, d.filename)}
              className="flex w-full cursor-pointer items-center gap-2 border-0 border-b border-line bg-transparent py-1.5 text-left text-sec text-live"
            >
              <span className="flex-1 truncate" title={d.filename}>
                {d.filename}
              </span>
              <span className="font-data text-micro text-ink-3">
                {d.size > 0 ? `${Math.round(d.size / 1024)} kB` : "↗"}
              </span>
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}

// Review-Economy-Kinds sollen nicht wie un-gereviewte Arbeit aussehen: klare
// Label + Ton statt des rohen Event-kind (siehe hermes_cli/auto_release.py /
// Review-Skip-Gates).
const REVIEW_ECONOMY_KIND_META: Record<string, { label: string; tone: "ok" | "info" }> = {
  review_skipped_deterministic: { label: "Gates-verifiziert (Review übersprungen)", tone: "ok" },
  review_deferred_to_tip: { label: "Urteil am Kettenende", tone: "info" },
};

export function AktivitaetTab({
  events,
  now,
  loading,
}: {
  events: Array<{ id: number; kind: string; note: string | null; at: number }>;
  now: number;
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="fleet-empty" style={{ padding: "12px 4px" }}>
        <p className="fleet-empty-sub">Lade Aktivität …</p>
      </div>
    );
  }
  if (events.length === 0) {
    return (
      <p className="px-0.5 py-2 text-sec text-ink-3">
        {de.fleet.detailActivityEmpty}
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-0.5">
      {events.slice(0, 20).map((ev) => {
        const age = elapsedSeconds(ev.at, now);
        const ageLabel = age != null ? fmtSeconds(age) : "Zeit ungültig";
        const kindMeta = REVIEW_ECONOMY_KIND_META[ev.kind];
        return (
          <div key={ev.id} className="fleet-activity-row">
            <span className="fleet-activity-time">{ageLabel}</span>
            <span
              className="fleet-activity-kind"
              title={kindMeta?.label ?? ev.kind}
              style={kindMeta?.tone === "ok" ? { color: "var(--color-status-ok)", borderColor: "color-mix(in oklab, var(--color-status-ok) 35%, transparent)" } : undefined}
            >
              {kindMeta?.label ?? ev.kind}
            </span>
            {ev.note ? (
              <span className="fleet-activity-note" title={ev.note}>{ev.note}</span>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function LogTab({ taskId }: { taskId: string }) {
  // WorkerLogTail gibt es schon im WorkerCard — wir renutzen es.
  return <WorkerLogTail taskId={taskId} />;
}

function ErgebnisTab({
  verdicts,
  deliverables,
  chainCost,
}: {
  verdicts: Array<{ task_id: string; reviewer_profile: string | null; review_run_state: string; verifier_verdict: string | null }>;
  deliverables: Array<{ filename: string; url: string; size: number }>;
  chainCost: CostDisplayValue;
}) {
  if (verdicts.length === 0 && deliverables.length === 0 && chainCost.value == null) {
    return (
      <p className="px-0.5 py-2 text-sec text-ink-3">
        {de.fleet.detailErgebnisEmpty}
      </p>
    );
  }

  return (
    <>
      {/* Review-Verdicts */}
      {verdicts.length > 0 ? (
        <div>
          <Eyebrow className="mb-1.5">Review-Verdicts</Eyebrow>
          {verdicts.map((v) => (
            <div key={`${v.task_id}-${v.reviewer_profile ?? "reviewer"}`} className="flex items-center gap-2 border-b border-line py-1.5 text-sec text-ink-2">
              <span className="flex-1">{v.reviewer_profile ?? "reviewer"}</span>
              <span
                className={cn(
                  "rounded-full border px-[7px] py-0.5 font-data text-micro",
                  v.verifier_verdict === "APPROVED" ? "border-status-ok/35 text-status-ok" : "border-status-warn/40 text-status-warn",
                )}
              >
                {v.verifier_verdict ?? v.review_run_state ?? "—"}
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {/* Deliverables — Auth-geschützte Endpoints: openAuthedApiFile statt raw href */}
      {deliverables.length > 0 ? (
        <div>
          <Eyebrow className="mb-1.5">{de.fleet.detailDeliverables}</Eyebrow>
          {deliverables.map((d) => (
            <button
              key={d.url || d.filename}
              type="button"
              onClick={() => void openAuthedApiFile(d.url, d.filename)}
              className="flex w-full cursor-pointer items-center gap-2 border-0 border-b border-line bg-transparent py-1.5 text-left text-sec text-live"
            >
              <span className="flex-1 truncate" title={d.filename}>
                {d.filename}
              </span>
              <span className="font-data text-micro text-ink-3">
                {d.size > 0 ? `${Math.round(d.size / 1024)} kB` : "↗"}
              </span>
            </button>
          ))}
        </div>
      ) : null}

      {/* Kosten-Beitrag */}
      {chainCost.value != null ? (
        <div className="fleet-kv" style={{ marginTop: 4 }}>
          <div className="fleet-kv-k">Kosten-Beitrag zur Kette</div>
          <div className="fleet-kv-v">{fmtUsdDisplay(chainCost)}</div>
        </div>
      ) : null}
    </>
  );
}
