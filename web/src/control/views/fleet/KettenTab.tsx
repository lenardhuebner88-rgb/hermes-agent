/**
 * Ketten-Subtab (Jetzt-zentriert) + Ketten-Graph + Fortschritts-Ring.
 *
 * Aus FleetView.tsx extrahiert — reine Zerlegung, kein Verhalten geändert.
 */
import { useState, useCallback, useMemo } from "react";
import {
  fmtSeconds,
  fmtTokens,
  fmtUsd,
  profileInitial,
  profileColorClass,
  buildChainChips,
  buildSegments,
  pickFocusNode,
  chainProgress,
  type ChainChipDef,
  type SegmentKind,
} from "../../lib/fleetHub";
import { formatEffectiveCost } from "../../lib/derive";
import { de } from "../../i18n/de";
import { useChainGraph, useHermesChainCosts, useHermesReviewVerdicts } from "../../hooks/useControlData";
import type { BoardResponse, BoardTask } from "../../lib/types";
import type { ChainCostsResponse } from "../../lib/schemas";
import { type ChainNode } from "./shared";

// ─── Ketten-Subtab ────────────────────────────────────────────────────────────

interface KettenTabProps {
  board: BoardResponse | null;
  initialRootId: string | null;
  now: number;
  /** Callback: öffnet den Karten-Detail-Drawer. chainNodes erlaubt dem Drawer, Kettenkosten zu zeigen. */
  onOpenNodeDetail: (taskId: string, chainNodes?: ChainNode[]) => void;
}

export function KettenTab({ board, initialRootId, now, onOpenNodeDetail }: KettenTabProps) {
  // Alle Board-Tasks (flat, alle Spalten)
  const allBoardTasks: BoardTask[] = (board?.columns ?? []).flatMap((c) => c.tasks);

  // Ketten-Chips aus Board-Tasks ableiten
  const chips = buildChainChips(
    allBoardTasks.map((t) => ({
      id: t.id,
      title: t.title,
      root_id: t.root_id,
      status: t.status,
      completed_at: t.completed_at,
    })),
  );

  // Ausgewählte Kette:
  // - initialRootId (von Worker-Drawer via "Kette öffnen") hat Priorität, aber nur einmalig.
  // - Wenn Board-Daten erst nach Mount laden (cold start: chips zunächst leer), wird
  //   auto-selektiert via useMemo (erste aktive Kette oder erste verfügbare).
  // - userSelectedRootId: User-Auswahl überschreibt Auto-Select. Null = keine Auswahl noch.
  const [userSelectedRootId, setUserSelectedRootId] = useState<string | null>(initialRootId);

  // Derived State: selectedRootId wird auto-berechnet, nicht setState!
  // Falls userSelectedRootId oder initialRootId: use das. Sonst auto: erste aktive oder erste Kette.
  const selectedRootId = useMemo(() => {
    if (userSelectedRootId) return userSelectedRootId;
    return chips.find((c) => c.state === "active")?.rootId ?? chips[0]?.rootId ?? null;
  }, [userSelectedRootId, chips]);

  const handleChipSelect = useCallback((rootId: string) => {
    setUserSelectedRootId(rootId);
  }, []);

  // Chain-Graph für die ausgewählte Kette
  const { data: chainGraph, loading: chainLoading } = useChainGraph(selectedRootId);
  const nodes = chainGraph?.nodes ?? [];

  // AC-3: Ketten-Kosten aus dem server-seitigen Rollup (GET /tasks/{id}/chain-
  // costs — dieselbe Quelle wie die Flow-Receipt-Leiste und ChainVizView), nicht
  // clientseitig aus den Node-Summen abgeleitet.
  const chainCosts = useHermesChainCosts(selectedRootId);

  // Verdicts für Gate-Status
  const verdicts = useHermesReviewVerdicts();

  if (chips.length === 0) {
    return (
      <div className="fleet-empty">
        <p className="fleet-empty-title">{de.fleet.kettenLeer}</p>
        <p className="fleet-empty-sub">{de.fleet.kettenLeerDesc}</p>
      </div>
    );
  }

  return (
    <>
      {/* Ketten-Chips */}
      <div className="fleet-kchips">
        {chips.map((chip) => (
          <button
            key={chip.rootId}
            type="button"
            className={`fleet-kchip${selectedRootId === chip.rootId ? " fleet-kchip-on" : ""}`}
            onClick={() => handleChipSelect(chip.rootId)}
            aria-pressed={selectedRootId === chip.rootId}
          >
            {chip.state === "active" ? (
              <ChainProgressRing progress={chip.progress} total={chip.total} />
            ) : chip.state === "pending" ? (
              <span className="fleet-kchip-pending" aria-hidden="true">⏳</span>
            ) : (
              <span className="fleet-kchip-ok" aria-hidden="true">✓</span>
            )}
            {chip.label.length > 22 ? chip.label.slice(0, 22) + "…" : chip.label}
          </button>
        ))}
      </div>

      {/* Ketten-Inhalt */}
      {selectedRootId && (chainLoading && !chainGraph) ? (
        <div className="fleet-empty">
          <p className="fleet-empty-sub" style={{ fontFamily: "var(--hc-font-mono)", fontSize: 11 }}>Lade Kette …</p>
        </div>
      ) : selectedRootId && nodes.length > 0 ? (
        <KettenGraph
          rootId={selectedRootId}
          nodes={nodes}
          now={now}
          chips={chips}
          verdicts={(verdicts.data?.reviews ?? []).map((v) => ({
            task_id: v.task_id,
            task_status: v.task_status,
            review_run_state: v.review_run_state ?? "pending",
            reviewer_profile: v.reviewer_profile,
          }))}
          chainCosts={chainCosts.data}
          chainCostsLoading={chainCosts.loading}
          onOpenNodeDetail={onOpenNodeDetail}
        />
      ) : selectedRootId ? (
        <div className="fleet-empty">
          <p className="fleet-empty-sub">Keine Ketten-Nodes geladen.</p>
        </div>
      ) : null}
    </>
  );
}

