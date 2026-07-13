// @vitest-environment jsdom
//
// RisikoTab — Autonomie-Kontrollzentrum (★ FINAL, c_2103a234). Fixtures mirror
// the REAL backend payload shapes (hermes_cli/kanban_db.py `release_gate_parked`
// decision-queue entry, plugins/kanban/dashboard/plugin_api.py `get_release_status`
// and `get_task` / TaskDetailResponseSchema), not hand-stubbed shapes missing
// fields — belegter Fehlmodus 2026-07-02 (grüne Fake-Dict-Tests ließen echte
// Bugs durch).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { RisikoTab } from "./RisikoTab";
import { de } from "../../i18n/de";
import type { KanbanDecision } from "../../lib/schemas";
import type { ReleaseStatusResponse, ReleaseModeResponse } from "../../lib/schemas";

const TASK_ID = "t_9f21ac04";

// ── Fixtures — real payload shapes ──────────────────────────────────────────

const RELEASE_GATE_COMMANDS = [
  "cd /home/piet/.hermes/hermes-agent/web",
  "npm run build",
  "test -f /home/piet/.hermes/hermes-agent/hermes_cli/web_dist/index.html",
  "curl -fsS http://127.0.0.1:9119/control >/dev/null",
];

const RELEASE_GATE_DECISION: KanbanDecision = {
  kind: "release_gate_parked",
  task_id: TASK_ID,
  title: "Merge Dashboard-Fix nach main",
  reason: "Release gate parked — awaiting GO",
  age_seconds: 640,
  suggested_command: RELEASE_GATE_COMMANDS.join(" && "),
  release_gate: {
    root_id: "t_1a2b3c4d",
    source_task_id: TASK_ID,
    merge_commit: "a1b2c3d4e5f6",
    commands: RELEASE_GATE_COMMANDS,
    suggested_command: RELEASE_GATE_COMMANDS.join(" && "),
  },
};

// GET /api/plugins/kanban/release-status — real shape (get_release_status).
const RELEASE_STATUS_FIXTURE: ReleaseStatusResponse = {
  autonomous: true,
  max_tier_autonomous: "review",
  recent: [
    {
      task_id: "t_8aeb1773",
      created_at: Math.floor(Date.now() / 1000) - 7200,
      payload: { outcome: "deployed", detail: "operator-cockpit-s7" },
    },
  ],
  anchors: ["release/pre-deploy/8aeb1773f"],
};

// GET /api/plugins/kanban/release-mode — real shape (get_release_mode_endpoint,
// AD-S4 + 2026-07-08 follow-ups: red_streak, max_in_progress, and the
// "Parallele Worker pro Profil" lever's max_in_progress_per_profile /
// max_concurrent_per_repo / serialize_by_repo).
const RELEASE_MODE_FIXTURE: ReleaseModeResponse = {
  autonomous: true,
  max_tier_autonomous: "review",
  pause_on_red_streak: 3,
  red_streak: 1,
  max_in_progress: 3,
  max_in_progress_per_profile: 1,
  max_concurrent_per_repo: 1,
  serialize_by_repo: true,
};

function defaultFetchImpl(url: string) {
  const u = String(url);
  if (u.includes("/release-concurrency")) return Promise.resolve({ ok: true, max_in_progress: 3 });
  if (u.includes("/release-mode")) return Promise.resolve(RELEASE_MODE_FIXTURE);
  if (u.includes("/release-gate")) return Promise.resolve({ ok: true });
  if (u.includes("/release-status")) return Promise.resolve(RELEASE_STATUS_FIXTURE);
  if (u.includes("/runs/failures")) return Promise.resolve({ hours: 48, count: 0, truncated: false, failures: [] });
  if (u.includes("/lanes")) return Promise.resolve({ lanes: [], profiles: [] });
  return Promise.resolve({});
}

beforeEach(() => {
  vi.clearAllMocks();
  fetchJSONMock.mockImplementation(defaultFetchImpl);
});

