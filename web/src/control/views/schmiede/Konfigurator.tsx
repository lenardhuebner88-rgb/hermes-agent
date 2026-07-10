import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { TriangleAlert } from "lucide-react";
import { FleetPanel, KpiTile, SignalLabel } from "../../components/leitstand";
import { CopyButton } from "../backlog/CopyButton";
import { FALLBACK_MODELS, type LaneModelOption } from "../lanes/api";
import type { ForgeSelection, PromptForgeCatalog } from "./catalog";
import { compose } from "./composer";
import { score } from "./heuristic";
import { generatePrompt } from "./api";

const INPUT_CLS =
  "min-h-12 w-full rounded-card border border-line bg-surface-2 px-2 py-1.5 text-base text-ink sm:text-sm";

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
          <Field label="Ziel-CLI" htmlFor="schmiede-target">
            <Select id="schmiede-target" value={targetId} onChange={setTargetId} options={catalog.targets.map((t) => ({ value: t.id, label: t.label }))} />
          </Field>
          <Field label="Task-Typ" htmlFor="schmiede-task-type">
            <Select id="schmiede-task-type" value={taskTypeId} onChange={setTaskTypeId} options={catalog.taskTypes.map((t) => ({ value: t.id, label: t.label }))} />
          </Field>
          <Field label="Modus" htmlFor="schmiede-mode">
            <Select id="schmiede-mode" value={modeId} onChange={setModeId} options={catalog.modes.map((m) => ({ value: m.id, label: m.label }))} />
          </Field>
          <Field label="Modell (Ziel)" htmlFor="schmiede-model">
            <Select id="schmiede-model" value={modelId} onChange={setModelId} options={modelList.map((m) => ({ value: m.id, label: m.label }))} />
          </Field>
        </div>
        <div className="mt-3 grid grid-cols-1 gap-2">
          <Field label="Beschreibe dein Problem (in normalen Worten)" htmlFor="schmiede-problem">
            <textarea
              id="schmiede-problem"
              aria-label="Beschreibe dein Problem in normalen Worten"
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
            className="inline-flex min-h-12 items-center justify-center gap-2 rounded-card border border-live bg-live/10 px-4 text-sm font-medium text-live transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? "Generiere …" : "Prompt generieren"}
          </button>
          <p className="text-xs text-ink-3">Die KI baut daraus einen sauberen Prompt mit messbaren Akzeptanzkriterien — du musst keine Dateien/Scope angeben.</p>
        </div>
        {target ? <p className="mt-2 text-xs text-ink-3">{target.mechanicNote}</p> : null}
      </FleetPanel>

      <FleetPanel
        eyebrow="Ergebnis"
        meta={output ? <CopyButton text={output.text} label="Kopieren" copiedLabel="Kopiert" /> : undefined}
      >
        {loading ? (
          <p className="text-ink-2 text-sm">Generiere Prompt …</p>
        ) : output ? (
          <>
            {output.fallback ? (
              <div className="mb-2 flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />KI nicht erreichbar — einfache Vorlage genutzt. Erneut versuchen oder den Text von Hand anpassen.</div>
            ) : null}
            <pre className="font-data tabular-nums max-h-[440px] overflow-auto whitespace-pre-wrap break-words rounded-card bg-surface-0 p-3 text-xs leading-relaxed text-ink">{output.text}</pre>
          </>
        ) : (
          <p className="text-ink-2 text-sm">Beschreibe dein Problem oben und klicke „Prompt generieren".</p>
        )}
      </FleetPanel>

      {rating ? (
        <FleetPanel eyebrow="Qualitätsprüfung">
          <KpiTile label="Qualitäts-Score" value={`${rating.score} / ${rating.max}`} className="mb-3" />
          <ul className="grid grid-cols-1 gap-1 text-sm">
            {rating.checks.map((c) => (
              <li key={c.id} className="flex items-center gap-2">
                <SignalLabel tone={c.status === "pass" ? "ok" : c.status === "fail" ? "alert" : "neutral"} label={c.status === "pass" ? "Bestanden" : c.status === "fail" ? "Fehlt" : "Nicht relevant"} />
                <span className={c.status === "fail" ? "text-ink" : "text-ink-2"}>{c.label}</span>
                {c.status === "fail" ? <span className="text-ink-3 text-xs">— {c.rationale}</span> : null}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-ink-3">Volle Punktzahl = gut · unter der Hälfte = Drift-Risiko. Es zählen nur die für diesen Task-Typ relevanten Checks; „–" = nicht relevant.</p>
        </FleetPanel>
      ) : null}
    </div>
  );
}

function Field({ label, htmlFor, children }: { label: string; htmlFor: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-1 gap-1 text-sm">
      <label htmlFor={htmlFor} className="font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3">{label}</label>
      {children}
    </div>
  );
}

function Select({ id, value, onChange, options }: { id: string; value: string; onChange: (v: string) => void; options: Array<{ value: string; label: string }> }) {
  return (
    <select id={id} className={INPUT_CLS} value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
}
