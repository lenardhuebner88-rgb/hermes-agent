import { CheckCircle2, GitCompareArrows, Loader2, UploadCloud } from "lucide-react";
import { useState } from "react";

import { fetchJSON } from "../../../lib/api";
import { de } from "../../i18n/de";
import { SignalChip, SignalLabel } from "../leitstand";

const DEFAULT_PROSE = `# Plan title
**Goal:** One sentence.

## Slice: First slice
- lane: coder
- done-when: Observable done signal.
- files: path/a, path/b

## Slice: Next slice
- done-when: Observable done signal.
`;

interface PreviewChild {
  title: string;
  assignee?: string | null;
  parents?: number[];
  review_tier?: string | null;
}

interface CompilePreviewResponse {
  ok?: boolean;
  children: PreviewChild[];
  repairs: string[];
  warnings: string[];
}

interface IngestProseResponse {
  ok?: boolean;
  root_task_id?: string;
  child_ids?: string[];
}

type FreigabeMode = "operator" | "sofort";

interface PlanComposerProps {
  onIngestSuccess: () => void;
}

export function PlanComposer({ onIngestSuccess }: PlanComposerProps) {
  const [prose, setProse] = useState(DEFAULT_PROSE);
  const [preview, setPreview] = useState<CompilePreviewResponse | null>(null);
  const [busy, setBusy] = useState<"idle" | "preview" | "ingest">("idle");
  const [error, setError] = useState<string | null>(null);
  const [ingestedRoot, setIngestedRoot] = useState<string | null>(null);
  const [freigabeMode, setFreigabeMode] = useState<FreigabeMode>("operator");

  async function handlePreview() {
    setBusy("preview");
    setError(null);
    setIngestedRoot(null);
    try {
      const result = await fetchJSON<CompilePreviewResponse>("/api/plugins/kanban/planspecs/compile-preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prose }),
      });
      setPreview(result);
    } catch (e: unknown) {
      setPreview(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("idle");
    }
  }

  async function handleIngest() {
    if (!preview) return;
    setBusy("ingest");
    setError(null);
    try {
      const result = await fetchJSON<IngestProseResponse>("/api/plugins/kanban/planspecs/ingest-prose", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prose, freigabe: freigabeMode }),
      });
      setIngestedRoot(result.root_task_id ?? "ok");
      onIngestSuccess();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("idle");
    }
  }

  const previewBusy = busy === "preview";
  const ingestBusy = busy === "ingest";
  const canPreview = prose.trim().length > 0 && busy === "idle";
  const canIngest = preview != null && busy === "idle";

  return (
    <section className="mb-3 grid min-w-0 gap-3 rounded-panel border border-line bg-surface-1 p-3 text-ink">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <label htmlFor="plan-composer-prose" className="text-xs font-semibold text-ink-2">
          {de.fleet.planProseLabel}
        </label>
        {ingestedRoot ? (
          <span className="inline-flex max-w-full items-center gap-1">
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-status-ok" aria-hidden="true" />
            <SignalChip tone="ok" label={ingestedRoot} className="max-w-full font-data" title={ingestedRoot} />
          </span>
        ) : null}
      </div>

      <textarea
        id="plan-composer-prose"
        aria-label={de.fleet.planProseLabel}
        value={prose}
        onChange={(event) => {
          setProse(event.target.value);
          setPreview(null);
          setIngestedRoot(null);
        }}
        className="min-h-48 w-full min-w-0 resize-y rounded-card border border-line bg-surface-2 p-3 font-data text-[12px] leading-5 text-ink outline-none placeholder:text-ink-3 focus:border-live/60"
      />

      <div className="flex min-w-0 flex-col gap-2 sm:flex-row">
        <button
          type="button"
          className="inline-flex min-h-12 min-w-0 flex-1 items-center justify-center gap-2 rounded-card border border-live/40 bg-live/10 px-3 py-2 text-sec font-medium text-bronze-hi hover:bg-live/15 disabled:cursor-not-allowed disabled:opacity-45"
          onClick={() => void handlePreview()}
          disabled={!canPreview}
          aria-busy={previewBusy}
          title={de.fleet.planCompilePreview}
        >
          {previewBusy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <GitCompareArrows className="h-4 w-4" aria-hidden="true" />}
          <span>{de.fleet.planCompilePreview}</span>
        </button>
        <div className="flex min-w-0 flex-1 flex-col gap-2 sm:flex-row">
          <label htmlFor="plan-composer-freigabe" className="sr-only">
            {de.fleet.planFreigabeModeLabel}
          </label>
          <select
            id="plan-composer-freigabe"
            value={freigabeMode}
            onChange={(event) => setFreigabeMode(event.target.value as FreigabeMode)}
            disabled={ingestBusy}
            className="min-h-12 min-w-0 rounded-card border border-line bg-surface-2 px-3 py-2 text-sec font-medium text-ink-2 outline-none focus:border-live/60 disabled:cursor-not-allowed disabled:opacity-45"
          >
            <option value="operator">{de.fleet.planFreigabeOperator}</option>
            <option value="sofort">{de.fleet.planFreigabeSofort}</option>
          </select>
          <button
            type="button"
            className="inline-flex min-h-12 min-w-0 flex-1 items-center justify-center gap-2 rounded-card border border-line bg-surface-2 px-3 py-2 text-sec font-medium text-ink-2 hover:border-live/40 hover:text-bronze-hi disabled:cursor-not-allowed disabled:opacity-45"
            onClick={() => void handleIngest()}
            disabled={!canIngest}
            aria-busy={ingestBusy}
            title={de.fleet.planIngest}
          >
            {ingestBusy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <UploadCloud className="h-4 w-4" aria-hidden="true" />}
            <span>{de.fleet.planIngest}</span>
          </button>
        </div>
      </div>

      {error ? (
        <div role="alert" className="rounded-card border border-status-alert/30 bg-status-alert/10 p-2">
          <SignalLabel tone="alert" label={error} />
        </div>
      ) : null}

      {preview ? (
        <section aria-label={de.fleet.planCompilePreviewResult} className="grid min-w-0 gap-3 rounded-card border border-line bg-surface-2 p-3">
          <div className="grid min-w-0 gap-2">
            <div className="text-xs font-semibold text-ink-2">{de.fleet.planChildren}</div>
            <div className="grid min-w-0 gap-1.5">
              {preview.children.map((child, index) => (
                <div key={`${child.title}-${index}`} className="grid min-w-0 gap-1 border-b border-line-soft pb-2 last:border-b-0 last:pb-0">
                  <div className="min-w-0 truncate text-sm font-medium text-ink" title={child.title}>{child.title}</div>
                  <div className="flex min-w-0 flex-wrap gap-1.5 text-[11px] text-ink-3">
                    <span className="rounded-card border border-line px-2 py-0.5">{de.fleet.planChildLane}: {child.assignee || "coder"}</span>
                    <span className="rounded-card border border-line px-2 py-0.5">{de.fleet.planChildParents}: {(child.parents ?? []).join(", ") || de.fleet.planChildNone}</span>
                    {child.review_tier ? <span className="rounded-card border border-line px-2 py-0.5">{de.fleet.planChildTier}: {child.review_tier}</span> : null}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <SignalList title={de.fleet.planRepairs} items={preview.repairs} tone="ok" />
          <SignalList title={de.fleet.planWarnings} items={preview.warnings} tone="warn" />
        </section>
      ) : null}
    </section>
  );
}

function SignalList({ title, items, tone }: { title: string; items: string[]; tone: "ok" | "warn" }) {
  if (items.length === 0) return null;

  return (
    <div className="grid min-w-0 gap-1.5">
      <div className="text-xs font-semibold text-ink-2">{title}</div>
      <div className="flex min-w-0 flex-wrap gap-1.5">
        {items.map((item, index) => (
          <SignalLabel key={`${title}-${index}`} tone={tone} label={item} className="max-w-full break-words" />
        ))}
      </div>
    </div>
  );
}