afterEach(() => {
  cleanup();
  // Cancel any leftover fake timer (e.g. an in-flight release-gate poll deadline,
  // or TriageStrip's own 30s refresh interval) before the next test installs a
  // fresh fake-timer environment — an uncancelled one bled a failure into the
  // NEXT test in the file (deterministic ordering bug, not flake).
  vi.clearAllTimers();
  vi.useRealTimers();
});

function renderRisikoTab(overrides: {
  releaseGateDecisions?: KanbanDecision[];
  blockedTasks?: Array<{ id: string; title: string; status: string; block_reason?: string | null; root_id?: string | null }>;
  releaseStatus?: ReleaseStatusResponse | null;
  releaseMode?: ReleaseModeResponse | null;
  onReleaseModeChanged?: () => void | Promise<void>;
} = {}) {
  return render(
    <MemoryRouter>
      <RisikoTab
        blockedTasks={overrides.blockedTasks ?? []}
        reliability={null}
        systemHealth={null}
        pressureStatus={null}
        activeWorkers={[]}
        lanesCatalog={null}
        releaseGateDecisions={overrides.releaseGateDecisions ?? []}
        releaseMode={overrides.releaseMode ?? RELEASE_MODE_FIXTURE}
        onReleaseModeChanged={overrides.onReleaseModeChanged}
        releaseStatus={overrides.releaseStatus ?? RELEASE_STATUS_FIXTURE}
      />
    </MemoryRouter>,
  );
}

