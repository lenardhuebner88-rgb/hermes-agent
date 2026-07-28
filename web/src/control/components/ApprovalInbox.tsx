/**
 * ApprovalInbox — die zwei Freigabe-Rückstaus des Operators an einer Stelle:
 *
 *   1. Funnel-Drafts (GET /funnel/drafts): fertige Vorschläge aus family /
 *      discord-idea / fo-gap-audit, die auf Freigabe warten, bevor sie echte
 *      Bau-Karten werden. Aktionen: approve / revise (mit Text) / dismiss.
 *   2. Disposition-Items (GET /disposition-items): offene Risiken/Follow-ups
 *      aus abgeschlossenen Tasks. Aktionen: accept / create-fix-task /
 *      dismiss (mit optionalem Grund).
 *
 * Eingehängt in CommandHome direkt unter dem Operator-Digest: die Ausnahme
 * erscheint dort über den /decision-queue-Feed ohnehin zuerst — die Auflösung
 * ist damit einen Klick entfernt statt in einem anderen Tab.
 *
 * Vertrag (Task-Brief): KEIN optimistisches Ausblenden — eine Zeile
 * verschwindet erst, wenn der Server die Aktion bestätigt und der Reload die
 * Liste ohne sie zurückliefert. Schlägt eine Aktion fehl, bleibt die Zeile
 * im Bestätigungszustand offen und zeigt den Grund aus der Backend-Antwort
 * (409/404-Detail) direkt an der Zeile. Leer ist der Normalfall und wird als
 * ruhiger Zustand gerendert (DESIGN.md Empty-State-Doktrin), nicht als
 * Kasten, der wie ein Ladefehler wirkt.
 *
 * Steinbruch-Logik aus dem entfernten DispositionLifecycle (Commit 39f49a14a):
 * Zwei-Schritt-Bestätigung mit pending-State, scope-note-Items ohne Fix-Task,
 * sichtbare Evidence. Das Layout ist neu (Leitstand-Tokens, ch-*-Idiome,
 * Touch-Ziele ≥44px) — der alte FlowView, für den es gebaut war, existiert
 * nicht mehr.
 */
import { useState } from "react";
import { Check, Link2, Pencil, Trash2, Wrench } from "lucide-react";
import { cn } from "@/lib/utils";
import { useDispositionActions, useDispositionItems, useFunnelDraftActions, useFunnelDrafts } from "../hooks/flowTriageDisposition";
import type { DispositionItem, FunnelDraft } from "../lib/schemas";
import { fmtRelativeTime, nowSec } from "../lib/derive";
import { Eyebrow } from "./primitives";

const SOURCE_LABELS: Record<string, string> = {
  family: "Family",
  "discord-idea": "Discord",
  "fo-gap-audit": "Gap-Audit",
};

const TYP_LABELS: Record<DispositionItem["typ"], string> = {
  risk: "Risiko",
  follow_up: "Follow-up",
  still_open: "Noch offen",
};

const SEVERITY_LABELS: Record<DispositionItem["severity"], string> = {
  "real-risk": "Echtes Risiko",
  "scope-note": "Scope-Hinweis",
  none: "Kein Risiko",
};

const DISPOSITION_LABELS: Record<DispositionItem["disposition"], string> = {
  done: "erledigt",
  delegate: "delegieren",
  defer: "vertagen",
  drop: "verwerfen",
};

const t = {
  eyebrow: "Freigaben & Dispositionen",
  funnel: "Funnel-Drafts",
  disposition: "Disposition-Items",
  empty:
    "Nichts wartet auf Freigabe. Vorschläge aus Family, Discord und Gap-Audit sowie Risiken aus Task-Abschlüssen erscheinen hier, sobald sie eine Entscheidung brauchen.",
  approve: "Freigeben",
  revise: "Überarbeiten",
  dismiss: "Verwerfen",
  accept: "Akzeptieren",
  fixTask: "Fix-Task anlegen",
  confirm: "Bestätigen",
  cancel: "Abbrechen",
  working: "arbeite…",
  approveHint: "legt den Build-Task als verlinktes Kind an (startklar — der Dispatcher übernimmt)",
  reviseHint: "schickt den Draft mit deinem Text zurück in den Spec-Loop",
  dismissDraftHint: "archiviert den Wunsch — er wird NICHT gebaut",
  acceptHint: "markiert das Item als akzeptiert — kein weiterer Worker-Einsatz",
  fixTaskHint: "legt einen echten Kanban-Task in Triage an, der das Problem behebt",
  dismissHint: "archiviert das Item; der Grund landet als Kommentar am Quell-Task",
  reasonLabel: "Grund (optional)",
  reasonPlaceholder: "Warum wird dieses Item verworfen?",
  draftLabel: "Draft-Text (Pflicht)",
  spendAlert: "Draft nennt externe Modelle ohne Kosten-Abschnitt — Freigabe wird vom Backend abgelehnt, bis ein „Kosten: …“-Kommentar ergänzt ist.",
  operatorEdited: "vom Operator bearbeitet",
  revisionOf: "Revision",
  openTask: "Task ansehen",
  scopeNoteHint: "Scope-Hinweis — kein Fix-Task nötig",
};