// ─── SVG-Fortschritts-Ring ────────────────────────────────────────────────────

function ChainProgressRing({ progress, total }: { progress: number; total: number }) {
  const r = 6;
  const circ = 2 * Math.PI * r; // ~37.7
  const dash = progress * circ;
  const gap = circ - dash;
  return (
    <svg className="fleet-ring" viewBox="0 0 16 16" aria-label={`${Math.round(progress * 100)}%`}>
      <circle className="fleet-ring-bg" cx="8" cy="8" r={r} />
      {total > 0 ? (
        <circle
          className="fleet-ring-fg"
          cx="8"
          cy="8"
          r={r}
          strokeDasharray={`${dash.toFixed(2)} ${gap.toFixed(2)}`}
        />
      ) : null}
    </svg>
  );
}

// ─── Ketten-Graph (Jetzt-zentriert) ───────────────────────────────────────────

interface KettenGraphProps {
  rootId: string;
  nodes: ChainNode[];
  now: number;
  chips: ChainChipDef[];
  verdicts: Array<{ task_id: string; task_status: string; review_run_state: string; reviewer_profile: string | null }>;
  /** Server-seitiger Ketten-Kosten-Rollup (AC-3). null ⇒ noch nicht geladen. */
  chainCosts?: ChainCostsResponse | null;
  chainCostsLoading?: boolean;
  /** Callback: öffnet den Karten-Detail-Drawer + übergibt die Ketten-Nodes für Kostendarstellung. */
  onOpenNodeDetail: (taskId: string, chainNodes: ChainNode[]) => void;
}

