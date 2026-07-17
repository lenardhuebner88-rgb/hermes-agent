import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { WorkerActionResponseSchema, TerminateRunResponseSchema, TaskDetailResponseSchema, parseOrThrow } from "../lib/schemas";
import type { TaskDetailResponse } from "../lib/schemas";
import type { TaskStatus } from "../lib/types";
import { extractDetail } from "./internal";

// Operator stage transitions. Wraps PATCH /tasks/{id} (the same endpoint the
// kanban drawer uses) so a Fleet stage button has a REAL effect: Plan
// (triage→todo), Dispatch (todo→ready, auto-dispatches), Reopen
// (blocked→ready). Review completion stays worker/verdict-owned. The 409 "blocked by
// parent(s)" detail is surfaced verbatim so the guard is honest, not silent.
export interface TaskActionExtra {
  block_reason?: string;
  summary?: string;
}

export function useTaskAction(onDone?: () => void | Promise<void>) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const run = useCallback(async (taskId: string, status: TaskStatus, extra?: TaskActionExtra) => {
    setBusyId(taskId);
    setErrorById((prev) => ({ ...prev, [taskId]: "" }));
    try {
      const res = await fetchJSON<{ task?: unknown }>(
        `/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`,
        { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status, ...extra }) },
      );
      await onDone?.();
      return { ok: true as const, res };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: detail }));
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyId(null);
    }
  }, [onDone]);
  const clearError = useCallback((taskId: string) => setErrorById((prev) => ({ ...prev, [taskId]: "" })), []);
  return { busyId, errorById, run, clearError };
}


// S6: Answer an operator-question hold. Kommentar + Unblock sind eine atomare
// Backend-Transition; nur der sofortige Dispatcher-Tick folgt best-effort:
//   1. POST /tasks/{id}/answer    — Kommentar + eligibility-CAS + Unblock
//   2. POST /workers/0/action     — Dispatcher-Tick, damit der Worker sofort startet
// Der Retry-Worker liest den Kommentar über build_worker_context. `doneIds`
// hält erfolgreich beantwortete Tasks fest, bis der Board-Poll die Zeile
// fallen lässt.
export function useAnswerQuestion() {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [doneIds, setDoneIds] = useState<Record<string, boolean>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const run = useCallback(async (taskId: string, answer: string) => {
    const text = answer.trim();
    if (!text) {
      const detail = "Antwort darf nicht leer sein.";
      setErrorById((prev) => ({ ...prev, [taskId]: detail }));
      return { ok: false as const, detail };
    }
    setBusyId(taskId);
    setErrorById((prev) => ({ ...prev, [taskId]: "" }));
    try {
      // 1. Antwort + Entblockung atomar. Ein zweiter Tab kann zwischen diesen
      //    beiden Writes keinen verwaisten Kommentar mehr erzeugen.
      await fetchJSON<{ ok?: boolean }>(
        `/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}/answer`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ answer: text }) },
      );
      // 2. Dispatcher-Tick (run_id 0 als Platzhalter, reiner Tick ohne Run-Bezug).
      await fetchJSON<{ ok?: boolean; detail?: string }>(
        "/api/plugins/kanban/workers/0/action",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "dispatch", confirm: true, reason: "Operator-Antwort auf Blockgrund — Worker neu gestartet (FleetAnswerQuestion)" }) },
      );
      if (aliveRef.current) setDoneIds((prev) => ({ ...prev, [taskId]: true }));
      return { ok: true as const };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: detail }));
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyId(null);
    }
  }, []);

  return { busyId, doneIds, errorById, run };
}


// K3: Inline-Resolve für eine Verifier-Ablehnung (review_rejected) direkt am
// CommandHome — die EINE dominante Auflösung: Task entblocken (PATCH ready;
// der Server mappt das auf unblock_task) + ein Dispatcher-Tick, damit der
// Fix-Lauf sofort startet statt auf den nächsten Gateway-Tick zu warten.
// Der Coder-Retry sieht das Verifier-Feedback automatisch im worker_context.
// `doneIds` hält erfolgreich gestartete Tasks fest, bis der Decision-Queue-Poll
// die Zeile von selbst fallen lässt.
export function useFixRedispatch() {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [doneIds, setDoneIds] = useState<Record<string, boolean>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const run = useCallback(async (taskId: string) => {
    setBusyId(taskId);
    setErrorById((prev) => ({ ...prev, [taskId]: "" }));
    try {
      await fetchJSON<{ task?: unknown }>(
        `/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`,
        { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: "ready" }) },
      );
      // run_id ist für action=dispatch irrelevant (reiner Tick, kein Run-Bezug)
      // — 0 als Platzhalter, wie der Endpoint es erlaubt.
      await fetchJSON<{ ok?: boolean; detail?: string }>(
        "/api/plugins/kanban/workers/0/action",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "dispatch", confirm: true, reason: "Fix-Lauf nach Verifier-Ablehnung (CommandHome)" }) },
      );
      if (aliveRef.current) setDoneIds((prev) => ({ ...prev, [taskId]: true }));
      return { ok: true as const };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: detail }));
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyId(null);
    }
  }, []);
  return { busyId, doneIds, errorById, run };
}