describe("RisikoTab — Release-Gate needcard", () => {
  it("keeps clipped risk-card titles recoverable", () => {
    const title = "Risiko ".repeat(80);
    renderRisikoTab({
      releaseGateDecisions: [{ ...RELEASE_GATE_DECISION, title }],
    });

    const titleNode = document.querySelector(".rk-nc-title");
    expect(titleNode?.textContent).toBe(title);
    expect(titleNode?.getAttribute("title")).toBe(title);
  });

  it("keeps clipped triage titles and reasons fully recoverable", async () => {
    const title = "Risk title ".repeat(60);
    const reason = "REQUEST_CHANGES — evidence detail ".repeat(60);
    fetchJSONMock.mockImplementation((url: string) => {
      if (String(url).includes("/runs/failures")) {
        return Promise.resolve({
          hours: 48,
          count: 1,
          truncated: false,
          failures: [{
            run_id: 42,
            task_id: TASK_ID,
            title,
            profile: "reviewer",
            outcome: "blocked",
            reason,
            ended_at: 1_783_800_000,
            task_status: "blocked",
            model_override: null,
          }],
        });
      }
      return defaultFetchImpl(url);
    });

    renderRisikoTab();

    const byExactText = (selector: string, text: string) =>
      Array.from(document.querySelectorAll<HTMLElement>(selector)).find((node) => node.textContent === text);
    await waitFor(() => expect(byExactText("span", title)).toBeTruthy());
    const titleNode = byExactText("span", title) as HTMLElement;
    const reasonNode = byExactText("p", reason) as HTMLElement;
    expect(reasonNode).toBeTruthy();
    expect(titleNode.getAttribute("title")).toBe(title);
    expect(reasonNode.getAttribute("title")).toBe(reason);
    expect(screen.getByText("failed/blocked · letzte 48h · jüngster Run pro Task").getAttribute("title"))
      .toBe("failed/blocked · letzte 48h · jüngster Run pro Task");
  });

  it("renders the empty state when nothing needs the operator", () => {
    renderRisikoTab();
    expect(screen.getByText(de.fleet.risikoLeerState)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Release-Gate ausführen" })).toBeNull();
  });

  it("renders the parked task, its root/merge context and the Release-Gate button", () => {
    renderRisikoTab({ releaseGateDecisions: [RELEASE_GATE_DECISION] });

    expect(screen.getByText(RELEASE_GATE_DECISION.title)).toBeTruthy();
    expect(screen.getByText(/Root t_1a2b3c4d/)).toBeTruthy();
    expect(screen.getByText(/Merge a1b2c3d4e5f6/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Release-Gate ausführen" })).toBeTruthy();
  });

  it("two-step confirm calls the release-gate endpoint with the task id, not on the first (arming) click", async () => {
    renderRisikoTab({ releaseGateDecisions: [RELEASE_GATE_DECISION] });

    fireEvent.click(screen.getByRole("button", { name: "Release-Gate ausführen" }));
    expect(fetchJSONMock).not.toHaveBeenCalledWith(
      expect.stringContaining("/release-gate"),
      expect.anything(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Sicher? Erneut klicken" }));

    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledWith(
        `/api/plugins/kanban/tasks/${TASK_ID}/release-gate`,
        expect.objectContaining({ method: "POST", body: JSON.stringify({ confirm: true }) }),
      );
    });
    expect(await screen.findByText("Release-Gate grün")).toBeTruthy();
  });
});

describe("RisikoTab — S2-Fix: activation polling", () => {
  // GET /api/plugins/kanban/tasks/{id} — real shape (get_task / TaskDetailResponseSchema).
  function taskDetail(status: string, events: Array<{ id: number; kind: string; created_at: number }> = []) {
    return {
      task: { id: TASK_ID, title: RELEASE_GATE_DECISION.title, status, block_reason: null },
      comments: [],
      runs: [],
      events,
      deliverables: [],
      links: { parents: [], children: [] },
    };
  }

  it("shows the intermediate polling state, then settles green once the detached restart finishes", async () => {
    vi.useFakeTimers();
    let getTaskCalls = 0;
    fetchJSONMock.mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/release-gate") && init?.method === "POST") {
        return Promise.resolve({ ok: true, status: "activating", unit: "hermes-release-gate-t_9f21ac04" });
      }
      if (u === `/api/plugins/kanban/tasks/${TASK_ID}`) {
        getTaskCalls += 1;
        // 1st call = poll baseline (still blocked/activating), 2nd = settled done.
        return Promise.resolve(taskDetail(getTaskCalls === 1 ? "blocked" : "done"));
      }
      return defaultFetchImpl(u);
    });

    renderRisikoTab({ releaseGateDecisions: [RELEASE_GATE_DECISION] });
    fireEvent.click(screen.getByRole("button", { name: "Release-Gate ausführen" }));
    fireEvent.click(screen.getByRole("button", { name: "Sicher? Erneut klicken" }));

    // Flush the immediate "activating" POST response + the poll baseline GET
    // (both plain awaited promises, no timer involved yet).
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText(/Aktivierung läuft \(Neustart\)/)).toBeTruthy();
    // Button must NOT optimistically report done on the immediate "activating" response.
    expect(screen.queryByText("Release-Gate grün")).toBeNull();

    await act(async () => { await vi.advanceTimersByTimeAsync(4000); }); // one poll tick -> settles done
    expect(screen.getByText("Release-Gate grün")).toBeTruthy();
  });

  it("surfaces a new operator_escalation event as a failed activation, not a false green", async () => {
    vi.useFakeTimers();
    let getTaskCalls = 0;
    fetchJSONMock.mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/release-gate") && init?.method === "POST") {
        return Promise.resolve({ ok: true, status: "activating", unit: "hermes-release-gate-t_9f21ac04" });
      }
      if (u === `/api/plugins/kanban/tasks/${TASK_ID}`) {
        getTaskCalls += 1;
        if (getTaskCalls === 1) return Promise.resolve(taskDetail("blocked")); // baseline, no escalation yet
        return Promise.resolve(taskDetail("blocked", [
          { id: 501, kind: "operator_escalation", created_at: Math.floor(Date.now() / 1000) },
        ]));
      }
      return defaultFetchImpl(u);
    });

    renderRisikoTab({ releaseGateDecisions: [RELEASE_GATE_DECISION] });
    fireEvent.click(screen.getByRole("button", { name: "Release-Gate ausführen" }));
    fireEvent.click(screen.getByRole("button", { name: "Sicher? Erneut klicken" }));

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText(/Aktivierung läuft \(Neustart\)/)).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });

    expect(screen.queryByText("Release-Gate grün")).toBeNull();
    expect(screen.getByRole("button", { name: "Release-Gate ausführen" })).toBeTruthy();
  });

  it("treats a dropped fetch during the restart window as transient and keeps polling instead of failing", async () => {
    vi.useFakeTimers();
    let getTaskCalls = 0;
    fetchJSONMock.mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/release-gate") && init?.method === "POST") {
        return Promise.resolve({ ok: true, status: "activating", unit: "hermes-release-gate-t_9f21ac04" });
      }
      if (u === `/api/plugins/kanban/tasks/${TASK_ID}`) {
        getTaskCalls += 1;
        if (getTaskCalls === 1) return Promise.resolve(taskDetail("blocked")); // baseline
        if (getTaskCalls === 2) return Promise.reject(new Error("Failed to fetch")); // dashboard mid-restart
        return Promise.resolve(taskDetail("done"));
      }
      return defaultFetchImpl(u);
    });

    renderRisikoTab({ releaseGateDecisions: [RELEASE_GATE_DECISION] });
    fireEvent.click(screen.getByRole("button", { name: "Release-Gate ausführen" }));
    fireEvent.click(screen.getByRole("button", { name: "Sicher? Erneut klicken" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText(/Aktivierung läuft \(Neustart\)/)).toBeTruthy();

    await act(async () => { await vi.advanceTimersByTimeAsync(4000); }); // dropped fetch — must not surface as an error
    expect(screen.queryByText(/fehlgeschlagen/i)).toBeNull();
    expect(screen.getByText(/Aktivierung läuft \(Neustart\)/)).toBeTruthy();

    await act(async () => { await vi.advanceTimersByTimeAsync(4000); }); // next tick settles green
    expect(screen.getByText("Release-Gate grün")).toBeTruthy();
  });
});

