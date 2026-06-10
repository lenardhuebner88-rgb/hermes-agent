import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Hero } from "./Hero";
import { heroAccent } from "../lib/tones";
import type { ToneName } from "../lib/types";

/**
 * Smoke tests for the unified Hero primitive. Like the other control component
 * tests these render to static markup (no jsdom) and assert on the HTML.
 */
describe("Hero", () => {
  describe("heroAccent", () => {
    const cases: Array<[ToneName, string]> = [
      ["red", "var(--hc-red)"],
      ["rose", "var(--hc-red)"],
      ["amber", "var(--hc-amber)"],
      ["emerald", "var(--hc-emerald)"],
      ["cyan", "var(--hc-cyan)"],
      ["sky", "var(--hc-cyan)"],
      ["indigo", "var(--hc-cyan)"],
      ["violet", "var(--hc-accent)"],
      ["zinc", "var(--hc-accent)"],
    ];
    it.each(cases)("maps %s → %s", (tone, expected) => {
      expect(heroAccent(tone)).toBe(expected);
    });
  });

  it("renders eyebrow, statement title and subtitle", () => {
    const html = renderToStaticMarkup(
      <Hero eyebrow="Flow" title="Drei Aufträge laufen" subtitle="Live aus dem Board." />,
    );
    expect(html).toContain("Flow");
    expect(html).toContain("Drei Aufträge laufen");
    expect(html).toContain("Live aus dem Board.");
    expect(html).toContain("hc-hero");
    expect(html).toContain("hc-type-title");
  });

  it("renders the count as an aurora display number with its hint", () => {
    const html = renderToStaticMarkup(
      <Hero eyebrow="Postfach" title="Was braucht mich" count={39} countHint="39 warten" />,
    );
    expect(html).toContain("39");
    expect(html).toContain("39 warten");
    expect(html).toContain("hc-aurora-text");
    expect(html).toContain("hc-type-display");
  });

  it("drives the shell accent from the tone", () => {
    const calm = renderToStaticMarkup(<Hero eyebrow="x" title="y" tone="emerald" />);
    expect(calm).toContain("--hc-hero-accent:var(--hc-emerald)");
    const alarm = renderToStaticMarkup(<Hero eyebrow="x" title="y" tone="red" />);
    expect(alarm).toContain("--hc-hero-accent:var(--hc-red)");
  });

  it("renders an optional status pill and primary action", () => {
    const html = renderToStaticMarkup(
      <Hero
        eyebrow="x"
        title="y"
        status={{ label: "Alles ruhig", tone: "emerald", dot: "live" }}
        action={<button type="button">Auftrag</button>}
      />,
    );
    expect(html).toContain("Alles ruhig");
    expect(html).toContain("Auftrag");
  });

  it("tightens padding in compact density", () => {
    const airy = renderToStaticMarkup(<Hero eyebrow="x" title="y" />);
    expect(airy).toContain("p-5");
    const compact = renderToStaticMarkup(<Hero eyebrow="x" title="y" density="compact" />);
    expect(compact).toContain("p-4");
  });
});
