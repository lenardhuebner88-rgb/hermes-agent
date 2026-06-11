import { useCallback, useEffect, useMemo, useState } from "react";
import { Check, Plus, Trash2 } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { StatusPill, ToneCallout } from "../components/atoms";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import type { Density } from "../hooks/useDensity";
import {
  activateLane,
  createLane,
  deleteLane,
  editorRows,
  loadLanes,
  profilesFromEditorRows,
  updateLane,
  FALLBACK_MODELS,
  type EditorRow,
  type Lane,
  type LaneModelOption,
  type LanesResponse,
} from "./lanes/api";

// Lane strings live here (not in i18n/de.ts) so this feature touches no
// shared files a parallel session may be editing.
const t = {
  title: "Lanes",
  intro:
    "Welches KI-Modell arbeitet in welcher Rolle? Preset oben wählen, pro Rolle das Modell anpassen, dann übernehmen — gilt ab dem nächsten Worker-Start.",
  presetLabel: "Preset",
  apply: "Übernehmen",
  applied: "Aktiv",
  activeSuffix: "(aktiv)",
  dirtyHint: "Änderungen noch nicht übernommen",
  profilesPanel: "Rollen & Modelle",
  presetsPanel: "Presets",
  standardOption: (model: string) => `Standard (${model})`,
  claudeAuto: "Claude (Modell automatisch)",
  builtin: "Mitgeliefert",
  active: "Aktiv",
  remove: "Löschen",
  confirmDelete: (name: string) => `Preset „${name}" wirklich löschen?`,
  confirmYes: "Bestätigen",
  confirmNo: "Abbrechen",
  newPresetPlaceholder: "Name für neues Preset",
  saveAsPreset: "Auswahl als Preset speichern",
  emptyTitle: "Keine Presets",
  emptyDesc: "Beim ersten Laden werden api-standard und max-abo angelegt.",
  loading: "Lade Modelle …",
  retry: "Erneut versuchen",
};

// Kurze, nicht-technische Rollen-Hinweise. Fallback: kein Hinweis.
const ROLE_HINTS: Record<string, string> = {
  coder: "Schreibt Code",
  "coder-claude": "Schreibt Code (Claude)",
  reviewer: "Prüft Code",
  critic: "Zweitmeinung",
  research: "Recherche",
  verifier: "Testet & verifiziert",
  admin: "Server-Verwaltung",
  premium: "Schwere Spezialfälle",
  "family-ui": "Family-App Design",
  "fo-brain": "Family-App Planung",
};

/** Grouped model dropdown. Eine unbekannte Auswahl (z. B. ein von Hand
 *  gesetztes Modell) bleibt als eigene Option sichtbar statt zu verschwinden. */
function ModelSelect({
  row,
  models,
  disabled,
  onChange,
}: {
  row: EditorRow;
  models: LaneModelOption[];
  disabled: boolean;
  onChange: (choice: string) => void;
}) {
  const groups = useMemo(() => {
    const out: { group: string; options: LaneModelOption[] }[] = [];
    for (const m of models) {
      const g = out.find((x) => x.group === m.group);
      if (g) g.options.push(m);
      else out.push({ group: m.group, options: [m] });
    }
    return out;
  }, [models]);

  const knownValues = new Set<string>(["", "claude-cli|"]);
  for (const m of models) knownValues.add(`${m.runtime}|${m.id}`);
  const unknown = !knownValues.has(row.choice) ? row.choice : null;

  return (
    <select
      value={row.choice}
      aria-label={`Modell für ${row.profile}`}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      className="min-h-11 w-full rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-base text-white sm:min-h-9 sm:w-64 sm:text-sm"
    >
      <option value="">{t.standardOption(row.defaultLabel)}</option>
      {groups.map(({ group, options }) => (
        <optgroup key={group} label={group}>
          {group.startsWith("Claude") ? (
            <option value="claude-cli|">{t.claudeAuto}</option>
          ) : null}
          {options.map((m) => (
            <option key={m.id} value={`${m.runtime}|${m.id}`}>
              {m.label}
            </option>
          ))}
        </optgroup>
      ))}
      {unknown ? (
        <option value={unknown}>{unknown.slice(unknown.indexOf("|") + 1) || unknown}</option>
      ) : null}
    </select>
  );
}

