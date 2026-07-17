import { ExternalLink, TriangleAlert } from "lucide-react";

import { de } from "../../i18n/de";
import { CommissionButton } from "../../components/fleet/CommissionButton";
import { DrawerShell } from "../../components/leitstand";
import { Eyebrow } from "../../components/primitives";
import { nextActionForFoItem } from "../../lib/foBacklog";
import type { BacklogDetail, BacklogItem } from "../../lib/schemas";
import type { CommissionState } from "../../hooks/commissionCapture";
import { CopyButton } from "./CopyButton";
import { operatorBrief, RISK_TONE, sourceRef, STATUS_TONE } from "./shared";

function SectionLines({ title, lines, fallback }: { title: string; lines: string[] | undefined; fallback: string }) {
  const visible = lines?.filter((line) => line.trim() !== "") ?? [];
  return (
    <section className="border-t border-line-soft pt-3">
      <Eyebrow>{title}</Eyebrow>
      {visible.length ? (
        <ul className="mt-2 space-y-1.5">
          {visible.map((line, index) => (
            <li key={`${title}-${index}-${line}`} className="break-words text-body text-ink">{line}</li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-body text-ink-2">{fallback}</p>
      )}
    </section>
  );
}

function DetailMetric({ label, value, tone }: { label: string; value: string; tone?: "ok" | "warn" | "alert" }) {
  const dotClass = tone === "ok" ? "bg-status-ok" : tone === "warn" ? "bg-status-warn" : tone === "alert" ? "bg-status-alert" : "bg-ink-3";
  return (
    <div className="min-w-0 rounded-card border border-line bg-surface-2 px-3 py-2.5">
      <Eyebrow>{label}</Eyebrow>
      <div className="mt-1 flex items-center gap-1.5 text-sec font-medium text-ink">
        {tone ? <span aria-hidden className={`size-1.5 shrink-0 rounded-full ${dotClass}`} /> : null}
        <span className="truncate">{value}</span>
      </div>
    </div>
  );
}

function metricTone(tone: string | undefined): "ok" | "warn" | "alert" | undefined {
  if (tone === "emerald") return "ok";
  if (tone === "amber") return "warn";
  if (tone === "red" || tone === "rose") return "alert";
  return undefined;
}

export interface FoDetailContentProps {
  item: BacklogItem;
  detail?: BacklogDetail;
  loading: boolean;
  error?: string;
  commissionPrompt?: string;
  onCommission?: () => void;
  commissionState?: CommissionState;
  commissionError?: string;
}

export function FoDetailContent({
  item,
  detail,
  loading,
  error,
  commissionPrompt,
  onCommission,
  commissionState,
  commissionError,
}: FoDetailContentProps) {
  const brief = operatorBrief(item, detail);

  return (
    <div className="space-y-4">
          {error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{error}</div> : null}
          {loading && !detail ? <div className="rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink-2">{de.backlog.loading}</div> : null}

          <section className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <DetailMetric label="Status" value={item.status || "-"} tone={metricTone(STATUS_TONE[item.status])} />
            <DetailMetric label="Risk" value={item.risk || "-"} tone={metricTone(RISK_TONE[item.risk] ?? "amber")} />
            <DetailMetric label="Owner" value={item.owner || "-"} tone={item.owner ? undefined : "warn"} />
            <DetailMetric label="Area" value={item.area || "-"} />
          </section>

          <section className="rounded-card border border-line bg-surface-2 px-3 py-2">
            <Eyebrow>Next Action</Eyebrow>
            <p className="mt-1 text-body text-ink">{nextActionForFoItem(item, detail)}</p>
          </section>

          <SectionLines title="Decision / Why now" lines={detail?.decision} fallback={item.excerpt || "Keine explizite Entscheidung im Body gefunden."} />
          <SectionLines title="Acceptance Criteria" lines={detail?.acceptance_criteria} fallback="Keine Akzeptanzkriterien gefunden." />
          <SectionLines title="Current Evidence / Last Proof" lines={detail?.proofs} fallback={item.result || "Kein letzter Beleg gefunden."} />
          <SectionLines title="Blockers" lines={detail?.blockers} fallback="Keine Blocker im Body gefunden." />

          <section className="border-t border-line-soft pt-3">
            <Eyebrow>Source path/ref</Eyebrow>
            <dl className="mt-2 space-y-2 text-body">
              <div><dt className="font-display text-micro uppercase tracking-[0.08em] text-ink-3">Path</dt><dd className="break-words font-data text-ink">{detail?.source_path || sourceRef(item)}</dd></div>
              <div><dt className="font-display text-micro uppercase tracking-[0.08em] text-ink-3">Ref</dt><dd className="break-words font-data text-ink">{detail?.source_ref || "git:origin/main"}</dd></div>
            </dl>
          </section>

          {onCommission ? (
            <section className="border-t border-line-soft pt-3">
              <CommissionButton variant="full" state={commissionState} onClick={() => onCommission()} />
              {commissionState === "done" ? (
                <p className="mt-2 text-sec text-status-ok">In der Fleet geparkt (Stufe Plan) — im Hermes-Tab mit Dispatch starten, dann Verify / Ship.</p>
              ) : commissionState === "error" && commissionError ? (
                <p className="mt-2 text-sec text-status-alert">{de.fleet.commissionFailed}: {commissionError}</p>
              ) : (
                <p className="mt-2 text-sec text-ink-3">{de.fleet.commissionTitle}</p>
              )}
            </section>
          ) : null}

          <div className="grid gap-2 sm:grid-cols-2">
            <CopyButton text={brief} label="Copy operator brief" copiedLabel="Brief kopiert" />
            <CopyButton text={commissionPrompt} label="Copy implementation prompt" copiedLabel={de.backlog.commissionCopied} />
          </div>

          {detail?.links?.length ? (
            <section className="border-t border-line-soft pt-3">
              <Eyebrow>Links</Eyebrow>
              <div className="mt-2 space-y-1">
                {detail.links.map((link) => (
                  <a key={`${link.label}-${link.href}`} href={link.href} target="_blank" rel="noreferrer" className="flex min-h-12 min-w-0 items-center gap-2 text-body text-live hover:text-bronze-hi">
                    <ExternalLink className="h-3.5 w-3.5 shrink-0" />
                    <span className="truncate">{link.label}</span>
                  </a>
                ))}
              </div>
            </section>
          ) : null}
    </div>
  );
}

export function FoDetailDrawer({
  item,
  detail,
  onClose,
  ...contentProps
}: FoDetailContentProps & { onClose: () => void }) {
  return (
    <DrawerShell
      eyebrow="Backlog-Detail"
      title={item.title}
      ariaLabel={`Backlog-Detail: ${item.title}`}
      onClose={onClose}
      headerExtra={(
        <p className="mt-1 truncate font-data text-micro tabular-nums text-ink-3">
          {detail?.source_path || sourceRef(item)}
        </p>
      )}
    >
      <FoDetailContent item={item} detail={detail} {...contentProps} />
    </DrawerShell>
  );
}