type FunnelPending =
  | { id: string; kind: "approve" }
  | { id: string; kind: "revise"; text: string }
  | { id: string; kind: "dismiss" };

type DispositionPending =
  | { id: string; kind: "accept" }
  | { id: string; kind: "fix" }
  | { id: string; kind: "dismiss"; reason: string };

function fleetTaskLink(taskId: string) {
  return `/control/fleet?task=${encodeURIComponent(taskId)}`;
}

function ActionError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="w-full text-xs leading-snug text-status-alert">
      {message}
    </p>
  );
}

function ConfirmBar({ busy, confirmLabel, hint, error, onConfirm, onCancel, confirmDisabled }: {
  busy: boolean;
  confirmLabel: string;
  hint: string;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
  confirmDisabled?: boolean;
}) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      <button
        type="button"
        disabled={busy || confirmDisabled}
        onClick={onConfirm}
        className="ch-btn ch-btn-primary min-h-11 px-3 text-xs font-medium"
      >
        {busy ? t.working : `${confirmLabel} · ${t.confirm}`}
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={onCancel}
        className="ch-btn min-h-11 px-3 text-xs"
      >
        {t.cancel}
      </button>
      <span className="basis-full text-xs text-ink-3 sm:basis-auto sm:flex-1">{hint}</span>
      <ActionError message={error} />
    </div>
  );
}

function FunnelDraftRow({ draft, actions, now }: {
  draft: FunnelDraft;
  actions: ReturnType<typeof useFunnelDraftActions>;
  now: number;
}) {
  const [pending, setPending] = useState<FunnelPending | null>(null);
  const isPending = pending?.id === draft.id ? pending : null;
  const busy = actions.busy;

  async function act(kind: "approve" | "revise" | "dismiss") {
    let ok = false;
    if (kind === "approve") ok = await actions.approveDraft(draft.id);
    else if (kind === "dismiss") ok = await actions.dismissDraft(draft.id);
    else if (isPending?.kind === "revise") ok = await actions.reviseDraft(draft.id, isPending.text);
    // Nur bei Server-Erfolg schließen — bei Fehlschlag bleibt die Zeile im
    // Bestätigungszustand und der Grund steht in actions.error (Zeile unten).
    if (ok) setPending(null);
  }

  return (
    <li className="ch-card px-3.5 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-full border border-line px-2 py-0.5 text-micro font-medium text-ink-2">
          {SOURCE_LABELS[draft.created_by] ?? draft.created_by}
        </span>
        {draft.operator_edited ? (
          <span className="rounded-full border border-line px-2 py-0.5 text-micro text-ink-3">{t.operatorEdited}</span>
        ) : null}
        {draft.revision_of ? (
          <span className="rounded-full border border-line px-2 py-0.5 font-data text-micro text-ink-3">{t.revisionOf} {draft.revision_of}</span>
        ) : null}
        {draft.completed_at != null ? (
          <span className="font-data text-micro tabular-nums text-ink-3">{fmtRelativeTime(draft.completed_at, now)}</span>
        ) : null}
        <a
          href={fleetTaskLink(draft.id)}
          className="ml-auto inline-flex min-h-11 shrink-0 items-center gap-1 rounded-card border border-line px-2 text-xs text-ink-2 hover:border-live hover:text-ink"
        >
          <Link2 className="h-3.5 w-3.5" />
          {t.openTask}
        </a>
      </div>
      <p className="mt-1.5 text-sm font-semibold leading-snug text-ink">{draft.title || draft.id}</p>
      {draft.draft_excerpt ? (
        <p className="mt-1 line-clamp-3 break-words text-xs leading-relaxed text-ink-2">{draft.draft_excerpt}</p>
      ) : null}
      {draft.spend_alert ? (
        <p className="mt-1.5 text-xs leading-snug text-status-warn">{t.spendAlert}</p>
      ) : null}

      {isPending?.kind === "revise" ? (
        <div className="mt-2">
          <label className="text-xs text-ink-3" htmlFor={`revise-${draft.id}`}>{t.draftLabel}</label>
          <textarea
            id={`revise-${draft.id}`}
            className="mt-1 w-full rounded-card border border-line bg-surface-1 px-2 py-1.5 font-data text-xs text-ink placeholder:text-ink-3 focus:border-live focus:outline-none"
            rows={6}
            value={isPending.text}
            disabled={busy}
            onChange={(e) => setPending({ id: draft.id, kind: "revise", text: e.target.value })}
          />
          <ConfirmBar
            busy={busy}
            confirmLabel={t.revise}
            hint={t.reviseHint}
            error={actions.error}
            confirmDisabled={!isPending.text.trim()}
            onConfirm={() => void act("revise")}
            onCancel={() => setPending(null)}
          />
        </div>
      ) : isPending ? (
        <ConfirmBar
          busy={busy}
          confirmLabel={isPending.kind === "approve" ? t.approve : t.dismiss}
          hint={isPending.kind === "approve" ? t.approveHint : t.dismissDraftHint}
          error={actions.error}
          onConfirm={() => void act(isPending.kind)}
          onCancel={() => setPending(null)}
        />
      ) : (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => setPending({ id: draft.id, kind: "approve" })}
            className="ch-btn ch-btn-primary min-h-11 gap-1.5 px-3 text-xs font-medium"
          >
            <Check className="h-3.5 w-3.5" />
            {t.approve}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => setPending({ id: draft.id, kind: "revise", text: draft.draft_text ?? draft.draft_excerpt ?? "" })}
            className="ch-btn min-h-11 gap-1.5 px-3 text-xs"
          >
            <Pencil className="h-3.5 w-3.5" />
            {t.revise}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => setPending({ id: draft.id, kind: "dismiss" })}
            className="ch-btn min-h-11 gap-1.5 px-3 text-xs"
          >
            <Trash2 className="h-3.5 w-3.5" />
            {t.dismiss}
          </button>
        </div>
      )}
    </li>
  );
}