interface EditorActions {
  onSelect: (laneId: string) => void;
  onApply: (lane: Lane, rows: EditorRow[], needsSave: boolean) => void;
  onCreate: (name: string, rows: EditorRow[]) => void;
  onDelete: (lane: Lane) => void;
}

/** Pure presentation editor — exported for tests (rendered with fixtures).
 *  Gets remounted (key) when the selected lane changes, so rows/dirty reset. */
export function LanesEditor({
  data,
  lane,
  busy,
  actions,
  initialPendingDelete = null,
}: {
  data: LanesResponse;
  lane: Lane;
  busy: boolean;
  actions: EditorActions;
  initialPendingDelete?: string | null;
}) {
  const models = data.models && data.models.length > 0 ? data.models : FALLBACK_MODELS;
  const [rows, setRows] = useState<EditorRow[]>(() => editorRows(lane, data.profiles, models));
  const [dirty, setDirty] = useState(false);
  const [newName, setNewName] = useState("");
  // Inline-Zwei-Schritt fürs Löschen (FlowView-Muster) statt window.confirm.
  const [pendingDelete, setPendingDelete] = useState<string | null>(initialPendingDelete);

  const applyDisabled = busy || (!dirty && lane.active);

  return (
    <div className="space-y-4">
      <p className="text-sm hc-soft">{t.intro}</p>

      {/* Kopf: Preset wählen + EIN Klick übernehmen. */}
      <FleetPanel eyebrow={t.presetLabel} meta={dirty ? t.dirtyHint : null}>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <select
            value={lane.id}
            aria-label={t.presetLabel}
            disabled={busy}
            onChange={(e) => actions.onSelect(e.target.value)}
            className="min-h-11 w-full rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-base text-white sm:min-h-9 sm:w-64 sm:text-sm"
          >
            {data.lanes.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name} {l.active ? t.activeSuffix : ""}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            className="hc-hit sm:ml-auto"
            disabled={applyDisabled}
            onClick={() => actions.onApply(lane, rows, dirty)}
          >
            <Check className="h-4 w-4" />
            {!dirty && lane.active ? t.applied : t.apply}
          </Button>
        </div>
      </FleetPanel>

      {/* Rollen-Liste: pro Profil genau EIN Modell-Dropdown. */}
      <FleetPanel eyebrow={t.profilesPanel}>
        <ul className="divide-y divide-[var(--hc-border)]">
          {rows.map((row, i) => (
            <li
              key={row.profile}
              className="flex flex-col gap-1.5 py-2.5 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between sm:gap-3"
            >
              <div className="min-w-0">
                <div className="truncate text-sm text-white" title={row.description}>
                  {row.profile}
                </div>
                {ROLE_HINTS[row.profile] ? (
                  <div className="text-xs hc-dim">{ROLE_HINTS[row.profile]}</div>
                ) : null}
              </div>
              <ModelSelect
                row={row}
                models={models}
                disabled={busy}
                onChange={(choice) => {
                  setRows(rows.map((r, j) => (j === i ? { ...r, choice } : r)));
                  setDirty(true);
                }}
              />
            </li>
          ))}
        </ul>
      </FleetPanel>

      {/* Presets: anlegen + aufräumen. Auswahl passiert oben. */}
      <FleetPanel eyebrow={t.presetsPanel}>
        <ul className="divide-y divide-[var(--hc-border)]">
          {data.lanes.map((l) => (
            <li key={l.id} className="flex flex-wrap items-center gap-2 py-2 first:pt-0 last:pb-0">
              <span className="text-sm text-white">{l.name}</span>
              {l.active ? <StatusPill tone="emerald" label={t.active} size="sm" /> : null}
              {l.builtin ? (
                <span className="rounded bg-white/5 px-1.5 py-0.5 text-xs hc-dim">{t.builtin}</span>
              ) : null}
              {!l.active ? (
                pendingDelete === l.id ? (
                  <span className="ml-auto inline-flex flex-wrap items-center gap-2">
                    <span className="hc-type-label hc-soft">{t.confirmDelete(l.name)}</span>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        actions.onDelete(l);
                        setPendingDelete(null);
                      }}
                      className="inline-flex min-h-11 items-center rounded-full border border-red-400/40 bg-red-400/10 px-2.5 text-xs text-red-200 disabled:opacity-40 sm:min-h-7"
                    >
                      {busy ? "…" : t.confirmYes}
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => setPendingDelete(null)}
                      className="inline-flex min-h-11 items-center rounded-full border border-[var(--hc-border-strong)] px-2.5 text-xs hc-soft sm:min-h-7"
                    >
                      {t.confirmNo}
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    aria-label={`${t.remove} ${l.name}`}
                    disabled={busy}
                    onClick={() => setPendingDelete(l.id)}
                    className="hc-hit ml-auto inline-flex w-11 items-center justify-center rounded-md border border-[var(--hc-border)] text-xs hc-dim hover:text-white"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )
              ) : null}
            </li>
          ))}
        </ul>
        <div className="mt-3 flex flex-col gap-2 border-t border-[var(--hc-border)] pt-3 sm:flex-row sm:items-center">
          <input
            type="text"
            value={newName}
            aria-label={t.newPresetPlaceholder}
            placeholder={t.newPresetPlaceholder}
            onChange={(e) => setNewName(e.target.value)}
            className="min-h-11 w-full rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-base text-white sm:min-h-9 sm:w-64 sm:text-sm"
          />
          <Button
            size="xs"
            ghost
            className="hc-hit"
            disabled={busy || newName.trim() === ""}
            onClick={() => {
              actions.onCreate(newName.trim(), rows);
              setNewName("");
            }}
          >
            <Plus className="h-3.5 w-3.5" />
            {t.saveAsPreset}
          </Button>
        </div>
      </FleetPanel>
    </div>
  );
}

