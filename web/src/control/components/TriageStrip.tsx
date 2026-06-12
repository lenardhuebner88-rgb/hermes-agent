import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, RotateCw, Zap } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { fmtClock } from "../lib/derive";
import { profileLabel } from "../lib/tones";
import { FleetPanel } from "./fleet/atoms";
import { ToneCallout } from "./atoms";
import {
  ESCALATION_MODEL,
  escalationPatchSequence,
  escalationPlan,
  type LanesRuntimeInfo,
} from "../views/lanes/api";
import { triageRequeueState } from "./triage";

// Phase F (Programm 3): Fehler-Triage mit Ein-Klick-Eskalation. Gescheiterte
// Runs der letzten 48h werden eine Queue mit Aktionen statt ein Suchauftrag:
// „Nochmal" (Task wieder ready) und „Nochmal stärker" (= Phase-B-
// model_override auf die Premium-Lane + requeue). Zwei-Schritt-Confirm nach
// Fleet-Muster. Lebt in der FlowView; die Inbox bleibt für Inhalts-
// Entscheidungen (Grill-Entscheid §7.6).
const t = {
  eyebrow: "Fehler-Triage",
  meta: (h: number) => `failed/blocked · letzte ${h}h · jüngster Run pro Task`,
  empty: "Keine offenen Fehler — nichts zu triagieren.",
  retry: "Nochmal",
  escalate: "Nochmal stärker",
  retryHint: "stellt den Task wieder ready (gleiche Lane)",
  escalateQueuedHint: "eskaliert die schon eingereihte Karte, bevor der Dispatcher sie zieht",
  confirm: "Bestätigen",
  cancel: "Abbrechen",
  done: (id: string) => `${id} wieder eingereiht.`,
  doneEscalated: (id: string, model: string) => `${id} eskaliert auf ${model} und wieder eingereiht.`,
  doneReassigned: (id: string, model: string) =>
    `${id} auf premium umgehängt (${model}) und wieder eingereiht.`,
};

export interface TriageFailure {
  run_id: number;
  task_id: string;
  title: string;
  profile: string | null;
  outcome: string;
  reason: string | null;
  ended_at: number;
  task_status: string;
  model_override: string | null;
  auto_retry_count?: number;
  auto_retry_limit?: number;
}

interface FailuresResponse {
  hours: number;
  count: number;
  truncated: boolean;
  failures: TriageFailure[];
}

type PendingAction = { taskId: string; kind: "retry" | "escalate" } | null;

