// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { Kanon } from "./Kanon";
import type { BlockCategory, PromptForgeCatalog } from "./catalog";

afterEach(cleanup);

const CATEGORIES: BlockCategory[] = ["core", "long-run", "optional"];

const FIXTURE: PromptForgeCatalog = {
  version: 1,
  blocks: Array.from({ length: 12 }, (_, i) => ({
    id: `b${i + 1}`,
    letter: String.fromCharCode(65 + i),
    label: `Block ${i + 1}`,
    description: `Beschreibung ${i + 1}`,
    body: `Body text for block ${i + 1}.`,
    source: `Quelle ${i + 1}`,
    category: CATEGORIES[i % CATEGORIES.length],
  })),
  taskTypes: [
    {
      id: "tt1",
      label: "Rohe Vorlage",
      blockIds: ["b1"],
      typeBody: "",
      defaultDoneWhen: "",
      checklist: [],
      rawTemplate: "Dies ist die raw template Vorlage.",
      source: "Quelle TT",
    },
  ],
  modes: [
    {
      id: "m1",
      label: "Modus-Vorlage",
      description: "Beschreibung Modus",
      overrides: {},
      rawPreset: "Dies ist die raw preset Vorlage.",
      source: "Quelle M",
    },
  ],
  targets: [],
  heuristic: [],
  evalEvidence: [],
};

describe("Kanon", () => {
  it("rendert jeden Baustein-Body auf bg-surface-0 statt Upstream-Creme", () => {
    render(<Kanon catalog={FIXTURE} />);
    for (const block of FIXTURE.blocks) {
      const code = screen.getByText(block.body);
      expect(code.tagName).toBe("CODE");
      expect(code.className).toMatch(/\bbg-surface-0\b/);
    }
  });

  it("behält Kopieren-Buttons für jeden Baustein bei", () => {
    render(<Kanon catalog={FIXTURE} />);
    const buttons = screen.getAllByRole("button", { name: "Kopieren" });
    expect(buttons.length).toBe(FIXTURE.blocks.length + FIXTURE.taskTypes.length + FIXTURE.modes.length);
  });

  it("rendert Nachbar-<pre>-Blöcke für Rohe Vorlagen und Modus-Vorlagen unverändert", () => {
    render(<Kanon catalog={FIXTURE} />);
    const preRaw = screen.getByText(FIXTURE.taskTypes[0].rawTemplate);
    expect(preRaw.tagName).toBe("PRE");
    expect(preRaw.className).toMatch(/\bbg-surface-0\b/);

    const preMode = screen.getByText(FIXTURE.modes[0].rawPreset);
    expect(preMode.tagName).toBe("PRE");
    expect(preMode.className).toMatch(/\bbg-surface-0\b/);
  });
});
