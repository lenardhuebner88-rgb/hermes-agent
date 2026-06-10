import { useCallback, useEffect, useState } from "react";
import { Check, Plus, Trash2 } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { StatusPill, ToneCallout } from "../components/atoms";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import type { Density } from "../hooks/useDensity";
import {
  activateLane,
  createLane,
  deleteLane,
  loadLanes,
  updateLane,
  rowsFromLane,
  profilesFromRows,
  MODEL_SUGGESTIONS,
  type DraftRow,
  type Lane,
  type LaneCatalogProfile,
  type LanesResponse,
} from "./lanes/api";

// Lane strings live here (not in i18n/de.ts) so this feature touches no
// shared files a parallel session may be editing.
const t = {
  title: "Lanes",
  intro:
    "Eine Lane ist ein schaltbares Preset: Profil → Runtime + Modell. Genau eine Lane ist aktiv; sie greift ab dem nächsten Worker-Spawn (kein Gateway-Restart). Vorrang: Task-Override > aktive Lane > Profil-Default.",
  active: "Aktiv",
  builtin: "Preset",
  activate: "Aktivieren",
  save: "Speichern",
  remove: "Löschen",
  addRow: "Profil hinzufügen",
  newLane: "Neue Lane",
  namePlaceholder: "Name der Lane",
  create: "Anlegen",
  profileCol: "Profil",
  runtimeCol: "Runtime",
  modelCol: "Modell",
  runtimeDefault: "(Profil-Default)",
  modelPlaceholder: "Modell (leer = Profil-Default)",
  emptyTitle: "Keine Lanes",
  emptyDesc: "Beim ersten Laden werden api-standard und max-abo angelegt.",
  loading: "Lade Lanes …",
  confirmDelete: (name: string) => `Lane „${name}" wirklich löschen?`,
  confirmActivate: (name: string) =>
    `Lane „${name}" aktivieren? Gilt ab dem nächsten Worker-Spawn.`,
  noProfiles: "Keine Profile gemappt — alle Profile laufen auf ihrem Config-Default.",
};

const PROFILE_DATALIST_ID = "lanes-profile-options";
const MODEL_DATALIST_ID = "lanes-model-options";

interface LaneActions {
  onActivate: (lane: Lane) => void;
  onDelete: (lane: Lane) => void;
  onSave: (lane: Lane, rows: DraftRow[], name: string) => void;
}

