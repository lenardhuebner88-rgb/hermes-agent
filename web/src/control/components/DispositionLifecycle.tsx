import { useState } from "react";
import { CheckCircle, Link2, Trash2, Wrench } from "lucide-react";
import { ToneCallout } from "./atoms";
import { FleetPanel } from "./fleet/atoms";
import { useDispositionItems, useDispositionActions } from "../hooks/useControlData";
import type { DispositionItem } from "../lib/schemas";

// i18n: Klartexte konsistent mit FunnelFreigaben (kein t()-Wrapper notwendig hier —
// FunnelFreigaben nutzt auch nur ein einfaches const-Objekt).
const t = {
  eyebrow: "Disposition-Items",
  meta: "offene Follow-ups & Risiken aus abgeschlossenen Tasks",
  empty: "Keine offenen Disposition-Items.",
  accept: "Akzeptieren",
  fixTask: "Fix-Task anlegen",
  dismiss: "Verwerfen",
  confirm: "Bestätigen",
  cancel: "Abbrechen",
  acceptHint: "markiert das Item als akzeptiert — kein weiterer Worker-Einsatz",
  fixTaskHint: "legt einen echten Kanban-Task an, der das Problem behebt",
  dismissHint: "archiviert das Item mit deinem Grund",
  reasonLabel: "Grund (Pflicht)",
  reasonPlaceholder: "Warum wird dieses Item verworfen?",
  quellTask: "Quell-Task ansehen",
  typLabels: {
    risk: "Risiko",
    follow_up: "Follow-up",
    still_open: "Noch offen",
  } as Record<string, string>,
  severityLabels: {
    "real-risk": "Echtes Risiko",
    "scope-note": "Scope-Hinweis",
    none: "Kein Risiko",
  } as Record<string, string>,
};

type PendingAction =
  | { id: string; kind: "accept" }
  | { id: string; kind: "fix" }
  | { id: string; kind: "dismiss"; reason: string };

function severityBadgeClass(severity: DispositionItem["severity"]): string {
  if (severity === "real-risk") return "border-red-500/40 bg-red-500/10 text-red-200";
  if (severity === "scope-note") return "border-amber-400/30 bg-amber-400/10 text-amber-100";
  return "border-white/15 bg-white/5 text-white/50";
}

function typBadgeClass(typ: DispositionItem["typ"]): string {
  if (typ === "risk") return "border-red-500/30 text-red-200";
  if (typ === "follow_up") return "border-sky-400/30 text-sky-200";
  return "border-white/15 text-white/50";
}

