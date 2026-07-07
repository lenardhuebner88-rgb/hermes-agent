// @vitest-environment jsdom
//
// Release-Gate block (moved here from the /control Postfach, S-fleet-risiko-btn):
// a parked post-merge release gate renders with its root/merge context and the
// shared two-step ReleaseGateButton. Fixture mirrors the REAL backend payload
// shape (hermes_cli/kanban_db.py `release_gate_parked` decision-queue entry +
// hermes_cli/kanban_worktrees.py `_RELEASE_GATE_COMMANDS`), not a hand-stubbed
// shape missing fields.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { RisikoTab } from "./RisikoTab";
import { de } from "../../i18n/de";
import type { KanbanDecision } from "../../lib/schemas";

const TASK_ID = "t_9f21ac04";

// Realistisches Fixture — geerntet aus dem echten Event-Payload
// (hermes_cli/kanban_worktrees.py: root_id/source_task/merge_commit/commands)
// und dem decision_queue-Mapping (hermes_cli/kanban_db.py `release_gate_parked`).
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

function defaultFetchImpl(url: string) {
  const u = String(url);
  if (u.includes("/release-gate")) return Promise.resolve({ ok: true });
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
});

function renderRisikoTab(releaseGateDecisions: KanbanDecision[]) {
  return render(
    <RisikoTab
      allPlanspecs={[]}
      blockedTasks={[]}
      reliability={null}
      systemHealth={null}
      pressureStatus={null}
      activeWorkers={[]}
      lanesCatalog={null}
      releaseGateDecisions={releaseGateDecisions}
      onNavigateToPlan={() => undefined}
    />,
  );
}

describe("RisikoTab — Release-Gate block", () => {
  it("renders nothing when there are no parked release gates", () => {
    renderRisikoTab([]);
    expect(screen.queryByText(de.fleet.risikoReleaseGateTitle)).toBeNull();
    expect(screen.queryByRole("button", { name: "Release-Gate ausführen" })).toBeNull();
  });

  it("renders the parked task, its root/merge context and the Release-Gate button", () => {
    renderRisikoTab([RELEASE_GATE_DECISION]);

    expect(screen.getByText(de.fleet.risikoReleaseGateTitle)).toBeTruthy();
    expect(screen.getByText(RELEASE_GATE_DECISION.title)).toBeTruthy();
    expect(screen.getByText(/Root t_1a2b3c4d/)).toBeTruthy();
    expect(screen.getByText(/Merge a1b2c3d4e5f6/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Release-Gate ausführen" })).toBeTruthy();
  });

  it("two-step confirm calls the release-gate endpoint with the task id, not on the first (arming) click", async () => {
    renderRisikoTab([RELEASE_GATE_DECISION]);

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
