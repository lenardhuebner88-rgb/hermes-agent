import { readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";

const DEFAULT_PALETTE_CLASS =
  /\b(?:text|bg|border|ring|shadow)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)(?:-[0-9]{2,3})?(?:\/[0-9]{1,3})?\b/;

function productionSources(directory: "views" | "components"): string[] {
  const paths: string[] = [];
  const walk = (relativeDirectory: string) => {
    for (const entry of readdirSync(new URL(`${relativeDirectory}/`, import.meta.url), { withFileTypes: true })) {
      const path = `${relativeDirectory}/${entry.name}`;
      if (entry.isDirectory()) {
        if (entry.name !== "jarvis") walk(path);
      } else if (/\.(?:ts|tsx)$/.test(entry.name) && !/\.(?:test|stories)\./.test(entry.name)) {
        paths.push(path);
      }
    }
  };
  walk(`../${directory}`);
  return paths.sort();
}

const productionFiles = [...productionSources("views"), ...productionSources("components")];

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

describe("Control production palette token guard", () => {
  it("detects a representative Tailwind default-palette class", () => {
    expect("rounded text-rose-500").toMatch(DEFAULT_PALETTE_CLASS);
  });

  it.each(productionFiles)("%s uses no Tailwind default-palette color classes", (path) => {
    expect(source(path)).not.toMatch(DEFAULT_PALETTE_CLASS);
  });
});

// Deliberate scope boundary: neutral white/black opacity utilities are not
// covered here. Scrims and prose neutrals need a separate semantic-token pass.
