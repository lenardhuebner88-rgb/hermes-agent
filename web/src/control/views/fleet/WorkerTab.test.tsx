// @vitest-environment jsdom
//
// Puls-Leitstand Worker-Subtab (Variante B) + Worker-Drawer-Steuerung (Gap 1).
//
// Der Tab pollt jetzt zwei zusätzliche Endpunkte: den Live-Ticker
// (/runs/live-events, useRunLiveEvents) auf Tab-Ebene und — sobald der Drawer
// eines laufenden Workers offen ist — die Notiz-Historie (/…/activity). Die
// fetchJSON-Mock routet daher nach URL: Ticker + Activity liefern leer, nur der
// Worker-Aktions-Endpunkt trägt das per-Test-Ergebnis. Die Aktions-Assertions
// suchen ihren Call gezielt (actionCall()) statt über die Gesamt-Call-Zahl.
//
// Fixture worker mirrors the real /workers/active row shape (plugin_api.py
// list_active_workers: r.id AS run_id, straight off task_runs — an integer the
// SPA's WorkerSchema z.coerce.string()s).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { WorkerTab } from "./WorkerTab";
import type { BoardResponse, BoardTask, Worker } from "../../lib/types";

const EMPTY_LIVE_EVENTS = { events: [], count: 0, latest_id: null, checked_at: 0 };
const EMPTY_ACTIVITY = { task_id: "t_abc123", events: [] };

type FetchOpts = { body?: string; method?: string };

// Route fetchJSON by URL: the ticker + drawer activity poll always resolve
// empty; only the worker action / terminate endpoint returns `actionImpl`.
function routeFetch(actionImpl: (url: string, opts?: FetchOpts) => Promise<unknown>) {
  fetchJSONMock.mockImplementation((url: string, opts?: FetchOpts) => {
    if (typeof url === "string") {
      if (url.includes("/runs/live-events")) return Promise.resolve(EMPTY_LIVE_EVENTS);
      if (url.includes("/activity")) return Promise.resolve(EMPTY_ACTIVITY);
    }
    return actionImpl(url, opts);
  });
}