describe("RisikoTab — Operator-Halts", () => {
  it("renders an operator-question block reason with the inline AnswerQuestion form", () => {
    renderRisikoTab({
      blockedTasks: [{ id: "t_op1", title: "state.db-Retention: Prune+VACUUM", status: "blocked", block_reason: "operator hold: needs credentials" }],
    });
    expect(screen.getByText("state.db-Retention: Prune+VACUUM")).toBeTruthy();
    expect(screen.getByText(de.fleet.answerTitle)).toBeTruthy();
  });

  it("does not classify a plain retry-eligible block as an operator halt (mirrors backend auto-retry classification)", () => {
    renderRisikoTab({
      blockedTasks: [{ id: "t_retry1", title: "transient network blip", status: "blocked", block_reason: "connection reset, retrying" }],
    });
    expect(screen.queryByText("transient network blip")).toBeNull();
    expect(screen.getByText(de.fleet.risikoLeerState)).toBeTruthy();
  });
});

describe("RisikoTab — Hero cockpit (read)", () => {
  it("shows the green AUTONOM headline when release.autonomous is true", () => {
    renderRisikoTab({ releaseMode: { ...RELEASE_MODE_FIXTURE, autonomous: true } });
    expect(screen.getByText("AUTONOM")).toBeTruthy();
  });

  it("shows the grey Kill-Switch-AUS headline when release.autonomous is false", () => {
    renderRisikoTab({ releaseMode: { ...RELEASE_MODE_FIXTURE, autonomous: false } });
    expect(screen.getByText("Kill-Switch AUS")).toBeTruthy();
  });

  it("reads the concurrency stepper value from the real release-mode max_in_progress, not a fabricated default", () => {
    renderRisikoTab({ releaseMode: { ...RELEASE_MODE_FIXTURE, max_in_progress: 5 } });
    expect(screen.getByText("5")).toBeTruthy();
  });

  it("shows the real red_streak/pause_on_red_streak safety line", () => {
    renderRisikoTab({ releaseMode: { ...RELEASE_MODE_FIXTURE, pause_on_red_streak: 3, red_streak: 2 } });
    expect(screen.getByText(/Auto-Stopp nach/)).toBeTruthy();
    expect(screen.getByText("2/3")).toBeTruthy();
  });

  it("falls back to the Guards-aktiv line when pause_on_red_streak is 0 (disabled)", () => {
    renderRisikoTab({ releaseMode: { ...RELEASE_MODE_FIXTURE, pause_on_red_streak: 0, red_streak: 0 } });
    expect(screen.getByText(/kein Auto-Stopp-Schwellenwert konfiguriert/)).toBeTruthy();
  });
});

