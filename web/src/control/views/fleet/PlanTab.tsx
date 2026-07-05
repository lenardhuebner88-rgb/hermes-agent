/**
 * Plan-Cockpit (Freigabe): PlanTab + PlanSpec-Cockpit + Modell-Select + Token-Budget-Block.
 *
 * Aus FleetView.tsx extrahiert — reine Zerlegung, kein Verhalten geändert.
 */
import { useState, useMemo, useRef, useEffect } from "react";
import {
  fmtUsd,
  planSpecAwaitsPlanAction,
  planSpecHasParkedSignedChain,
  budgetTone,
  derivePlanLanes,
  buildApproveRequest,
  fmtResetAt,
  normalizeUsageWindowLabel,
  deriveEffectivePlanPath,
} from "../../lib/fleetHub";
import { de } from "../../i18n/de";
import { usePlanSpecDetail } from "../../hooks/useControlData";
import type { RunsCostsResponse, LanesCatalogResponse } from "../../lib/schemas";
import { PlanComposer } from "../../components/fleet/PlanComposer";
import { fetchJSON } from "@/lib/api";
import type { PlanSpecRecord } from "./shared";

// ─── Plan-Cockpit (Freigabe) ──────────────────────────────────────────────────

interface PlanTabProps {
  allPlanspecs: PlanSpecRecord[];
  costs: RunsCostsResponse | null;
  lanesCatalog: LanesCatalogResponse | null;
  accountUsage: import("../../lib/types").AccountUsageResponse | null;
  onApproveSuccess: () => void;
  onShowDetail: (ps: PlanSpecRecord) => void;
}

export function PlanTab({ allPlanspecs, costs, lanesCatalog, accountUsage, onApproveSuccess, onShowDetail }: PlanTabProps) {
  // PlanSpecs, die Operator-Freigabe oder den Start einer signierten, geparkten Kette brauchen.
  const pendingSpecs = allPlanspecs.filter((ps) => planSpecAwaitsPlanAction(ps));
  const pendingPaths = pendingSpecs.map((ps) => ps.path);

  // selectedPath hält nur die aktive User-Wahl; effectivePath wird ABGELEITET:
  // fällt der gespeicherte Pfad nach Approve/Reload aus pendingPaths heraus,
  // springt die Auswahl automatisch auf den nächsten wartenden Eintrag.
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const effectivePath = deriveEffectivePlanPath(selectedPath, pendingPaths);
  const selectedSpec = pendingSpecs.find((ps) => ps.path === effectivePath) ?? null;

  if (pendingSpecs.length === 0) {
    return (
      <>
        <PlanComposer onIngestSuccess={onApproveSuccess} />
        <div className="fleet-empty">
          <p className="fleet-empty-title">{de.fleet.planLeer}</p>
          <p className="fleet-empty-sub">{de.fleet.planLeerDesc}</p>
        </div>
      </>
    );
  }

  return (
    <>
      <PlanComposer onIngestSuccess={onApproveSuccess} />

      {/* Liste wartender PlanSpecs — wenn mehr als eine, als auswählbare Chips */}
      {pendingSpecs.length > 1 ? (
        <div className="fleet-kchips" style={{ marginBottom: 4 }}>
          {pendingSpecs.map((ps) => (
            <button
              key={ps.path}
              type="button"
              className={`fleet-kchip${effectivePath === ps.path ? " fleet-kchip-on" : ""}`}
              onClick={() => setSelectedPath(ps.path)}
              aria-pressed={effectivePath === ps.path}
            >
              {(ps.topic || ps.filename).length > 22
                ? (ps.topic || ps.filename).slice(0, 22) + "…"
                : (ps.topic || ps.filename)}
            </button>
          ))}
        </div>
      ) : null}

      {selectedSpec ? (
        <PlanSpecCockpit
          // key remountet das Cockpit pro Spec — sonst überlebt lokaler State
          // (approveState='success', injectScout, Lane-Wahl) den Sprung auf den
          // nächsten wartenden Spec und sperrt dessen Freigabe-Button.
          key={selectedSpec.path}
          ps={selectedSpec}
          costs={costs}
          lanesCatalog={lanesCatalog}
          accountUsage={accountUsage}
          onApproveSuccess={() => {
            // Nach Approve: gespeicherten Pfad zurücksetzen → Ableitung
            // springt automatisch auf den nächsten wartenden Eintrag.
            setSelectedPath(null);
            onApproveSuccess();
          }}
          onHold={() => setSelectedPath(null)}
          onShowDetail={onShowDetail}
        />
      ) : null}
    </>
  );
}

