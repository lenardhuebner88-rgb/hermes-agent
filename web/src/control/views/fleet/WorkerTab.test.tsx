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
const EMPTY_TIMELINE = {
  run: {
    id: "482", task_id: "t_abc123", profile: "coder", status: "running",
    outcome: null, error: null, summary: null,
    started_at: 1782500000, ended_at: null, duration_seconds: 300,
  },
  items: [],
  count: 0,
  truncated: false,
};
const EMPTY_TASK_DETAIL = {
  task: {},
  comments: [],
  runs: [],
  events: [],
  deliverables: [],
  links: { parents: [], children: [], parent_states: [], child_states: [] },
};

type FetchOpts = { body?: string; method?: string };

interface ExtraRoutes {
  /** GET /tasks/{id} (Task-Detail fürs Drawer: Worktree/Branch/Retries/Tier). */
  taskDetail?: (url: string) => Promise<unknown>;
  /** GET /runs/{run_id} (Outcome nach Run-Ende). Default: 404. */
  runDetail?: (url: string) => Promise<unknown>;
  /** GET /runs/live-events (Ticker-Inhalt). */
  liveEvents?: (url: string) => Promise<unknown>;
}

// Route fetchJSON by URL: Ticker/Activity/Timeline/Task-Detail liefern leere
// Defaults; nur der Worker-Aktions-Endpunkt trägt das per-Test-Ergebnis.
// Extras erlauben pro Test echte Payloads für Detail/Outcome/Ticker.
function routeFetch(actionImpl: (url: string, opts?: FetchOpts) => Promise<unknown>, extras: ExtraRoutes = {}) {
  fetchJSONMock.mockImplementation((url: string, opts?: FetchOpts) => {
    if (typeof url === "string") {
      if (url.includes("/runs/live-events")) {
        return extras.liveEvents ? extras.liveEvents(url) : Promise.resolve(EMPTY_LIVE_EVENTS);
      }
      if (url.includes("/activity")) return Promise.resolve(EMPTY_ACTIVITY);
      if (url.includes("/timeline")) return Promise.resolve(EMPTY_TIMELINE);
      if (/\/tasks\/[^/?]+(\?|$)/.test(url)) {
        return extras.taskDetail ? extras.taskDetail(url) : Promise.resolve(EMPTY_TASK_DETAIL);
      }
      if (/\/runs\/\d+(\?|$)/.test(url) && opts?.method !== "POST") {
        return extras.runDetail
          ? extras.runDetail(url)
          : Promise.reject(new Error('404: {"detail":"run 482 not found"}'));
      }
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
  token_status: "no_live_sample",
  token_status_reason: "task_runs has no live token counters for this active worker yet",
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
  token_status: "live",
  token_status_reason: null,
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

  it("zeigt im Drawer einen klaren Platzhalter, wenn keine Live-Tokens vorliegen", () => {
    renderDrawer([FIXTURE_WORKER], BOARD_WITH_CHAIN);
    expect(document.body.textContent).toContain("Tokens");
    expect(document.body.textContent).toContain("noch keine Live-Tokens");
  });

  it("lädt Ereignisse, Aktivität und Logs eines Fremd-Board-Workers aus dessen Board", async () => {
    const foreignWorker: Worker = {
      ...FIXTURE_WORKER,
      board_slug: "health-track",
    };
    routeFetch((url) => {
      if (url.includes("/log")) {
        return Promise.resolve({ exists: true, content: "health-track canary log", truncated: false });
      }
      return Promise.resolve({ ok: true });
    });

    render(
      <WorkerTab
        activeWorkers={[foreignWorker]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500300}
        initialOpen={foreignWorker}
        onOpenChain={() => {}}
        currentBoard="default"
        eventBoards={["default", "health-track"]}
      />,
    );

    await waitFor(() => {
      const urls = fetchJSONMock.mock.calls.map(([url]) => String(url));
      expect(urls.some((url) => url === "/api/plugins/kanban/runs/live-events?board=default")).toBe(true);
      expect(urls.some((url) => url === "/api/plugins/kanban/runs/live-events?board=health-track")).toBe(true);
      expect(urls.some((url) => url.includes("/activity?limit=12&board=health-track"))).toBe(true);
    });

    expect(screen.queryByRole("button", { name: "Anstoßen" })).toBeNull();
    expect(screen.getByText(/Fremd-Board · Worker nur beobachten/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Log" }));

    await waitFor(() => {
      const urls = fetchJSONMock.mock.calls.map(([url]) => String(url));
      expect(urls.some((url) => url.includes("/log?tail=16384&board=health-track"))).toBe(true);
      expect(screen.getByText("health-track canary log")).toBeTruthy();
    });
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

    expect(screen.getAllByText("Worker beendet").length).toBeGreaterThan(0);
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

// ─── Worker-Tab V2 (Mockup 2026-07-27) ──────────────────────────────────────

describe("Worker-Tab V2 — Token-Kachel, Queue-Vorschau, Ticker-Dedupe", () => {
  it("Token-Kachel zeigt ehrlich den Closeout-Hinweis statt 0, wenn keine Live-Stichprobe vorliegt", () => {
    render(
      <WorkerTab
        activeWorkers={[FIXTURE_WORKER]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500300}
        initialOpen={null}
        onOpenChain={() => {}}
        cap={3}
        doneToday={4}
      />,
    );
    expect(screen.getByText("Token")).toBeTruthy();
    expect(document.body.textContent).toContain("Werte beim Closeout");
    expect(document.body.textContent).not.toContain("0 Token");
  });

  it("Token-Kachel trägt die Tages-Summe als Sub-Zeile, wenn sie aus dem costs-Hook kommt", () => {
    const { container } = render(
      <WorkerTab
        activeWorkers={[FIXTURE_WORKER]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500300}
        initialOpen={null}
        onOpenChain={() => {}}
        tokensToday={14_680_000}
      />,
    );
    expect(container.textContent).toContain("heute 14,7M");
  });

  it("Queue trägt das ehrliche Scope-Suffix (Board), weil Slots/Tokens alle Boards aggregieren", () => {
    const boardWithQueue: BoardResponse = {
      ...BOARD_WITH_CHAIN,
      columns: [
        ...BOARD_WITH_CHAIN.columns,
        { name: "ready", tasks: [{ ...BOARD_TASK, id: "t_q1", title: "Nächster Job", status: "ready", priority: 5 }] },
      ],
    };
    render(
      <WorkerTab
        activeWorkers={[FIXTURE_WORKER]}
        board={boardWithQueue}
        reliability={null}
        now={1782500300}
        initialOpen={null}
        onOpenChain={() => {}}
      />,
    );
    expect(screen.getByText(/Queue \(Board\)/)).toBeTruthy();
  });

  it("Leerzustand zeigt die Queue-Vorschau (nächste wartende Tasks) am ersten freien Slot", () => {
    const boardWithQueue: BoardResponse = {
      ...BOARD_WITH_CHAIN,
      columns: [
        { name: "ready", tasks: [
          { ...BOARD_TASK, id: "t_q1", title: "Nächster Job", status: "ready", priority: 5 },
          { ...BOARD_TASK, id: "t_q2", title: "Zweiter Job", status: "ready", priority: 1 },
        ] },
      ],
    };
    render(
      <WorkerTab
        activeWorkers={[]}
        board={boardWithQueue}
        reliability={null}
        now={1782500300}
        initialOpen={null}
        onOpenChain={() => {}}
        cap={2}
      />,
    );
    expect(screen.getByText("Nächste in Queue")).toBeTruthy();
    expect(screen.getByText("Nächster Job")).toBeTruthy();
    expect(screen.getByText("Zweiter Job")).toBeTruthy();
    expect(screen.getByText("P5")).toBeTruthy();
  });

  it("Live-Ticker dedupliziert identische Notizen zu einer Zeile mit ×N-Badge, Sonder-Kinds bleiben", async () => {
    const hb = (id: number, at: number) => ({
      id,
      board_slug: null,
      run_id: 482,
      task_id: "t_abc123",
      task_title: "Fix flaky test",
      profile: "coder",
      kind: "heartbeat",
      note: "receiving stream response",
      at,
    });
    const claimed = {
      ...hb(4, 1782500295),
      kind: "claimed",
      note: null,
    };
    routeFetch(() => Promise.resolve({ ok: true }), {
      liveEvents: () =>
        Promise.resolve({
          events: [claimed, hb(3, 1782500290), hb(2, 1782500280), hb(1, 1782500270)],
          count: 4,
          latest_id: 4,
          checked_at: 1782500300,
        }),
    });
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
    await waitFor(() => expect(screen.getByText("×3")).toBeTruthy());
    // Der claimed-Marker bleibt eigenständig (wird nie wegdedupliziert).
    expect(screen.getByText(/Slot geclaimt/)).toBeTruthy();
    // Genau EINE Heartbeat-Zeile statt dreier identischer.
    expect(screen.getAllByText(/receiving stream response/)).toHaveLength(1);
  });
});

describe("Worker-Tab V2 — Liveness, Drawer-Anreicherung, Outcome, Re-Poll", () => {
  const LIVENESS_WORKER: Worker = {
    ...FIXTURE_WORKER,
    liveness_state: "suspect",
    liveness_reason: "Heartbeat 4 min alt",
    liveness_observed_at: 1782500290,
  };

  it("Liveness-Orb auf der Karte + Liveness-Zeile mit Grund im Drawer", () => {
    render(
      <WorkerTab
        activeWorkers={[LIVENESS_WORKER]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500300}
        initialOpen={LIVENESS_WORKER}
        onOpenChain={() => {}}
      />,
    );
    const orb = document.querySelector('[role="img"][aria-label^="Liveness:"]');
    expect(orb).toBeTruthy();
    expect(orb?.getAttribute("aria-label")).toContain("verdächtig");
    expect(orb?.getAttribute("aria-label")).toContain("Heartbeat 4 min alt");
    // Drawer-Zeile
    expect(screen.getByText("Liveness")).toBeTruthy();
    expect(document.body.textContent).toContain("Heartbeat 4 min alt");
    expect(document.body.textContent).toContain("beobachtet");
  });

  it("Drawer zeigt Claim-Host, Expiry-Countdown, Worktree/Branch mit Copy, Retries und Review-Tier", async () => {
    routeFetch(() => Promise.resolve({ ok: true }), {
      taskDetail: () =>
        Promise.resolve({
          task: {
            branch_name: "kanban/t_abc123",
            workspace_path: "/home/piet/.hermes/hermes-agent/.worktrees/kanban/t_abc123",
            auto_retry_count: 2,
            review_tier: "critical",
          },
          comments: [],
          runs: [],
          events: [],
          deliverables: [],
          links: { parents: [], children: [], parent_states: [], child_states: [] },
        }),
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    renderDrawer([FIXTURE_WORKER], BOARD_WITH_CHAIN);

    // Claim aus dem Worker-Payload (sofort, ohne Nachladung).
    expect(screen.getByText("Claim")).toBeTruthy();
    expect(document.body.textContent).toContain("lock-482");
    expect(screen.getByText("Claim läuft ab")).toBeTruthy();

    await waitFor(() => {
      expect(document.body.textContent).toContain("/home/piet/.hermes/hermes-agent/.worktrees/kanban/t_abc123");
    });
    expect(document.body.textContent).toContain("kanban/t_abc123");
    expect(screen.getByText("Auto-Retries")).toBeTruthy();
    expect(screen.getByText("Review-Tier")).toBeTruthy();
    expect(document.body.textContent).toContain("critical");

    // Copy-Button mit Feedback.
    const copyBtn = screen.getByRole("button", { name: "Worktree kopieren" });
    fireEvent.click(copyBtn);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("/home/piet/.hermes/hermes-agent/.worktrees/kanban/t_abc123"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Worktree kopieren" }).textContent).toBe("kopiert"));
  });

  it("Outcome nach Run-Ende: lädt /runs/{run_id} und zeigt Ausgang, Verdict, Summary, Tokens", async () => {
    routeFetch(() => Promise.resolve({ ok: true }), {
      runDetail: () =>
        Promise.resolve({
          run: {
            id: 482,
            task_id: "t_abc123",
            profile: "coder",
            status: "done",
            outcome: "completed",
            summary: "Alles erledigt und verifiziert.",
            error: null,
            started_at: 1782500000,
            ended_at: 1782500350,
            input_tokens: 5000,
            output_tokens: 900,
            cost_usd: 0.12,
            metadata: { verdict: "APPROVED" },
          },
        }),
    });

    const { rerender } = renderDrawer([FIXTURE_WORKER], BOARD_WITH_CHAIN);
    rerender(
      <WorkerTab
        activeWorkers={[]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500400}
        initialOpen={FIXTURE_WORKER}
        onOpenChain={() => {}}
      />,
    );

    await waitFor(() => expect(screen.getByText("Run-Ausgang")).toBeTruthy());
    expect(document.body.textContent).toContain("completed");
    expect(document.body.textContent).toContain("APPROVED");
    expect(document.body.textContent).toContain("Alles erledigt und verifiziert.");
    expect(document.body.textContent).toContain("5,0k → 900");
    expect(document.body.textContent).toContain("$0,12");
    expect(document.body.textContent).toContain("beendet");
  });

  it("Nach Nudge wird der Worker-Poll sofort erneuert (onWorkersChanged), nicht erst nach 5 s", async () => {
    routeFetch(() => Promise.resolve({ ok: true, action: "nudge", run_id: 482, task_id: "t_abc123", detail: "Nudge gesetzt." }));
    const onWorkersChanged = vi.fn();
    render(
      <WorkerTab
        activeWorkers={[FIXTURE_WORKER]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500300}
        initialOpen={FIXTURE_WORKER}
        onOpenChain={() => {}}
        onWorkersChanged={onWorkersChanged}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Anstoßen" }));
    await waitFor(() => expect(onWorkersChanged).toHaveBeenCalled());
  });

  it("Nach Terminate wird ebenfalls sofort neu gepollt", async () => {
    routeFetch(() => Promise.resolve({ ok: true, run_id: 482, task_id: "t_abc123" }));
    const onWorkersChanged = vi.fn();
    render(
      <WorkerTab
        activeWorkers={[FIXTURE_WORKER]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500300}
        initialOpen={FIXTURE_WORKER}
        onOpenChain={() => {}}
        onWorkersChanged={onWorkersChanged}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Beenden" }));
    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));
    await waitFor(() => expect(onWorkersChanged).toHaveBeenCalled());
  });

  it("Run-Timeline im Drawer rendert Ereignisse aus /runs/{run_id}/timeline", async () => {
    routeFetch(() => Promise.resolve({ ok: true }));
    fetchJSONMock.mockImplementation((url: string) => {
      if (typeof url === "string") {
        if (url.includes("/runs/live-events")) return Promise.resolve(EMPTY_LIVE_EVENTS);
        if (url.includes("/activity")) return Promise.resolve(EMPTY_ACTIVITY);
        if (url.includes("/timeline")) {
          return Promise.resolve({
            ...EMPTY_TIMELINE,
            items: [
              { kind: "run_started", at: 1782500000, source: "run", payload: { profile: "coder" }, offset_seconds: 0, delta_seconds: 0 },
              { kind: "heartbeat", at: 1782500100, source: "event", payload: { note: "receiving stream response" }, offset_seconds: 100, delta_seconds: 100 },
              { kind: "model_confirmed", at: 1782500120, source: "event", payload: null, offset_seconds: 120, delta_seconds: 20 },
            ],
            count: 3,
          });
        }
        if (/\/tasks\/[^/?]+(\?|$)/.test(url)) return Promise.resolve(EMPTY_TASK_DETAIL);
      }
      return Promise.resolve({ ok: true });
    });
    renderDrawer([FIXTURE_WORKER], BOARD_WITH_CHAIN);
    await waitFor(() => expect(screen.getByText("Run-Timeline")).toBeTruthy());
    await waitFor(() => expect(document.body.textContent).toContain("receiving stream response"));
    expect(document.body.textContent).toContain("Run gestartet");
    expect(document.body.textContent).toContain("Modell");
    expect(document.body.textContent).toContain("jetzt");
  });
});

// ─── Review-Fixes 2026-07-28: F1 (Drawer-Snapshot), F2 (Queue), F3 (Fenster) ──

describe("Review-Fixes — F1: beendeter Worker zeigt keine Live-Werte mehr", () => {
  it("Drawer im Snapshot-Modus: kein Läuft-seit, keine Liveness-/Heartbeat-/Laufzeit-Zeile, kein Orb", () => {
    const { rerender } = renderDrawer([FIXTURE_WORKER], BOARD_WITH_CHAIN);
    // Vor dem Umschlag: Live-Werte sichtbar.
    expect(document.body.textContent).toContain("läuft seit");
    expect(screen.getByText("Liveness")).toBeTruthy();

    rerender(
      <WorkerTab
        activeWorkers={[]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500900}
        initialOpen={FIXTURE_WORKER}
        onOpenChain={() => {}}
      />,
    );

    // Nach dem Umschlag: der eingefrorene Snapshot darf nicht weiter „leben".
    expect(document.body.textContent).not.toContain("läuft seit");
    expect(screen.queryByText("Liveness")).toBeNull();
    expect(screen.queryByText("Heartbeat")).toBeNull();
    expect(screen.queryByText("Laufzeit")).toBeNull();
    expect(document.querySelector('[role="img"][aria-label^="Liveness:"]')).toBeNull();
    // Der Beendet-Zustand ist sichtbar (Kopf + Outcome-Platzhalter), die
    // Claim-/Task-Identität bleibt als Datenzeile stehen.
    expect(screen.getAllByText("Worker beendet").length).toBeGreaterThan(0);
    expect(document.body.textContent).toContain("lock-482");
  });
});

describe("Review-Fixes — F2: Queue-Vorschau trennt ready von scheduled", () => {
  const scheduledTask: BoardTask = {
    ...BOARD_TASK,
    id: "t_sched1",
    title: "Geparkter Root",
    status: "scheduled",
    priority: 120,
  };
  const readyTask: BoardTask = {
    ...BOARD_TASK,
    id: "t_ready1",
    title: "Echter Kandidat",
    status: "ready",
    priority: 1,
  };

  function renderEmpty(board: BoardResponse) {
    return render(
      <WorkerTab
        activeWorkers={[]}
        board={board}
        reliability={null}
        now={1782500300}
        initialOpen={null}
        onOpenChain={() => {}}
        cap={1}
      />,
    );
  }

  it("scheduled steht niemals in der ready-Hauptliste, sondern separat beschriftet", () => {
    const board: BoardResponse = {
      ...BOARD_WITH_CHAIN,
      columns: [
        { name: "scheduled", tasks: [scheduledTask] },
        { name: "ready", tasks: [readyTask] },
      ],
    };
    const { container } = renderEmpty(board);
    const text = container.textContent ?? "";
    expect(screen.getByText("Nächste in Queue")).toBeTruthy();
    expect(screen.getByText("Echter Kandidat")).toBeTruthy();
    expect(screen.getByText("wartet auf Freigabe/Termin")).toBeTruthy();
    expect(screen.getByText("Geparkter Root")).toBeTruthy();
    // Reihenfolge: ready-Block vor dem scheduled-Block, geparkter Root erst
    // NACH seinem eigenen Label — nie als „Nächste".
    expect(text.indexOf("Echter Kandidat")).toBeLessThan(text.indexOf("wartet auf Freigabe/Termin"));
    expect(text.indexOf("Geparkter Root")).toBeGreaterThan(text.indexOf("wartet auf Freigabe/Termin"));
  });

  it("nur scheduled in der Queue → kein Queue-Block für den Dispatcher (zieht nur ready)", () => {
    const board: BoardResponse = {
      ...BOARD_WITH_CHAIN,
      columns: [{ name: "scheduled", tasks: [scheduledTask] }],
    };
    renderEmpty(board);
    expect(screen.queryByText("Nächste in Queue")).toBeNull();
    expect(screen.getByText("wartet auf Freigabe/Termin")).toBeTruthy();
    expect(screen.getByText("Geparkter Root")).toBeTruthy();
  });
});

describe("Review-Fixes — F3/F4: Fenster-Beschriftung folgt der Herkunft", () => {
  it("Runtime-Cap-Fenster wird als Cap beschriftet, nicht als p90", () => {
    // FIXTURE_WORKER: eta_p50/p90 = null, max_runtime_seconds = 1800 → source cap.
    const { container } = render(
      <WorkerTab
        activeWorkers={[FIXTURE_WORKER]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500300}
        initialOpen={null}
        onOpenChain={() => {}}
      />,
    );
    expect(container.textContent).toContain("Cap");
    expect(container.textContent).not.toContain("p90");
    // F4: ohne echtes Perzentil zeichnet der Ring keinen Füllbogen für ein
    // ungeerdetes Fenster — hier grounded via cap, Bogen erlaubt; der
    // ungeerdete Fall (kein eta, kein cap) ist per fleetHub-Test gedeckt.
  });

  it("echtes eta_p90 darf p90 beschriften", () => {
    const worker: Worker = {
      ...FIXTURE_WORKER,
      eta_p50_seconds: 400,
      eta_p90_seconds: 1200,
    };
    const { container } = render(
      <WorkerTab
        activeWorkers={[worker]}
        board={BOARD_WITH_CHAIN}
        reliability={null}
        now={1782500300}
        initialOpen={null}
        onOpenChain={() => {}}
      />,
    );
    expect(container.textContent).toContain("p90");
    expect(container.textContent).not.toContain("Cap");
  });
});