export function LanesView(_props: { density?: Density }) {
  const [data, setData] = useState<LanesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const [failCount, setFailCount] = useState(0);

  const reload = useCallback(async () => {
    try {
      setData(await loadLanes());
      setError(null);
      setFailCount(0);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setFailCount((n) => n + 1);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  // Selbstheilung: schlägt der Erstload fehl (z. B. "Failed to fetch" am
  // Handy direkt nach dem Foregrounding), retried der View mit mildem
  // Backoff statt für immer "Lade Modelle …" zu zeigen. Nur solange noch
  // keine Daten da sind — Fehler späterer Aktionen überschreibt kein
  // automatischer Reload.
  useEffect(() => {
    if (data !== null || failCount === 0) return;
    const timer = setTimeout(() => void reload(), Math.min(5_000 * failCount, 30_000));
    return () => clearTimeout(timer);
  }, [data, failCount, reload]);

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

  const lanes = data?.lanes ?? [];
  const lane =
    lanes.find((l) => l.id === selectedId) ?? lanes.find((l) => l.active) ?? lanes[0] ?? null;

  const actions: EditorActions = {
    onSelect: (laneId) => setSelectedId(laneId),
    onApply: (target, rows, needsSave) =>
      void run(async () => {
        if (needsSave) {
          await updateLane(target.id, { profiles: profilesFromEditorRows(rows) });
        }
        if (!target.active) await activateLane(target.id);
      }),
    onCreate: (name, rows) =>
      void run(async () => {
        const res = await createLane(name, profilesFromEditorRows(rows));
        setSelectedId(res.lane.id);
      }),
    onDelete: (target) => void run(() => deleteLane(target.id)),
  };

  return (
    <section aria-label={t.title} className="space-y-4">
      <h2 className="text-lg font-semibold text-white">{t.title}</h2>
      {error ? (
        <ToneCallout tone="red">
          <span className="flex items-center justify-between gap-3">
            <span>{error}</span>
            <Button size="xs" ghost className="hc-hit" onClick={() => void reload()} disabled={busy}>
              {t.retry}
            </Button>
          </span>
        </ToneCallout>
      ) : null}
      {data === null ? (
        <p className="text-sm hc-dim">{t.loading}</p>
      ) : lane === null ? (
        <FleetEmptyState title={t.emptyTitle} desc={t.emptyDesc} />
      ) : (
        <LanesEditor
          key={`${lane.id}:${lane.updated_at ?? 0}`}
          data={data}
          lane={lane}
          busy={busy}
          actions={actions}
        />
      )}
    </section>
  );
}