function DispositionItemRow({ item, actions }: {
  item: DispositionItem;
  actions: ReturnType<typeof useDispositionActions>;
}) {
  const [pending, setPending] = useState<DispositionPending | null>(null);
  const isPending = pending?.id === item.id ? pending : null;
  const busy = actions.busy;
  const isScopeNote = item.severity === "scope-note";

  async function act(kind: "accept" | "fix" | "dismiss") {
    let ok = false;
    if (kind === "accept") ok = await actions.acceptDisposition(item.id);
    else if (kind === "fix") ok = await actions.createFixTaskFromDisposition(item.id);
    else if (isPending?.kind === "dismiss") ok = await actions.dismissDisposition(item.id, isPending.reason);
    if (ok) setPending(null);
  }

  return (
    <li className="ch-card px-3.5 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn(
          "rounded-full border px-2 py-0.5 text-micro font-medium",
          item.typ === "risk" ? "border-line text-status-warn" : "border-line text-ink-2",
        )}>
          {TYP_LABELS[item.typ] ?? item.typ}
        </span>
        <span className={cn(
          "rounded-full border border-line px-2 py-0.5 text-micro",
          item.severity === "real-risk" ? "text-status-alert" : "text-ink-3",
        )}>
          {SEVERITY_LABELS[item.severity] ?? item.severity}
        </span>
        <span className="font-data text-micro text-ink-3">{DISPOSITION_LABELS[item.disposition] ?? item.disposition}</span>
        <a
          href={fleetTaskLink(item.source_task_id)}
          className="ml-auto inline-flex min-h-11 shrink-0 items-center gap-1 rounded-card border border-line px-2 text-xs text-ink-2 hover:border-live hover:text-ink"
        >
          <Link2 className="h-3.5 w-3.5" />
          {t.openTask}
        </a>
      </div>
      <p className="mt-1.5 break-words text-sm font-medium leading-snug text-ink">
        {item.next_action || item.source_task_id}
      </p>
      {item.evidence ? (
        <p className="mt-1.5 whitespace-pre-wrap break-words rounded-card border border-line-soft bg-surface-1 px-2 py-1.5 font-data text-xs text-ink-2">
          {item.evidence}
        </p>
      ) : null}

      {isPending?.kind === "dismiss" ? (
        <div className="mt-2">
          <label className="text-xs text-ink-3" htmlFor={`dismiss-${item.id}`}>{t.reasonLabel}</label>
          <textarea
            id={`dismiss-${item.id}`}
            className="mt-1 w-full rounded-card border border-line bg-surface-1 px-2 py-1.5 text-xs text-ink placeholder:text-ink-3 focus:border-live focus:outline-none"
            rows={2}
            placeholder={t.reasonPlaceholder}
            value={isPending.reason}
            disabled={busy}
            onChange={(e) => setPending({ id: item.id, kind: "dismiss", reason: e.target.value })}
          />
          <ConfirmBar
            busy={busy}
            confirmLabel={t.dismiss}
            hint={t.dismissHint}
            error={actions.error}
            onConfirm={() => void act("dismiss")}
            onCancel={() => setPending(null)}
          />
        </div>
      ) : isPending ? (
        <ConfirmBar
          busy={busy}
          confirmLabel={isPending.kind === "accept" ? t.accept : t.fixTask}
          hint={isPending.kind === "accept" ? t.acceptHint : t.fixTaskHint}
          error={actions.error}
          onConfirm={() => void act(isPending.kind)}
          onCancel={() => setPending(null)}
        />
      ) : (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => setPending({ id: item.id, kind: "accept" })}
            className="ch-btn min-h-11 gap-1.5 px-3 text-xs"
          >
            <Check className="h-3.5 w-3.5" />
            {t.accept}
          </button>
          {isScopeNote ? (
            <span className="text-xs text-ink-3">{t.scopeNoteHint}</span>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() => setPending({ id: item.id, kind: "fix" })}
              className="ch-btn min-h-11 gap-1.5 px-3 text-xs"
            >
              <Wrench className="h-3.5 w-3.5" />
              {t.fixTask}
            </button>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={() => setPending({ id: item.id, kind: "dismiss", reason: "" })}
            className="ch-btn min-h-11 gap-1.5 px-3 text-xs"
          >
            <Trash2 className="h-3.5 w-3.5" />
            {t.dismiss}
          </button>
        </div>
      )}
    </li>
  );
}

