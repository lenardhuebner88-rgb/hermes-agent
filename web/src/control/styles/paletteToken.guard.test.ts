import { readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Jede Utility-Familie, die eine Farbe annimmt — nicht nur text/bg/border.
// fill-/stroke- (Icons, Charts) und die Gradient-Stops from/via/to sind genau
// die Stellen, an denen Default-Paletten sonst unbemerkt zurückkommen.
const DEFAULT_PALETTE_CLASS =
  /\b(?:text|bg|border|ring|shadow|fill|stroke|outline|divide|decoration|accent|caret|from|via|to)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)(?:-[0-9]{2,3})?(?:\/[0-9]{1,3})?\b/;

function productionSources(directory: "views" | "components"): string[] {
  const paths: string[] = [];
  const walk = (relativeDirectory: string) => {
    for (const entry of readdirSync(new URL(`${relativeDirectory}/`, import.meta.url), { withFileTypes: true })) {
      const path = `${relativeDirectory}/${entry.name}`;
      if (entry.isDirectory()) {
        // jarvis: eigener Design-Bereich laut DESIGN.md. __tests__/__mocks__:
        // kein Produktionscode, sonst schlägt der Guard auf Fixtures an.
        if (!["jarvis", "__tests__", "__mocks__"].includes(entry.name)) walk(path);
      } else if (/\.(?:ts|tsx)$/.test(entry.name) && !/\.(?:test|spec|stories)\./.test(entry.name)) {
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
  it.each([
    "rounded text-rose-500",
    "size-4 fill-red-500",
    "stroke-emerald-400",
    "bg-linear-to-r from-sky-500 via-indigo-400 to-purple-600",
    "divide-zinc-700",
    "outline-amber-300",
    "accent-pink-500",
    "caret-blue-400",
    "decoration-teal-300",
  ])("detects the default-palette class in %s", (sample) => {
    expect(sample).toMatch(DEFAULT_PALETTE_CLASS);
  });

  it.each([
    "text-status-ok border-line bg-surface-2",
    "text-ink-2 fill-status-alert stroke-status-warn",
    // Keine Farbe, nur Layout/Form — darf nie anschlagen.
    "rounded-2xl border px-3 py-0.5 shadow-card to-100%",
  ])("does not flag token-based utilities in %s", (sample) => {
    expect(sample).not.toMatch(DEFAULT_PALETTE_CLASS);
  });

  it.each(productionFiles)("%s uses no Tailwind default-palette color classes", (path) => {
    expect(source(path)).not.toMatch(DEFAULT_PALETTE_CLASS);
  });
});

// Deliberate scope boundary: neutral white/black opacity utilities are not
// covered here. Scrims and prose neutrals need a separate semantic-token pass.
