import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { FleetPanel } from "../../components/fleet/atoms";
import { CopyButton } from "../backlog/CopyButton";
import { FALLBACK_MODELS, type LaneModelOption } from "../lanes/api";
import type { ForgeSelection, PromptForgeCatalog } from "./catalog";
import { compose } from "./composer";
import { score } from "./heuristic";

const INPUT_CLS =
  "min-h-11 w-full rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-base text-white sm:min-h-9 sm:text-sm";

export function Konfigurator({ catalog, models }: { catalog: PromptForgeCatalog; models?: LaneModelOption[] }) {
  const modelList = models && models.length > 0 ? models : FALLBACK_MODELS;
  const [selection, setSelection] = useState<ForgeSelection>(() => ({
    targetId: catalog.targets[0]?.id ?? "generic",
    taskTypeId: catalog.taskTypes[0]?.id ?? "audit",
    modeId: catalog.modes[0]?.id ?? "stop-on-doubt",
    modelId: modelList[0]?.id ?? "",
    slots: { task: "", scope: "", maxTurns: 20 },
  }));

  const preview = useMemo(() => compose(selection, catalog), [selection, catalog]);
  const rating = useMemo(() => score(preview, selection.taskTypeId), [preview, selection.taskTypeId]);
  const target = catalog.targets.find((t) => t.id === selection.targetId);
  const isLoop = target?.wrapMode === "interval-loop";
  const isGoal = target?.wrapMode === "completion-condition" || isLoop;

  const set = (patch: Partial<ForgeSelection>) => setSelection((s) => ({ ...s, ...patch }));
  const setSlot = (patch: Partial<ForgeSelection["slots"]>) =>
    setSelection((s) => ({ ...s, slots: { ...s.slots, ...patch } }));

  return (
    <div className="grid gap-4">
      <FleetPanel eyebrow="Konfigurator">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Ziel-CLI">
            <Select value={selection.targetId} onChange={(v) => set({ targetId: v })} options={catalog.targets.map((t) => ({ value: t.id, label: t.label }))} />
          </Field>
          <Field label="Task-Typ">
            <Select value={selection.taskTypeId} onChange={(v) => set({ taskTypeId: v })} options={catalog.taskTypes.map((t) => ({ value: t.id, label: t.label }))} />
          </Field>
          <Field label="Modus">
            <Select value={selection.modeId} onChange={(v) => set({ modeId: v })} options={catalog.modes.map((m) => ({ value: m.id, label: m.label }))} />
          </Field>
          <Field label="Modell">
            <Select value={selection.modelId} onChange={(v) => set({ modelId: v })} options={modelList.map((m) => ({ value: m.id, label: m.label }))} />
          </Field>
        </div>
        <div className="mt-3 grid gap-3">
          <Field label="Aufgabe (Datei + Symptom + Outcome)">
            <textarea className={`${INPUT_CLS} min-h-[64px]`} value={selection.slots.task} onChange={(e) => setSlot({ task: e.target.value })} placeholder="z.B. auth.ts: Login-Race → deterministische Session-Erstellung" />
          </Field>
          <Field label="Scope (Datei / Verzeichnis)">
            <input className={INPUT_CLS} value={selection.slots.scope} onChange={(e) => setSlot({ scope: e.target.value })} placeholder="src/auth" />
          </Field>
          {isGoal ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {isLoop ? (
                <Field label="Intervall (Minuten, leer = self-paced)">
                  <input type="number" min={1} className={INPUT_CLS} value={selection.slots.intervalMinutes ?? ""} onChange={(e) => setSlot({ intervalMinutes: e.target.value ? Number(e.target.value) : undefined })} />
                </Field>
              ) : null}
              <Field label={isLoop ? "Max Runden" : "Max Turns"}>
                <input type="number" min={1} className={INPUT_CLS} value={selection.slots.maxTurns ?? ""} onChange={(e) => setSlot({ maxTurns: e.target.value ? Number(e.target.value) : undefined })} />
              </Field>
            </div>
          ) : null}
        </div>
        {target ? <p className="mt-3 text-xs hc-dim">{target.mechanicNote}</p> : null}
      </FleetPanel>

      <FleetPanel eyebrow="Live-Vorschau" meta={<CopyButton text={preview} label="Kopieren" copiedLabel="Kopiert" />}>
        <pre className="hc-mono max-h-[420px] overflow-auto whitespace-pre-wrap rounded-md bg-black/30 p-3 text-xs leading-relaxed text-white/90">{preview}</pre>
      </FleetPanel>

      <FleetPanel eyebrow="Qualitäts-Score" meta={<span className="hc-mono text-sm">{rating.score} / {rating.max}</span>}>
        <ul className="grid gap-1 text-sm">
          {rating.checks.map((c) => (
            <li key={c.id} className="flex items-center gap-2">
              <span className={c.status === "pass" ? "text-emerald-400" : c.status === "fail" ? "text-rose-400" : "hc-dim"}>
                {c.status === "pass" ? "✓" : c.status === "fail" ? "✗" : "–"}
              </span>
              <span className={c.status === "fail" ? "text-white" : "hc-soft"}>{c.label}</span>
              {c.status === "fail" ? <span className="hc-dim text-xs">— {c.rationale}</span> : null}
            </li>
          ))}
        </ul>
        <p className="mt-2 text-xs hc-dim">8–10 = gut · 5–7 = akzeptabel · &lt;5 = Drift-Risiko. „–" = für diesen Task-Typ nicht relevant.</p>
      </FleetPanel>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="grid gap-1 text-sm">
      <span className="hc-eyebrow">{label}</span>
      {children}
    </label>
  );
}

function Select({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: Array<{ value: string; label: string }> }) {
  return (
    <select className={INPUT_CLS} value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
}
