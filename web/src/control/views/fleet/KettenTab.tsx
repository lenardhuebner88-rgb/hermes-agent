/**
 * Ketten-Subtab — Redesign nach Mockup B v4.
 *
 * 6 Sektionen: Ketten-Liste, Active-Chain-Header, Step-Pipeline,
 * Active-Step-Detail (mit Model-Row + GGFM-Override-Badge), Upcoming-Steps,
 * Done + Gate-Teaser.
 *
 * Join: useHermesWorkers() → worker.task_id === node.id für effective_model,
 * model_override, heartbeat_age, run_progress, eta_p50_seconds.
 *
 * Reines Frontend-Redesign — keine Backend-Änderung, keine Logik-Änderung.
 */
import { useState, useCallback, useMemo } from "react";
import {
  fmtSeconds,
  fmtTokens,
  fmtUsd,
  fmtDurationClock,
  profileInitial,
  premiumLaneMarker,

  buildChainChips,
  pickFocusNode,
  chainProgress,
  heartbeatAge,
} from "../../lib/fleetHub";
import { formatEffectiveCost } from "../../lib/derive";
import { de } from "../../i18n/de";
import {
  useChainGraph,
  useHermesChainCosts,
  useHermesReviewVerdicts,
  useHermesWorkers,
} from "../../hooks/useControlData";
import type { BoardResponse, BoardTask, Worker } from "../../lib/types";
import type { ChainCostsResponse } from "../../lib/schemas";
import { type ChainNode } from "./shared";

import "./ketten-v4.css";

// ─── Ketten-Subtab ────────────────────────────────────────────────────────────

interface KettenTabProps {
  board: BoardResponse | null;
  initialRootId: string | null;
  now: number;
  onOpenNodeDetail: (taskId: string, chainNodes?: ChainNode[]) => void;
  selectedNodeId?: string | null;
  detailControlsId?: string;
}

