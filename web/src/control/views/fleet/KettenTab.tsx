/**
 * Ketten-Subtab — Redesign nach Mockup B v4.
 *
 * 6 Sektionen: Ketten-Liste, Active-Chain-Header, Step-Pipeline,
 * Active-Step-Detail (mit Model-Row + GGFM-Override-Badge), Upcoming-Steps,
 * Done + Gate-Teaser.
 *
 * Join: useHermesWorkers() → worker.task_id === node.id für persistierte
 * Modellroute, Override-Hinweis, Heartbeat, Run-Fortschritt und ETA.
 *
 * Die Modellroute stammt ausschließlich aus dem konkreten task_runs-Datensatz.
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
import { fetchJSON } from "@/lib/api";
import { de } from "../../i18n/de";
import { useChainGraph } from "../../hooks/chainFlow";
import { useHermesChainCosts } from "../../hooks/costsUsage";
import { useHermesReviewVerdicts } from "../../hooks/reviewVerdicts";
import { useHermesWorkers } from "../../hooks/workersBoard";
import type { BoardResponse, BoardTask, ChainStationSummary, ChainSummary, Worker } from "../../lib/types";
import { type ChainChipDef } from "../../lib/fleetHub";
import type { ChainCostsResponse } from "../../lib/schemas";
import { FleetSourceFreshness } from "./FleetSourceFreshness";
import { type ChainNode } from "./shared";
import { ModelRouteBadge } from "../../components/fleet/ModelRouteBadge";

import "./ketten-v4.css";

// ─── Ketten-Subtab ────────────────────────────────────────────────────────────

// ── Laufkarte (LK-1, Design-Board c_ee80d956 / e_a4b27b43) ───────────────────
// Eine Kette rendert als zusammenhängender Block mit ihren Stationen
// untereinander — nicht als einzelne Zeile in einer flachen Liste (AC-1).
// Datenquelle ist die Stationsliste aus dem Ketten-Lesevertrag
// (PlanSpec 2026-07-27-ketten-lesevertrag-backend.md, LV-2). Liefert die
// Zusammenfassung die Liste noch nicht, degradiert die Karte sichtbar auf
// Kennung, Zustand und Fortschritt — sie zeigt nie erfundene Stationen.

type LaufChainState = "laeuft" | "angebrochen" | "gehalten" | "wartet" | "fertig";

/**
 * LK-2 AC-1: Sichtbarkeit folgt dem Bedarf an Aufmerksamkeit, nicht dem Alter.
 * angebrochen (höchste Aufmerksamkeit) → laeuft → gehalten → wartet → fertig.
 */
const LAUF_ATTENTION_RANK: Record<LaufChainState, number> = {
  angebrochen: 0,
  laeuft: 1,
  gehalten: 2,
  wartet: 3,
  fertig: 4,
};

/** Sichtbare Stationen, bevor die Kürzungszeile mit der Gesamtzahl greift (AC-6). */
const LAUF_VISIBLE_STATIONS = 4;

/** LV-1-Backend-Zustand gewinnt; sonst Fallback aus dem Chip-Zustand. */
function deriveChainState(chip: ChainChipDef, summary: ChainSummary | undefined): LaufChainState {
  const s = summary?.state;
  if (s === "laeuft" || s === "angebrochen" || s === "gehalten" || s === "fertig") return s;
  switch (chip.state) {
    case "completed": return "fertig";
    case "active": return "laeuft";
    case "blocked":
    case "held": return chip.done > 0 ? "angebrochen" : "gehalten";
    default: return chip.done > 0 ? "angebrochen" : "wartet";
  }
}

/** Zustandszeile in Worten: Zustand UND Fortschritt, nie nur Farbe (AC-4). */
function laufEyebrow(state: LaufChainState, done: number, total: number): string {
  switch (state) {
    case "laeuft": return de.fleet.laufEyebrowLaeuft(Math.min(done + 1, Math.max(total, 1)), total);
    case "angebrochen": return de.fleet.laufEyebrowAngebrochen(done, total);
    case "gehalten": return done > 0 ? de.fleet.laufEyebrowGehaltenDurch(done, total) : de.fleet.laufEyebrowGehaltenLeer;
    case "fertig": return de.fleet.laufEyebrowFertig(done, total);
    default: return de.fleet.laufEyebrowWartetLeer;
  }
}

