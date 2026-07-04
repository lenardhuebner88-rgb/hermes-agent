import { useCallback, useEffect, useMemo, useState } from "react";

import {
  FALLBACK_MODELS,
  type EditorRow,
  type Lane,
  type LaneModelOption,
  type LanesResponse,
  editorRows,
  entryFromChoice,
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
  return `${option.runtime}|${option.id}`;
}

function modelsForSelect(models: LaneModelOption[]): LaneModelOption[] {
  return models.filter((model) => model.locked !== true && model.id.trim() !== "");
}

function rowForProfile(rows: EditorRow[], profile: string | null): EditorRow | null {
  if (!profile) return null;
  return rows.find((row) => row.profile === profile) ?? null;
}

function applyChoice(row: EditorRow, choice: string, models: LaneModelOption[]): EditorRow {
  const entry = entryFromChoice(choice);
  if (!entry) {
    return {
      ...row,
      choice: "",
      worker_runtime: row.worker_runtime,
      provider: null,
      model: null,
      fallbackProviders: [],
    };
  }
  const model = models.find((candidate) => optionValue(candidate) === choice);
  const runtime = entry.worker_runtime ?? row.worker_runtime;
  return {
    ...row,
    choice,
    worker_runtime: runtime,
    provider: runtime === "hermes" ? model?.provider ?? row.defaultProvider ?? null : null,
    model: entry.model ?? null,
    fallbackProviders: runtime === "hermes" ? row.fallbackProviders : [],
  };
}

function guardEntry(row: EditorRow) {
  return {
    worker_runtime: row.worker_runtime,
    provider: row.provider ?? row.defaultProvider ?? null,
    model: row.model,
  };
}

export function LaneQuickSwitch() {
  const [data, setData] = useState<LanesResponse | null>(null);
  const [selectedProfile, setSelectedProfile] = useState<string | null>(null);
  const [choice, setChoice] = useState("");
  const [state, setState] = useState<SaveState>("loading");
  const [message, setMessage] = useState<string | null>(null);

  const models = useMemo(() => data?.models?.length ? data.models : FALLBACK_MODELS, [data]);
  const modelOptions = useMemo(() => modelsForSelect(models), [models]);
  const lane = activeLane(data);
  const rows = useMemo(() => (lane ? editorRows(lane, data?.profiles ?? [], models) : []), [data?.profiles, lane, models]);
  const editableRows = rows.filter((row) => !row.locked);
  const selectedRow = rowForProfile(rows, selectedProfile);
  const selectedChoice = selectedRow ? choice : "";
  const hasChange = Boolean(selectedRow && selectedChoice !== selectedRow.choice);

  const refresh = useCallback(async (reason?: string) => {
    setState("loading");
    const next = await loadLanes();
    const nextLane = activeLane(next);
    const nextRows = nextLane ? editorRows(nextLane, next.profiles, next.models?.length ? next.models : FALLBACK_MODELS) : [];
    const preferredProfile = selectedProfile ?? nextRows.find((row) => !row.locked)?.profile ?? null;
    const preferredRow = rowForProfile(nextRows, preferredProfile) ?? nextRows.find((row) => !row.locked) ?? null;
    setData(next);
    setSelectedProfile(preferredRow?.profile ?? null);
    setChoice(preferredRow?.choice ?? "");
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
        setChoice(firstRow?.choice ?? "");
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
    setChoice(row?.choice ?? "");
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

  return (
    <section className="fleet-lane-switch" aria-label="Lane-Modell-Schnellschalter">
      <div className="fleet-lane-switch__header">
        <div>
          <p className="fleet-lane-switch__eyebrow">Aktive Lane</p>
          <strong>{lane.name}</strong>
        </div>
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
    </section>
  );
}