// Worker-Drawer-Steuerung (Gap 1, Fleet Worker-Tab): unlock/nudge/hold/resume/
// restart via POST /workers/{run_id}/action, terminate via the sibling POST
// /runs/{run_id}/terminate — see plugin_api.py for both endpoints' full
// semantics. A guard refusal comes back as {ok:false} at HTTP 200 (never a
// thrown error) and is surfaced exactly like useRepairDeliverable handles it;
// a thrown error (404/409/5xx) goes through extractDetail so the backend's
// own detail text is shown verbatim (AC-2 — never swallow).
export type WorkerLifecycleAction = "unlock" | "nudge" | "hold" | "resume" | "restart" | "dispatch";


export function useWorkerLifecycle() {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const run = useCallback(async (runId: string, action: WorkerLifecycleAction) => {
    setBusyId(runId);
    setErrorById((prev) => ({ ...prev, [runId]: "" }));
    try {
      const raw = await fetchJSON<unknown>(
        `/api/plugins/kanban/workers/${encodeURIComponent(runId)}/action`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, confirm: true }) },
      );
      const res = parseOrThrow(WorkerActionResponseSchema, raw, "Worker-Aktion");
      if (!res.ok) {
        const detail = res.detail || "Aktion abgelehnt.";
        if (aliveRef.current) setErrorById((prev) => ({ ...prev, [runId]: detail }));
        return { ok: false as const, detail };
      }
      return { ok: true as const, detail: res.detail };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [runId]: detail }));
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyId(null);
    }
  }, []);

  const terminate = useCallback(async (runId: string) => {
    setBusyId(runId);
    setErrorById((prev) => ({ ...prev, [runId]: "" }));
    try {
      const raw = await fetchJSON<unknown>(
        `/api/plugins/kanban/runs/${encodeURIComponent(runId)}/terminate`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) },
      );
      const res = parseOrThrow(TerminateRunResponseSchema, raw, "Worker-Terminate");
      if (!res.ok) {
        const detail = "Beenden abgelehnt.";
        if (aliveRef.current) setErrorById((prev) => ({ ...prev, [runId]: detail }));
        return { ok: false as const, detail };
      }
      return { ok: true as const };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [runId]: detail }));
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyId(null);
    }
  }, []);

  const clearError = useCallback((runId: string) => setErrorById((prev) => ({ ...prev, [runId]: "" })), []);

  return { busyId, errorById, run, terminate, clearError };
}


// R1: Inline-Repair für ein deliverable_posted_not_completed direkt am
// CommandHome — die EINE dominante Auflösung: den fehlenden kanban_complete-
// Schritt nachschließen (POST /tasks/<id>/repair, blocked→done, synth. Run,
// deliverable_protocol_repaired-Event). confirm:true ist immer gesetzt — der
// Knopf SELBST ist die Bestätigung (zwei-Klick-Arming in der UI). Der Endpoint
// gibt eine Guard-Ablehnung als {ok:false} bei HTTP 200 zurück; das fangen wir
// ab und zeigen den Grund inline, statt zu werfen. `doneIds` hält reparierte
// Tasks fest, bis der Decision-Queue-Poll die Zeile von selbst fallen lässt.
export function useRepairDeliverable() {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [doneIds, setDoneIds] = useState<Record<string, boolean>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const run = useCallback(async (taskId: string) => {
    setBusyId(taskId);
    setErrorById((prev) => ({ ...prev, [taskId]: "" }));
    try {
      const res = await fetchJSON<{ ok?: boolean; detail?: string }>(
        `/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}/repair`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true, actor: "control-dashboard" }) },
      );
      if (res?.ok === false) {
        const detail = res.detail || "Repair nicht möglich.";
        if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: detail }));
        return { ok: false as const, detail };
      }
      if (aliveRef.current) setDoneIds((prev) => ({ ...prev, [taskId]: true }));
      return { ok: true as const };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: detail }));
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyId(null);
    }
  }, []);
  return { busyId, doneIds, errorById, run };
}


// S2-Fix: the POST above returns immediately with `status: "activating"` — the
// gate/fixer/restart runs afterwards in a detached systemd unit (self-termination
// trap, see kanban_worktrees.spawn_release_gate_activation). Marking the button
// "done" on that immediate response is the bug this polls around. Settle signal
// is the SAME GET /tasks/{id} the drawer already uses (TaskDetailResponseSchema):
// task.status -> done/archived = green; a NEW operator_escalation event (id above
// the pre-activation baseline, so a pre-existing escalation never false-positives)
// = failed. Dropped fetches during the restart window are swallowed and retried —
// never counted as failure (reset-tolerance requirement).
const RELEASE_GATE_POLL_INTERVAL_MS = 4000;

