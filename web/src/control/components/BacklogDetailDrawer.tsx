import { useEffect, useRef, useState } from "react";
import { Check, ClipboardCopy, ExternalLink, X } from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import { cn } from "@/lib/utils";
import type { ToneName } from "../lib/types";
import { toneClasses } from "../lib/tones";
import { tokens } from "../lib/tokens";
import { DUR, EASE_OUT, EASE_RISE } from "../lib/motion";
import { StatusPill, ToneCallout } from "./atoms";
import { CommissionButton } from "./fleet/CommissionButton";
import { Markdown } from "./Markdown";
import { de } from "../i18n/de";
import type { CommissionState } from "../hooks/useControlData";

interface BacklogDetailDrawerProps {
  title: string;
  id: string;
  chips?: Array<{ label: string; tone?: string }>;
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

const supportedTones: ToneName[] = ["emerald", "cyan", "sky", "indigo", "amber", "rose", "red", "zinc", "violet"];

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
      <motion.button
        type="button"
        className="absolute inset-0 bg-black/60"
        aria-label={de.orchestrator.drawerClose}
        onClick={onClose}
        initial={backdrop.initial}
        animate={backdrop.animate}
        exit={backdrop.exit}
        transition={backdrop.transition}
      />
      <motion.aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="backlog-detail-title"
        className={cn(
          "hc-card absolute right-0 top-0 flex h-full w-full max-w-md flex-col rounded-none border-y-0 border-l shadow-2xl",
          "border-[var(--hc-border)]",
        )}
        initial={drawer.initial}
        animate={drawer.animate}
        exit={drawer.exit}
        transition={drawer.transition}
      >
        <header className="flex items-start justify-between gap-3 border-b border-[var(--hc-border)] p-5 pb-4">
          <div className="min-w-0">
            <h2 id="backlog-detail-title" className="truncate text-lg font-semibold text-white">
              {title}
            </h2>
            <p className="mt-1 truncate text-xs hc-mono hc-dim">{id}</p>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            className="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-white/10 bg-white/[.03] text-zinc-200 hover:bg-white/[.07]"
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
                <StatusPill
                  key={`${index}-${chip.label}`}
                  tone={normalizeTone(chip.tone)}
                  label={chip.label}
                  size="sm"
                />
              ))}
            </div>
          ) : null}

          {nextAction ? (
            <section className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-3 py-2">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-emerald-200">{de.orchestrator.detailNextAction}</h3>
              <p className="mt-1 text-sm text-white">{nextAction}</p>
            </section>
          ) : null}

          <section className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide hc-dim">{de.orchestrator.detailProofTimeline}</h3>
            {visibleProofs.length ? (
              <ol className="mt-2 space-y-2">
                {visibleProofs.map((line, index) => (
                  <li key={`${index}-${line}`} className="grid grid-cols-[auto_minmax(0,1fr)] gap-2 text-sm text-white">
                    <span className="hc-mono text-xs hc-dim">{index + 1}</span>
                    <span className="break-words">{line}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="mt-1 text-sm hc-soft">{de.orchestrator.proofMissing}</p>
            )}
          </section>

          {visibleSourceRef.length ? (
            <section className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2">
              <h3 className="text-xs font-semibold uppercase tracking-wide hc-dim">{de.orchestrator.detailSourceRef}</h3>
              <dl className="mt-2 space-y-2">
                {visibleSourceRef.map((field, index) => (
                  <div key={`${index}-${field.label}`}>
                    <dt className="text-[11px] font-semibold uppercase tracking-wide hc-dim">{field.label}</dt>
                    <dd className="mt-0.5 whitespace-pre-wrap break-words text-sm text-white">{field.value}</dd>
                  </div>
                ))}
              </dl>
            </section>
          ) : null}

          {visibleLinks.length ? (
            <section className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2">
              <h3 className="text-xs font-semibold uppercase tracking-wide hc-dim">{de.orchestrator.detailLinks}</h3>
              <div className="mt-2 space-y-1">
                {visibleLinks.map((link) => (
                  <a
                    key={`${link.href}-${link.label}`}
                    href={link.href}
                    target="_blank"
                    rel="noreferrer"
                    className="flex min-h-9 items-center gap-2 rounded-md px-2 text-sm text-cyan-200 hover:bg-cyan-500/10 focus:outline-none focus:ring-2 focus:ring-cyan-400/60"
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
                  className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2"
                  style={{ borderRadius: tokens.radius.lg }}
                >
                  <dt className="text-xs font-semibold uppercase tracking-wide hc-dim">{field.label}</dt>
                  <dd className="mt-1 whitespace-pre-wrap break-words text-sm text-white">{field.value}</dd>
                </div>
              ))}
            </dl>
          ) : null}

          {onCommission ? (
            <section className="rounded-lg border border-[var(--hc-border)] bg-white/[.02] px-3 py-3">
              <CommissionButton variant="full" state={commissionState} onClick={() => onCommission()} />
              {commissionState === "done" ? (
                <p className="mt-2 text-xs text-emerald-300">In der Fleet geparkt (Stufe Plan) — im Hermes-Tab mit Dispatch starten.</p>
              ) : commissionState === "error" && commissionError ? (
                <p className="mt-2 text-xs text-red-300">{de.fleet.commissionFailed}: {commissionError}</p>
              ) : (
                <p className="mt-2 text-xs hc-dim">{de.fleet.commissionTitle}</p>
              )}
            </section>
          ) : null}

          {operatorBrief ? (
            <button
              type="button"
              onClick={copyBrief}
              className={cn(
                "flex w-full items-center justify-center gap-2 rounded-lg border px-3 py-2.5 text-sm font-medium transition focus:outline-none focus:ring-2 focus:ring-cyan-400/60",
                briefCopied
                  ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                  : "border-white/15 bg-white/[.03] text-zinc-100 hover:bg-white/[.06]",
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
                "flex w-full items-center justify-center gap-2 rounded-lg border px-3 py-2.5 text-sm font-medium transition",
                copied
                  ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                  : "border-cyan-500/30 bg-cyan-500/10 text-cyan-200 hover:bg-cyan-500/15",
              )}
            >
              {copied ? <Check className="h-4 w-4" /> : <ClipboardCopy className="h-4 w-4" />}
              {copied ? de.orchestrator.commissionCopied : de.orchestrator.commissionDrawer}
            </button>
          ) : null}

          {error ? (
            <ToneCallout tone="red">
              <p className="whitespace-pre-wrap break-words hc-mono">{error}</p>
            </ToneCallout>
          ) : loading ? (
            <div className={cn("rounded-lg border px-3 py-2 text-sm", toneClasses("zinc"))}>
              <p className="whitespace-pre-wrap break-words hc-mono">{de.orchestrator.loading}</p>
            </div>
          ) : (
            <section className="rounded-lg border border-white/10 bg-white/[.03] px-4 py-3">
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide hc-dim">{de.orchestrator.detailSpec}</h3>
              <Markdown body={body} />
            </section>
          )}
        </div>
      </motion.aside>
    </div>
  );
}

function normalizeTone(tone?: string): ToneName {
  if (tone && supportedTones.includes(tone as ToneName)) return tone as ToneName;
  return "zinc";
}
