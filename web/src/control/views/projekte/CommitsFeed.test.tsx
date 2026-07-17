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
});
