import { AlertTriangle, FileText } from "lucide-react";
import { DrawerShell } from "../../components/leitstand";
import { ProseMarkdown } from "../../components/ProseMarkdown";
import { SkeletonCard } from "../../components/primitives";
import { useProjectReceipt } from "../../hooks/useControlData";
import { de } from "../../i18n/de";
import { fmtRelativeTime, nowSec } from "../../lib/derive";
import type { ProjectReceiptEntry } from "../../lib/schemas";
import { receiptEpoch } from "./derive";

const t = de.projekte;

/** Lese-Sheet für ein einzelnes Agent-Receipt (Stage 12). Header (Titel,
 *  Agent-Badge, relatives Alter) kommt aus der Feed-Zeile — sofort sichtbar;
 *  der Markdown-Body wird erst beim Öffnen geholt (mounted-only Hook, gleiche
 *  Doktrin wie useProjectDetail). Renderer ist der geteilte ProseMarkdown
 *  (gleiche Lesetypografie wie der Bibliothek-Lesesaal; HTML bleibt aus).
 *  `truncated` zeigt einen ruhigen Hinweis statt still zu kürzen. */
export function ReceiptSheet({
  receipt,
  onClose,
}: {
  receipt: ProjectReceiptEntry;
  onClose: () => void;
}) {
  const content = useProjectReceipt(receipt.agent, receipt.filename);
  const data = content.data;
  const now = nowSec();
  const title = receipt.title || receipt.filename;

  return (
    <DrawerShell
      eyebrow={t.receiptsEyebrow}
      title={title}
      icon={FileText}
      onClose={onClose}
      ariaLabel={title}
      closeLabel={t.receiptSheetClose}
      headerExtra={
        <p className="mt-2 flex min-w-0 flex-wrap items-center gap-x-1.5 text-micro text-ink-3">
          <span className="inline-flex max-w-full items-center rounded-card border border-line bg-surface-1 px-1.5 py-0.5 font-data text-ink-2">
            <span className="truncate">{receipt.agent || "—"}</span>
          </span>
          <span className="shrink-0 font-data tabular-nums">
            {fmtRelativeTime(receiptEpoch(receipt.mtime), now)}
          </span>
        </p>
      }
      widthClassName="tab:w-[min(560px,75vw)]"
    >
      {content.loading && !data ? <SkeletonCard rows={5} /> : null}
      {content.error ? (
        <div
          role="alert"
          className="mb-3 flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"
        >
          <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
          {t.receiptSheetError}
        </div>
      ) : null}
      {data ? (
        <div className="min-w-0 space-y-3">
          {data.truncated ? (
            <p className="rounded-card border border-line-soft bg-surface-2 px-3 py-2 text-micro text-ink-2">
              {t.receiptTruncated}
            </p>
          ) : null}
          <ProseMarkdown>{data.markdown}</ProseMarkdown>
        </div>
      ) : null}
    </DrawerShell>
  );
}