// ─── PlanSpec-Cockpit (eine PlanSpec freigeben) ────────────────────────────────

interface PlanSpecCockpitProps {
  ps: PlanSpecRecord;
  costs: RunsCostsResponse | null;
  lanesCatalog: LanesCatalogResponse | null;
  accountUsage: import("../../lib/types").AccountUsageResponse | null;
  onApproveSuccess: () => void;
  onHold: () => void;
  onShowDetail: (ps: PlanSpecRecord) => void;
}

function PlanSpecCockpit({ ps, costs, lanesCatalog, accountUsage, onApproveSuccess, onHold, onShowDetail }: PlanSpecCockpitProps) {
  const isSignedParkedChain = planSpecHasParkedSignedChain(ps);
  // PlanSpec-Detail (subtasks mit lane) laden
  const detail = usePlanSpecDetail(ps.path);

  // Lane-Konfiguration ableiten
  const lanes = useMemo(() => {
    if (detail.data?.subtasks) {
      return derivePlanLanes(detail.data.subtasks);
    }
    return [];
  }, [detail.data]);

  // Preset-Defaults je Lane aus lanesCatalog
  const presetDefaults = useMemo<Record<string, string>>(() => {
    const profiles = lanesCatalog?.profiles ?? [];
    const result: Record<string, string> = {};
    for (const p of profiles) {
      if (p.name && p.default_model) {
        result[p.name] = p.default_model;
      }
    }
    return result;
  }, [lanesCatalog]);

  // Modell-Optionen je Lane
  const modelOptions = lanesCatalog?.models ?? [];

  // Lokaler Zustand: Modell-Auswahl je Lane (initial = Preset-Default)
  // Reset wenn sich lanes ändern (neue PlanSpec ausgewählt)
  const [laneModels, setLaneModels] = useState<Record<string, string>>(() => {
    return {};
  });

  // Scout-Toggle (Default: aus)
  const [injectScout, setInjectScout] = useState(false);

  // Freigabe-State
  const [approveState, setApproveState] = useState<"idle" | "busy" | "success" | "error">("idle");
  const [approveError, setApproveError] = useState<string | null>(null);
  const [releaseArmed, setReleaseArmed] = useState(false);
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  async function handleApprove() {
    if (!ps.kanban_root_task_id) return;
    setApproveState("busy");
    setApproveError(null);
    const body = buildApproveRequest(
      ps.kanban_root_task_id,
      // Merge lokale Auswahl über Presets
      { ...presetDefaults, ...laneModels },
      presetDefaults,
      injectScout,
    );
    try {
      await fetchJSON<unknown>("/api/plugins/kanban/planspecs/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!aliveRef.current) return;
      setApproveState("success");
      // Kurze Pause, dann Callback
      window.setTimeout(() => {
        if (aliveRef.current) onApproveSuccess();
      }, 600);
    } catch (e: unknown) {
      if (!aliveRef.current) return;
      setApproveState("error");
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes("409")) {
        setApproveError(de.fleet.planFreigebenFehler409);
      } else if (msg.includes("404")) {
        setApproveError(de.fleet.planFreigebenFehler404);
      } else {
        setApproveError(de.fleet.planFreigebenFehlerUnbekannt);
      }
    }
  }

  async function handleChainStart() {
    if (!ps.kanban_root_task_id) return;
    if (!releaseArmed) {
      setReleaseArmed(true);
      window.setTimeout(() => {
        if (aliveRef.current) setReleaseArmed(false);
      }, 4000);
      return;
    }
    setApproveState("busy");
    setApproveError(null);
    try {
      await fetchJSON<unknown>(`/api/plugins/kanban/tasks/${encodeURIComponent(ps.kanban_root_task_id)}/flow-release`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ release_level: "live" }),
      });
      if (!aliveRef.current) return;
      setApproveState("success");
      window.setTimeout(() => {
        if (aliveRef.current) onApproveSuccess();
      }, 600);
    } catch (e: unknown) {
      if (!aliveRef.current) return;
      setApproveState("error");
      const msg = e instanceof Error ? e.message : String(e);
      setApproveError(msg.includes("404") ? de.fleet.planFreigebenFehler404 : de.fleet.planKetteStartenFehler);
    } finally {
      if (aliveRef.current) setReleaseArmed(false);
    }
  }

  // Worktree-Isolation: nur anzeigen wenn Feld existiert
  const hasWorktreeField = detail.data != null && "worktree_isolation" in (detail.data as object);

  return (
    <>
      {/* Kopfkarte (amber) */}
      <div className="fleet-plan-kopf">
        <div className="fleet-plan-kopf-n">
          {ps.topic || ps.filename}
          <span className={`fleet-ps-badge ${isSignedParkedChain ? "fleet-ps-badge-ok" : "fleet-ps-badge-amber"}`} style={{ marginLeft: "auto" }}>
            {isSignedParkedChain ? "signiert · geparkt" : "freigabe: operator"}
          </span>
        </div>
        {detail.data?.goal ? (
          <div className="fleet-plan-kopf-sub">{detail.data.goal}</div>
        ) : null}
        <div className="fleet-plan-kopf-meta">
          {ps.kanban_child_total > 0 ? (
            <span>{de.fleet.kartenGeplant(ps.kanban_child_total)}</span>
          ) : ps.subtask_count > 0 ? (
            <span>{de.fleet.kartenGeplant(ps.subtask_count)}</span>
          ) : null}
          {ps.binding ? <span>binding</span> : null}
          {ps.freigabe ? <span>freigabe: {ps.freigabe}</span> : null}
          <button
            type="button"
            className="fleet-plan-volltext-btn"
            onClick={() => onShowDetail(ps)}
            aria-label="PlanSpec-Volltext öffnen"
          >
            Volltext
          </button>
        </div>
      </div>

      {/* Lane-Konfiguration */}
      {!isSignedParkedChain && lanes.length > 0 ? (
        <div className="fleet-lane-cfg">
          {lanes.map(({ lane, description }) => {
            const currentModel = laneModels[lane] ?? presetDefaults[lane] ?? "";
            const isChanged = laneModels[lane] != null && laneModels[lane] !== presetDefaults[lane];
            return (
              <div key={lane} className="fleet-lane-row">
                <span className="fleet-lane-ln">{lane}</span>
                <span className="fleet-lane-ld">{description.length > 30 ? description.slice(0, 30) + "…" : description}</span>
                <ModelSelect
                  lane={lane}
                  value={currentModel}
                  options={modelOptions}
                  changed={isChanged}
                  onChange={(model) => setLaneModels((prev) => ({ ...prev, [lane]: model }))}
                />
              </div>
            );
          })}
        </div>
      ) : null}

      {/* Toggles */}
      {!isSignedParkedChain ? (
      <div className="fleet-lane-cfg">
        {/* Scout vorab */}
        <div className="fleet-tgl-row">
          <span style={{ fontWeight: 600, fontSize: 12 }}>{de.fleet.planScoutVorab}</span>
          <span className="fleet-tgl-td">{de.fleet.planScoutDesc}</span>
          <button
            type="button"
            role="switch"
            aria-checked={injectScout}
            className={`fleet-switch${injectScout ? "" : " fleet-switch-aus"}`}
            style={{ minWidth: 40, minHeight: 40, display: "flex", alignItems: "center", justifyContent: "center" }}
            onClick={() => setInjectScout((v) => !v)}
            aria-label={de.fleet.planScoutVorab}
          />
        </div>

        {/* Live-Test: read-only Pill */}
        <div className="fleet-tgl-row">
          <span style={{ fontWeight: 600, fontSize: 12 }}>{de.fleet.planLiveTest}</span>
          <span className="fleet-tgl-td">{de.fleet.planLiveTestDesc}</span>
          {ps.live_test_depth ? (
            <span className="fleet-sel" style={{ pointerEvents: "none", opacity: 0.85 }}>
              {ps.live_test_depth}
            </span>
          ) : (
            <span style={{ fontFamily: "var(--hc-font-mono)", fontSize: 10, color: "var(--fleet-t3)" }}>—</span>
          )}
        </div>

        {/* Worktree-Isolation: nur wenn Feld existiert */}
        {hasWorktreeField ? (
          <div className="fleet-tgl-row" style={{ borderBottom: "none" }}>
            <span style={{ fontWeight: 600, fontSize: 12 }}>{de.fleet.planWorktreeIsoliert}</span>
            <span className="fleet-tgl-td">{de.fleet.planWorktreeDesc}</span>
            <span className="fleet-sel" style={{ pointerEvents: "none", opacity: 0.7 }}>
              {String((detail.data as Record<string, unknown>)["worktree_isolation"] ?? "—")}
            </span>
          </div>
        ) : null}
      </div>
      ) : (
        <div className="fleet-lane-cfg" data-testid="signed-chain-start-card">
          <div className="fleet-tgl-row" style={{ borderBottom: "none" }}>
            <span style={{ fontWeight: 600, fontSize: 12 }}>{de.fleet.planKetteSigniert}</span>
            <span className="fleet-tgl-td">{de.fleet.planKetteSigniertDesc}</span>
            <span className="fleet-sel" style={{ pointerEvents: "none", opacity: 0.85 }}>
              {ps.kanban_root_status || ps.kanban_state || "scheduled"}
            </span>
          </div>
        </div>
      )}

      {/* Token-Budget-Block */}
      <TokenBudgetBlock accountUsage={accountUsage} costs={costs} />

      {/* Fehler-Anzeige */}
{approveError ? <div className="fleet-plan-msg fleet-plan-msg-error">{approveError}</div> : null}

      {/* Erfolgs-Anzeige */}
      {approveState === "success" ? (
        <div className="fleet-plan-msg fleet-plan-msg-success">
          {isSignedParkedChain ? de.fleet.planKetteStartenErfolg : de.fleet.planFreigebenErfolg}
        </div>
      ) : null}

      {/* Aktions-Buttons */}
      <div className="fleet-actions">
        <button
          type="button"
          className={`fleet-btn ${isSignedParkedChain ? "fleet-btn-start" : "fleet-btn-frei"}`}
          style={{ flex: 2 }}
          onClick={() => void (isSignedParkedChain ? handleChainStart() : handleApprove())}
          disabled={approveState === "busy" || approveState === "success" || !ps.kanban_root_task_id}
          aria-busy={approveState === "busy"}
        >
          {approveState === "busy"
            ? (isSignedParkedChain ? de.fleet.planKetteStartenBusy : "Freigabe läuft …")
            : isSignedParkedChain
            ? (releaseArmed ? de.fleet.planKetteStartenConfirm : de.fleet.planKetteStarten)
            : de.fleet.planFreigeben}
        </button>
        <button
          type="button"
          className="fleet-btn"
          onClick={onHold}
          disabled={approveState === "busy"}
        >
          {de.fleet.planHalten}
        </button>
      </div>
    </>
  );
}