describe("RisikoTab — Hero cockpit (write)", () => {
  it("POSTs {autonomous} to /release-mode on toggle click and reloads on success", async () => {
    const onReleaseModeChanged = vi.fn();
    fetchJSONMock.mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/release-mode") && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toEqual({ autonomous: false });
        return Promise.resolve({ ok: true, autonomous: false, max_tier_autonomous: "review", pause_on_red_streak: 3, red_streak: 1, max_in_progress: 3 });
      }
      return defaultFetchImpl(u);
    });
    renderRisikoTab({ releaseMode: { ...RELEASE_MODE_FIXTURE, autonomous: true }, onReleaseModeChanged });

    fireEvent.click(screen.getByRole("switch", { name: "Autonomie-Kill-Switch" }));

    await waitFor(() => expect(onReleaseModeChanged).toHaveBeenCalledTimes(1));
  });

  it("POSTs {max_tier_autonomous} to /release-mode on a Reichweite-segment click", async () => {
    const onReleaseModeChanged = vi.fn();
    fetchJSONMock.mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/release-mode") && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toEqual({ max_tier_autonomous: "critical" });
        return Promise.resolve({ ok: true, autonomous: true, max_tier_autonomous: "critical", pause_on_red_streak: 3, red_streak: 1, max_in_progress: 3 });
      }
      return defaultFetchImpl(u);
    });
    renderRisikoTab({ releaseMode: { ...RELEASE_MODE_FIXTURE, max_tier_autonomous: "review" }, onReleaseModeChanged });

    fireEvent.click(screen.getByRole("button", { name: "critical" }));

    await waitFor(() => expect(onReleaseModeChanged).toHaveBeenCalledTimes(1));
  });

  it("uses the real standard/review/critical tier enum, not the mockup's review/high/critical", () => {
    renderRisikoTab();
    expect(screen.getByRole("button", { name: "standard" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "review" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "critical" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "high" })).toBeNull();
  });

  it("POSTs {max_in_progress} to /release-concurrency on the 'Max. Worker gesamt' stepper click", async () => {
    const onReleaseModeChanged = vi.fn();
    fetchJSONMock.mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/release-concurrency") && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toEqual({ max_in_progress: 4 });
        return Promise.resolve({ ok: true, max_in_progress: 4 });
      }
      return defaultFetchImpl(u);
    });
    renderRisikoTab({ releaseMode: { ...RELEASE_MODE_FIXTURE, max_in_progress: 3 }, onReleaseModeChanged });

    fireEvent.click(screen.getByRole("button", { name: "mehr Worker gesamt" }));

    await waitFor(() => expect(onReleaseModeChanged).toHaveBeenCalledTimes(1));
  });

  it("clamps the 'Max. Worker gesamt' stepper decrement at 1 and never posts below it", () => {
    renderRisikoTab({ releaseMode: { ...RELEASE_MODE_FIXTURE, max_in_progress: 1 } });
    const decrement = screen.getByRole("button", { name: "weniger Worker gesamt" }) as HTMLButtonElement;
    expect(decrement.disabled).toBe(true);
  });

  it("surfaces a write error inline instead of swallowing it", async () => {
    fetchJSONMock.mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/release-mode") && init?.method === "POST") {
        return Promise.resolve({ ok: false, detail: "config.yaml gesperrt" });
      }
      return defaultFetchImpl(u);
    });
    renderRisikoTab();

    fireEvent.click(screen.getByRole("switch", { name: "Autonomie-Kill-Switch" }));

    expect(await screen.findByText("config.yaml gesperrt")).toBeTruthy();
  });
});