/** Age-Format der Referenz: „6 min“, „14 h“, „3 Tagen“. */
function fmtChainAge(seconds: number): string {
  if (seconds < 90) return de.fleet.laufAgeGeradeEben;
  const min = Math.round(seconds / 60);
  if (min < 60) return `${min} min`;
  const h = Math.round(min / 60);
  if (h < 48) return `${h} h`;
  return `${Math.round(h / 24)} Tagen`;
}

/** Stationszustand → Segment-Klasse. Unbekanntes degradiert zu „offen“. */
function stationStateClass(state: ChainStationSummary["state"] | undefined): "is-done" | "is-now" | "is-wait" | "is-held" {
  switch (state) {
    case "fertig": return "is-done";
    case "laeuft": return "is-now";
    case "gehalten": return "is-held";
    default: return "is-wait";
  }
}

interface LaufkarteProps {
  chip: ChainChipDef;
  summary: ChainSummary | undefined;
  selected: boolean;
  now: number;
  readOnly: boolean;
  releaseBusy: boolean;
  releaseError: string | null;
  onSelect: () => void;
  onRelease: () => void;
}

function Laufkarte({ chip, summary, selected, now, readOnly, releaseBusy, releaseError, onSelect, onRelease }: LaufkarteProps) {
  const state = deriveChainState(chip, summary);
  const kennung = summary?.kennung?.trim() ? summary.kennung.trim() : chip.label;
  const titel = summary && summary.root_title.trim() && summary.root_title.trim() !== kennung
    ? summary.root_title.trim()
    : null;
  const stations = Array.isArray(summary?.stations) ? summary.stations : null;
  const stationsTotal = summary?.stations_total ?? stations?.length ?? 0;
  const visibleStations = stations ? stations.slice(0, LAUF_VISIBLE_STATIONS) : [];
  const hiddenStations = Math.max(0, stationsTotal - visibleStations.length);

  const footParts: string[] = [];
  const ageSeconds = summary?.state_age_seconds ?? null;
  if (ageSeconds != null) {
    const age = fmtChainAge(ageSeconds);
    footParts.push(
      state === "laeuft" ? de.fleet.laufAgeLaeuft(age)
        : state === "gehalten" ? de.fleet.laufAgeWartet(age)
          : de.fleet.laufAgeSteht(age),
    );
  }
  if (state === "laeuft" && summary?.total_cost_usd != null) footParts.push(fmtUsd(summary.total_cost_usd));

  // AC-7: der einzige Startknopf am Block wirkt auf die ganze Kette; es gibt
  // keine Möglichkeit, eine einzelne Station zu starten.
  const counts = summary?.status_counts;
  const heldCount = (counts?.blocked ?? 0) + (counts?.parked ?? 0) + (counts?.scheduled ?? 0);
  const runningCount = counts?.running ?? 0;
  const canRelease = !readOnly && (summary
    ? heldCount > 0 && runningCount === 0
    : chip.state === "blocked" || chip.state === "held");

  return (
    <article className={`lk-card${selected ? " lk-card-active" : ""}`} data-state={state}>
      <button type="button" className="lk-select" onClick={onSelect} aria-pressed={selected}>
        <p className="lk-eyeb"><span className="lk-led" aria-hidden="true" />{laufEyebrow(state, chip.done, chip.total)}</p>
        <span className="lk-kennung" title={kennung}>{kennung}</span>
        {titel ? <span className="lk-titel" title={titel}>{titel}</span> : null}
      </button>
      {stations ? (
        <ol className="lk-stationen">
          {visibleStations.map((st) => {
            const stClass = stationStateClass(st.state);
            const meta: { text: string; em?: boolean }[] = [];
            if (st.lane) meta.push({ text: st.lane });
            if (stClass === "is-done") {
              if (st.runtime_seconds != null) meta.push({ text: fmtSeconds(st.runtime_seconds) });
              if (st.cost_usd != null) meta.push({ text: fmtUsd(st.cost_usd) });
            } else if (stClass === "is-now") {
              const runSecs = st.started_at != null ? Math.max(0, Math.round(now - st.started_at)) : st.runtime_seconds;
              meta.push({ text: runSecs != null ? de.fleet.laufStationLaeuft(fmtSeconds(runSecs)) : de.fleet.kettenStateActive, em: true });
            } else if (stClass === "is-held") {
              meta.push({ text: st.wait_reason?.trim() || de.fleet.laufStationWartet });
            } else {
              meta.push({ text: de.fleet.laufStationBereit });
            }
            return (
              <li key={st.id} className={`lk-st ${stClass}`}>
                <span className="lk-notch" aria-hidden="true"><b /></span>
                <span className="lk-name">{st.title}</span>
                <span className="lk-meta">
                  {meta.map((m) => <span key={m.text} className={m.em ? "lk-meta-em" : undefined}>{m.text}</span>)}
                </span>
                {stClass === "is-done" ? <span className="lk-stamp" aria-hidden="true">✓</span> : null}
              </li>
            );
          })}
          {hiddenStations > 0 ? (
            <li className="lk-more">{de.fleet.laufWeitere(hiddenStations, stationsTotal)}</li>
          ) : null}
        </ol>
      ) : null}
      {footParts.length > 0 || canRelease ? (
        <div className="lk-foot">
          <span className="lk-age">{footParts.join(" · ")}</span>
          {canRelease ? (
            <button type="button" className="lk-freigeben" onClick={onRelease} disabled={releaseBusy}>
              {releaseBusy ? de.fleet.laufFreigebenBusy : de.fleet.laufFreigeben}
            </button>
          ) : null}
        </div>
      ) : null}
      {releaseError ? <p className="lk-fehler" role="alert">{releaseError}</p> : null}
    </article>
  );
}

