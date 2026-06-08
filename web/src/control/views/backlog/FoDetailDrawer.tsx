import { useEffect } from "react";
import { ExternalLink, X } from "lucide-react";
import { motion, useReducedMotion } from "motion/react";

import { de } from "../../i18n/de";
import { ToneCallout } from "../../components/atoms";
import { CommissionButton } from "../../components/fleet/CommissionButton";
import { Stat } from "../../components/primitives";
import { DUR, EASE_OUT, EASE_RISE } from "../../lib/motion";
import { nextActionForFoItem } from "../../lib/foBacklog";
import type { BacklogDetail, BacklogItem } from "../../lib/schemas";
import type { CommissionState } from "../../hooks/useControlData";
import { CopyButton } from "./CopyButton";
import { operatorBrief, RISK_TONE, sourceRef, STATUS_TONE } from "./shared";

function SectionLines({ title, lines, fallback }: { title: string; lines: string[] | undefined; fallback: string }) {
  const visible = lines?.filter((line) => line.trim() !== "") ?? [];
  return (
    <section className="border-t border-[var(--hc-border)] pt-3">
      <h3 className="text-[11px] font-semibold uppercase text-zinc-400">{title}</h3>
      {visible.length ? (
        <ul className="mt-2 space-y-1.5">
          {visible.map((line, index) => (
            <li key={`${title}-${index}-${line}`} className="break-words text-sm text-zinc-100">{line}</li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-sm hc-soft">{fallback}</p>
      )}
    </section>
  );
}

export function FoDetailDrawer({
  item,
  detail,
  loading,
  error,
  commissionPrompt,
  onCommission,
  commissionState,
  commissionError,
  onClose,
}: {
  item: BacklogItem;
  detail?: BacklogDetail;
  loading: boolean;
  error?: string;
  commissionPrompt?: string;
  onCommission?: () => void;
  commissionState?: CommissionState;
  commissionError?: string;
  onClose: () => void;
}) {
  const reduce = useReducedMotion();
  const brief = operatorBrief(item, detail);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const backdrop = reduce
    ? { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 }, transition: { duration: 0 } }
    : { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 }, transition: { duration: DUR.med, ease: EASE_OUT } };
  const drawer = reduce
    ? { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 }, transition: { duration: 0 } }
    : { initial: { opacity: 0, x: 32 }, animate: { opacity: 1, x: 0 }, exit: { opacity: 0, x: 32 }, transition: { duration: DUR.slow, ease: EASE_RISE } };

  return (
    <div className="fixed inset-0 z-50">
      <motion.button
        type="button"
        className="absolute inset-0 bg-black/60"
        aria-label="Schließen"
        onClick={onClose}
        initial={backdrop.initial}
        animate={backdrop.animate}
        exit={backdrop.exit}
        transition={backdrop.transition}
      />
      <motion.aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="fo-detail-title"
        className="hc-surface-raised absolute right-0 top-0 flex h-full w-full max-w-xl flex-col rounded-none border-y-0 border-l border-[var(--hc-border)] shadow-2xl"
        initial={drawer.initial}
        animate={drawer.animate}
        exit={drawer.exit}
        transition={drawer.transition}
      >
        <header className="flex items-start justify-between gap-3 border-b border-[var(--hc-border)] p-5">
          <div className="min-w-0">
            <h2 id="fo-detail-title" className="text-lg font-semibold text-white">{item.title}</h2>
            <p className="mt-1 truncate text-xs hc-mono hc-dim">{detail?.source_path || sourceRef(item)}</p>
          </div>
          <button type="button" className="grid h-9 w-9 shrink-0 place-items-center rounded-md border border-white/10 bg-white/[.03] text-zinc-200 hover:bg-white/[.07]" aria-label="Schließen" onClick={onClose}>
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
          {error ? <ToneCallout tone="red">{error}</ToneCallout> : null}
          {loading && !detail ? <ToneCallout tone="zinc">{de.backlog.loading}</ToneCallout> : null}

          <section className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="Status" value={item.status || "-"} tone={STATUS_TONE[item.status] ?? "zinc"} />
            <Stat label="Risk" value={item.risk || "-"} tone={RISK_TONE[item.risk] ?? "amber"} />
            <Stat label="Owner" value={item.owner || "-"} tone={item.owner ? "cyan" : "amber"} />
            <Stat label="Area" value={item.area || "-"} tone="zinc" />
          </section>

          <section className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-2">
            <h3 className="text-[11px] font-semibold uppercase text-emerald-200">Next Action</h3>
            <p className="mt-1 text-sm text-white">{nextActionForFoItem(item, detail)}</p>
          </section>

          <SectionLines title="Decision / Why now" lines={detail?.decision} fallback={item.excerpt || "Keine explizite Entscheidung im Body gefunden."} />
          <SectionLines title="Acceptance Criteria" lines={detail?.acceptance_criteria} fallback="Keine Akzeptanzkriterien gefunden." />
          <SectionLines title="Current Evidence / Last Proof" lines={detail?.proofs} fallback={item.result || "Kein letzter Beleg gefunden."} />
          <SectionLines title="Blockers" lines={detail?.blockers} fallback="Keine Blocker im Body gefunden." />

          <section className="border-t border-[var(--hc-border)] pt-3">
            <h3 className="text-[11px] font-semibold uppercase text-zinc-400">Source path/ref</h3>
            <dl className="mt-2 space-y-2 text-sm">
              <div><dt className="text-[10px] uppercase text-zinc-500">Path</dt><dd className="break-words hc-mono text-zinc-100">{detail?.source_path || sourceRef(item)}</dd></div>
              <div><dt className="text-[10px] uppercase text-zinc-500">Ref</dt><dd className="break-words hc-mono text-zinc-100">{detail?.source_ref || "git:origin/main"}</dd></div>
            </dl>
          </section>

          {onCommission ? (
            <section className="border-t border-[var(--hc-border)] pt-3">
              <CommissionButton variant="full" state={commissionState} onClick={() => onCommission()} />
              {commissionState === "done" ? (
                <p className="mt-2 text-xs text-emerald-300">In der Fleet geparkt (Stufe Plan) — im Hermes-Tab mit Dispatch starten, dann Verify / Ship.</p>
              ) : commissionState === "error" && commissionError ? (
                <p className="mt-2 text-xs text-red-300">{de.fleet.commissionFailed}: {commissionError}</p>
              ) : (
                <p className="mt-2 text-xs hc-dim">{de.fleet.commissionTitle}</p>
              )}
            </section>
          ) : null}

          <div className="grid gap-2 sm:grid-cols-2">
            <CopyButton text={brief} label="Copy operator brief" copiedLabel="Brief kopiert" />
            <CopyButton text={commissionPrompt} label="Copy implementation prompt" copiedLabel={de.backlog.commissionCopied} />
          </div>

          {detail?.links?.length ? (
            <section className="border-t border-[var(--hc-border)] pt-3">
              <h3 className="text-[11px] font-semibold uppercase text-zinc-400">Links</h3>
              <div className="mt-2 space-y-1">
                {detail.links.map((link) => (
                  <a key={`${link.label}-${link.href}`} href={link.href} target="_blank" rel="noreferrer" className="flex min-w-0 items-center gap-2 text-sm text-cyan-200 hover:text-cyan-100">
                    <ExternalLink className="h-3.5 w-3.5 shrink-0" />
                    <span className="truncate">{link.label}</span>
                  </a>
                ))}
              </div>
            </section>
          ) : null}
        </div>
      </motion.aside>
    </div>
  );
}