async function patchTask(taskId: string, body: Record<string, unknown>): Promise<void> {
  await fetchJSON(`/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function TriageStrip() {
  const [data, setData] = useState<FailuresResponse | null>(null);
  const [pending, setPending] = useState<PendingAction>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lanes, setLanes] = useState<LanesRuntimeInfo | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await fetchJSON<FailuresResponse>("/api/plugins/kanban/runs/failures?hours=48&limit=20"));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    const firstLoad = window.setTimeout(() => void load(), 0);
    const id = window.setInterval(() => void load(), 30000);
    return () => {
      window.clearTimeout(firstLoad);
      window.clearInterval(id);
    };
  }, [load]);

  useEffect(() => {
    // Lane-Katalog einmalig, nur für den ehrlichen Eskalations-Hint —
    // fail-soft: ohne Katalog bleibt der neutrale Standard-Hint.
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetchJSON<LanesRuntimeInfo>("/api/plugins/kanban/lanes");
        if (!cancelled) setLanes(res);
      } catch {
        /* Katalog ist nur Komfort */
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const act = useCallback(async (failure: TriageFailure, kind: "retry" | "escalate") => {
    setBusy(true);
    setError(null);
    try {
      if (kind === "escalate") {
        const plan = escalationPlan(failure.profile, lanes);
        const patches = escalationPatchSequence(plan);
        if (patches.length === 0) {
          setError(plan.hint);
          return;
        }
        for (const patch of patches) {
          await patchTask(failure.task_id, patch);
        }
        setNotice(plan.reassigns
          ? t.doneReassigned(failure.task_id, ESCALATION_MODEL)
          : t.doneEscalated(failure.task_id, ESCALATION_MODEL));
      } else {
        await patchTask(failure.task_id, { status: "ready" });
        setNotice(t.done(failure.task_id));
      }
      void load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setPending(null);
    }
  }, [load, lanes]);

  // Leere Triage = keine Leiste (kein Rauschen für den Nicht-Nutzer).
  if (data !== null && data.failures.length === 0 && !error && !notice) return null;

  return (
    <FleetPanel eyebrow={t.eyebrow} meta={t.meta(data?.hours ?? 48)}>
      {error ? <ToneCallout tone="red">{error}</ToneCallout> : null}
      {notice ? <ToneCallout tone="emerald">{notice}</ToneCallout> : null}
      {data === null ? <p className="text-sm hc-dim">…</p> : data.failures.length === 0 ? (
        <p className="text-sm hc-dim">{t.empty}</p>
      ) : (
        <ul className="space-y-1.5">
          {data.failures.map((f) => {
            const isPending = pending?.taskId === f.task_id ? pending : null;
            const escalation = escalationPlan(f.profile, lanes);
            const requeue = triageRequeueState(f.task_status);
            return (
              <li key={f.task_id} className="rounded-md border border-red-500/20 px-3 py-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-red-300" />
                  <span className="min-w-0 flex-1 basis-56 truncate text-[0.85rem] font-medium text-white">{f.title}</span>
                  <span className="hc-mono shrink-0 rounded-full border border-red-500/30 px-2 py-0.5 text-[0.68rem] text-red-200">{f.outcome}</span>
                  <span className="hc-mono shrink-0 text-[0.72rem] hc-soft">{f.profile ? (profileLabel[f.profile] ?? f.profile) : "—"}</span>
                  {(f.auto_retry_count ?? 0) > 0 ? <span className="hc-mono shrink-0 rounded-full border border-amber-400/30 px-2 py-0.5 text-[0.68rem] text-amber-200">Auto {Math.min(f.auto_retry_count ?? 0, f.auto_retry_limit ?? 2)}/{f.auto_retry_limit ?? 2}</span> : null}
                  <span className="hc-mono shrink-0 text-[0.72rem] hc-dim">{fmtClock(f.ended_at)}</span>
                </div>
                {f.reason ? <p className="mt-1 line-clamp-2 text-[0.76rem] hc-dim">{f.reason}</p> : null}
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {requeue.requeued ? (
                    <span className="hc-mono inline-flex min-h-9 items-center rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 text-[0.72rem] text-emerald-200">
                      {requeue.label}
                    </span>
                  ) : null}
                  {isPending ? (
                    <>
                      <button
                        type="button"
                        disabled={busy || (isPending.kind === "escalate" && escalation.disabled)}
                        onClick={() => void act(f, isPending.kind)}
                        className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 py-1 text-[0.78rem] font-medium text-[var(--hc-accent-text)] disabled:opacity-50"
                      >
                        {isPending.kind === "escalate" ? <Zap className="h-3.5 w-3.5" /> : <RotateCw className="h-3.5 w-3.5" />}
                        {isPending.kind === "escalate" ? t.escalate : t.retry} · {t.confirm}
                      </button>
                      <button type="button" disabled={busy} onClick={() => setPending(null)} className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-3 py-1 text-[0.78rem] hc-soft">{t.cancel}</button>
                      {isPending.kind === "escalate" ? (
                        <span className={escalation.warns ? "text-[0.72rem] text-amber-200" : "text-[0.72rem] hc-dim"}>{escalation.hint}</span>
                      ) : (
                        <span className="text-[0.72rem] hc-dim">{t.retryHint}</span>
                      )}
                    </>
                  ) : (
                    <>
                      {requeue.requeued ? null : (
                        <button type="button" disabled={busy} onClick={() => setPending({ taskId: f.task_id, kind: "retry" })} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-white/10 px-3 py-1 text-[0.78rem] hc-soft hover:bg-white/5">
                          <RotateCw className="h-3.5 w-3.5" />{t.retry}
                        </button>
                      )}
                      <button type="button" disabled={busy} onClick={() => setPending({ taskId: f.task_id, kind: "escalate" })} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-amber-500/30 px-3 py-1 text-[0.78rem] text-amber-200 hover:bg-amber-500/10">
                        <Zap className="h-3.5 w-3.5" />{t.escalate}
                      </button>
                      {requeue.requeued ? (
                        <span className="text-[0.72rem] hc-dim">{t.escalateQueuedHint}</span>
                      ) : null}
                    </>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </FleetPanel>
  );
}
