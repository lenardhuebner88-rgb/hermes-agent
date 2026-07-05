/**
 * Karten-Detail-Drawer (Overlay) + seine Tabs (Übersicht/Aktivität/Log/Ergebnis).
 *
 * Aus FleetView.tsx extrahiert — reine Zerlegung, kein Verhalten geändert.
 */
import { useState } from "react";
import {
  fmtSeconds,
  fmtTokens,
  fmtUsd,
  chainTotalCostUsdWithSource,
  type CostDisplayValue,
  profileInitial,
  profileColorClass,
} from "../../lib/fleetHub";
import { de } from "../../i18n/de";
import { useWorkerActivity, useHermesReviewVerdicts, useTaskBodyOnDemand, useTaskDeliverablesOnDemand } from "../../hooks/useControlData";
import { Overlay } from "../../components/Overlay";
import { WorkerLogTail } from "../../components/WorkerCard";
import { openAuthedApiFile } from "@/lib/api";
import { fmtUsdDisplay, type ChainNode } from "./shared";
import { FleetTaskActions } from "./TaskActions";
import { AnswerQuestion } from "./AnswerQuestion";
import { isOperatorQuestion } from "../../lib/fleet";

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
  const [tab, setTab] = useState<DetailTab>("uebersicht");
  const [copied, setCopied] = useState(false);

  // On-Demand-Daten (nur bei offenem Drawer)
  const taskBody = useTaskBodyOnDemand(taskId);
  const deliverablesResult = useTaskDeliverablesOnDemand(taskId);
  const activity = useWorkerActivity(taskId);
  const verdicts = useHermesReviewVerdicts();

  const task = taskBody.data?.task ?? null;
  const runs = taskBody.data?.runs ?? [];
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

  const latestRun = runs[0] ?? null;
  const elapsedSec = latestRun?.runtime_seconds ?? (latestRun?.started_at ? Math.max(0, now - latestRun.started_at) : null);

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
    <Overlay onClose={onClose} ariaLabel={`Task ${taskId} Details`} maxWidthClassName="max-w-lg">
      <div data-fleet-theme className="fleet-drawer-inner">
        {/* Grab */}
        <div className="fleet-grab" />

        {/* Kopf */}
        <div className="fleet-dr-head">
          <div className={`fleet-avatar fleet-avatar-gross ${profileColorClass(task?.assignee ?? "")}`}>
            {profileInitial(task?.assignee ?? "?")}
          </div>
          <div className="fleet-dr-title">
            <span style={{ fontSize: 14 }}>{task?.title || taskId}</span>
            <span>
              <button
                type="button"
                className="fleet-copy-id"
                onClick={handleCopy}
                title={de.fleet.detailKopieren}
                aria-label={de.fleet.detailKopieren}
              >
                {taskId}
                <span style={{ fontSize: 9, opacity: 0.7 }}>{copied ? " ✓" : " ⊕"}</span>
              </button>
            </span>
          </div>
          <button
            type="button"
            className="fleet-btn"
            style={{ flex: "0 0 auto", padding: "6px 10px", minHeight: "auto" }}
            onClick={onClose}
            aria-label={de.fleet.detailSchliessen}
          >
            ✕
          </button>
        </div>

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
            <FleetTaskActions
              taskId={taskId}
              status={task.status ?? ""}
              chainRootId={chainRootId}
              onChanged={onChanged}
              onCancelled={onClose}
            />
          </div>
        ) : null}
      </div>
    </Overlay>
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
      {/* Status-Badge */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{
          fontFamily: "var(--hc-font-mono)", fontSize: 10,
          padding: "3px 8px", borderRadius: 999,
          border: `1px solid ${task.status === "running" ? "rgba(55,224,255,.4)" : task.status === "done" ? "rgba(67,214,154,.35)" : "var(--fleet-linie)"}`,
          color: task.status === "running" ? "var(--fleet-puls)" : task.status === "done" ? "var(--fleet-gruen)" : "var(--fleet-t2)",
        }}>
          {task.status ?? "—"}
        </span>
        {task.review_tier ? (
          <span style={{ fontFamily: "var(--hc-font-mono)", fontSize: 10, padding: "3px 8px", borderRadius: 999, border: "1px solid var(--fleet-linie)", color: "var(--fleet-t3)" }}>
            {task.review_tier}
          </span>
        ) : null}
      </div>

      {/* Block-Reason */}
      {task.block_reason ? (
        <div style={{ background: "rgba(245,168,60,.08)", border: "1px solid rgba(245,168,60,.3)", borderRadius: 10, padding: "8px 10px", fontSize: 11.5, color: "var(--fleet-signal)", lineHeight: 1.5 }}>
          {de.fleet.detailLabelBlockReason}: {task.block_reason}
        </div>
      ) : null}

      {/* Operator-Frage beantworten (S6) — nur wenn blockiert + operator_question */}
      {task.status === "blocked" && isOperatorQuestion(task.block_reason) ? (
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
          <div style={{ font: "500 9.5px/1 var(--hc-font-sans)", color: "var(--fleet-t3)", letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
            {de.fleet.detailBodyLabel}
          </div>
          <div style={{ font: "400 12px/1.6 var(--hc-font-sans)", color: "var(--fleet-t2)", borderLeft: "2px solid var(--fleet-puls)", paddingLeft: 10, maxHeight: 160, overflowY: "auto", overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>
            {task.body}
          </div>
        </div>
      ) : null}

      {/* Acceptance-Criteria */}
      {acList.length > 0 ? (
        <div>
          <div style={{ font: "500 9.5px/1 var(--hc-font-sans)", color: "var(--fleet-t3)", letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
            {de.fleet.detailAcceptanceLabel}
          </div>
          <ul style={{ paddingLeft: 16, margin: 0, display: "flex", flexDirection: "column", gap: 4 }}>
            {acList.slice(0, 8).map((item, i) => (
              <li key={i} style={{ font: "400 11.5px/1.45 var(--hc-font-sans)", color: "var(--fleet-t2)" }}>
                {item}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Deliverables-Liste — Auth-geschützte Endpoints: openAuthedApiFile statt raw href */}
      {deliverables.length > 0 ? (
        <div>
          <div style={{ font: "500 9.5px/1 var(--hc-font-sans)", color: "var(--fleet-t3)", letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
            {de.fleet.detailDeliverables}
          </div>
          {deliverables.map((d) => (
            <button
              key={d.url || d.filename}
              type="button"
              onClick={() => void openAuthedApiFile(d.url, d.filename)}
              style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", font: "400 11.5px/1 var(--hc-font-sans)", color: "var(--fleet-puls)", background: "none", border: "none", cursor: "pointer", width: "100%", textAlign: "left", borderBottom: "1px solid var(--fleet-linie)" }}
            >
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {d.filename}
              </span>
              <span style={{ fontFamily: "var(--hc-font-mono)", fontSize: 10, color: "var(--fleet-t3)" }}>
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
      <p style={{ font: "400 11.5px/1.4 var(--hc-font-sans)", color: "var(--fleet-t3)", padding: "8px 2px" }}>
        {de.fleet.detailActivityEmpty}
      </p>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {events.slice(0, 20).map((ev) => {
        const age = ev.at > 0 ? Math.max(0, now - ev.at) : null;
        const kindMeta = REVIEW_ECONOMY_KIND_META[ev.kind];
        return (
          <div key={ev.id} className="fleet-activity-row">
            <span className="fleet-activity-time">{age != null ? fmtSeconds(age) : "—"}</span>
            <span
              className="fleet-activity-kind"
              style={kindMeta?.tone === "ok" ? { color: "var(--fleet-gruen)", borderColor: "rgba(67,214,154,.35)" } : undefined}
            >
              {kindMeta?.label ?? ev.kind}
            </span>
            {ev.note ? (
              <span className="fleet-activity-note">{ev.note}</span>
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
      <p style={{ font: "400 11.5px/1.4 var(--hc-font-sans)", color: "var(--fleet-t3)", padding: "8px 2px" }}>
        {de.fleet.detailErgebnisEmpty}
      </p>
    );
  }

  return (
    <>
      {/* Review-Verdicts */}
      {verdicts.length > 0 ? (
        <div>
          <div style={{ font: "500 9.5px/1 var(--hc-font-sans)", color: "var(--fleet-t3)", letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
            Review-Verdicts
          </div>
          {verdicts.map((v) => (
            <div key={`${v.task_id}-${v.reviewer_profile ?? "reviewer"}`} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", borderBottom: "1px solid var(--fleet-linie)", font: "400 11.5px/1 var(--hc-font-sans)", color: "var(--fleet-t2)" }}>
              <span style={{ flex: 1 }}>{v.reviewer_profile ?? "reviewer"}</span>
              <span style={{
                fontFamily: "var(--hc-font-mono)", fontSize: 10,
                padding: "2px 7px", borderRadius: 999,
                border: `1px solid ${v.verifier_verdict === "APPROVED" ? "rgba(67,214,154,.35)" : "rgba(245,168,60,.4)"}`,
                color: v.verifier_verdict === "APPROVED" ? "var(--fleet-gruen)" : "var(--fleet-signal)",
              }}>
                {v.verifier_verdict ?? v.review_run_state ?? "—"}
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {/* Deliverables — Auth-geschützte Endpoints: openAuthedApiFile statt raw href */}
      {deliverables.length > 0 ? (
        <div>
          <div style={{ font: "500 9.5px/1 var(--hc-font-sans)", color: "var(--fleet-t3)", letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
            {de.fleet.detailDeliverables}
          </div>
          {deliverables.map((d) => (
            <button
              key={d.url || d.filename}
              type="button"
              onClick={() => void openAuthedApiFile(d.url, d.filename)}
              style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", font: "400 11.5px/1 var(--hc-font-sans)", color: "var(--fleet-puls)", background: "none", border: "none", cursor: "pointer", width: "100%", textAlign: "left", borderBottom: "1px solid var(--fleet-linie)" }}
            >
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {d.filename}
              </span>
              <span style={{ fontFamily: "var(--hc-font-mono)", fontSize: 10, color: "var(--fleet-t3)" }}>
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