const RELEASE_GATE_POLL_TIMEOUT_MS = 6 * 60 * 1000;


function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => { setTimeout(resolve, ms); });
}


async function fetchTaskDetailSoft(taskId: string): Promise<TaskDetailResponse | null> {
  try {
    const raw = await fetchJSON<unknown>(`/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`);
    return parseOrThrow(TaskDetailResponseSchema, raw, "tasks/detail");
  } catch {
    return null; // transient drop during the detached restart — caller retries
  }
}


export function useReleaseGateExecute() {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [activatingIds, setActivatingIds] = useState<Record<string, boolean>>({});
  const [doneIds, setDoneIds] = useState<Record<string, boolean>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const pollUntilSettled = useCallback(async (taskId: string): Promise<{ ok: boolean; detail?: string }> => {
    const baseline = await fetchTaskDetailSoft(taskId);
    const baselineEscalationId = Math.max(
      -1,
      ...(baseline?.events ?? []).filter((e) => e.kind === "operator_escalation").map((e) => e.id),
    );
    const deadline = Date.now() + RELEASE_GATE_POLL_TIMEOUT_MS;
    while (aliveRef.current && Date.now() < deadline) {
      await sleep(RELEASE_GATE_POLL_INTERVAL_MS);
      if (!aliveRef.current) break;
      const data = await fetchTaskDetailSoft(taskId);
      if (data == null) continue; // dropped fetch — transient, retry
      const status = data.task?.status;
      if (status === "done" || status === "archived") {
        return { ok: true };
      }
      const escalation = data.events.find((e) => e.kind === "operator_escalation" && e.id > baselineEscalationId);
      if (escalation) {
        const lastNote = [...data.comments].reverse().find((c) => (c.body ?? "").includes("Release-gate"));
        return { ok: false, detail: lastNote?.body || "Release-Gate an Operator eskaliert." };
      }
    }
    return { ok: false, detail: "Aktivierung dauert länger als erwartet — Status prüfen." };
  }, []);

  const run = useCallback(async (taskId: string) => {
    setBusyId(taskId);
    setErrorById((prev) => ({ ...prev, [taskId]: "" }));
    try {
      const res = await fetchJSON<{ ok?: boolean; detail?: string; status?: string }>(
        `/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}/release-gate`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true }) },
      );
      if (res?.ok === false) {
        const detail = res.detail || res.status || "Release-Gate fehlgeschlagen.";
        if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: detail }));
        return { ok: false as const, detail };
      }
      if (res.status === "activating") {
        setBusyId(null);
        if (aliveRef.current) setActivatingIds((prev) => ({ ...prev, [taskId]: true }));
        const settled = await pollUntilSettled(taskId);
        if (aliveRef.current) setActivatingIds((prev) => ({ ...prev, [taskId]: false }));
        if (settled.ok) {
          if (aliveRef.current) setDoneIds((prev) => ({ ...prev, [taskId]: true }));
          return { ok: true as const };
        }
        if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: settled.detail || "Aktivierung fehlgeschlagen." }));
        return { ok: false as const, detail: settled.detail };
      }
      if (aliveRef.current) setDoneIds((prev) => ({ ...prev, [taskId]: true }));
      return { ok: true as const, detail: res.detail || res.status };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: detail }));
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyId(null);
    }
  }, [pollUntilSettled]);
  return { busyId, activatingIds, doneIds, errorById, run };
}


// Naht 3: Inline-Veto für eine Autoresearch-Eskalation direkt am CommandHome —
// POST /tasks/<id>/veto-escalation archiviert die Eskalation UND schreibt
// freigabe_vetoed, sodass der Stratege (reflect) lernt, das Signal künftig nicht
// mehr hochzuspülen. Der Endpoint gibt 409 bei Nicht-Eskalationen; das wirft
// fetchJSON, wir fangen es und zeigen den Grund inline. `doneIds` hält vetote
// Tasks fest, bis der Decision-Queue-Poll die Zeile von selbst fallen lässt.
export function useVetoEscalation() {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [doneIds, setDoneIds] = useState<Record<string, boolean>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const run = useCallback(async (taskId: string) => {
    setBusyId(taskId);
    setErrorById((prev) => ({ ...prev, [taskId]: "" }));
    try {
      const res = await fetchJSON<{ ok?: boolean; detail?: string }>(
        `/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}/veto-escalation`,
        { method: "POST", headers: { "Content-Type": "application/json" } },
      );
      if (res?.ok === false) {
        const detail = res.detail || "Veto nicht möglich.";
        if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: detail }));
        return { ok: false as const, detail };
      }
      if (aliveRef.current) setDoneIds((prev) => ({ ...prev, [taskId]: true }));
      return { ok: true as const };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: detail }));
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyId(null);
    }
  }, []);
  return { busyId, doneIds, errorById, run };
}

