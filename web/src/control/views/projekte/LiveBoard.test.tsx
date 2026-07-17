import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { ProjectAgent } from "../../lib/schemas";

import { LiveBoard } from "./LiveBoard";

const TMUX_AGENT: ProjectAgent = {
  kind: "kimi",
  label: "work:2 kimi",
  task: null,
  project: "hermes-infra",
  since: 1784238000,
  source: "tmux",
  tmux_session: "work",
  tmux_window: "2",
  assignee: null,
  operator: null,
};

const KANBAN_AGENT: ProjectAgent = {
  kind: "kanban",
  label: "t_ab12cd34",
  task: "Slice FO-12 bauen",
  project: "family-organizer",
  since: 1784237000,
  source: "kanban",
  tmux_session: null,
  tmux_window: null,
  assignee: "coder",
  operator: null,
};

const CLAIM_AGENT: ProjectAgent = {
  kind: "claude",
  label: "2026-07-17_2015_claude_claim",
  task: "Claim-Task",
  project: "hermes-infra",
  since: 1784236000,
  source: "coordination",
  tmux_session: null,
  tmux_window: null,
  assignee: null,
  operator: "piet",
};

const UNASSIGNED: ProjectAgent = {
  kind: "grok",
  label: "scratch:0 grok",
  task: null,
  project: null,
  since: 1784235000,
  source: "tmux",
  tmux_session: "scratch",
  tmux_window: "0",
  assignee: null,
  operator: null,
};

const NAMES = { "hermes-infra": "Hermes Infra", "family-organizer": "Family Organizer" };

function renderBoard(agents: ProjectAgent[]) {
  return renderToStaticMarkup(
    <LiveBoard agents={agents} projectNames={NAMES} now={1784240000} onKillSession={vi.fn()} />,
  );
}

describe("LiveBoard", () => {
  it("groups by project with resolved names and trails Unzugeordnet", () => {
    const html = renderBoard([CLAIM_AGENT, KANBAN_AGENT, TMUX_AGENT, UNASSIGNED]);
    expect(html).toContain("Wer arbeitet gerade");
    expect(html).toContain("Hermes Infra");
    expect(html).toContain("Family Organizer");
    expect(html).toContain("Unzugeordnet");
    // Process-carrying project leads; unassigned trails.
    expect(html.indexOf("Hermes Infra")).toBeLessThan(html.indexOf("Family Organizer"));
    expect(html.indexOf("Family Organizer")).toBeLessThan(html.indexOf("Unzugeordnet"));
  });

  it("answers wer/woran/für-wen per row: kind, task, source, lane, operator", () => {
    const html = renderBoard([KANBAN_AGENT, CLAIM_AGENT]);
    expect(html).toContain("Slice FO-12 bauen");
    expect(html).toContain("Kanban-Task");
    expect(html).toContain("Lane coder");
    expect(html).toContain("Claim-Task");
    expect(html).toContain("für piet");
    expect(html).toContain("Check-in");
  });

  it("keeps the kill affordance exclusive to tmux rows with structured targets", () => {
    const html = renderBoard([TMUX_AGENT, KANBAN_AGENT, CLAIM_AGENT]);
    const kills = html.match(/aria-label="Session [^"]* beenden"/g) ?? [];
    expect(kills).toHaveLength(1);
  });

  it("renders a calm empty state when nobody is working", () => {
    const html = renderBoard([]);
    expect(html).toContain("Gerade arbeitet niemand an diesen Projekten.");
  });
});