function LaneRowEditor({
  row,
  onChange,
  onRemove,
}: {
  row: DraftRow;
  onChange: (next: DraftRow) => void;
  onRemove: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <input
        type="text"
        value={row.profile}
        list={PROFILE_DATALIST_ID}
        aria-label={t.profileCol}
        placeholder={t.profileCol}
        onChange={(e) => onChange({ ...row, profile: e.target.value })}
        className="hc-mono w-36 rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-xs text-white"
      />
      <select
        value={row.runtime}
        aria-label={t.runtimeCol}
        onChange={(e) =>
          onChange({ ...row, runtime: e.target.value as DraftRow["runtime"] })
        }
        className="rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-xs text-white"
      >
        <option value="">{t.runtimeDefault}</option>
        <option value="hermes">hermes</option>
        <option value="claude-cli">claude-cli</option>
      </select>
      <input
        type="text"
        value={row.model}
        list={MODEL_DATALIST_ID}
        aria-label={t.modelCol}
        placeholder={t.modelPlaceholder}
        onChange={(e) => onChange({ ...row, model: e.target.value })}
        className="hc-mono w-48 rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-xs text-white"
      />
      <button
        type="button"
        aria-label={`${t.remove} ${row.profile}`}
        onClick={onRemove}
        className="inline-flex min-h-11 items-center px-1 text-xs hc-dim hover:text-white"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function LaneCard({
  lane,
  busy,
  actions,
}: {
  lane: Lane;
  busy: boolean;
  actions: LaneActions;
}) {
  const [rows, setRows] = useState<DraftRow[]>(() => rowsFromLane(lane));
  const [name, setName] = useState(lane.name);
  const [dirty, setDirty] = useState(false);

  const edit = (next: DraftRow[]) => {
    setRows(next);
    setDirty(true);
  };

  const eyebrow = (
    <span className="inline-flex min-w-0 items-center gap-2">
      <span className="truncate normal-case tracking-normal text-white">{lane.name}</span>
      {lane.active ? <StatusPill tone="emerald" label={t.active} size="sm" /> : null}
      {lane.builtin ? <span className="rounded bg-white/5 px-1.5 py-0.5 text-xs hc-dim">{t.builtin}</span> : null}
    </span>
  );

  return (
    <FleetPanel eyebrow={eyebrow}>
      <div className="flex flex-wrap items-center justify-end gap-2">
        {!lane.active ? (
          <Button
            size="xs"
            className="min-h-11"
            disabled={busy}
            onClick={() => {
              if (window.confirm(t.confirmActivate(lane.name))) actions.onActivate(lane);
            }}
          >
            <Check className="h-3.5 w-3.5" />
            {t.activate}
          </Button>
        ) : null}
        <Button
          size="xs"
          ghost
          className="min-h-11"
          disabled={busy || !dirty}
          onClick={() => {
            actions.onSave(lane, rows, name);
            setDirty(false);
          }}
        >
          {t.save}
        </Button>
        {!lane.active ? (
          <Button
            size="xs"
            ghost
            className="min-h-11"
            disabled={busy}
            onClick={() => {
              if (window.confirm(t.confirmDelete(lane.name))) actions.onDelete(lane);
            }}
          >
            <Trash2 className="h-3.5 w-3.5" />
            {t.remove}
          </Button>
        ) : null}
      </div>

      <div className="mt-3 space-y-2">
        <input
          type="text"
          value={name}
          aria-label={t.namePlaceholder}
          onChange={(e) => {
            setName(e.target.value);
            setDirty(true);
          }}
          className="w-56 rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-sm text-white"
        />
        {rows.length === 0 ? <p className="text-xs hc-dim">{t.noProfiles}</p> : null}
        {rows.map((row, i) => (
          <LaneRowEditor
            key={i}
            row={row}
            onChange={(next) => edit(rows.map((r, j) => (j === i ? next : r)))}
            onRemove={() => edit(rows.filter((_, j) => j !== i))}
          />
        ))}
        <Button
          size="xs"
          ghost
          className="min-h-11"
          onClick={() => edit([...rows, { profile: "", runtime: "", model: "" }])}
        >
          <Plus className="h-3.5 w-3.5" />
          {t.addRow}
        </Button>
      </div>
    </FleetPanel>
  );
}

/** Pure presentation panel — exported for tests (rendered with fixtures). */
export function LanesPanel({
  data,
  busy,
  actions,
  onCreate,
}: {
  data: LanesResponse;
  busy: boolean;
  actions: LaneActions;
  onCreate: (name: string) => void;
}) {
  const [newName, setNewName] = useState("");
  return (
    <div className="space-y-4">
      <p className="text-sm hc-soft">{t.intro}</p>
      <datalist id={PROFILE_DATALIST_ID}>
        {data.profiles.map((p: LaneCatalogProfile) => (
          <option key={p.name} value={p.name}>
            {p.worker_runtime}
            {p.default_model ? ` · ${p.default_model}` : ""}
          </option>
        ))}
      </datalist>
      <datalist id={MODEL_DATALIST_ID}>
        {MODEL_SUGGESTIONS.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>

      {data.lanes.length === 0 ? (
        <FleetEmptyState title={t.emptyTitle} desc={t.emptyDesc} />
      ) : (
        data.lanes.map((lane) => (
          <LaneCard key={lane.id} lane={lane} busy={busy} actions={actions} />
        ))
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-[var(--hc-border)] pt-3">
        <input
          type="text"
          value={newName}
          aria-label={t.namePlaceholder}
          placeholder={t.namePlaceholder}
          onChange={(e) => setNewName(e.target.value)}
          className="w-56 rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-sm text-white"
        />
        <Button
          size="xs"
          className="min-h-11"
          disabled={busy || newName.trim() === ""}
          onClick={() => {
            onCreate(newName.trim());
            setNewName("");
          }}
        >
          <Plus className="h-3.5 w-3.5" />
          {t.create}
        </Button>
      </div>
    </div>
  );
}

export function LanesView(_props: { density?: Density }) {
  const [data, setData] = useState<LanesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      setData(await loadLanes());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const run = useCallback(
    async (op: () => Promise<unknown>) => {
      setBusy(true);
      try {
        await op();
        await reload();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [reload],
  );

  const actions: LaneActions = {
    onActivate: (lane) => void run(() => activateLane(lane.id)),
    onDelete: (lane) => void run(() => deleteLane(lane.id)),
    onSave: (lane, rows, name) =>
      void run(() =>
        updateLane(lane.id, { name, profiles: profilesFromRows(rows) }),
      ),
  };

  return (
    <section aria-label={t.title} className="space-y-4">
      <h2 className="text-lg font-semibold text-white">{t.title}</h2>
      {error ? <ToneCallout tone="red">{error}</ToneCallout> : null}
      {data === null ? (
        <p className="text-sm hc-dim">{t.loading}</p>
      ) : (
        <LanesPanel
          data={data}
          busy={busy}
          actions={actions}
          onCreate={(name) => void run(() => createLane(name, {}))}
        />
      )}
    </section>
  );
}
