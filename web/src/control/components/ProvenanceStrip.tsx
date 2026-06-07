import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import type { VaultProvenanceResponse } from "../lib/types";
import { Led } from "./atoms";

interface Props {
  data: VaultProvenanceResponse | null;
  error?: string | null;
}

// Compact "wer arbeitet gerade / wer lieferte zuletzt" tile for the Übersicht.
// Mirrors the shared Vault coordination board + receipts (one source of truth via
// the same activity-overview helper the CLI uses). Read-only.
export function ProvenanceStrip({ data, error }: Props) {
  const isUnknown = !data || Boolean(error);
  const opens = data?.open_sessions ?? [];
  const receipts = data?.recent_receipts ?? [];
  const stale = data?.stale_count ?? 0;
  const detail = error ?? data?.error ?? null;

  return (
    <section className="hc-card border border-zinc-600/25 bg-zinc-600/10 px-3 py-2" title={detail || de.provenance.title}>
      <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-normal text-zinc-200">
        <Led kind={isUnknown ? "idle" : stale > 0 ? "warn" : "live"} size={9} />
        <span>{de.provenance.title}</span>
        {stale > 0 ? (
          <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-200">
            {de.provenance.staleBadge(stale)}
          </span>
        ) : null}
      </div>

      {detail ? (
        <p className="mt-2 text-[11px] text-amber-200/90">{detail}</p>
      ) : (
        <div className="mt-2 grid gap-3 sm:grid-cols-2">
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-normal hc-dim">{de.provenance.openTitle}</div>
            {opens.length === 0 ? (
              <p className="mt-1 text-[11px] hc-dim">{de.provenance.openEmpty}</p>
            ) : (
              <ul className="mt-1 space-y-1">
                {opens.map((s) => (
                  <li key={s.path} className="min-w-0 truncate text-[11px] text-white" title={`${s.agent} · ${s.task}`}>
                    <span className="hc-mono text-zinc-300">[{s.agent}]</span> {s.task}
                    {s.stale ? <span className="ml-1 text-amber-300">⚠ {de.provenance.staleInline}</span> : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-normal hc-dim">{de.provenance.recentTitle}</div>
            {receipts.length === 0 ? (
              <p className="mt-1 text-[11px] hc-dim">—</p>
            ) : (
              <ul className="mt-1 space-y-1">
                {receipts.slice(0, 5).map((r) => (
                  <li key={r.path} className={cn("min-w-0 truncate text-[11px] hc-soft")} title={r.path}>
                    <span className="hc-mono text-zinc-400">{r.when}</span>{" "}
                    <span className="text-zinc-300">[{r.agent}]</span> {r.file}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