interface LaufDoneRowProps {
  chip: ChainChipDef;
  summary: ChainSummary | undefined;
  selected: boolean;
  now: number;
  onSelect: () => void;
}

/** Fertige Kette = abgestempelte Kompaktzeile (Referenz .done1). */
function LaufDoneRow({ chip, summary, selected, now, onSelect }: LaufDoneRowProps) {
  const kennung = summary?.kennung?.trim() ? summary.kennung.trim() : chip.label;
  const completedAt = summary?.latest_completed_at ?? chip.completedAt;
  const age = completedAt != null ? fmtChainAge(Math.max(0, Math.round(now - completedAt))) : null;
  return (
    <button
      type="button"
      className={`lk-done-row${selected ? " lk-done-row-active" : ""}`}
      onClick={onSelect}
      aria-pressed={selected}
    >
      <span className="lk-done-links">
        <span className="lk-done-led" aria-hidden="true" />
        <span className="lk-done-kennung" title={kennung}>{kennung}</span>
      </span>
      <span className="lk-done-meta">{chip.done}/{chip.total}{age ? ` · ${age}` : ""}</span>
    </button>
  );
}

interface KettenTabProps {
  board: BoardResponse | null;
  boardSlug?: string | null;
  workers?: Worker[];
  readOnly?: boolean;
  initialRootId: string | null;
  now: number;
  onOpenNodeDetail: (taskId: string, chainNodes?: ChainNode[]) => void;
  selectedNodeId?: string | null;
  detailControlsId?: string;
}

