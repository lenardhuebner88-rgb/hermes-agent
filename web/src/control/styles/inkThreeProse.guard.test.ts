import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const EXPLANATORY_PARAGRAPHS = [
  {
    path: "../views/AgentTerminalsView.tsx",
    fragment: "tmux speichert nur",
  },
  {
    path: "../views/TerminalHandoffPanel.tsx",
    fragment: "Nur Vorschau",
  },
  {
    path: "../views/DiktatView.tsx",
    fragment: "Felder sind leer",
  },
  {
    path: "../views/schmiede/Konfigurator.tsx",
    fragment: "du musst keine Dateien",
  },
  {
    path: "../views/schmiede/Konfigurator.tsx",
    fragment: "unter der Hälfte = Drift-Risiko",
  },
  {
    path: "../views/autoresearch/RunsList.tsx",
    fragment: "ältere Läufe stehen",
  },
  {
    path: "../views/agent-terminals/AnswerSheet.tsx",
    fragment: "Sheet kann geschlossen",
  },
  {
    path: "../views/agent-terminals/TerminalSelectOverlay.tsx",
    fragment: "lang drücken zum Markieren",
  },
] as const;

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

function paragraphClasses(path: string, fragment: string): string[] {
  const paragraph = [...source(path).matchAll(/<p\b[^>]*>[\s\S]*?<\/p>/g)]
    .map(([tag]) => tag)
    .find((tag) => tag.includes(fragment));

  expect(paragraph, `${path} must keep the explanatory paragraph containing "${fragment}"`).toBeDefined();

  const className = paragraph?.match(/\bclassName="([^"]*)"/)?.[1];
  expect(className, `${path} explanatory paragraph must keep a literal className`).toBeDefined();
  return className?.split(/\s+/).filter(Boolean) ?? [];
}

// Deliberately narrow: a repository-wide "ink-3 carries a sentence" detector
// would misclassify legitimate micro-labels. These known prose regressions
// are anchored by stable copy fragments instead.
describe("Control explanatory prose token guard", () => {
  it.each(EXPLANATORY_PARAGRAPHS)("$path keeps $fragment on the body-copy token", ({ path, fragment }) => {
    const classes = paragraphClasses(path, fragment);

    expect(classes).toContain("text-ink-2");
    expect(classes).not.toContain("text-ink-3");
  });
});