// ─── Modell-Select (je Lane) ──────────────────────────────────────────────────

interface ModelSelectProps {
  lane: string;
  value: string;
  options: LanesCatalogResponse["models"];
  changed: boolean;
  onChange: (model: string) => void;
}

function ModelSelect({ value, options, changed, onChange }: ModelSelectProps) {
  // Der Lanes-Katalog listet dieselbe Modell-ID mehrfach (je Provider) —
  // fürs <select> zählt nur die ID: erste Nennung gewinnt, sonst doppelte
  // Einträge + React-duplicate-key-Errors bei jedem Poll.
  const uniqueOptions = (options ?? []).filter(
    (o, i, arr) => arr.findIndex((x) => x.id === o.id) === i,
  );
  // Fallback: wenn keine Optionen, zeige freies Textfeld-ähnliches Display
  if (uniqueOptions.length === 0) {
    return (
      <span className="fleet-sel" style={{ opacity: 0.6 }}>{value || "—"}</span>
    );
  }

  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={`fleet-sel${changed ? " fleet-sel-puls" : ""}`}
      style={{
        background: "var(--fleet-karte)",
        border: `1px solid ${changed ? "rgba(55,224,255,.4)" : "var(--fleet-linie-stark)"}`,
        color: changed ? "var(--fleet-puls)" : "var(--fleet-t1)",
        borderRadius: 9,
        padding: "6px 9px",
        font: "500 11px var(--hc-font-mono)",
        cursor: "pointer",
        minHeight: 40,
        minWidth: 90,
      }}
      aria-label={`Modell für Lane ${value}`}
    >
      {value && !uniqueOptions.find((o) => o.id === value) ? (
        <option value={value}>{value}</option>
      ) : null}
      {uniqueOptions.map((o) => (
        <option key={o.id} value={o.id}>{o.label || o.id}</option>
      ))}
    </select>
  );
}

