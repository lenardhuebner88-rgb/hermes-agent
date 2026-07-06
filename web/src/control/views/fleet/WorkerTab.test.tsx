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
import type { BoardResponse, BoardTask, Worker } from "../../lib/types";

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

const UPDATED_WORKER: Worker = {
  ...FIXTURE_WORKER,
  run_id: "777",
  profile: "verifier",
  task_assignee: "verifier",
  started_at: 1782500100,
  last_heartbeat_at: 1782500360,
  effective_model: "claude-sonnet-4",
  input_tokens: 1200,
  output_tokens: 80,
};

const BOARD_TASK: BoardTask = {
  id: "t_abc123",
  title: "Fix flaky test",
  status: "running",
  assignee: "coder",
  priority: 0,
  created_at: 1782499900,
  started_at: 1782500000,
  completed_at: null,
  branch_name: "fleet-worker-drawer",
  latest_summary: null,
  link_counts: { parents: 0, children: 1 },
  comment_count: 0,
  progress: null,
  age: null,
  tenant: "orchestrator",
  root_id: "root_abc123",
  epic_id: null,
};

const BOARD_WITH_CHAIN: BoardResponse = {
  columns: [{ name: "running", tasks: [BOARD_TASK] }],
  tenants: ["orchestrator"],
  assignees: ["coder"],
  latest_event_id: 1,
  source_errors: [],
  now: 1782500300,
};

function renderDrawer(activeWorkers: Worker[] = [FIXTURE_WORKER], board: BoardResponse | null = null, onOpenChain = () => {}) {
  return render(
    <WorkerTab
      activeWorkers={activeWorkers}
      board={board}
      reliability={null}
      now={1782500300}
      initialOpen={FIXTURE_WORKER}
      onOpenChain={onOpenChain}
    />,
  );
}

describe("Worker-Drawer-Steuerung (Gap 1)", () => {
  it("bindet den offenen Drawer an task_id und nutzt nach Poll-Update den frischen Run", async () => {
    fetchJSONMock.mockResolvedValue({ ok: true, action: "nudge", run_id: 777, task_id: "t_abc123", detail: "Nudge auf neuem Run." });

    const { rerender } = renderDrawer([FIXTURE_WORKER], BOARD_WITH_CHAIN);
    expect(screen.getByText(/Run 482/)).toBeTruthy();

    rerender(
      <WorkerTab
        activeWorkers={[UPDATED_WORKER]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500360}
        initialOpen={FIXTURE_WORKER}
        onOpenChain={() => {}}
      />,
    );

    expect(screen.getByText(/Run 777/)).toBeTruthy();
    expect(screen.getAllByText("verifier").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "Anstoßen" }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalledTimes(1));
    const [url, opts] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/plugins/kanban/workers/777/action");
    expect(JSON.parse(opts.body)).toEqual({ action: "nudge", confirm: true });
  });

  it("zeigt bei verschwundenem Task den Beendet-Zustand und keine Worker-Aktionsbuttons", () => {
    const onOpenChain = vi.fn();
    const { rerender } = renderDrawer([FIXTURE_WORKER], BOARD_WITH_CHAIN, onOpenChain);

    rerender(
      <WorkerTab
        activeWorkers={[]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500400}
        initialOpen={FIXTURE_WORKER}
        onOpenChain={onOpenChain}
      />,
    );

    expect(screen.getByText("Worker beendet")).toBeTruthy();
    expect(screen.getByText(/nicht mehr in den aktiven Workern/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Anstoßen" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Entsperren" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Anhalten" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Neu starten" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Beenden" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Kette öffnen" }));
    expect(onOpenChain).toHaveBeenCalledWith("root_abc123");
    expect(screen.getByRole("button", { name: "Schließen" })).toBeTruthy();
  });

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
