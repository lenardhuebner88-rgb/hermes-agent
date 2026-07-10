import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, RotateCw, Zap } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { fmtClock } from "../lib/derive";
import { profileLabel } from "../lib/tones";
import { FleetPanel, SignalChip, SignalLabel } from "./leitstand";
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
// Fleet-Muster. Nach dem Abriss (S5) im Fleet-Risiko-Subtab gemountet (vorher
// FlowView); die Inbox bleibt für Inhalts-Entscheidungen (Grill-Entscheid §7.6).
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
      {error ? <div role="alert" className="rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2"><SignalLabel tone="alert" label={error} /></div> : null}
      {notice ? <div role="status" className="rounded-card border border-status-ok/30 bg-status-ok/10 px-3 py-2"><SignalLabel tone="ok" label={notice} /></div> : null}
      {data === null ? <p className="text-sec text-ink-3">…</p> : data.failures.length === 0 ? (
        <p className="text-sec text-ink-3">{t.empty}</p>
      ) : (
        <ul className="space-y-1.5">
          {data.failures.map((f) => {
            const isPending = pending?.taskId === f.task_id ? pending : null;
            const escalation = escalationPlan(f.profile, lanes);
            const requeue = triageRequeueState(f.task_status);
            return (
              <li key={f.task_id} className="rounded-card border border-status-alert/30 bg-surface-2 px-3 py-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-status-alert" />
                  <span className="min-w-0 flex-1 basis-56 truncate text-sec font-medium text-ink">{f.title}</span>
                  <SignalChip tone="alert" label={f.outcome} className="shrink-0 font-data" />
                  <span className="shrink-0 font-data text-micro text-ink-2">{f.profile ? (profileLabel[f.profile] ?? f.profile) : "—"}</span>
                  {(f.auto_retry_count ?? 0) > 0 ? <SignalChip tone="warn" label={`Auto ${Math.min(f.auto_retry_count ?? 0, f.auto_retry_limit ?? 2)}/${f.auto_retry_limit ?? 2}`} className="shrink-0 font-data" /> : null}
                  <span className="shrink-0 font-data text-micro tabular-nums text-ink-3">{fmtClock(f.ended_at)}</span>
                </div>
                {f.reason ? <p className="mt-1 line-clamp-2 text-sec text-ink-3">{f.reason}</p> : null}
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {requeue.requeued ? (
                    <SignalChip tone="ok" label={requeue.label ?? "eingereiht"} className="min-h-12 font-data" />
                  ) : null}
                  {isPending ? (
                    <>
                      <button
                        type="button"
                        disabled={busy || (isPending.kind === "escalate" && escalation.disabled)}
                        onClick={() => void act(f, isPending.kind)}
                        className="inline-flex min-h-12 items-center gap-1.5 rounded-card border border-live/40 bg-live/10 px-3 py-1 text-sec font-medium text-bronze-hi disabled:opacity-50"
                      >
                        {isPending.kind === "escalate" ? <Zap className="h-3.5 w-3.5" /> : <RotateCw className="h-3.5 w-3.5" />}
                        {isPending.kind === "escalate" ? t.escalate : t.retry} · {t.confirm}
                      </button>
                      <button type="button" disabled={busy} onClick={() => setPending(null)} className="inline-flex min-h-12 items-center rounded-card border border-line px-3 py-1 text-sec text-ink-2 hover:bg-surface-3 hover:text-ink">{t.cancel}</button>
                      {isPending.kind === "escalate" ? (
                        <span className={escalation.warns ? "text-micro text-status-warn" : "text-micro text-ink-3"}>{escalation.hint}</span>
                      ) : (
                        <span className="text-micro text-ink-3">{t.retryHint}</span>
                      )}
                    </>
                  ) : (
                    <>
                      {requeue.requeued ? null : (
                        <button type="button" disabled={busy} onClick={() => setPending({ taskId: f.task_id, kind: "retry" })} className="inline-flex min-h-12 items-center gap-1.5 rounded-card border border-line px-3 py-1 text-sec text-ink-2 hover:bg-surface-3 hover:text-ink">
                          <RotateCw className="h-3.5 w-3.5" />{t.retry}
                        </button>
                      )}
                      <button type="button" disabled={busy} onClick={() => setPending({ taskId: f.task_id, kind: "escalate" })} className="inline-flex min-h-12 items-center gap-1.5 rounded-card border border-line bg-surface-2 px-3 py-1 text-sec text-ink-2 hover:border-live/40 hover:text-bronze-hi">
                        <AlertTriangle className="h-3.5 w-3.5" />{t.escalate}
                      </button>
                      {requeue.requeued ? (
                        <span className="text-micro text-ink-3">{t.escalateQueuedHint}</span>
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
