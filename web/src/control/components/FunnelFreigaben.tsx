import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Lightbulb, Rocket } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { fmtClock } from "../lib/derive";
import { FleetPanel } from "./fleet/atoms";
import { ToneCallout } from "./atoms";
import { Markdown } from "./Markdown";

// Demand-Funnel Freigabe-Queue: fertige Drafts aus dem Wunsch-Trichter
// (family / discord-idee / fo-gap-audit), die auf den Operator-Klick warten.
// Freigeben legt den Build-Task als verlinktes Kind an (ready, Dispatcher
// übernimmt) — danach verschwindet der Draft hier und die Kette übernimmt
// das Flow-Board. Zwei-Schritt-Confirm nach TriageStrip-Muster.
const t = {
  eyebrow: "Funnel-Freigaben",
  meta: "fertige Drafts aus dem Wunsch-Trichter · Freigeben = Build-Task starten",
  empty: "Keine Drafts warten auf Freigabe.",
  approve: "Freigeben → bauen",
  approveHint: "legt den Build-Task an (ready) — der Worker setzt den Draft um, Gates wie immer",
  confirm: "Bestätigen",
  cancel: "Abbrechen",
  showDraft: "Draft ansehen",
  hideDraft: "Draft einklappen",
  noDraft: "Kein Draft-Text gefunden — Referenzen stehen im Ursprungs-Task.",
  done: (id: string) => `Freigegeben — Build-Task ${id} ist eingereiht.`,
};

const SOURCE_LABEL: Record<string, string> = {
  family: "Familie",
  "discord-idee": "Discord-Idee",
  "fo-gap-audit": "Daten-Audit",
};

export interface FunnelDraft {
  id: string;
  title: string;
  created_by: string;
  assignee: string | null;
  completed_at: number;
  draft_excerpt: string | null;
}

interface DraftsResponse {
  drafts: FunnelDraft[];
}

export function FunnelFreigaben() {
  const [data, setData] = useState<DraftsResponse | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await fetchJSON<DraftsResponse>("/api/plugins/kanban/funnel/drafts?days=30"));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), 30000);
    return () => window.clearInterval(id);
  }, [load]);

  const approve = useCallback(async (draft: FunnelDraft) => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetchJSON<{ task: { id: string } }>(
        `/api/plugins/kanban/funnel/drafts/${encodeURIComponent(draft.id)}/approve`,
        { method: "POST" },
      );
      setNotice(t.done(res.task.id));
      void load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setPendingId(null);
    }
  }, [load]);

  // Leere Queue = kein Panel (kein Rauschen für den Nicht-Nutzer).
  if (data !== null && data.drafts.length === 0 && !error && !notice) return null;

  return (
    <FleetPanel eyebrow={t.eyebrow} meta={t.meta}>
      {error ? <ToneCallout tone="red">{error}</ToneCallout> : null}
      {notice ? <ToneCallout tone="emerald">{notice}</ToneCallout> : null}
      {data === null ? <p className="text-sm hc-dim">…</p> : data.drafts.length === 0 ? (
        <p className="text-sm hc-dim">{t.empty}</p>
      ) : (
        <FreigabenList
          drafts={data.drafts}
          pendingId={pendingId}
          openId={openId}
          busy={busy}
          onApprove={(d) => void approve(d)}
          onPending={setPendingId}
          onToggleOpen={(id) => setOpenId(openId === id ? null : id)}
        />
      )}
    </FleetPanel>
  );
}

// Pure Listen-Darstellung — separat exportiert für den statischen Render-Test.
export function FreigabenList({
  drafts,
  pendingId,
  openId,
  busy,
  onApprove,
  onPending,
  onToggleOpen,
}: {
  drafts: FunnelDraft[];
  pendingId: string | null;
  openId: string | null;
  busy: boolean;
  onApprove: (d: FunnelDraft) => void;
  onPending: (id: string | null) => void;
  onToggleOpen: (id: string) => void;
}) {
  return (
    <ul className="space-y-1.5">
      {drafts.map((d) => {
            const isPending = pendingId === d.id;
            const isOpen = openId === d.id;
            return (
              <li key={d.id} className="rounded-md border border-[var(--hc-accent-border)] px-3 py-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <Lightbulb className="h-3.5 w-3.5 shrink-0 text-amber-200" />
                  <span className="min-w-0 flex-1 basis-56 truncate text-[0.85rem] font-medium text-white">{d.title}</span>
                  <span className="hc-mono shrink-0 rounded-full border border-white/15 px-2 py-0.5 text-[0.68rem] hc-soft">
                    {SOURCE_LABEL[d.created_by] ?? d.created_by}
                  </span>
                  <span className="hc-mono shrink-0 text-[0.72rem] hc-dim">{fmtClock(d.completed_at)}</span>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {isPending ? (
                    <>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => onApprove(d)}
                        className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 py-1 text-[0.78rem] font-medium text-[var(--hc-accent-text)] disabled:opacity-50"
                      >
                        <Rocket className="h-3.5 w-3.5" />
                        {t.approve} · {t.confirm}
                      </button>
                      <button type="button" disabled={busy} onClick={() => onPending(null)} className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-3 py-1 text-[0.78rem] hc-soft">{t.cancel}</button>
                      <span className="text-[0.72rem] hc-dim">{t.approveHint}</span>
                    </>
                  ) : (
                    <button type="button" disabled={busy} onClick={() => onPending(d.id)} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-emerald-500/30 px-3 py-1 text-[0.78rem] text-emerald-200 hover:bg-emerald-500/10">
                      <Rocket className="h-3.5 w-3.5" />{t.approve}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => onToggleOpen(d.id)}
                    className="inline-flex min-h-9 items-center gap-1 rounded-md border border-white/10 px-3 py-1 text-[0.78rem] hc-soft hover:bg-white/5"
                  >
                    {isOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                    {isOpen ? t.hideDraft : t.showDraft}
                  </button>
                </div>
                {isOpen ? (
                  <div className="mt-2 rounded-md border border-white/10 bg-black/20 p-3 text-[0.8rem]">
                    {d.draft_excerpt ? <Markdown body={d.draft_excerpt} /> : <p className="hc-dim">{t.noDraft}</p>}
                  </div>
                ) : null}
              </li>
            );
      })}
    </ul>
  );
}
