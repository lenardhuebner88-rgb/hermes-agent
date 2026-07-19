import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ProjectCommitFeedEntry } from "../../lib/schemas";

import { CommitsFeed } from "./CommitsFeed";

const COMMITS: ProjectCommitFeedEntry[] = [
  {
    project: "hermes-infra",
    project_name: "Hermes Infra",
    hash: "abc123def",
    message: "projekte-tab: Live-Board",
    author: "kimi",
    committed_at: 1784239000,
    age_seconds: 1000,
  },
  {
    project: "family-organizer",
    project_name: "Family Organizer",
    hash: "987fedcba",
    message: "fix: listen caps",
    author: "claude",
    committed_at: 1784238000,
    age_seconds: 2000,
  },
];

const ATTRIBUTED_COMMITS: ProjectCommitFeedEntry[] = [
  {
    project: "hermes-infra",
    project_name: "Hermes Infra",
    hash: "feedc0de1",
    message: "kanban(t_1a2b3c4d): attribution badge",
    author: "Claude Builder",
    committed_at: 1784239900,
    age_seconds: 100,
    attribution: {
      kind: "kanban",
      pack: null,
      task_id: "t_1a2b3c4d",
      lane: "grok-builder",
      model: "grok-4.5",
      label: null,
    },
  },
  {
    project: "hermes-infra",
    project_name: "Hermes Infra",
    hash: "feedc0de2",
    message: "loop(hermes-feature-forge): attribution badge",
    author: "Claude Builder",
    committed_at: 1784239800,
    age_seconds: 200,
    attribution: {
      kind: "loop",
      pack: "hermes-feature-forge",
      task_id: null,
      lane: null,
      model: null,
      label: "hermes-feature-forge",
    },
  },
];

describe("CommitsFeed", () => {
  it("renders every row with project tag, subject, hash, author and age", () => {
    const html = renderToStaticMarkup(<CommitsFeed commits={COMMITS} now={1784240000} />);
    expect(html).toContain("Alle Commits");
    expect(html).toContain("Hermes Infra");
    expect(html).toContain("Family Organizer");
    expect(html).toContain("projekte-tab: Live-Board");
    expect(html).toContain("abc123def");
    expect(html).toContain("kimi");
    expect(html).toContain("claude");
    // Backend merge order is kept (newest first).
    expect(html.indexOf("projekte-tab: Live-Board")).toBeLessThan(html.indexOf("fix: listen caps"));
  });

  it("renders a calm empty state without commits", () => {
    const html = renderToStaticMarkup(<CommitsFeed commits={[]} now={1784240000} />);
    expect(html).toContain("Keine Commits in den registrierten Projekten gefunden.");
  });

  it("renders lane and model for kanban commits and pack for loop commits", () => {
    const html = renderToStaticMarkup(
      <CommitsFeed commits={ATTRIBUTED_COMMITS} now={1784240000} />,
    );
    expect(html).toContain("grok-builder · grok-4.5");
    expect(html).toContain("loop · hermes-feature-forge");
    expect(html).not.toContain("Claude Builder");
  });

  it("keeps the git author for null or missing attribution", () => {
    const commits: ProjectCommitFeedEntry[] = [
      { ...COMMITS[0], author: "null-attribution", attribution: null },
      { ...COMMITS[1], author: "missing-attribution" },
    ];
    const html = renderToStaticMarkup(<CommitsFeed commits={commits} now={1784240000} />);
    expect(html).toContain("null-attribution");
    expect(html).toContain("missing-attribution");
  });
});
