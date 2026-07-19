import { describe, expect, it } from "vitest";

import {
  parseOrThrow,
  ProjectDetailResponseSchema,
  ProjectsCommitsResponseSchema,
} from "../schemas";

const BASE_COMMIT = {
  project: "hermes-infra",
  project_name: "Hermes Infra",
  hash: "feedc0de1",
  message: "kanban(t_1a2b3c4d): attribution badge",
  author: "Claude Builder",
  committed_at: 1784239900,
  age_seconds: 100,
};

const KANBAN_ATTRIBUTION = {
  kind: "kanban",
  pack: null,
  task_id: "t_1a2b3c4d",
  lane: "grok-builder",
  model: "grok-4.5",
  label: null,
};

describe("project commit attribution schemas", () => {
  it("accepts an older feed payload without attribution", () => {
    const parsed = parseOrThrow(
      ProjectsCommitsResponseSchema,
      { generated_at: 1784240000, errors: [], commits: [BASE_COMMIT] },
      "commits-without-attribution",
    );
    expect(parsed.commits[0].attribution).toBeUndefined();
  });

  it("returns the typed attribution object and catches malformed values", () => {
    const parsed = parseOrThrow(
      ProjectsCommitsResponseSchema,
      {
        generated_at: 1784240000,
        errors: [],
        commits: [
          { ...BASE_COMMIT, attribution: KANBAN_ATTRIBUTION },
          { ...BASE_COMMIT, hash: "feedc0de2", attribution: "legacy-garbage" },
          { ...BASE_COMMIT, hash: "feedc0de3", attribution: null },
        ],
      },
      "commits-with-attribution",
    );
    expect(parsed.commits[0].attribution).toEqual(KANBAN_ATTRIBUTION);
    expect(parsed.commits[0].attribution?.model).toBe("grok-4.5");
    expect(parsed.commits[1].attribution).toBeNull();
    expect(parsed.commits[2].attribution).toBeNull();
  });

  it("parses the same attribution object on project-detail commits", () => {
    const parsed = parseOrThrow(
      ProjectDetailResponseSchema,
      {
        generated_at: 1784240000,
        slug: "hermes-infra",
        name: "Hermes Infra",
        repo_path: "/tmp/hermes-infra",
        parent: null,
        links: [],
        recent_commits: [{ ...BASE_COMMIT, attribution: KANBAN_ATTRIBUTION }],
        receipts: [],
        kanban_tasks: null,
        loops: [],
        agents: [],
        errors: [],
      },
      "detail-with-attribution",
    );
    expect(parsed.recent_commits[0].attribution).toEqual(KANBAN_ATTRIBUTION);
  });
});
