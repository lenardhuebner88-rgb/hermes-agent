import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { ChevronDown } from "lucide-react";

import {
  FALLBACK_MODELS,
  type EditorRow,
  type Lane,
  type LaneModelOption,
  type LanesResponse,
  applyChoice,
  editorRows,
  laneChoiceWarning,
  loadLanes,
  profilesFromEditorRows,
  smokeCheckLaneConfig,
  updateLane,
} from "../lanes/api";

type SaveState = "idle" | "loading" | "checking" | "saving";

function activeLane(data: LanesResponse | null): Lane | null {
  if (!data) return null;
  return data.lanes.find((lane) => lane.id === data.active_id) ?? data.lanes.find((lane) => lane.active) ?? null;
}

function laneRevision(lane: Lane): string {
  return `${lane.updated_at ?? "no-updated-at"}:${JSON.stringify(lane.profiles)}`;
}

function optionValue(option: LaneModelOption): string {
  return `${option.runtime}|${option.provider ?? ""}|${option.id}`;
}

function rowChoice(row: EditorRow): string {
  if (row.choice === "") return "";
  return `${row.worker_runtime}|${row.provider ?? ""}|${row.model ?? ""}`;
}

function modelsForSelect(models: LaneModelOption[]): LaneModelOption[] {
  return models.filter((model) => model.locked !== true && model.id.trim() !== "");
}

function rowForProfile(rows: EditorRow[], profile: string | null): EditorRow | null {
  if (!profile) return null;
  return rows.find((row) => row.profile === profile) ?? null;
}

function guardEntry(row: EditorRow) {
  return {
    worker_runtime: row.worker_runtime,
    provider: row.provider ?? row.defaultProvider ?? null,
    model: row.model,
  };
}

/** Effektives Modell-Label der aktiven Auswahl fürs Summary: Override-Modell,
 *  sonst der Profil-Default. Nur live vorhandene Werte, kein Fake. */
function effectiveModelLabel(row: EditorRow | null, models: LaneModelOption[]): string | null {
  if (!row) return null;
  if (row.model) {
    const match = models.find((m) => m.id === row.model && (m.provider ?? "") === (row.provider ?? ""));
    return match?.label ?? row.model;
  }
  return row.defaultLabel;
}

/** Effektiver Provider fürs Summary: Override- oder Default-Provider; auf
 *  claude-cli-Lanes (kein Provider) die Runtime als Kennung. null = nichts live. */
function effectiveProviderLabel(row: EditorRow | null): string | null {
  if (!row) return null;
  const provider = row.provider ?? row.defaultProvider;
  if (provider) return provider;
  return row.worker_runtime === "claude-cli" ? "claude-cli" : null;
}