function KettenGraph({ rootId, nodes, now, verdicts, chainCosts, chainCostsLoading, onOpenNodeDetail }: KettenGraphProps) {
  const { pct, done, total } = chainProgress(nodes);
  const focusNode = pickFocusNode(nodes);
  const segments: SegmentKind[] = buildSegments(nodes);
  // AC-3: Ketten-Kosten aus dem server-seitigen Rollup, gerettet aus ChainVizViews
  // ChainSummary (formatEffectiveCost auf den totals) — realer Ist-$ statt Node-Summe.
  const costTotals = chainCosts?.totals;
  const costTokens = costTotals ? costTotals.input_tokens + costTotals.output_tokens : 0;
  const costText = costTotals
    ? formatEffectiveCost({ cost_usd: costTotals.cost_usd, cost_effective_usd: costTotals.cost_effective_usd, tokens: costTokens }).text
    : chainCostsLoading ? "…" : "—";

  // ETA aus dem Fokus-Node
  const focusLaufzeit = focusNode?.latest_run?.runtime_seconds ?? null;
  const focusHbAge = focusNode?.latest_run?.heartbeat_age_seconds ?? null;

  // Root-Task: ältester erstellter Node (level 0 oder kleinster level)
  const rootNode = [...nodes].sort((a, b) => a.level - b.level)[0] ?? null;
  const rootCreatedAt = rootNode?.created_at ?? 0;
  const rootStartLabel = rootCreatedAt > 0 ? fmtSeconds(Math.max(0, now - rootCreatedAt)) + " her" : "—";

  // Offene Nodes (pending: scheduled/ready/todo/blocked) — NICHT der Fokus-Node
  const openNodes = [...nodes]
    .sort((a, b) => a.level - b.level)
    .filter((n) => n.id !== focusNode?.id && (n.status === "scheduled" || n.status === "ready" || n.status === "todo" || n.status === "blocked"));

  // Fertige Nodes
  const doneNodes = [...nodes]
    .filter((n) => n.status === "done" || n.status === "archived")
    .sort((a, b) => (a.level - b.level));

  // Gate-Node (Review-Status)
  const gateVerdicts = verdicts.filter((v) => v.task_id === rootId || nodes.some((n) => n.id === v.task_id));
  const gateLabel = gateVerdicts.length > 0
    ? `${gateVerdicts[0].reviewer_profile ?? "reviewer"}`
    : "reviewer";
  const gateMeta = gateVerdicts.length > 0
    ? gateVerdicts[0].review_run_state
    : `wartet auf Karte ${total}`;

  const [fertigOpen, setFertigOpen] = useState(false);

  return (
    <>
      {/* Fortschritts-Kopf */}
      <div className="fleet-prog">
        <div className="fleet-prog-top">
          <span className="fleet-prog-pz">{pct} %</span>
          <span className="fleet-prog-pl">
            {focusNode?.status === "running"
              ? de.fleet.kettenKarteLaeuft(done + 1, total)
              : de.fleet.kettenKarteWartet(done + 1, total)}
          </span>
          <span className="fleet-prog-eta">
            {focusLaufzeit != null ? `ETA ~${fmtSeconds(focusLaufzeit)}` : "—"}
            {costText !== "—" ? ` · ${costText}` : ""}
          </span>
        </div>

        {/* Segment-Leiste */}
        <div className="fleet-segs">
          {segments.map((kind, i) => (
            <div
              key={i}
              className={`fleet-seg${kind === "done" ? " fleet-seg-done" : kind === "active" ? " fleet-seg-active" : ""}`}
            />
          ))}
        </div>
        <div className="fleet-seg-l">
          <span>Root {rootStartLabel}</span>
          <span>{gateLabel} · Gate</span>
        </div>
      </div>

      {/* Fokus-Karte */}
      {focusNode ? (
        <button
          type="button"
          className="fleet-fokus text-left"
          onClick={() => onOpenNodeDetail(focusNode.id, nodes)}
          aria-label={`Node ${focusNode.title} öffnen`}
        >
          <div className="fleet-wk-top">
            <div className={`fleet-avatar fleet-avatar-gross ${profileColorClass(focusNode.assignee ?? "")}`}>
              {profileInitial(focusNode.assignee ?? "?")}
            </div>
            <div className="fleet-wk-name">
              {focusNode.assignee ?? "—"}
              <span>{focusNode.id.slice(0, 10)}</span>
            </div>
            {focusNode.status === "running" && focusHbAge != null ? (
              <div className="fleet-led">
                <span className="fleet-led-dot" />
                ♥ {fmtSeconds(focusHbAge)}
              </div>
            ) : (
              <div className="fleet-led" style={{ color: "var(--fleet-t3)", boxShadow: "none" }}>
                {focusNode.status}
              </div>
            )}
          </div>

          <div className="fleet-wk-task" style={{ WebkitLineClamp: 3, fontSize: 13 }}>
            {focusNode.title}
          </div>

          {focusNode.latest_run?.profile ? (
            <div className="fleet-wk-note">
              {focusNode.latest_run.profile}
              {focusNode.latest_run.heartbeat_age_seconds != null
                ? ` · ♥ ${fmtSeconds(focusNode.latest_run.heartbeat_age_seconds)}`
                : ""}
            </div>
          ) : null}

          {(() => {
            const rp = focusNode.status === "running" ? focusNode.latest_run?.run_progress : null;
            const effective =
              rp != null ? rp
              : focusNode.progress && focusNode.progress.total > 0
                ? focusNode.progress.done / focusNode.progress.total
              : focusNode.status === "running" ? 0.58 : 0;
            const estimated = rp == null;
            return (
              <div className="fleet-rail" title={estimated ? "Fortschritt geschätzt (DAG/Heuristik)" : "Fortschritt (Runtime-Cap)"}>
                <div className="fleet-rail-fill" style={{ width: `${Math.round(effective * 100)}%` }} />
              </div>
            );
          })()}

          <div className="fleet-wk-meta">
            {focusNode.latest_run?.profile ? <b>{focusNode.latest_run.profile.replace(/^claude-/, "").split("-")[0] ?? focusNode.latest_run.profile}</b> : null}
            {(focusNode.input_tokens > 0 || focusNode.output_tokens > 0) ? (
              <span>{fmtTokens(focusNode.input_tokens)} → {fmtTokens(focusNode.output_tokens)} tok</span>
            ) : null}
            <span className="fleet-meta-right">
              {focusNode.status === "running" ? "Karte läuft" : `Karte: ${focusNode.status}`}
              {focusNode.latest_run?.runtime_seconds != null
                ? ` seit ${fmtSeconds(focusNode.latest_run.runtime_seconds)}`
                : ""}
            </span>
          </div>
        </button>
      ) : null}

      {/* Danach-Queue */}
      {openNodes.length > 0 ? (
        <div className="fleet-danach">
          {openNodes.slice(0, 3).map((n, i) => (
            <button
              key={n.id}
              type="button"
              className="fleet-q text-left"
              onClick={() => onOpenNodeDetail(n.id, nodes)}
              aria-label={`Node ${n.title} öffnen`}
            >
              <span className="fleet-q-idx">{done + 1 + i + 1}</span>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {n.title}
              </span>
              <span className="fleet-q-assignee">{n.assignee ?? "—"}</span>
            </button>
          ))}
        </div>
      ) : null}

      {/* Fertig-Gruppe (kollabiert) */}
      {doneNodes.length > 0 ? (
        <div className="fleet-fertig-grp">
          <button
            type="button"
            className="fleet-f-row"
            style={{ fontWeight: 600, color: "var(--fleet-t3)", opacity: 1, fontSize: 10.5 }}
            onClick={() => setFertigOpen((v) => !v)}
            aria-expanded={fertigOpen}
          >
            <span className="fleet-f-ok" aria-hidden="true">✓</span>
            <span style={{ flex: 1 }}>{de.fleet.kettenFertigGruppe} ({doneNodes.length})</span>
            <span style={{ fontFamily: "var(--hc-font-mono)", fontSize: 10 }}>
              {fertigOpen ? "▲" : "▼"}
            </span>
          </button>
          {fertigOpen ? doneNodes.map((n) => (
            <button
              key={n.id}
              type="button"
              className="fleet-f-row"
              onClick={() => onOpenNodeDetail(n.id, nodes)}
              aria-label={`Node ${n.title} öffnen`}
            >
              <span className="fleet-f-ok" aria-hidden="true">✓</span>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {n.title}
              </span>
              <span className="fleet-f-meta">
                {n.cost_usd > 0 ? fmtUsd(n.cost_usd) : null}
                {n.latest_run?.runtime_seconds != null
                  ? ` · ${fmtSeconds(n.latest_run.runtime_seconds)}`
                  : ""}
              </span>
            </button>
          )) : null}
        </div>
      ) : null}

      {/* Gate-Ziellinie */}
      <div className="fleet-ziel">
        <span className="fleet-ziel-raute" aria-hidden="true" />
        Release-Gate · {gateLabel}
        <span className="fleet-ziel-meta">{gateMeta}</span>
      </div>
    </>
  );
}
