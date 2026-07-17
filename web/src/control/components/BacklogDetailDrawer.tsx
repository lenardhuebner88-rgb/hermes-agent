import { useEffect, useRef, useState } from "react";
import { Check, ClipboardCopy, ExternalLink, TriangleAlert, X } from "lucide-react";
import { m, useReducedMotion } from "motion/react";
import { cn } from "@/lib/utils";
import { DUR, EASE_OUT, EASE_RISE } from "../lib/motion";
import { Eyebrow } from "./primitives";
import { SignalChip, type SignalTone } from "./leitstand";
import { CommissionButton } from "./fleet/CommissionButton";
import { Markdown } from "./Markdown";
import { de } from "../i18n/de";
import type { CommissionState } from "../hooks/useControlData";

interface BacklogDetailDrawerProps {
  title: string;
  id: string;
  chips?: Array<{ label: string; tone?: SignalTone }>;
  fields?: Array<{ label: string; value: string }>;
  proofTimeline?: string[];
  nextAction?: string;
  sourceRef?: Array<{ label: string; value: string }>;
  links?: Array<{ label: string; href: string }>;
  body: string;
  loading?: boolean;
  error?: string;
  commissionPrompt?: string;
  operatorBrief?: string;
  /** When set, renders the "create real Kanban card" button (Backlog→Kanban). */
  onCommission?: () => void;
  commissionState?: CommissionState;
  commissionError?: string;
  onClose: () => void;
}

