import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const scopedFiles = [
  "views/backlog/BacklogSections.tsx",
  "views/backlog/FoBacklogQueueTable.tsx",
  "components/BacklogCard.tsx",
  "components/FoBacklogCard.tsx",
  "views/backlog/FoDetailDrawer.tsx",
  "views/orchestrator/OrchestratorSections.tsx",
  "views/orchestrator/OrchestratorQueueTable.tsx",
  "views/OrchestratorBacklogView.tsx",
  "components/BacklogDetailDrawer.tsx",
  "views/orchestrator/shared.ts",
  "components/fleet/CommissionButton.tsx",
  "views/orchestrator/ControlsBar.tsx",
] as const;

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

describe("W4 canonical status vocabulary source guards", () => {
  it.each(scopedFiles)("%s has no retired status primitives", (path) => {
    expect(source(path)).not.toMatch(/StatusPill|ToneCallout|toneClasses/);
  });

  it.each([
    "views/backlog/BacklogSections.tsx",
    "views/backlog/FoBacklogQueueTable.tsx",
    "components/BacklogCard.tsx",
    "components/FoBacklogCard.tsx",
  ])("%s imports the shared leitstand signal primitive and defines no clone", (path) => {
    const contents = source(path);
    expect(contents).toMatch(/from ["'](?:\.\.\/\.\.\/components\/leitstand|\.\/leitstand)["']/);
    expect(contents).not.toMatch(/function Signal(?:Label|Chip)/);
  });
});