// ─── Token-Budget-Block ───────────────────────────────────────────────────────

function TokenBudgetBlock({
  accountUsage,
  costs,
}: {
  accountUsage: import("../../lib/types").AccountUsageResponse | null;
  costs: RunsCostsResponse | null;
}) {
  const providers = accountUsage?.providers ?? [];

  // Pro Provider eine Gruppe — nur Fenster mit verwertbarem used_percent;
  // Provider ohne einen einzigen solchen Fenster fallen ganz raus.
  const groups = providers
    .map((prov) => ({
      title: prov.title || prov.provider,
      plan: prov.plan,
      windows: prov.windows.filter((w) => w.used_percent != null),
    }))
    .filter((g) => g.windows.length > 0);

  return (
    <div className="fleet-budget-g">
      <div className="fleet-bg-head">
        <span className="fleet-bg-t">{de.fleet.planTokenBudget}</span>
      </div>

      {groups.length === 0 ? (
        <p style={{ font: "400 11px/1.4 var(--hc-font-sans)", color: "var(--fleet-t3)" }}>
          {de.fleet.planBudgetNichtVerfuegbar}
        </p>
      ) : (
        groups.map((group, gi) => {
          // Frühester reset_at dieser Gruppe (Provider kann mehrere Fenster mit
          // unterschiedlichen Reset-Zeiten haben, z.B. Session + Woche).
          const validResets = group.windows
            .map((w) => w.reset_at)
            .filter((r): r is string => Boolean(r))
            .map((r) => ({ raw: r, t: new Date(r).getTime() }))
            .filter((x) => !isNaN(x.t));
          const earliestReset = validResets.length > 0
            ? validResets.reduce((min, x) => (x.t < min.t ? x : min)).raw
            : null;

          return (
            <div key={gi} style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: gi > 0 ? 4 : 0 }}>
              <div className="fleet-bg-head">
                <span style={{ font: "500 9.5px/1 var(--hc-font-sans)", color: "var(--fleet-t3)", letterSpacing: "0.08em", textTransform: "uppercase" }}>
                  {group.title}
                  {group.plan ? <span style={{ textTransform: "none", letterSpacing: 0, opacity: 0.75 }}> · {group.plan}</span> : null}
                </span>
                {earliestReset ? (
                  <code style={{ fontFamily: "var(--hc-font-mono)", fontSize: 10, color: "var(--fleet-t3)" }}>
                    {de.fleet.planTokenReset(fmtResetAt(earliestReset))}
                  </code>
                ) : null}
              </div>

              {group.windows.map((w, i) => {
                const pct = w.used_percent ?? 0;
                const tone = budgetTone(w.used_percent);
                const barColor = tone === "danger"
                  ? "linear-gradient(90deg,color-mix(in srgb, var(--fleet-rot) 50%, transparent),var(--fleet-rot))"
                  : tone === "warn"
                  ? "linear-gradient(90deg,rgba(245,168,60,.4),var(--fleet-signal))"
                  : "linear-gradient(90deg,rgba(67,214,154,.5),var(--fleet-gruen))";

                return (
                  <div key={i} className="fleet-bg-row">
                    <span className="fleet-bg-bl">{normalizeUsageWindowLabel(w.label, w.window_key)}</span>
                    <div className="fleet-bg-bar">
                      <i style={{ width: `${Math.min(100, pct)}%`, background: barColor }} />
                    </div>
                    <span className="fleet-bg-bv" style={{
                      color: tone === "danger" ? "var(--fleet-rot)" : tone === "warn" ? "var(--fleet-signal)" : "var(--fleet-t1)",
                    }}>
                      {Math.round(pct)} %
                    </span>
                  </div>
                );
              })}
            </div>
          );
        })
      )}

      {/* Kosten heute + Woche */}
      <div style={{ display: "flex", gap: 12, marginTop: 4 }}>
        {costs?.today?.actual_cost_usd != null ? (
          <div className="fleet-kv" style={{ flex: 1 }}>
            <div className="fleet-kv-k">{de.fleet.planKostenHeute}</div>
            <div className="fleet-kv-v">{fmtUsd(costs.today.actual_cost_usd)}</div>
          </div>
        ) : null}
        {costs?.window?.actual_cost_usd != null ? (
          <div className="fleet-kv" style={{ flex: 1 }}>
            <div className="fleet-kv-k">{de.fleet.planKostenWoche}</div>
            <div className="fleet-kv-v">{fmtUsd(costs.window.actual_cost_usd)}</div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