export function BacklogDetailDrawer({
  title,
  id,
  chips,
  fields,
  proofTimeline,
  nextAction,
  sourceRef,
  links,
  body,
  loading = false,
  error,
  commissionPrompt,
  operatorBrief,
  onCommission,
  commissionState,
  commissionError,
  onClose,
}: BacklogDetailDrawerProps) {
  const [copied, setCopied] = useState(false);
  const [briefCopied, setBriefCopied] = useState(false);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const reduce = useReducedMotion();

  useEffect(() => {
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      previouslyFocused?.focus();
    };
  }, [onClose]);

  const visibleFields = fields?.filter((field) => field.value.trim() !== "") ?? [];
  const visibleSourceRef = sourceRef?.filter((field) => field.value.trim() !== "") ?? [];
  const visibleProofs = proofTimeline?.filter((line) => line.trim() !== "") ?? [];
  const visibleLinks = links?.filter((link) => link.label.trim() !== "" && link.href.trim() !== "") ?? [];
  const backdrop = reduce
    ? { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 }, transition: { duration: 0 } }
    : { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 }, transition: { duration: DUR.med, ease: EASE_OUT } };
  const drawer = reduce
    ? { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 }, transition: { duration: 0 } }
    : { initial: { opacity: 0, x: 32 }, animate: { opacity: 1, x: 0 }, exit: { opacity: 0, x: 32 }, transition: { duration: DUR.slow, ease: EASE_RISE } };

  const copyCommission = async () => {
    if (!commissionPrompt) return;
    try {
      await navigator.clipboard.writeText(commissionPrompt);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard blocked — non-critical */
    }
  };

  const copyBrief = async () => {
    if (!operatorBrief) return;
    try {
      await navigator.clipboard.writeText(operatorBrief);
      setBriefCopied(true);
      window.setTimeout(() => setBriefCopied(false), 1800);
    } catch {
      /* clipboard blocked — non-critical */
    }
  };

  return (
    <div className="fixed inset-0 z-50">
      <m.button
        type="button"
        className="absolute inset-0 bg-surface-0/80"
        aria-label={de.orchestrator.drawerClose}
        onClick={onClose}
        initial={backdrop.initial}
        animate={backdrop.animate}
        exit={backdrop.exit}
        transition={backdrop.transition}
      />
      <m.aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="backlog-detail-title"
        className={cn(
          "absolute right-0 top-0 flex h-full w-full max-w-md flex-col rounded-none border-y-0 border-l border-line bg-surface-1 shadow-2xl",
        )}
        initial={drawer.initial}
        animate={drawer.animate}
        exit={drawer.exit}
        transition={drawer.transition}
      >
        <header className="flex items-start justify-between gap-3 border-b border-line-soft p-5 pb-4">
          <div className="min-w-0">
            <h2 id="backlog-detail-title" className="truncate font-display text-h2 font-semibold text-ink">
              {title}
            </h2>
            <p className="mt-1 truncate font-data text-micro tabular-nums text-ink-3">{id}</p>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            className="grid size-12 shrink-0 place-items-center rounded-card border border-line bg-surface-2 text-ink-2 hover:bg-surface-3 hover:text-ink focus:outline-none focus:ring-2 focus:ring-live/60"
            aria-label={de.orchestrator.drawerClose}
            onClick={onClose}
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
          {chips?.length ? (
            <div className="flex flex-wrap gap-2">
              {chips.map((chip, index) => (
                <SignalChip
                  key={`${index}-${chip.label}`}
                  tone={chip.tone ?? "neutral"}
                  label={chip.label}
                />
              ))}
            </div>
          ) : null}

          {nextAction ? (
            <section className="rounded-card border border-line bg-surface-2 px-3 py-2">
              <Eyebrow>{de.orchestrator.detailNextAction}</Eyebrow>
              <p className="mt-1 text-sec text-ink">{nextAction}</p>
            </section>
          ) : null}

          <section className="rounded-card border border-line bg-surface-2 px-3 py-2">
            <Eyebrow>{de.orchestrator.detailProofTimeline}</Eyebrow>
            {visibleProofs.length ? (
              <ol className="mt-2 space-y-2">
                {visibleProofs.map((line, index) => (
                  <li key={`${index}-${line}`} className="grid grid-cols-[auto_minmax(0,1fr)] gap-2 text-sec text-ink">
                    <span className="font-data text-micro tabular-nums text-ink-3">{index + 1}</span>
                    <span className="break-words">{line}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="mt-1 text-sec text-ink-2">{de.orchestrator.proofMissing}</p>
            )}
          </section>

          {visibleSourceRef.length ? (
            <section className="rounded-card border border-line bg-surface-2 px-3 py-2">
              <Eyebrow>{de.orchestrator.detailSourceRef}</Eyebrow>
              <dl className="mt-2 space-y-2">
                {visibleSourceRef.map((field, index) => (
                  <div key={`${index}-${field.label}`}>
                    <dt className="font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3">{field.label}</dt>
                    <dd className="mt-0.5 whitespace-pre-wrap break-words font-data text-sec tabular-nums text-ink">{field.value}</dd>
                  </div>
                ))}
              </dl>
            </section>
          ) : null}

          {visibleLinks.length ? (
            <section className="rounded-card border border-line bg-surface-2 px-3 py-2">
              <Eyebrow>{de.orchestrator.detailLinks}</Eyebrow>
              <div className="mt-2 space-y-1">
                {visibleLinks.map((link) => (
                  <a
                    key={`${link.href}-${link.label}`}
                    href={link.href}
                    target="_blank"
                    rel="noreferrer"
                    className="flex min-h-12 items-center gap-2 rounded-card px-2 text-sec text-live hover:bg-surface-3 hover:text-bronze-hi focus:outline-none focus:ring-2 focus:ring-live/60"
                  >
                    <ExternalLink className="h-3.5 w-3.5 shrink-0" />
                    <span className="break-words">{link.label}</span>
                  </a>
                ))}
              </div>
            </section>
          ) : null}

          {visibleFields.length ? (
            <dl className="space-y-2">
              {visibleFields.map((field, index) => (
                <div
                  key={`${index}-${field.label}`}
                  className="rounded-card border border-line bg-surface-2 px-3 py-2"
                >
                  <dt className="font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3">{field.label}</dt>
                  <dd className="mt-1 whitespace-pre-wrap break-words text-sec text-ink">{field.value}</dd>
                </div>
              ))}
            </dl>
          ) : null}

          {onCommission ? (
            <section className="rounded-card border border-line bg-surface-2 px-3 py-3">
              <CommissionButton
                variant="full"
                state={commissionState}
                onClick={() => onCommission()}
              />
              {commissionState === "done" ? (
                <p className="mt-2 flex items-start gap-2 text-sec text-status-ok"><span aria-hidden className="mt-1 size-1.5 shrink-0 rounded-full bg-status-ok" />In der Fleet geparkt (Stufe Plan) — im Hermes-Tab mit Dispatch starten.</p>
              ) : commissionState === "error" && commissionError ? (
                <p className="mt-2 flex items-start gap-2 text-sec text-status-alert"><span aria-hidden className="mt-1 size-1.5 shrink-0 rounded-full bg-status-alert" />{de.fleet.commissionFailed}: {commissionError}</p>
              ) : (
                <p className="mt-2 text-sec text-ink-3">{de.fleet.commissionTitle}</p>
              )}
            </section>
          ) : null}

          {operatorBrief ? (
            <button
              type="button"
              onClick={copyBrief}
              className={cn(
                "flex min-h-12 w-full items-center justify-center gap-2 rounded-card border border-live bg-live/10 px-3 text-sec font-medium text-bronze-hi transition hover:bg-live/20 focus:outline-none focus:ring-2 focus:ring-live/60",
              )}
            >
              {briefCopied ? <Check className="h-4 w-4" /> : <ClipboardCopy className="h-4 w-4" />}
              {briefCopied ? de.orchestrator.operatorBriefCopied : de.orchestrator.operatorBrief}
            </button>
          ) : null}

          {commissionPrompt ? (
            <button
              type="button"
              onClick={copyCommission}
              className={cn(
                "flex min-h-12 w-full items-center justify-center gap-2 rounded-card border border-live bg-live/10 px-3 text-sec font-medium text-bronze-hi transition hover:bg-live/20 focus:outline-none focus:ring-2 focus:ring-live/60",
              )}
            >
              {copied ? <Check className="h-4 w-4" /> : <ClipboardCopy className="h-4 w-4" />}
              {copied ? de.orchestrator.commissionCopied : de.orchestrator.commissionDrawer}
            </button>
          ) : null}

          {error ? (
            <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert">
              <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
              <p className="whitespace-pre-wrap break-words font-data">{error}</p>
            </div>
          ) : loading ? (
            <div className="rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink-2">
              <p className="whitespace-pre-wrap break-words">{de.orchestrator.loading}</p>
            </div>
          ) : (
            <section className="rounded-card border border-line bg-surface-2 px-4 py-3">
              <Eyebrow className="mb-2">{de.orchestrator.detailSpec}</Eyebrow>
              <Markdown body={body} />
            </section>
          )}
        </div>
      </m.aside>
    </div>
  );
}