describe("RisikoTab — Hero cockpit: 'Parallele Worker pro Profil' (coupled lever, 2026-07-08)", () => {
  it("POSTs BOTH max_in_progress_per_profile and max_concurrent_per_repo with the same N on increment", async () => {
    const onReleaseModeChanged = vi.fn();
    fetchJSONMock.mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/release-concurrency") && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toEqual({
          max_in_progress_per_profile: 2,
          max_concurrent_per_repo: 2,
        });
        return Promise.resolve({ ok: true, max_in_progress_per_profile: 2, max_concurrent_per_repo: 2 });
      }
      return defaultFetchImpl(u);
    });
    renderRisikoTab({
      releaseMode: { ...RELEASE_MODE_FIXTURE, max_in_progress: 3, max_in_progress_per_profile: 1, max_concurrent_per_repo: 1 },
      onReleaseModeChanged,
    });

    fireEvent.click(screen.getByRole("button", { name: "mehr parallele Worker pro Profil" }));

    await waitFor(() => expect(onReleaseModeChanged).toHaveBeenCalledTimes(1));
  });

  it("clamps the increment at the global max_in_progress ceiling", () => {
    renderRisikoTab({
      releaseMode: { ...RELEASE_MODE_FIXTURE, max_in_progress: 2, max_in_progress_per_profile: 2, max_concurrent_per_repo: 2 },
    });
    const increment = screen.getByRole("button", { name: "mehr parallele Worker pro Profil" }) as HTMLButtonElement;
    expect(increment.disabled).toBe(true);
  });

  it("clamps the decrement at 1 and never posts below it", () => {
    renderRisikoTab({
      releaseMode: { ...RELEASE_MODE_FIXTURE, max_in_progress_per_profile: 1, max_concurrent_per_repo: 1 },
    });
    const decrement = screen.getByRole("button", { name: "weniger parallele Worker pro Profil" }) as HTMLButtonElement;
    expect(decrement.disabled).toBe(true);
  });

  it("shows the stale-main hint when N > 1", () => {
    renderRisikoTab({
      releaseMode: { ...RELEASE_MODE_FIXTURE, max_in_progress: 3, max_in_progress_per_profile: 2, max_concurrent_per_repo: 2 },
    });
    expect(screen.getByText(de.fleet.risikoParallelWorkerStaleMainHint)).toBeTruthy();
    expect(screen.queryByText(de.fleet.risikoParallelWorkerStrictHint)).toBeNull();
  });

  it("shows the neutral strict-serial hint when N == 1", () => {
    renderRisikoTab({
      releaseMode: { ...RELEASE_MODE_FIXTURE, max_in_progress_per_profile: 1, max_concurrent_per_repo: 1 },
    });
    expect(screen.getByText(de.fleet.risikoParallelWorkerStrictHint)).toBeTruthy();
    expect(screen.queryByText(de.fleet.risikoParallelWorkerStaleMainHint)).toBeNull();
  });

  it("falls back to max_concurrent_per_repo as the displayed floor when max_in_progress_per_profile is null (unlimited)", () => {
    renderRisikoTab({
      releaseMode: { ...RELEASE_MODE_FIXTURE, max_in_progress_per_profile: null, max_concurrent_per_repo: 2 },
    });
    expect(screen.getByRole("group", { name: "Reichweite" })).toBeTruthy(); // sanity: hero rendered
    const stepperButtons = screen.getAllByRole("button", { name: /parallele Worker pro Profil/ });
    expect(stepperButtons.length).toBe(2);
  });

  it("surfaces a write error inline instead of swallowing it", async () => {
    fetchJSONMock.mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/release-concurrency") && init?.method === "POST") {
        return Promise.resolve({ ok: false, detail: "config.yaml gesperrt" });
      }
      return defaultFetchImpl(u);
    });
    renderRisikoTab({
      releaseMode: { ...RELEASE_MODE_FIXTURE, max_in_progress: 3, max_in_progress_per_profile: 1, max_concurrent_per_repo: 1 },
    });

    fireEvent.click(screen.getByRole("button", { name: "mehr parallele Worker pro Profil" }));

    expect(await screen.findByText("config.yaml gesperrt")).toBeTruthy();
  });
});
