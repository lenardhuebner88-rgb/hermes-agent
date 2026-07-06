// @vitest-environment jsdom
//
// Gap 1 — Worker-Drawer-Steuerung (nudge/unlock/hold/restart/terminate).
//
// Fixture worker mirrors the real /workers/active row shape (plugin_api.py
// list_active_workers: r.id AS run_id, straight off task_runs — an integer
// the SPA's WorkerSchema z.coerce.string()s; the Worker type here reflects
// the ALREADY-coerced string, same as every other Fleet component consumes it).
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { WorkerTab } from "./WorkerTab";
import type { Worker } from "../../lib/types";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const FIXTURE_WORKER: Worker = {
  run_id: "482",
  task_id: "t_abc123",
  task_title: "Fix flaky test",
  task_status: "running",
  task_assignee: "coder",
  profile: "coder",
  worker_pid: 88213,
  started_at: 1782500000,
  claim_lock: "lock-482",
  claim_expires: 1782500600,
  last_heartbeat_at: 1782500300,
  max_runtime_seconds: 1800,
  run_status: "running",
  run_outcome: null,
  block_reason: null,
  inspect: null,
  last_heartbeat_note: null,
  last_heartbeat_note_at: null,
  eta_p50_seconds: null,
  eta_p90_seconds: null,
  step_key: null,
  model_override: null,
  effective_model: null,
  input_tokens: null,
  output_tokens: null,
  run_progress: null,
};

function renderDrawer() {
  return render(
    <WorkerTab
      activeWorkers={[FIXTURE_WORKER]}
      board={null}
      reliability={null}
      now={1782500300}
      initialOpen={FIXTURE_WORKER}
      onOpenChain={() => {}}
    />,
  );
}

describe("Worker-Drawer-Steuerung (Gap 1)", () => {
  it("Nudge feuert direkt POST /workers/{run_id}/action mit action=nudge (kein Arming — einzige No-Kill-Aktion)", async () => {
    fetchJSONMock.mockResolvedValue({ ok: true, action: "nudge", run_id: 482, task_id: "t_abc123", detail: "Nudge als Kommentar gesetzt (kein Kill)." });

    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Anstoßen" }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalledTimes(1));
    const [url, opts] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/plugins/kanban/workers/482/action");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual({ action: "nudge", confirm: true });

    await waitFor(() => {
      expect(screen.getByText("Nudge als Kommentar gesetzt (kein Kill).")).toBeTruthy();
    });
  });

  it("Unlock armt zuerst (reclaim killt den Worker), zweiter Klick feuert action=unlock", async () => {
    fetchJSONMock.mockResolvedValue({ ok: true, action: "unlock", run_id: 482, task_id: "t_abc123", detail: "Claim gelöst — Task ist wieder beanspruchbar (ready)." });

    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Entsperren" }));
    expect(fetchJSONMock).not.toHaveBeenCalled();
    expect(screen.getByText("Worker stoppen und Claim lösen — Task wird wieder beanspruchbar (ready)?")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalledTimes(1));
    const [url, opts] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/plugins/kanban/workers/482/action");
    expect(JSON.parse(opts.body)).toEqual({ action: "unlock", confirm: true });
  });

  it("Hold armt zuerst, zweiter Klick feuert action=hold (operator_hold-Parken)", async () => {
    fetchJSONMock.mockResolvedValue({ ok: true, action: "hold", run_id: 482, task_id: "t_abc123", detail: "Worker gestoppt und Task als operator_hold geparkt (kein Auto-Redispatch)." });

    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Anhalten" }));
    expect(fetchJSONMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalledTimes(1));
    const [url, opts] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/plugins/kanban/workers/482/action");
    expect(JSON.parse(opts.body)).toEqual({ action: "hold", confirm: true });
  });

  it("Terminate armt zuerst (kein Fetch), zweiter Klick feuert POST /runs/{run_id}/terminate", async () => {
    fetchJSONMock.mockResolvedValue({ ok: true, run_id: 482, task_id: "t_abc123" });

    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Beenden" }));
    expect(fetchJSONMock).not.toHaveBeenCalled();
    // Armed: the truthful confirm text is visible, backend not yet called.
    expect(screen.getByText("Worker-Prozess beenden (SIGTERM→SIGKILL) und Lauf zurückholen?")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalledTimes(1));
    const [url, opts] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/plugins/kanban/runs/482/terminate");
    expect(opts.method).toBe("POST");
  });

  it("Backend-Fehlerdetail wird verbatim gezeigt (AC-2 — nie verschluckt)", async () => {
    fetchJSONMock.mockRejectedValueOnce(new Error("409: {\"detail\":\"run 482 already ended\"}"));

    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Anstoßen" }));

    await waitFor(() => {
      const err = screen.queryByRole("alert");
      expect(err).toBeTruthy();
      expect(err?.textContent).toContain("run 482 already ended");
    });
  });

  it("Guard-Ablehnung (ok:false bei HTTP 200) wird als Detail-Fehler gezeigt, nicht als stiller Erfolg", async () => {
    fetchJSONMock.mockResolvedValueOnce({ ok: false, action: "unlock", run_id: 482, task_id: "t_abc123", detail: "Kein aktiver Claim zum Lösen (Task nicht running)." });

    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Entsperren" }));
    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));

    await waitFor(() => {
      const err = screen.queryByRole("alert");
      expect(err).toBeTruthy();
      expect(err?.textContent).toContain("Kein aktiver Claim zum Lösen (Task nicht running).");
    });
  });
});