export function KettenTab({ board, initialRootId, now, onOpenNodeDetail, selectedNodeId = null, detailControlsId }: KettenTabProps) {
  const allBoardTasks: BoardTask[] = (board?.columns ?? []).flatMap((c) => c.tasks);

  const chips = buildChainChips(
    allBoardTasks.map((t) => ({
      id: t.id,
      title: t.title,
      root_id: t.root_id,
      status: t.status,
      completed_at: t.completed_at,
    })),
  );

  const [userSelectedRootId, setUserSelectedRootId] = useState<string | null>(initialRootId);

  const selectedRootId = useMemo(() => {
    if (userSelectedRootId) return userSelectedRootId;
    return chips.find((c) => c.state === "active")?.rootId ?? chips[0]?.rootId ?? null;
  }, [userSelectedRootId, chips]);

  const handleChipSelect = useCallback((rootId: string) => {
    setUserSelectedRootId(rootId);
  }, []);

  // FIX-2: aktive + wartende Ketten immer zeigen, fertige auf die jüngsten 3
  // cappen (chips sind bereits active→pending→completed sortiert).
  const [completedExpanded, setCompletedExpanded] = useState(false);
  const activeOrPendingChips = chips.filter((c) => c.state !== "completed");
  const completedChips = chips.filter((c) => c.state === "completed");
  const visibleCompletedChips = completedExpanded ? completedChips : completedChips.slice(0, 3);
  const hiddenCompletedCount = completedChips.length - 3;
  const visibleChips = [...activeOrPendingChips, ...visibleCompletedChips];

  const { data: chainGraph, loading: chainLoading } = useChainGraph(selectedRootId);
  const nodes = chainGraph?.nodes ?? [];

  const chainCosts = useHermesChainCosts(selectedRootId);
  const verdicts = useHermesReviewVerdicts();

  // === Worker-Join (v4): join ChainNode → Worker via task_id ===
  const { data: workersData } = useHermesWorkers();
  const workerByNodeId = useMemo(() => {
    const m = new Map<string, Worker>();
    const ws = workersData?.workers ?? [];
    for (const w of ws) {
      if (w.task_id) m.set(w.task_id, w);
    }
    return m;
  }, [workersData]);

  if (chips.length === 0) {
    return (
      <div className="ketten-v4">
        <div className="kt-empty">
          <p className="kt-empty-title">{de.fleet.kettenLeer}</p>
          <p className="kt-empty-sub">{de.fleet.kettenLeerDesc}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="ketten-v4">
      {/* ── SECTION 1: Ketten-Liste ───────────────────────────────────────── */}
      <div className="chain-list-header">
        <span className="section-title">Ketten</span>
        <span className="section-count">{chips.length}</span>
      </div>
      <div className="chain-list">
        {visibleChips.map((chip) => {
          const isActive = chip.state === "active";
          const isDone = chip.state === "completed";
          const pct = chip.total > 0 ? Math.round((chip.done / chip.total) * 100) : 0;

          return (
            <button
              key={chip.rootId}
              type="button"
              className={`chain-item${selectedRootId === chip.rootId ? " chain-item-active" : ""}${isDone ? " chain-item-done" : ""}`}
              onClick={() => handleChipSelect(chip.rootId)}
            >
              <span className={`chain-glyph ${isActive ? "glyph-active" : isDone ? "glyph-done" : "glyph-waiting"}`}>
                {isActive ? "▶" : isDone ? "✓" : "⋯"}
              </span>
              <div className="chain-content">
                <div className="chain-title-row">
                  <span className="chain-title">{chip.label}</span>
                  <span className={`chain-badge ${isActive ? "badge-running" : isDone ? "badge-done" : "badge-waiting"}`}>
                    {isActive ? "läuft" : isDone ? "fertig" : "wartet"}
                  </span>
                </div>
                <div className="chain-meta-row">
                  <span className="chain-mini-prog">
                    <span
                      className={`chain-mini-prog-fill ${isActive ? "fill-live" : isDone ? "fill-ok" : "fill-warn"}`}
                      style={{ width: `${pct}%` }}
                    />
                  </span>
                  <span className="chain-meta-text">{chip.done}/{chip.total}</span>
                </div>
              </div>
            </button>
          );
        })}
      </div>
      {completedChips.length > 3 ? (
        <button
          type="button"
          className="chain-expander"
          onClick={() => setCompletedExpanded((v) => !v)}
        >
          {completedExpanded ? "weniger anzeigen" : `+${hiddenCompletedCount} weitere fertige`}
        </button>
      ) : null}

      {/* ── Sections 2-6: Selected Chain ─────────────────────────────────── */}
      {selectedRootId && (chainLoading && !chainGraph) ? (
        <div className="kt-empty">
          <p className="kt-empty-sub">Lade Kette …</p>
        </div>
      ) : selectedRootId && nodes.length > 0 ? (
        <KettenGraphV4
          rootId={selectedRootId}
          nodes={nodes}
          now={now}
          workerByNodeId={workerByNodeId}
          verdicts={(verdicts.data?.reviews ?? []).map((v) => ({
            task_id: v.task_id,
            task_status: v.task_status,
            review_run_state: v.review_run_state ?? "pending",
            reviewer_profile: v.reviewer_profile,
          }))}
          chainCosts={chainCosts.data}
          chainCostsLoading={chainCosts.loading}
          onOpenNodeDetail={onOpenNodeDetail}
          selectedNodeId={selectedNodeId}
          detailControlsId={detailControlsId}
        />
      ) : selectedRootId ? (
        <div className="kt-empty">
          <p className="kt-empty-sub">Keine Ketten-Nodes geladen.</p>
        </div>
      ) : null}
    </div>
  );
}

// ─── Helper: Role color class ─────────────────────────────────────────────────

function roleColorClass(assignee: string | null): string {
  if (!assignee) return "ps-role-gate";
  if (/premium|opus/i.test(assignee)) return "ps-role-coder";
  if (/coder/i.test(assignee)) return "ps-role-coder";
  if (/reviewer|review/i.test(assignee)) return "ps-role-reviewer";
  if (/critic/i.test(assignee)) return "ps-role-critic";
  if (/integrator/i.test(assignee)) return "ps-role-integrator";
  return "ps-role-gate";
}

// FIX-4/FIX-5: Rollen-Präsenz + Rollen-Status aus den echten task_runs
// (`node.review_roles`), mit assignee als Fallback für ältere Payloads.
function hasRole(node: ChainNode, role: string): boolean {
  if ((node.review_roles ?? []).some((r) => r.profile === role)) return true;
  return node.assignee != null && new RegExp(role, "i").test(node.assignee);
}

type RoleTrackStatus = "done" | "pending" | "none";

function roleTrackStatus(node: ChainNode | null, role: string): RoleTrackStatus {
  const runs = (node?.review_roles ?? []).filter((r) => r.profile === role);
  if (runs.length === 0) return "none";
  if (runs.some((r) => r.verdict === "APPROVED" || r.status === "done")) return "done";
  return "pending";
}

const ROLE_TRACK_ORDER = ["reviewer", "critic", "verifier", "integrator"] as const;

function avatarClass(assignee: string | null): string {
  if (!assignee) return "avatar-default";
  if (/premium|opus/i.test(assignee)) return "avatar-premium";
  if (/coder/i.test(assignee)) return "avatar-coder";
  if (/reviewer|review/i.test(assignee)) return "avatar-reviewer";
  if (/critic/i.test(assignee)) return "avatar-critic";
  return "avatar-default";
}

// ─── KettenGraph v4 ────────────────────────────────────────────────────────────

interface KettenGraphV4Props {
  rootId: string;
  nodes: ChainNode[];
  now: number;
  workerByNodeId: Map<string, Worker>;
  verdicts: Array<{ task_id: string; task_status: string; review_run_state: string; reviewer_profile: string | null }>;
  chainCosts?: ChainCostsResponse | null;
  chainCostsLoading?: boolean;
  onOpenNodeDetail: (taskId: string, chainNodes: ChainNode[]) => void;
  selectedNodeId: string | null;
  detailControlsId?: string;
}

function KettenGraphV4({
  rootId,
  nodes,
  now,
  workerByNodeId,
  verdicts,
  chainCosts,
  chainCostsLoading,
  onOpenNodeDetail,
  selectedNodeId,
  detailControlsId,
}: KettenGraphV4Props) {
  const { pct, done, total } = chainProgress(nodes);
  const focusNode = pickFocusNode(nodes);

  // === Chain costs (server-side rollup) ===
  const costTotals = chainCosts?.totals;
  const costTokens = costTotals ? costTotals.input_tokens + costTotals.output_tokens : 0;
  const costText = costTotals
    ? formatEffectiveCost({ cost_usd: costTotals.cost_usd, cost_effective_usd: costTotals.cost_effective_usd, tokens: costTokens }).text
    : chainCostsLoading ? "…" : "—";

  const chainInputTokens = costTotals?.input_tokens ?? 0;
  const chainOutputTokens = costTotals?.output_tokens ?? 0;

  // === Focus node worker join ===
  const focusWorker = focusNode ? workerByNodeId.get(focusNode.id) ?? null : null;
  const focusEffectiveModel = focusWorker?.effective_model ?? focusNode?.latest_run?.profile ?? null;
  const focusModelOverride = focusWorker?.model_override ?? null;
  const focusHbAge = focusWorker
    ? heartbeatAge(focusWorker.last_heartbeat_at, now)
    : focusNode?.latest_run?.heartbeat_age_seconds ?? null;
  const focusRunProgress = focusWorker?.run_progress ?? focusNode?.latest_run?.run_progress ?? null;
  const focusEtaP50 = focusWorker?.eta_p50_seconds ?? null;
  const focusRuntime = focusNode?.latest_run?.runtime_seconds ?? null;

  // Active chain chip for ETA
  const chainEta = focusEtaP50 ?? focusRuntime;

  // === Node classification ===
  const orderedNodes = [...nodes].sort((a, b) => a.level - b.level);
  const upcomingNodes = orderedNodes.filter(
    (n) => n.id !== focusNode?.id && (n.status === "scheduled" || n.status === "ready" || n.status === "todo" || n.status === "blocked"),
  );
  const doneNodes = orderedNodes.filter((n) => n.status === "done" || n.status === "archived");

  // === Gate verdicts ===
  const gateVerdicts = verdicts.filter((v) => v.task_id === rootId || nodes.some((n) => n.id === v.task_id));
  const reviewRunState = gateVerdicts[0]?.review_run_state ?? "pending";

  // FIX-4: Rollen-Präsenz einheitlich aus den Review-Runs der Chain-Nodes
  // ableiten (statt Header-Chips vs. Pipeline aus unterschiedlichen Quellen).
  const hasReviewer = nodes.some((n) => hasRole(n, "reviewer"));
  const hasCritic = nodes.some((n) => hasRole(n, "critic"));
  const hasBlockage = nodes.some((n) => n.status === "blocked");

  const [doneExpanded, setDoneExpanded] = useState(false);

  return (
    <>
      {/* ── SECTION 2: Active Chain Header ────────────────────────────────── */}
      <div className="ach">
        <div className="ach-top">
          <div className="ach-pct-wrap">
            <span className="ach-pct">{pct}</span>
            <span className="ach-pct-sub">% · {done} / {total} Steps</span>
          </div>
          <span className="ach-state-badge">
            {focusNode?.status === "running" ? "läuft" : focusNode?.status === "blocked" ? "blockiert" : done >= total ? "fertig" : "wartet"}
          </span>
        </div>

        {/* Health: 3 separate chips — values only */}
        <div className="health-chips">
          <span className={`hchip ${hasBlockage ? "hchip-muted" : "hchip-ok"}`}>
            <span className={`hchip-icon ${hasBlockage ? "hi-muted" : "hi-ok"}`} />
            {hasBlockage ? "Blockaden" : "keine Blockaden"}
          </span>
          <span className={`hchip ${hasReviewer ? "hchip-info" : "hchip-muted"}`}>
            <span className={`hchip-icon ${hasReviewer ? "hi-info" : "hi-muted"}`} />
            {hasReviewer ? "Reviewer zugewiesen" : "kein Reviewer"}
          </span>
          <span className={`hchip ${hasCritic ? "hchip-ok" : "hchip-muted"}`}>
            <span className={`hchip-icon ${hasCritic ? "hi-ok" : "hi-muted"}`} />
            {hasCritic ? "Critic aktiv" : "kein Critic"}
          </span>
        </div>

        {/* Meta: values only, no labels */}
        <div className="ach-meta">
          {chainEta != null ? (
            <span className="ach-meta-item ach-meta-live">ETA ~{fmtSeconds(chainEta)}</span>
          ) : null}
          {chainEta != null && costText !== "—" ? <span className="ach-meta-sep">·</span> : null}
          {costText !== "—" ? <span className="ach-meta-item">{costText}</span> : null}
          {chainInputTokens > 0 || chainOutputTokens > 0 ? (
            <>
              <span className="ach-meta-sep">·</span>
              <span className="ach-meta-item">{fmtTokens(chainInputTokens)} → {fmtTokens(chainOutputTokens)} tok</span>
            </>
          ) : null}
        </div>
      </div>

      {/* ── SECTION 3: Step Pipeline ──────────────────────────────────────── */}
      {orderedNodes.length > 0 ? (
        <div className="pipe-wrap">
          <div className="pipe-header">
            <span>Pipeline</span>
            <span className="pipe-step-count">{done} / {total} Steps</span>
          </div>
          <div className="pipe-scroll">
            <div className="pipe">
              {orderedNodes.map((node, i) => {

                const isDone = node.status === "done" || node.status === "archived";
                const isBlocked = node.status === "blocked";
                const isRunning = node.status === "running";
                const worker = workerByNodeId.get(node.id);
                // FIX-3: Label = Rolle (nicht strippen); Sub = Modell, nur wenn
                // bekannt UND verschieden von der Rolle.
                const roleLabel = node.assignee ?? node.latest_run?.profile ?? "—";
                const nodeModel = worker?.effective_model ?? null;
                const showModelSub = nodeModel != null && nodeModel !== roleLabel;

                // Connector class (between this node and the next)
                let connectorClass = "pc-open";
                if (isDone) connectorClass = "pc-done";
                else if (isRunning) connectorClass = "pc-active";
                else if (isBlocked) connectorClass = "pc-warn";

                let iconClass = "";
                if (isDone) iconClass = "ps-done";
                else if (isRunning) iconClass = "ps-active";
                else if (isBlocked) iconClass = "ps-blocked";
                else iconClass = roleColorClass(node.assignee);

                return (
                  <div key={node.id} className="pstep">
                    {i < orderedNodes.length - 1 ? (
                      <div className={`pstep-connector ${connectorClass}`} />
                    ) : null}
                    <div className={`pstep-icon ${iconClass}`}>
                      {isDone ? "✓" : isRunning ? "▶" : (i + 1)}
                    </div>
                    <div className={`pstep-label ${isRunning ? "pstep-label-active" : isDone ? "pstep-label-done" : ""}`} title={roleLabel}>
                      {roleLabel}
                    </div>
                    {showModelSub ? (
                      <div className={`pstep-sub ${isRunning ? "pstep-sub-active" : ""}`}>{nodeModel}</div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      ) : null}

      {/* ── SECTION 3.5: Rollen-Track (fokussierter Slice) ───────────────── */}
      {focusNode ? (
        <div className="rtrack-wrap">
          <div className="rtrack-header">REVIEW (aktiver Slice)</div>
          <div className="rtrack-row">
            {ROLE_TRACK_ORDER.map((role, i) => {
              const state = roleTrackStatus(focusNode, role);
              const glyph = state === "done" ? "✓" : state === "pending" ? "⏳" : "–";
              return (
                <span key={role} className={`rtrack-item rtrack-${state}`}>
                  {role} {glyph}
                  {i < ROLE_TRACK_ORDER.length - 1 ? <span className="rtrack-sep">·</span> : null}
                </span>
              );
            })}
          </div>
        </div>
      ) : null}

      {/* ── SECTION 4: Active Step Detail ────────────────────────────────── */}
      {focusNode ? (
        <button
          type="button"
          className={`detail${selectedNodeId === focusNode.id ? " detail-selected" : ""}`}
          onClick={() => onOpenNodeDetail(focusNode.id, nodes)}
          aria-label={`Node ${focusNode.title} öffnen`}
          aria-expanded={selectedNodeId === focusNode.id}
          aria-controls={detailControlsId}
        >
          <div className="detail-header">
            <div
              className={`detail-avatar ${avatarClass(focusNode.assignee)}`}
              {...premiumLaneMarker(focusNode.assignee)}
            >
              {profileInitial(focusNode.assignee ?? "?")}
            </div>
            <div className="detail-meta">
              <div className="detail-role">
                {focusNode.assignee ?? "—"}
                {focusEffectiveModel ? <span className="detail-role-tier">· {focusEffectiveModel}</span> : null}
              </div>
              <div className="detail-task-id">{focusNode.id.slice(0, 12)}</div>
            </div>
            {/* Heartbeat LED — nur einmal, oben-rechts */}
            {focusNode.status === "running" && focusHbAge != null ? (
              <div className="detail-led">
                <span className="led-dot" />
                ♥ {fmtSeconds(focusHbAge)}
              </div>
            ) : null}
          </div>

          <div className="detail-title">{focusNode.title}</div>

          {/* === Model-Row with GGFM Override Badge (v4) === */}
          {focusEffectiveModel ? (
            <div className="model-row">
              <span className="model-icon">⚙</span>
              <span className="model-label">{focusEffectiveModel}</span>
              {focusModelOverride ? (
                <span className="model-override-badge" title={`Override: ${focusModelOverride}`}>
                  GGFM Override
                </span>
              ) : null}
            </div>
          ) : null}

          {/* Progress ring + values */}
          <div className="detail-bottom">
            <ProgressRing
              progress={
                focusRunProgress != null ? focusRunProgress
                : focusNode.progress && focusNode.progress.total > 0
                  ? focusNode.progress.done / focusNode.progress.total
                : focusNode.status === "running" ? 0.58 : 0
              }
            />
            {/* Values only, no labels */}
            <div className="values-row">
              {focusRuntime != null ? (
                <span className="val val-strong">{fmtDurationClock(focusRuntime)}</span>
              ) : null}
              {focusRuntime != null && (focusNode.input_tokens > 0 || focusNode.output_tokens > 0) ? (
                <span className="val-sep">·</span>
              ) : null}
              {focusNode.input_tokens > 0 || focusNode.output_tokens > 0 ? (
                <span className="val">
                  {fmtTokens(focusNode.input_tokens)} ↓ {fmtTokens(focusNode.output_tokens)} tok
                </span>
              ) : null}
              {focusEtaP50 != null ? (
                <>
                  <span className="val-sep">·</span>
                  <span className="val val-live">p50~{fmtSeconds(focusEtaP50)}</span>
                </>
              ) : null}
            </div>
          </div>
        </button>
      ) : null}

      {/* ── SECTION 5: Upcoming Steps ────────────────────────────────────── */}
      {upcomingNodes.length > 0 ? (
        <div className="upcoming">
          <div className="upcoming-header">
            <span>Upcoming</span>
            <span className="upcoming-count">{upcomingNodes.length}</span>
          </div>
          {upcomingNodes.map((n) => {
            const worker = workerByNodeId.get(n.id);
            const model = worker?.effective_model ?? n.latest_run?.profile ?? null;
            const hasOverride = worker?.model_override != null;
            return (
              <button
                key={n.id}
                type="button"
                className={`uitem${n.status === "blocked" ? " uitem-blocked" : ""}${selectedNodeId === n.id ? " uitem-selected" : ""}`}
                onClick={() => onOpenNodeDetail(n.id, nodes)}
                aria-label={`Node ${n.title} öffnen`}
                aria-expanded={selectedNodeId === n.id}
                aria-controls={detailControlsId}
              >
                <div className={`uavatar ${avatarClass(n.assignee)}`} {...premiumLaneMarker(n.assignee)}>
                  {profileInitial(n.assignee ?? "?")}
                </div>
                <div className="ucontent">
                  <div className={`urole ${n.assignee && /reviewer/i.test(n.assignee) ? "urole-reviewer" : n.assignee && /critic/i.test(n.assignee) ? "urole-critic" : n.assignee && /coder/i.test(n.assignee) ? "urole-coder" : ""}`}>
                    {n.assignee ?? "—"}
                  </div>
                  {model ? (
                    <div className={`umodel ${hasOverride ? "umodel-override" : ""}`}>
                      {model}
                      {hasOverride ? " · ⚠ override" : " · lane default"}
                    </div>
                  ) : null}
                  <div className="utitle">{n.title}</div>
                </div>
                <div className={`uwait${n.status === "blocked" ? " uwait-blocked" : ""}`}>
                  {n.status === "blocked" ? "blockiert" : "wartet"}
                </div>
              </button>
            );
          })}
        </div>
      ) : null}

      {/* ── SECTION 6: Done + Gate Teaser ────────────────────────────────── */}
      {doneNodes.length > 0 ? (
        <div className="done-section">
          <button
            type="button"
            className="done-header"
            onClick={() => setDoneExpanded((v) => !v)}
            aria-expanded={doneExpanded}
          >
            <span>Fertig</span>
            <span className="done-count">{doneNodes.length}</span>
            <span className="done-chev">{doneExpanded ? "▲" : "▼"}</span>
          </button>
          {doneExpanded ? doneNodes.map((n) => (
            <button
              key={n.id}
              type="button"
              className={`done-item${selectedNodeId === n.id ? " done-item-selected" : ""}`}
              onClick={() => onOpenNodeDetail(n.id, nodes)}
              aria-label={`Node ${n.title} öffnen`}
              aria-expanded={selectedNodeId === n.id}
              aria-controls={detailControlsId}
            >
              <span className="davatar">✓</span>
              <div className="dcontent">
                <div className="dtitle">{n.title}</div>
                <div className="dtime">
                  {n.cost_usd > 0 ? fmtUsd(n.cost_usd) : null}
                  {n.cost_usd > 0 && n.latest_run?.runtime_seconds != null ? " · " : ""}
                  {n.latest_run?.runtime_seconds != null ? fmtSeconds(n.latest_run.runtime_seconds) : ""}
                </div>
              </div>
            </button>
          )) : null}
        </div>
      ) : null}

      {/* Gate-Teaser */}
      <div className="gate-teaser">
        <div className="gate-icon" />
        <div className="gate-content">
          <div className="gate-label">Release-Gate</div>
          <div className={`gate-status ${reviewRunState === "request_changes" ? "gate-status-warn" : ""}`}>
            {reviewRunState === "approved" ? "approved" : reviewRunState === "request_changes" ? "Änderungen angefordert" : reviewRunState === "active" ? "Review läuft…" : "wartet"}
          </div>
        </div>
      </div>
    </>
  );
}

// ─── Progress Ring (SVG) ──────────────────────────────────────────────────────

function ProgressRing({ progress }: { progress: number }) {
  const pct = Math.max(0, Math.min(1, progress));
  const r = 16;
  const circ = 2 * Math.PI * r;
  const dash = pct * circ;
  return (
    <div className="progress-ring">
      <svg viewBox="0 0 40 40" width="42" height="42">
        <circle className="kt-ring-bg" cx="20" cy="20" r={r} />
        <circle
          className="kt-ring-fg"
          cx="20" cy="20" r={r}
          strokeDasharray={`${dash.toFixed(2)} ${circ.toFixed(2)}`}
        />
      </svg>
      <span className="progress-ring-text">{Math.round(pct * 100)}%</span>
    </div>
  );
}