export function ApprovalInbox() {
  const funnel = useFunnelDrafts();
  const disposition = useDispositionItems();
  const funnelActions = useFunnelDraftActions(funnel.reload);
  const dispositionActions = useDispositionActions(disposition.reload);
  const now = nowSec();

  const drafts = funnel.data?.drafts ?? [];
  const items = disposition.data?.items ?? [];
  const settling = (funnel.loading && funnel.data == null) || (disposition.loading && disposition.data == null);
  const loadError = (funnel.data == null && funnel.error) || (disposition.data == null && disposition.error);
  const staleNote = [funnel.data != null && funnel.error, disposition.data != null && disposition.error]
    .filter(Boolean)
    .join(" · ");

  return (
    <section aria-label={t.eyebrow} className="space-y-3">
      <div className="flex items-baseline justify-between gap-3">
        <Eyebrow>{t.eyebrow}</Eyebrow>
        {settling ? null : (
          <span className="shrink-0 truncate text-right text-[11px] text-ink-3">
            {drafts.length + items.length === 0
              ? "ruhig"
              : [
                  drafts.length ? `${drafts.length} ${drafts.length === 1 ? "Draft" : "Drafts"}` : null,
                  items.length ? `${items.length} ${items.length === 1 ? "Item" : "Items"}` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
          </span>
        )}
      </div>

      {settling ? (
        <div className="ch-skeleton h-20 w-full" />
      ) : loadError ? (
        <p className="ch-dashed px-4 py-4 text-sm text-status-warn">{loadError}</p>
      ) : drafts.length + items.length === 0 ? (
        <div className="ch-dashed px-4 py-4">
          <p className="text-sm text-ink-2">{t.empty}</p>
        </div>
      ) : (
        <div className="space-y-4">
          {drafts.length ? (
            <div className="space-y-2">
              <Eyebrow>{t.funnel}</Eyebrow>
              <ul className="space-y-2">
                {drafts.map((draft) => (
                  <FunnelDraftRow key={draft.id} draft={draft} actions={funnelActions} now={now} />
                ))}
              </ul>
            </div>
          ) : null}
          {items.length ? (
            <div className="space-y-2">
              <Eyebrow>{t.disposition}</Eyebrow>
              <ul className="space-y-2">
                {items.map((item) => (
                  <DispositionItemRow key={item.id} item={item} actions={dispositionActions} />
                ))}
              </ul>
            </div>
          ) : null}
          {staleNote ? <p className="text-xs text-status-warn">{staleNote}</p> : null}
        </div>
      )}
    </section>
  );
}