// The single worker-lifecycle call (POST /workers/{id}/action or /runs/{id}/terminate).
function actionCall() {
  return fetchJSONMock.mock.calls.find(
    ([u]) => typeof u === "string" && (/\/workers\/\d+\/action$/.test(u) || /\/runs\/\d+\/terminate$/.test(u)),
  ) as [string, FetchOpts] | undefined;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

beforeEach(() => {
  routeFetch(() => Promise.resolve({ ok: true }));
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
  heartbeat_ticks: [],
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

describe("Puls-Leitstand — Pulse-Strip, Swimlanes, Leerzustand", () => {
  it("Pulse-Strip zeigt belegte Slots, Heute-fertig und die Live-Token-Summe", () => {
    const { container } = render(
      <WorkerTab
        activeWorkers={[UPDATED_WORKER]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500360}
        initialOpen={null}
        onOpenChain={() => {}}
        cap={3}
        doneToday={11}
      />,
    );
    expect(screen.getByText("Slots")).toBeTruthy();
    expect(screen.getByText("Heute fertig")).toBeTruthy();
    expect(screen.getByText("Token")).toBeTruthy();
    // 1 laufender Worker von Cap 3; 11 heute fertig; Token-Summe 1200+80=1280 → 1,3k.
    expect(container.textContent).toContain("1/3");
    expect(container.textContent).toContain("11");
    expect(container.textContent).toContain("1,3k");
  });

  it("Leerzustand rendert freie Slot-Lanes plus die Verlaufsspur, nie ein schwarzes Loch", () => {
    render(
      <WorkerTab
        activeWorkers={[]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500360}
        initialOpen={null}
        onOpenChain={() => {}}
        cap={3}
        doneToday={12}
      />,
    );
    expect(screen.getAllByText("Slot frei").length).toBe(3);
    expect(screen.getByText("Verlaufsspur · letzte Ereignisse")).toBeTruthy();
    expect(screen.getByText("0/3")).toBeTruthy();
  });

  it("Tap auf eine Swimlane öffnet den Fokus-Drawer", async () => {
    render(
      <WorkerTab
        activeWorkers={[FIXTURE_WORKER]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500300}
        initialOpen={null}
        onOpenChain={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: "Schließen" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Worker coder öffnen" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Schließen" })).toBeTruthy());
  });
});

describe("Worker-Drawer-Steuerung (Gap 1)", () => {
  it("bindet den offenen Drawer an task_id und nutzt nach Poll-Update den frischen Run", async () => {
    routeFetch(() => Promise.resolve({ ok: true, action: "nudge", run_id: 777, task_id: "t_abc123", detail: "Nudge auf neuem Run." }));

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

    await waitFor(() => expect(actionCall()).toBeTruthy());
    const [url, opts] = actionCall()!;
    expect(url).toBe("/api/plugins/kanban/workers/777/action");
    expect(JSON.parse(opts.body!)).toEqual({ action: "nudge", confirm: true });
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
    routeFetch(() => Promise.resolve({ ok: true, action: "nudge", run_id: 482, task_id: "t_abc123", detail: "Nudge als Kommentar gesetzt (kein Kill)." }));

    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Anstoßen" }));

    await waitFor(() => expect(actionCall()).toBeTruthy());
    const [url, opts] = actionCall()!;
    expect(url).toBe("/api/plugins/kanban/workers/482/action");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body!)).toEqual({ action: "nudge", confirm: true });

    await waitFor(() => {
      expect(screen.getByText("Nudge als Kommentar gesetzt (kein Kill).")).toBeTruthy();
    });
  });

  it("Unlock armt zuerst (reclaim killt den Worker), zweiter Klick feuert action=unlock", async () => {
    routeFetch(() => Promise.resolve({ ok: true, action: "unlock", run_id: 482, task_id: "t_abc123", detail: "Claim gelöst — Task ist wieder beanspruchbar (ready)." }));

    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Entsperren" }));
    expect(actionCall()).toBeUndefined();
    expect(screen.getByText("Worker stoppen und Claim lösen — Task wird wieder beanspruchbar (ready)?")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));

    await waitFor(() => expect(actionCall()).toBeTruthy());
    const [url, opts] = actionCall()!;
    expect(url).toBe("/api/plugins/kanban/workers/482/action");
    expect(JSON.parse(opts.body!)).toEqual({ action: "unlock", confirm: true });
  });

  it("Hold armt zuerst, zweiter Klick feuert action=hold (operator_hold-Parken)", async () => {
    routeFetch(() => Promise.resolve({ ok: true, action: "hold", run_id: 482, task_id: "t_abc123", detail: "Worker gestoppt und Task als operator_hold geparkt (kein Auto-Redispatch)." }));

    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Anhalten" }));
    expect(actionCall()).toBeUndefined();

    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));

    await waitFor(() => expect(actionCall()).toBeTruthy());
    const [url, opts] = actionCall()!;
    expect(url).toBe("/api/plugins/kanban/workers/482/action");
    expect(JSON.parse(opts.body!)).toEqual({ action: "hold", confirm: true });
  });

  it("Terminate armt zuerst (kein Fetch), zweiter Klick feuert POST /runs/{run_id}/terminate", async () => {
    routeFetch(() => Promise.resolve({ ok: true, run_id: 482, task_id: "t_abc123" }));

    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Beenden" }));
    expect(actionCall()).toBeUndefined();
    // Armed: the truthful confirm text is visible, backend not yet called.
    expect(screen.getByText("Worker-Prozess beenden (SIGTERM→SIGKILL) und Lauf zurückholen?")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));

    await waitFor(() => expect(actionCall()).toBeTruthy());
    const [url, opts] = actionCall()!;
    expect(url).toBe("/api/plugins/kanban/runs/482/terminate");
    expect(opts.method).toBe("POST");
  });

  it("Backend-Fehlerdetail wird verbatim gezeigt (AC-2 — nie verschluckt)", async () => {
    routeFetch(() => Promise.reject(new Error("409: {\"detail\":\"run 482 already ended\"}")));

    renderDrawer();
    fireEvent.click(screen.getByRole("button", { name: "Anstoßen" }));

    await waitFor(() => {
      const err = screen.queryByRole("alert");
      expect(err).toBeTruthy();
      expect(err?.textContent).toContain("run 482 already ended");
    });
  });

  it("Guard-Ablehnung (ok:false bei HTTP 200) wird als Detail-Fehler gezeigt, nicht als stiller Erfolg", async () => {
    routeFetch(() => Promise.resolve({ ok: false, action: "unlock", run_id: 482, task_id: "t_abc123", detail: "Kein aktiver Claim zum Lösen (Task nicht running)." }));

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
