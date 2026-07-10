import { readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";

function productionSources(directory: "views" | "components"): string[] {
  const paths: string[] = [];
  const walk = (relativeDirectory: string) => {
    for (const entry of readdirSync(new URL(`${relativeDirectory}/`, import.meta.url), { withFileTypes: true })) {
      const path = `${relativeDirectory}/${entry.name}`;
      if (entry.isDirectory()) walk(path);
      else if (/\.(?:ts|tsx)$/.test(entry.name) && !/\.(?:test|stories)\./.test(entry.name)) paths.push(path);
    }
  };
  walk(directory);
  return paths.sort();
}

const productionFiles = [...productionSources("views"), ...productionSources("components")];

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

describe("W4 canonical status vocabulary source guards", () => {
  it.each(productionFiles)("%s has no retired status primitives", (path) => {
    const contents = source(path);
    expect(contents).not.toMatch(/StatusPill|ToneCallout|toneClasses/);
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
