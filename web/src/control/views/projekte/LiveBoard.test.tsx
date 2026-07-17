import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
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
  session_id: null,
  task_id: null,
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
  session_id: null,
  task_id: null,
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
  session_id: null,
  task_id: null,
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
  session_id: null,
  task_id: null,
};

// Real /api/projects/agents tmux row with the additive task correlation
// (backend 2026-07-17): session/task resolved from the tmux options
// @hermes_session_id/@hermes_task_id; task carries the resolved kanban title.
const CORRELATED_TMUX_AGENT: ProjectAgent = {
  kind: "kimi",
  label: "work:3 kimi",
  task: "B1-Frontend: Task-Korrelation im LiveBoard",
  project: "hermes-infra",
  since: 1784239000,
  source: "tmux",
  tmux_session: "work",
  tmux_window: "3",
  assignee: null,
  operator: null,
  session_id: "s_9f8e7d6c5b",
  task_id: "t_b1frontend",
};

const NAMES = { "hermes-infra": "Hermes Infra", "family-organizer": "Family Organizer" };

function renderBoard(agents: ProjectAgent[]) {
  // MemoryRouter: tmux-Zeilen rendern seit Stage 12 einen react-router Link
  // (Terminal-Deep-Link) — Link braucht einen Router-Kontext.
  return renderToStaticMarkup(
    <MemoryRouter>
      <LiveBoard agents={agents} projectNames={NAMES} now={1784240000} onKillSession={vi.fn()} />
    </MemoryRouter>,
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

  it("links rows with tmux_session to the terminal deep link, window optional", () => {
    // Fensterlose tmux-Zeile: Deep-Link trägt nur die Session (der Terminal-
    // Tab löst dann irgendein Fenster auf) und ist nicht killbar.
    const noWindow: ProjectAgent = {
      ...TMUX_AGENT,
      label: "work:5 kimi",
      since: 1784238500,
      tmux_window: null,
    };
    // Mit Fenster-NAME (backend additive 2026-07-18): der Deep-Link bevorzugt
    // tmux_window_name — agent-terminals adressiert Fenster über den Namen,
    // der Index (tmux_window) bleibt fürs Terminate-API.
    const namedWindow: ProjectAgent = {
      ...TMUX_AGENT,
      label: "work:6 codex",
      since: 1784238600,
      tmux_window: "6",
      tmux_window_name: "codex",
    };
    const html = renderBoard([TMUX_AGENT, noWindow, namedWindow, KANBAN_AGENT, CLAIM_AGENT]);
    // Mit Fenster (HTML-escaped &amp; im statischen Markup).
    expect(html).toContain('href="/control/agent-terminals?session=work&amp;window=2"');
    // Ohne Fenster: window-Parameter entfällt komplett.
    expect(html).toContain('href="/control/agent-terminals?session=work"');
    // Name schlägt Index.
    expect(html).toContain('href="/control/agent-terminals?session=work&amp;window=codex"');
    // Nur die drei tmux-Zeilen bekommen die Affordance — Kanban/Check-in nie.
    const links = html.match(/aria-label="Terminal öffnen: [^"]*"/g) ?? [];
    expect(links).toHaveLength(3);
    expect(html).toContain('aria-label="Terminal öffnen: work:2 kimi"');
    // Kill-Fluss unverändert: nur Zeilen mit session+window sind killbar.
    const kills = html.match(/aria-label="Session [^"]* beenden"/g) ?? [];
    expect(kills).toHaveLength(2);
  });

  it("renders a calm empty state when nobody is working", () => {
    const html = renderBoard([]);
    expect(html).toContain("Gerade arbeitet niemand an diesen Projekten.");
  });

  it("renders the task-id chip only when the backend resolved one", () => {
    const html = renderBoard([CORRELATED_TMUX_AGENT, TMUX_AGENT]);
    const rows = html.match(/<li[\s\S]*?<\/li>/g) ?? [];
    expect(rows).toHaveLength(2);
    const correlated = rows.find((row) => row.includes("work:3 kimi")) ?? "";
    const legacy = rows.find((row) => row.includes("work:2 kimi")) ?? "";
    // Chip: visible font-data id with the full id in the title; the headline
    // keeps preferring the resolved task title (agent.task ?? agent.label).
    expect(correlated).toContain('title="t_b1frontend"');
    expect(correlated).toContain(">t_b1frontend</span>");
    expect(correlated).toContain("B1-Frontend: Task-Korrelation im LiveBoard");
    // Row without markers renders exactly as before — no chip.
    expect(legacy).not.toContain("t_b1frontend");
  });

  it("gives rows and the kill button ≥44px touch targets below tab, desktop density above", () => {
    const html = renderBoard([TMUX_AGENT]);
    // Kill-Button: size-11 (44px) mobil; tab:size-7 stellt die bisherige
    // Desktop-Dichte wieder her.
    const button = /<button type="button" aria-label="Session work:2 kimi beenden"[^>]*class="([^"]*)">/.exec(html)?.[1] ?? "";
    expect(button).toContain("size-11");
    expect(button).toContain("tab:size-7");
    // Zeile: min-h-11 mobil; tab:min-h-0 zurück zur kompakten Dichte.
    const row = /<li class="([^"]*)">/.exec(html)?.[1] ?? "";
    expect(row).toContain("min-h-11");
    expect(row).toContain("tab:min-h-0");
  });
});
