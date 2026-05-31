import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AgentCard } from "./AgentCard";
import type { AgentLive, AgentTask } from "../lib/types";

const baseTask: AgentTask = {
  id: "t1",
  title: "Top task",
  priority: "med",
  progressPercent: 0,
};

const baseAgent: AgentLive = {
  id: "main",
  name: "Main",
  emoji: "🤖",
  status: "ready",
  model: "test-model",
  lastActive: 100,
  tasks: {
    queued: [],
    active: [],
    review: [],
    recentDone: [],
  },
  stuckSignal: false,
  activityPulse: 0,
  fleetHealth: {
    currentTask: "",
    heartbeat: 100,
    throughput: "0/h",
    currentTool: "-",
    lastOutput: "",
  },
  roleLabel: "Role",
  roleSummary: "Summary",
  escalationNote: null,
};

describe("AgentCard task progress", () => {
  it("renders meter bar for top task when progressPercent is > 0", () => {
    const html = renderToStaticMarkup(
      <AgentCard
        agent={{
          ...baseAgent,
          tasks: {
            ...baseAgent.tasks,
            active: [{ ...baseTask, progressPercent: 42 }],
          },
        }}
        density="airy"
        now={200}
      />,
    );

    expect(html).toContain("Aufgabenfortschritt");
    expect(html).toContain("42%");
  });

  it("does not render meter bar when progressPercent is 0", () => {
    const html = renderToStaticMarkup(
      <AgentCard
        agent={{
          ...baseAgent,
          tasks: {
            ...baseAgent.tasks,
            active: [{ ...baseTask, progressPercent: 0 }],
          },
        }}
        density="airy"
        now={200}
      />,
    );

    expect(html).not.toContain("Aufgabenfortschritt");
  });

  it("does not render meter bar when progressPercent is absent", () => {
    const taskWithoutProgress = {
      id: "t-no-progress",
      title: "Top task",
      priority: "med",
    } as AgentTask;

    const html = renderToStaticMarkup(
      <AgentCard
        agent={{
          ...baseAgent,
          tasks: {
            ...baseAgent.tasks,
            active: [taskWithoutProgress],
          },
        }}
        density="airy"
        now={200}
      />,
    );

    expect(html).not.toContain("Aufgabenfortschritt");
  });
});