export function LaneQuickSwitch() {
  const [data, setData] = useState<LanesResponse | null>(null);
  const [selectedProfile, setSelectedProfile] = useState<string | null>(null);
  const [choice, setChoice] = useState("");
  const [state, setState] = useState<SaveState>("loading");
  const [message, setMessage] = useState<string | null>(null);
  // Sekundäre Konfiguration: initial geschlossen (AC-2). Erfolgreiches Speichern
  // darf schließen, Fehler/Concurrency-Konflikte halten sie offen (AC-3).
  const [open, setOpen] = useState(false);
  const panelId = useId();

  const models = useMemo(() => data?.models?.length ? data.models : FALLBACK_MODELS, [data]);
  const modelOptions = useMemo(() => modelsForSelect(models), [models]);
  const lane = activeLane(data);
  const rows = useMemo(() => (lane ? editorRows(lane, data?.profiles ?? [], models) : []), [data?.profiles, lane, models]);
  const editableRows = rows.filter((row) => !row.locked);
  const selectedRow = rowForProfile(rows, selectedProfile);
  const selectedChoice = selectedRow ? choice : "";
  const hasChange = Boolean(selectedRow && selectedChoice !== rowChoice(selectedRow));

  const refresh = useCallback(async (reason?: string) => {
    setState("loading");
    const next = await loadLanes();
    const nextLane = activeLane(next);
    const nextRows = nextLane ? editorRows(nextLane, next.profiles, next.models?.length ? next.models : FALLBACK_MODELS) : [];
    const preferredProfile = selectedProfile ?? nextRows.find((row) => !row.locked)?.profile ?? null;
    const preferredRow = rowForProfile(nextRows, preferredProfile) ?? nextRows.find((row) => !row.locked) ?? null;
    setData(next);
    setSelectedProfile(preferredRow?.profile ?? null);
    setChoice(preferredRow ? rowChoice(preferredRow) : "");
    setMessage(reason ?? null);
    setState("idle");
  }, [selectedProfile]);

  useEffect(() => {
    let cancelled = false;
    async function run() {
      try {
        const next = await loadLanes();
        if (cancelled) return;
        const nextLane = activeLane(next);
        const nextRows = nextLane ? editorRows(nextLane, next.profiles, next.models?.length ? next.models : FALLBACK_MODELS) : [];
        const firstRow = nextRows.find((row) => !row.locked) ?? null;
        setData(next);
        setSelectedProfile(firstRow?.profile ?? null);
        setChoice(firstRow ? rowChoice(firstRow) : "");
        setState("idle");
      } catch (error) {
        if (cancelled) return;
        setMessage(error instanceof Error ? error.message : String(error));
        setState("idle");
      }
    }
    void run();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleProfileChange = useCallback((profile: string) => {
    const row = rowForProfile(rows, profile);
    setSelectedProfile(profile);
    setChoice(row ? rowChoice(row) : "");
    setMessage(null);
  }, [rows]);

  const handleSave = useCallback(async () => {
    if (!lane || !selectedRow || !hasChange) return;
    const warning = laneChoiceWarning(choice, models);
    if (warning) {
      setMessage(warning);
      return;
    }
    setState("checking");
    try {
      const fresh = await loadLanes();
      const freshLane = activeLane(fresh);
      if (!freshLane) throw new Error("Keine aktive Lane gefunden.");
      if (freshLane.id !== lane.id || laneRevision(freshLane) !== laneRevision(lane)) {
        setData(fresh);
        setMessage("Aktive Lane wurde parallel geändert — neu geladen. Bitte Auswahl prüfen und erneut speichern.");
        setState("idle");
        return;
      }
      const freshModels = fresh.models?.length ? fresh.models : models;
      const freshRows = editorRows(freshLane, fresh.profiles, freshModels);
      const targetRow = rowForProfile(freshRows, selectedRow.profile);
      if (!targetRow) throw new Error(`Profil ${selectedRow.profile} ist in der aktiven Lane nicht verfügbar.`);
      const updatedRow = applyChoice(targetRow, choice, freshModels);
      const check = await smokeCheckLaneConfig(updatedRow.profile, guardEntry(updatedRow));
      if (check.status !== "healthy") {
        setMessage(check.reason ?? "Spawn-Guard meldet Konflikt; Lane wurde nicht gespeichert.");
        setState("idle");
        return;
      }
      setState("saving");
      const nextProfiles = profilesFromEditorRows(
        freshRows.map((row) => (row.profile === updatedRow.profile ? updatedRow : row)),
      );
      await updateLane(freshLane.id, { profiles: nextProfiles });
      await refresh("Lane gespeichert; gilt ab dem nächsten Worker-Spawn.");
      // Nur der Erfolgspfad schließt die Disclosure — alle früheren Returns
      // (Concurrency, Guard-Konflikt, Fehler) lassen sie offen und sichtbar.
      setOpen(false);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
      setState("idle");
    }
  }, [choice, hasChange, lane, models, refresh, selectedRow]);

  if (state === "loading" && !data) {
    return <section className="fleet-lane-switch" aria-label="Lane-Modell-Schnellschalter">Lane wird geladen…</section>;
  }

  if (!lane || editableRows.length === 0) {
    return null;
  }

  // Summary (immer sichtbar, auch geschlossen): aktive Lane, effektives Profil,
  // Provider und Modell — nur soweit live vorhanden (AC-2).
  const summaryRow = selectedRow ?? editableRows[0] ?? null;
  const summaryProvider = effectiveProviderLabel(summaryRow);
  const summaryModel = effectiveModelLabel(summaryRow, models);
  const summaryFacts = [summaryRow?.profile, summaryProvider, summaryModel].filter(Boolean).join(" · ");

  return (
    <section className="fleet-lane-switch" aria-label="Lane-Modell-Schnellschalter">
      <button
        type="button"
        className="fleet-lane-switch__toggle"
        aria-label="Lane- und Modellkonfiguration"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((prev) => !prev)}
      >
        <span className="fleet-lane-switch__summary">
          <span className="fleet-lane-switch__eyebrow">Lane &amp; Modell</span>
          <span className="fleet-lane-switch__summary-lane" title={lane.name}>{lane.name}</span>
          {summaryFacts ? <span className="fleet-lane-switch__summary-facts" title={summaryFacts}>{summaryFacts}</span> : null}
        </span>
        <ChevronDown className="fleet-lane-switch__chevron" aria-hidden="true" />
      </button>

      {open ? (
        <div id={panelId} className="fleet-lane-switch__panel">
          <div className="fleet-lane-switch__panel-head">
            <span className="fleet-lane-switch__eyebrow">Aktive Lane · {lane.name}</span>
            <button type="button" className="fleet-lane-switch__ghost" onClick={() => void refresh()} disabled={state !== "idle"}>
              Neu laden
            </button>
          </div>
          <div className="fleet-lane-switch__grid">
            <label>
              Profil
              <select value={selectedProfile ?? ""} onChange={(event) => handleProfileChange(event.target.value)}>
                {editableRows.map((row) => (
                  <option key={row.profile} value={row.profile}>{row.profile}</option>
                ))}
              </select>
            </label>
            <label>
              Modell
              <select value={selectedChoice} onChange={(event) => { setChoice(event.target.value); setMessage(null); }}>
                <option value="">Standard ({selectedRow?.defaultLabel ?? "automatisch"})</option>
                {modelOptions.map((model) => (
                  <option key={optionValue(model)} value={optionValue(model)}>
                    {model.group ? `${model.group} · ` : ""}{model.label || model.id}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="fleet-lane-switch__footer">
            <button type="button" onClick={() => void handleSave()} disabled={!hasChange || state !== "idle"}>
              {state === "checking" ? "Prüfe…" : state === "saving" ? "Speichere…" : "Modell speichern"}
            </button>
            <span className="fleet-lane-switch__hint">Guard: Spawn-Check vor PUT /lanes/{lane.id}</span>
          </div>
          {message ? <p className="fleet-lane-switch__message" role="status">{message}</p> : null}
        </div>
      ) : null}
    </section>
  );
}
