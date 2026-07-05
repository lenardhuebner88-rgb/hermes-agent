import { CheckCircle2, GitCompareArrows, Loader2, UploadCloud } from "lucide-react";
import { useState } from "react";

import { fetchJSON } from "../../../lib/api";

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

interface PlanComposerProps {
  onIngestSuccess: () => void;
}

export function PlanComposer({ onIngestSuccess }: PlanComposerProps) {
  const [prose, setProse] = useState(DEFAULT_PROSE);
  const [preview, setPreview] = useState<CompilePreviewResponse | null>(null);
  const [busy, setBusy] = useState<"idle" | "preview" | "ingest">("idle");
  const [error, setError] = useState<string | null>(null);
  const [ingestedRoot, setIngestedRoot] = useState<string | null>(null);

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
        body: JSON.stringify({ prose }),
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
    <section className="mb-3 grid min-w-0 gap-3 rounded-lg border border-line bg-surface-1 p-3 text-ink">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <label htmlFor="plan-composer-prose" className="text-xs font-semibold text-ink-2">
          Prose Plan
        </label>
        {ingestedRoot ? (
          <span className="inline-flex max-w-full items-center gap-1 truncate rounded-lg border border-status-ok/30 bg-status-ok/10 px-2 py-1 text-[11px] text-status-ok">
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span className="truncate">{ingestedRoot}</span>
          </span>
        ) : null}
      </div>

      <textarea
        id="plan-composer-prose"
        aria-label="Prose Plan"
        value={prose}
        onChange={(event) => {
          setProse(event.target.value);
          setPreview(null);
          setIngestedRoot(null);
        }}
        className="min-h-48 w-full min-w-0 resize-y rounded-lg border border-line bg-surface-2 p-3 font-mono text-[12px] leading-5 text-ink outline-none placeholder:text-ink-3 focus:border-live/60"
      />

      <div className="flex min-w-0 flex-col gap-2 sm:flex-row">
        <button
          type="button"
          className="inline-flex min-h-10 min-w-0 flex-1 items-center justify-center gap-2 rounded-lg border border-live/40 bg-live/10 px-3 py-2 text-sm font-medium text-live hover:bg-live/15 disabled:cursor-not-allowed disabled:opacity-45"
          onClick={() => void handlePreview()}
          disabled={!canPreview}
          aria-busy={previewBusy}
          title="Compile preview"
        >
          {previewBusy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <GitCompareArrows className="h-4 w-4" aria-hidden="true" />}
          <span>Compile preview</span>
        </button>
        <button
          type="button"
          className="inline-flex min-h-10 min-w-0 flex-1 items-center justify-center gap-2 rounded-lg border border-line bg-surface-2 px-3 py-2 text-sm font-medium text-ink-2 hover:border-live/40 hover:text-live disabled:cursor-not-allowed disabled:opacity-45"
          onClick={() => void handleIngest()}
          disabled={!canIngest}
          aria-busy={ingestBusy}
          title="Ingest compiled plan"
        >
          {ingestBusy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <UploadCloud className="h-4 w-4" aria-hidden="true" />}
          <span>Ingest compiled plan</span>
        </button>
      </div>

      {error ? (
        <div role="alert" className="rounded-lg border border-status-alert/30 bg-status-alert/10 p-2 text-xs text-status-alert">
          {error}
        </div>
      ) : null}

      {preview ? (
        <section aria-label="Compile preview result" className="grid min-w-0 gap-3 rounded-lg border border-line bg-surface-2 p-3">
          <div className="grid min-w-0 gap-2">
            <div className="text-xs font-semibold text-ink-2">Children</div>
            <div className="grid min-w-0 gap-1.5">
              {preview.children.map((child, index) => (
                <div key={`${child.title}-${index}`} className="grid min-w-0 gap-1 border-b border-line-soft pb-2 last:border-b-0 last:pb-0">
                  <div className="min-w-0 truncate text-sm font-medium text-ink">{child.title}</div>
                  <div className="flex min-w-0 flex-wrap gap-1.5 text-[11px] text-ink-3">
                    <span className="rounded-lg border border-line px-2 py-0.5">lane: {child.assignee || "coder"}</span>
                    <span className="rounded-lg border border-line px-2 py-0.5">parents: {(child.parents ?? []).join(", ") || "none"}</span>
                    {child.review_tier ? <span className="rounded-lg border border-line px-2 py-0.5">tier: {child.review_tier}</span> : null}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <SignalList title="Repairs" items={preview.repairs} tone="ok" />
          <SignalList title="Warnings" items={preview.warnings} tone="warn" />
        </section>
      ) : null}
    </section>
  );
}

function SignalList({ title, items, tone }: { title: string; items: string[]; tone: "ok" | "warn" }) {
  if (items.length === 0) return null;
  const toneClass = tone === "ok"
    ? "border-status-ok/30 bg-status-ok/10 text-status-ok"
    : "border-status-warn/30 bg-status-warn/10 text-status-warn";

  return (
    <div className="grid min-w-0 gap-1.5">
      <div className="text-xs font-semibold text-ink-2">{title}</div>
      <div className="flex min-w-0 flex-wrap gap-1.5">
        {items.map((item, index) => (
          <span key={`${title}-${index}`} className={`max-w-full break-words rounded-lg border px-2 py-1 text-[11px] ${toneClass}`}>
            {item}
          </span>
        ))}
      </div>
    </div>
  );
}
