import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { FleetPanel } from "../../components/fleet/atoms";
import { CopyButton } from "../backlog/CopyButton";
import { FALLBACK_MODELS, type LaneModelOption } from "../lanes/api";
import type { ForgeSelection, PromptForgeCatalog } from "./catalog";
import { compose } from "./composer";
import { score } from "./heuristic";
import { generatePrompt } from "./api";

const INPUT_CLS =
  "min-h-11 w-full rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-base text-white sm:min-h-9 sm:text-sm";

const SCOPE_HINT = "Finde die relevanten Dateien selbst, bevor du etwas änderst — ändere nur, was die Aufgabe braucht.";

interface Output {
  text: string;
  /** true → KI nicht erreichbar, deterministische Vorlage genutzt. */
  fallback: boolean;
}

export function Konfigurator({ catalog, models }: { catalog: PromptForgeCatalog; models?: LaneModelOption[] }) {
  const modelList = models && models.length > 0 ? models : FALLBACK_MODELS;
  const [targetId, setTargetId] = useState(catalog.targets[0]?.id ?? "generic");
  const [taskTypeId, setTaskTypeId] = useState(catalog.taskTypes[0]?.id ?? "audit");
  const [modeId, setModeId] = useState(catalog.modes[0]?.id ?? "stop-on-doubt");
  const [modelId, setModelId] = useState(modelList[0]?.id ?? "");
  const [problem, setProblem] = useState("");
  const [output, setOutput] = useState<Output | null>(null);
  const [loading, setLoading] = useState(false);

  const target = catalog.targets.find((t) => t.id === targetId);
  const rating = useMemo(() => (output ? score(output.text, taskTypeId) : null), [output, taskTypeId]);

  const localFallback = (): string => {
    const selection: ForgeSelection = {
      targetId,
      taskTypeId,
      modeId,
      modelId,
      slots: { task: problem, scope: SCOPE_HINT, maxTurns: 20 },
    };
    return compose(selection, catalog);
  };

  const onGenerate = async () => {
    if (!problem.trim() || loading) return;
    setLoading(true);
    try {
      const res = await generatePrompt({ problem: problem.trim(), targetId, taskTypeId, modeId, modelId });
      setOutput(res.prompt ? { text: res.prompt, fallback: res.fallback } : { text: localFallback(), fallback: true });
    } catch {
      setOutput({ text: localFallback(), fallback: true });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      <FleetPanel eyebrow="Konfigurator">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Ziel-CLI">
            <Select value={targetId} onChange={setTargetId} options={catalog.targets.map((t) => ({ value: t.id, label: t.label }))} />
          </Field>
          <Field label="Task-Typ">
            <Select value={taskTypeId} onChange={setTaskTypeId} options={catalog.taskTypes.map((t) => ({ value: t.id, label: t.label }))} />
          </Field>
          <Field label="Modus">
            <Select value={modeId} onChange={setModeId} options={catalog.modes.map((m) => ({ value: m.id, label: m.label }))} />
          </Field>
          <Field label="Modell (Ziel)">
            <Select value={modelId} onChange={setModelId} options={modelList.map((m) => ({ value: m.id, label: m.label }))} />
          </Field>
        </div>
        <div className="mt-3 grid grid-cols-1 gap-2">
          <Field label="Beschreibe dein Problem (in normalen Worten)">
            <textarea
              className={`${INPUT_CLS} min-h-[96px]`}
              value={problem}
              onChange={(e) => setProblem(e.target.value)}
              placeholder="z.B. Der Backlog-Tab im Dashboard ist unübersichtlich — ich möchte die Tasks nach Kategorie und Status gruppiert und filterbar sehen."
            />
          </Field>
          <button
            type="button"
            onClick={onGenerate}
            disabled={!problem.trim() || loading}
            className="inline-flex min-h-11 items-center justify-center gap-2 rounded-md border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-4 text-sm font-medium text-[var(--hc-accent-text)] transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? "Generiere …" : "Prompt generieren"}
          </button>
          <p className="text-xs hc-dim">Die KI baut daraus einen sauberen Prompt mit messbaren Akzeptanzkriterien — du musst keine Dateien/Scope angeben.</p>
        </div>
        {target ? <p className="mt-2 text-xs hc-dim">{target.mechanicNote}</p> : null}
      </FleetPanel>

      <FleetPanel
        eyebrow="Ergebnis"
        meta={output ? <CopyButton text={output.text} label="Kopieren" copiedLabel="Kopiert" /> : undefined}
      >
        {loading ? (
          <p className="hc-soft text-sm">Generiere Prompt …</p>
        ) : output ? (
          <>
            {output.fallback ? (
              <p className="mb-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-xs text-amber-200">
                KI nicht erreichbar — einfache Vorlage genutzt. Erneut versuchen oder den Text von Hand anpassen.
              </p>
            ) : null}
            <pre className="hc-mono max-h-[440px] overflow-auto whitespace-pre-wrap break-words rounded-md bg-black/30 p-3 text-xs leading-relaxed text-white/90">{output.text}</pre>
          </>
        ) : (
          <p className="hc-soft text-sm">Beschreibe dein Problem oben und klicke „Prompt generieren".</p>
        )}
      </FleetPanel>

      {rating ? (
        <FleetPanel eyebrow="Qualitäts-Score" meta={<span className="hc-mono text-sm">{rating.score} / {rating.max}</span>}>
          <ul className="grid grid-cols-1 gap-1 text-sm">
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
      ) : null}
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="grid grid-cols-1 gap-1 text-sm">
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