export function KettenTab({ board, boardSlug = null, workers, readOnly = false, initialRootId, now, onOpenNodeDetail, selectedNodeId = null, detailControlsId }: KettenTabProps) {
  const allBoardTasks: BoardTask[] = useMemo(() => (board?.columns ?? []).flatMap((c) => c.tasks), [board]);

  const chips = useMemo(
    () => buildChainChips(
      allBoardTasks.map((t) => ({
        id: t.id,
        title: t.title,
        root_id: t.root_id,
        status: t.status,
        completed_at: t.completed_at,
      })),
      board?.chain_summaries,
    ),
    [allBoardTasks, board?.chain_summaries],
  );

  const summaryByRoot = useMemo(() => {
    const m = new Map<string, ChainSummary>();
    for (const s of board?.chain_summaries ?? []) m.set(s.root_id, s);
    return m;
  }, [board?.chain_summaries]);

  // LK-2 AC-1: sichtbare Reihenfolge folgt dem Bedarf an Aufmerksamkeit, nicht
  // dem Alter — angebrochen zuerst, dann laufend, gehalten, wartend, fertig.
  // LK-2 AC-4: bei gleichem Zustand deterministisch (Abschlusszeit absteigend,
  // dann root_id), damit zwei Darstellungen derselben unveränderten Daten
  // dieselbe Reihenfolge liefern.
  const laufChips = useMemo(() => {
    const ordered = [...chips];
    ordered.sort((a, b) => {
      const rankDelta =
        LAUF_ATTENTION_RANK[deriveChainState(a, summaryByRoot.get(a.rootId))] -
        LAUF_ATTENTION_RANK[deriveChainState(b, summaryByRoot.get(b.rootId))];
      if (rankDelta !== 0) return rankDelta;
      const completedDelta = (b.completedAt ?? 0) - (a.completedAt ?? 0);
      if (completedDelta !== 0) return completedDelta;
      return a.rootId.localeCompare(b.rootId);
    });
    return ordered;
  }, [chips, summaryByRoot]);

  const angebrochenCount = laufChips.filter(
    (chip) => deriveChainState(chip, summaryByRoot.get(chip.rootId)) === "angebrochen",
  ).length;

  const [userSelectedRootId, setUserSelectedRootId] = useState<string | null>(initialRootId);

  // FIX-2: offene Ketten (angebrochen/laufend/gehalten/wartend) in
  // Aufmerksamkeits-Reihenfolge zeigen, fertige auf die jüngsten 3 cappen.
  const [completedExpanded, setCompletedExpanded] = useState(false);
  const activeOrPendingChips = laufChips.filter(
    (chip) => deriveChainState(chip, summaryByRoot.get(chip.rootId)) !== "fertig",
  );
  const completedChips = laufChips.filter(
    (chip) => deriveChainState(chip, summaryByRoot.get(chip.rootId)) === "fertig",
  );
  const visibleCompletedChips = completedExpanded ? completedChips : completedChips.slice(0, 3);
  const hiddenCompletedCount = completedChips.length - 3;

  // Auswahl folgt der Aufmerksamkeits-Ordnung: oberste offene Laufkarte,
  // sonst die zuletzt fertiggestellte.
  const selectedRootId = (userSelectedRootId && chips.some((chip) => chip.rootId === userSelectedRootId))
    ? userSelectedRootId
    : activeOrPendingChips[0]?.rootId ?? completedChips[0]?.rootId ?? null;

  // Board-Switch Race-Fix: fetch nur mit einer rootId, die auch im aktuellen Board
  // vorkommt. Solange der alte selectedRootId noch nicht auf den neuen Chip-Bestand
  // umgeschwenkt ist, liefern wir null und unterdrücken den 404-Fetch.
  const allBoardRootIds = useMemo(() => {
    const ids = new Set<string>();
    for (const t of allBoardTasks) if (t.root_id) ids.add(t.root_id);
    for (const summary of board?.chain_summaries ?? []) ids.add(summary.root_id);
    return ids;
  }, [allBoardTasks, board?.chain_summaries]);
  const validRootId = useMemo(() => {
    if (!selectedRootId) return null;
    return allBoardRootIds.has(selectedRootId) ? selectedRootId : null;
  }, [selectedRootId, allBoardRootIds]);

  const handleChipSelect = useCallback((rootId: string) => {
    setUserSelectedRootId(rootId);
  }, []);

  // AC-7: „Kette freigeben“ wirkt über das bestehende Flow-Release auf die
  // ganze Kette (dasselbe Backend wie die Freigabe im Plan-Reiter).
  const [releaseBusyRoot, setReleaseBusyRoot] = useState<string | null>(null);
  const [releaseError, setReleaseError] = useState<{ rootId: string; message: string } | null>(null);
  const handleChainRelease = useCallback(async (rootId: string) => {
    setReleaseBusyRoot(rootId);
    setReleaseError(null);
    try {
      const query = boardSlug ? `?board=${encodeURIComponent(boardSlug)}` : "";
      await fetchJSON(`/api/plugins/kanban/tasks/${encodeURIComponent(rootId)}/flow-release${query}`, {
        method: "POST",
        body: JSON.stringify({ release_level: "live" }),
      });
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      setReleaseError({ rootId, message: de.fleet.laufFreigebenFehler(detail) });
    } finally {
      setReleaseBusyRoot(null);
    }
  }, [boardSlug]);

  const chainGraphState = useChainGraph(validRootId, boardSlug);
  const { data: chainGraph, loading: chainLoading } = chainGraphState;
  const nodes = chainGraph?.nodes ?? [];

  const chainCosts = useHermesChainCosts(validRootId, boardSlug);
  const verdicts = useHermesReviewVerdicts(boardSlug);

  // === Worker-Join (v4): join ChainNode → Worker via task_id ===
  const workersState = useHermesWorkers();
  const workersData = workersState.data;
  const workerByNodeId = useMemo(() => {
    const m = new Map<string, Worker>();
    const ws = workers ?? workersData?.workers ?? [];
    for (const w of ws) {
      if (w.task_id) m.set(w.task_id, w);
    }
    return m;
  }, [workers, workersData]);

  const freshness = (
    <FleetSourceFreshness sources={[
      { label: "Kettengraph", ...chainGraphState },
      { label: "Kettenkosten", ...chainCosts },
      { label: "Review-Signale", ...verdicts },
      { label: "Worker (Kette)", ...workersState },
    ]} />
  );

  // LK-2 AC-6: Ladend, leer und Fehler sind drei visuell unterscheidbare
  // Zustände. Der Ladezustand behält die Kettengeometrie (Karte mit Notch-
  // Stationen), damit die Liste beim Eintreffen der Daten nicht springt.
  if (board == null) {
    return (
      <div className="ketten-v4">
        {freshness}
        <div className="chain-list-header">
          <span className="section-title">{de.fleet.laufkartenTitle}</span>
          <span className="section-count" aria-hidden="true">···</span>
        </div>
        <div className="lk-list lk-list-loading" role="status" aria-label={de.fleet.kettenLaden}>
          {[0, 1, 2].map((n) => (
            <div className="lk-card lk-skeleton" data-state="wartet" aria-hidden="true" key={n}>
              <p className="lk-eyeb"><span className="lk-led" /><span className="lk-sk lk-sk-eyeb" /></p>
              <span className="lk-sk lk-sk-kennung" />
              <span className="lk-sk lk-sk-titel" />
              <ol className="lk-stationen">
                {[0, 1, 2].map((s) => (
                  <li className="lk-st is-wait" key={s}>
                    <span className="lk-notch"><b /></span>
                    <span className="lk-sk lk-sk-name" />
                    <span className="lk-sk lk-sk-meta" />
                  </li>
                ))}
              </ol>
              <span className="lk-sk lk-sk-foot" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  // LK-2 AC-5: Leerzustand nach Doktrin — Situation, Bewertung, nächste Aktion.
  // Eine leere Kettenliste ist kein Erfolgszustand und rendert ohne ok-Grün.
  if (chips.length === 0) {
    return (
      <div className="ketten-v4">
        {freshness}
        <div className="chain-list-header">
          <span className="section-title">{de.fleet.laufkartenTitle}</span>
          <span className="section-count">{de.fleet.laufKopfCount(0, 0)}</span>
        </div>
        <div className="kt-empty">
          <p className="kt-empty-title">{de.fleet.kettenLeer}</p>
          <p className="kt-empty-sub">{de.fleet.kettenLeerDesc}</p>
          <p className="kt-empty-action">{de.fleet.kettenLeerAktion}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="ketten-v4">
      {freshness}
      {/* ── SECTION 1: Ketten-Liste als Laufkarten ─────────────────────────── */}
      <div className="chain-list-header">
        <span className="section-title">{de.fleet.laufkartenTitle}</span>
        {/* LK-2 AC-3: Gesamtzahl und angebrochene getrennt, ohne Scrollen sichtbar. */}
        <span className="section-count">{de.fleet.laufKopfCount(chips.length, angebrochenCount)}</span>
      </div>
      <div className="lk-list">
        {activeOrPendingChips.map((chip) => (
          <Laufkarte
            key={chip.rootId}
            chip={chip}
            summary={summaryByRoot.get(chip.rootId)}
            selected={selectedRootId === chip.rootId}
            now={now}
            readOnly={readOnly}
            releaseBusy={releaseBusyRoot === chip.rootId}
            releaseError={releaseError?.rootId === chip.rootId ? releaseError.message : null}
            onSelect={() => handleChipSelect(chip.rootId)}
            onRelease={() => { void handleChainRelease(chip.rootId); }}
          />
        ))}
        {visibleCompletedChips.map((chip) => (
          <LaufDoneRow
            key={chip.rootId}
            chip={chip}
            summary={summaryByRoot.get(chip.rootId)}
            selected={selectedRootId === chip.rootId}
            now={now}
            onSelect={() => handleChipSelect(chip.rootId)}
          />
        ))}
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
          key={selectedRootId}
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
          readOnly={readOnly}
        />
      ) : selectedRootId && chainGraphState.error ? (
        <div className="kt-error" role="alert">
          <p className="kt-error-title">{de.fleet.kettenGraphFehler}</p>
          <p className="kt-error-sub">{chainGraphState.error}</p>
        </div>
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
  readOnly: boolean;
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
  readOnly,
}: KettenGraphV4Props) {
  const { pct, done, total } = chainProgress(nodes);
  const focusNode = pickFocusNode(nodes);

  // === Chain costs (server-side rollup) ===
  const costTotals = chainCosts?.totals;
  const costTokens = costTotals ? costTotals.input_tokens + costTotals.output_tokens : 0;
  const rawCostText = costTotals
    ? formatEffectiveCost({ cost_usd: costTotals.cost_usd ?? 0, cost_effective_usd: costTotals.cost_effective_usd ?? 0, tokens: costTokens }).text
    : chainCostsLoading ? "…" : "—";
  const realCostPartial = Boolean(
    costTotals
    && costTotals.run_count > 0
    && (
      costTotals.cost_usd_state !== "complete"
      || costTotals.cost_usd_total_runs == null
      || costTotals.cost_usd_known_runs == null
      || costTotals.cost_usd_known_runs !== costTotals.cost_usd_total_runs
    ),
  );
  const equivalentCostPartial = Boolean(
    costTotals
    && (
      (costTotals.cost_usd_equivalent_candidate_runs ?? 0) > 0
      || costTotals.cost_usd_equivalent != null
    )
    && (
      costTotals.cost_usd_equivalent_state !== "complete"
      || costTotals.cost_usd_equivalent_candidate_runs == null
      || costTotals.cost_usd_equivalent_known_runs == null
      || costTotals.cost_usd_equivalent_known_runs !== costTotals.cost_usd_equivalent_candidate_runs
    ),
  );
  const costText = (realCostPartial || equivalentCostPartial) && rawCostText !== "—"
    ? `≥${rawCostText}`
    : rawCostText;

  const chainInputTokens = costTotals?.input_tokens ?? 0;
  const chainOutputTokens = costTotals?.output_tokens ?? 0;

  // === Focus node worker join ===
  const focusWorker = focusNode ? workerByNodeId.get(focusNode.id) ?? null : null;
  const focusRoute = focusWorker ?? focusNode?.latest_run ?? null;
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
  const [showAllUpcoming, setShowAllUpcoming] = useState(false);
  const [showAllDone, setShowAllDone] = useState(false);
  const visibleUpcomingNodes = upcomingNodes.slice(0, showAllUpcoming ? undefined : 20);
  const visibleDoneNodes = doneNodes.slice(0, showAllDone ? undefined : 20);

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
            {focusNode?.status === "running"
              ? de.fleet.kettenStateActive
              : focusNode?.status === "blocked"
                ? de.fleet.kettenStateBlocked
                : done >= total
                  ? de.fleet.kettenStateCompleted
                  : de.fleet.kettenStatePending}
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
                // run-spezifische Telemetrie; niemals Rollenname als Modell.
                const roleLabel = node.assignee ?? node.latest_run?.profile ?? "—";
                const nodeRoute = worker ?? node.latest_run;

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
                    <div className={`pstep-sub ${isRunning ? "pstep-sub-active" : ""}`}>
                      {nodeRoute ? (
                        <ModelRouteBadge
                          requestedProvider={nodeRoute.requested_provider}
                          requestedModel={nodeRoute.requested_model}
                          activeProvider={nodeRoute.active_provider}
                          activeModel={nodeRoute.active_model}
                          modelState={nodeRoute.model_state}
                          modelSource={nodeRoute.model_source}
                          observedAt={nodeRoute.model_observed_at}
                        />
                      ) : (
                        <span className="text-micro text-ink-3">{de.worker.modelRouteNotStarted}</span>
                      )}
                    </div>
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
          disabled={readOnly}
          aria-label={readOnly ? `Node ${focusNode.title} · nur lesen` : `Node ${focusNode.title} öffnen`}
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

          <div className="detail-title" title={focusNode.title}>{focusNode.title}</div>

          {/* === Model-Row with GGFM Override Badge (v4) === */}
          <div className="model-row">
            <span className="model-icon">⚙</span>
            {focusRoute ? (
              <ModelRouteBadge
                requestedProvider={focusRoute.requested_provider}
                requestedModel={focusRoute.requested_model}
                activeProvider={focusRoute.active_provider}
                activeModel={focusRoute.active_model}
                modelState={focusRoute.model_state}
                modelSource={focusRoute.model_source}
                observedAt={focusRoute.model_observed_at}
              />
            ) : (
              <span className="text-micro text-ink-3">{de.worker.modelRouteNotStarted}</span>
            )}
            {focusModelOverride ? (
              <span className="model-override-badge" title={`Override: ${focusModelOverride}`}>
                GGFM Override
              </span>
            ) : null}
          </div>

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
            <div className="values-row">
              {focusRuntime != null ? (
                <span className="metric">
                  <span className="metric-label">Laufzeit</span>
                  <span className="val val-strong">{fmtDurationClock(focusRuntime)}</span>
                </span>
              ) : null}
              {focusRuntime != null && (focusNode.input_tokens > 0 || focusNode.output_tokens > 0) ? (
                <span className="val-sep">·</span>
              ) : null}
              {focusNode.input_tokens > 0 || focusNode.output_tokens > 0 ? (
                <span className="metric">
                  <span className="metric-label">Tokens</span>
                  <span className="val">
                    {fmtTokens(focusNode.input_tokens)} ↓ {fmtTokens(focusNode.output_tokens)} tok
                  </span>
                </span>
              ) : null}
              {focusEtaP50 != null ? (
                <>
                  <span className="val-sep">·</span>
                  <span className="metric">
                    <span className="metric-label">ETA</span>
                    <span className="val val-live">p50~{fmtSeconds(focusEtaP50)}</span>
                  </span>
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
          {visibleUpcomingNodes.map((n) => {
            const worker = workerByNodeId.get(n.id);
            const route = worker ?? n.latest_run;
            const hasOverride = worker?.model_override != null;
            return (
              <button
                key={n.id}
                type="button"
                className={`uitem${n.status === "blocked" ? " uitem-blocked" : ""}${selectedNodeId === n.id ? " uitem-selected" : ""}`}
                onClick={() => onOpenNodeDetail(n.id, nodes)}
                disabled={readOnly}
                aria-label={readOnly ? `Node ${n.title} · nur lesen` : `Node ${n.title} öffnen`}
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
                  <div className={`umodel ${hasOverride ? "umodel-override" : ""}`}>
                    {route ? (
                      <ModelRouteBadge
                        requestedProvider={route.requested_provider}
                        requestedModel={route.requested_model}
                        activeProvider={route.active_provider}
                        activeModel={route.active_model}
                        modelState={route.model_state}
                        modelSource={route.model_source}
                        observedAt={route.model_observed_at}
                      />
                    ) : (
                      <span className="text-micro text-ink-3">{de.worker.modelRouteNotStarted}</span>
                    )}
                  </div>
                  <div className="utitle" title={n.title}>{n.title}</div>
                </div>
                <div className={`uwait${n.status === "blocked" ? " uwait-blocked" : ""}`}>
                  {n.status === "blocked" ? "blockiert" : "wartet"}
                </div>
              </button>
            );
          })}
          {!showAllUpcoming && upcomingNodes.length > 20 ? (
            <button type="button" className="fleet-list-expander" onClick={() => setShowAllUpcoming(true)}>
              Weitere Upcoming anzeigen ({upcomingNodes.length - 20})
            </button>
          ) : null}
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
          {doneExpanded ? visibleDoneNodes.map((n) => (
            <button
              key={n.id}
              type="button"
              className={`done-item${selectedNodeId === n.id ? " done-item-selected" : ""}`}
              onClick={() => onOpenNodeDetail(n.id, nodes)}
              disabled={readOnly}
              aria-label={readOnly ? `Node ${n.title} · nur lesen` : `Node ${n.title} öffnen`}
              aria-expanded={selectedNodeId === n.id}
              aria-controls={detailControlsId}
            >
              <span className="davatar">✓</span>
              <div className="dcontent">
                <div className="dtitle" title={n.title}>{n.title}</div>
                <div className="dtime">
                  {n.cost_usd > 0 ? fmtUsd(n.cost_usd) : null}
                  {n.cost_usd > 0 && n.latest_run?.runtime_seconds != null ? " · " : ""}
                  {n.latest_run?.runtime_seconds != null ? fmtSeconds(n.latest_run.runtime_seconds) : ""}
                </div>
              </div>
            </button>
          )) : null}
          {doneExpanded && !showAllDone && doneNodes.length > 20 ? (
            <button type="button" className="fleet-list-expander" onClick={() => setShowAllDone(true)}>
              Weitere fertige Schritte anzeigen ({doneNodes.length - 20})
            </button>
          ) : null}
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