export function DispositionItemList({
  items,
  pending,
  busy,
  onAct,
  onPending,
  onDismissReasonChange,
}: {
  items: DispositionItem[];
  pending: PendingAction | null;
  busy: boolean;
  onAct: (item: DispositionItem, kind: "accept" | "fix" | "dismiss") => void;
  onPending: (p: PendingAction | null) => void;
  onDismissReasonChange: (reason: string) => void;
}) {
  return (
    <ul className="space-y-1.5">
      {items.map((item) => {
        const isPending = pending?.id === item.id ? pending : null;
        const dismissPending = isPending?.kind === "dismiss" ? isPending : null;
        return (
          <li key={item.id} className="rounded-md border border-[var(--hc-accent-border)] px-3 py-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`hc-mono shrink-0 rounded-full border px-2 py-0.5 text-[0.68rem] ${typBadgeClass(item.typ)}`}>
                {t.typLabels[item.typ] ?? item.typ}
              </span>
              <span className={`hc-mono shrink-0 rounded-full border px-2 py-0.5 text-[0.68rem] ${severityBadgeClass(item.severity)}`}>
                {t.severityLabels[item.severity] ?? item.severity}
              </span>
              <span className="min-w-0 flex-1 basis-56 truncate text-[0.85rem] font-medium text-white">
                {item.next_action ?? item.source_task_id}
              </span>
              <a
                href={`/control/backlog?focus=${encodeURIComponent(item.source_task_id)}`}
                className="inline-flex shrink-0 items-center gap-1 rounded border border-white/10 px-2 py-0.5 text-[0.72rem] hc-soft hover:bg-white/5"
              >
                <Link2 className="h-3 w-3" />
                {t.quellTask}
              </a>
            </div>

            {item.evidence ? (
              <p className="hc-mono mt-1.5 rounded bg-black/20 px-2 py-1 text-[0.75rem] text-white/70 whitespace-pre-wrap break-words">
                {item.evidence}
              </p>
            ) : null}

            <div className="mt-2 flex flex-wrap items-center gap-2">
              {isPending ? (
                <>
                  {dismissPending ? (
                    <div className="flex w-full flex-col gap-2">
                      <label className="text-[0.75rem] hc-dim">{t.reasonLabel}</label>
                      <textarea
                        className="w-full rounded border border-white/15 bg-black/30 px-2 py-1.5 text-[0.8rem] text-white placeholder:text-white/30 focus:outline-none"
                        rows={2}
                        placeholder={t.reasonPlaceholder}
                        value={dismissPending.reason}
                        onChange={(e) => onDismissReasonChange(e.target.value)}
                        disabled={busy}
                      />
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          disabled={busy || !dismissPending.reason.trim()}
                          onClick={() => onAct(item, "dismiss")}
                          className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-1 text-[0.78rem] text-red-200 disabled:opacity-50"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          {t.dismiss} · {t.confirm}
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => onPending(null)}
                          className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-3 py-1 text-[0.78rem] hc-soft"
                        >
                          {t.cancel}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => onAct(item, isPending.kind as "accept" | "fix")}
                        className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 py-1 text-[0.78rem] font-medium text-[var(--hc-accent-text)] disabled:opacity-50"
                      >
                        {isPending.kind === "accept" ? (
                          <CheckCircle className="h-3.5 w-3.5" />
                        ) : (
                          <Wrench className="h-3.5 w-3.5" />
                        )}
                        {isPending.kind === "accept" ? t.accept : t.fixTask} · {t.confirm}
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => onPending(null)}
                        className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-3 py-1 text-[0.78rem] hc-soft"
                      >
                        {t.cancel}
                      </button>
                      <span className="text-[0.72rem] hc-dim">
                        {isPending.kind === "accept" ? t.acceptHint : t.fixTaskHint}
                      </span>
                    </>
                  )}
                </>
              ) : (
                <>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onPending({ id: item.id, kind: "accept" })}
                    className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-emerald-500/30 px-3 py-1 text-[0.78rem] text-emerald-200 hover:bg-emerald-500/10"
                  >
                    <CheckCircle className="h-3.5 w-3.5" />
                    {t.accept}
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onPending({ id: item.id, kind: "fix" })}
                    className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-sky-500/30 px-3 py-1 text-[0.78rem] text-sky-200 hover:bg-sky-500/10"
                  >
                    <Wrench className="h-3.5 w-3.5" />
                    {t.fixTask}
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onPending({ id: item.id, kind: "dismiss", reason: "" })}
                    className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-red-500/25 px-3 py-1 text-[0.78rem] text-red-200 hover:bg-red-500/10"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                    {t.dismiss}
                  </button>
                </>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

export function DispositionLifecycle() {
  const { data, error, reload } = useDispositionItems();
  const { busy, error: actionError, acceptDisposition, dismissDisposition, createFixTaskFromDisposition } = useDispositionActions(reload);
  const [pending, setPending] = useState<PendingAction | null>(null);

  const items = data?.items ?? [];

  function handlePending(p: PendingAction | null) {
    setPending(p);
  }

  function handleDismissReasonChange(reason: string) {
    setPending((prev) => prev?.kind === "dismiss" ? { ...prev, reason } : prev);
  }

  async function handleAct(item: DispositionItem, kind: "accept" | "fix" | "dismiss") {
    if (kind === "accept") {
      await acceptDisposition(item.id);
    } else if (kind === "fix") {
      await createFixTaskFromDisposition(item.id);
    } else {
      const p = pending;
      if (p?.kind === "dismiss") {
        await dismissDisposition(item.id, p.reason);
      }
    }
    setPending(null);
  }

  // Render nothing when data not yet loaded and no error
  if (data == null && !error) return null;

  // Render nothing when there are no open items (quiet empty state matches panel idiom)
  if (items.length === 0 && !error) {
    return (
      <FleetPanel eyebrow={t.eyebrow} meta={<span className="hidden sm:inline">{t.meta}</span>}>
        <p className="text-[0.82rem] hc-dim">{t.empty}</p>
      </FleetPanel>
    );
  }

  return (
    <FleetPanel eyebrow={t.eyebrow} meta={<span className="hidden sm:inline">{t.meta}</span>}>
      {error ? <div className="mb-3"><ToneCallout tone="red">{error}</ToneCallout></div> : null}
      {actionError ? <div className="mb-3"><ToneCallout tone="red">{actionError}</ToneCallout></div> : null}
      {items.length > 0 ? (
        <DispositionItemList
          items={items}
          pending={pending}
          busy={busy}
          onAct={handleAct}
          onPending={handlePending}
          onDismissReasonChange={handleDismissReasonChange}
        />
      ) : (
        <p className="text-[0.82rem] hc-dim">{t.empty}</p>
      )}
    </FleetPanel>
  );
}
