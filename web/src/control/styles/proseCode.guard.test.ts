import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Guards the mobile-legibility contract for rendered Markdown code (.hc-prose)
// in the Control reader. The global `code { @apply text-background }` rule from
// @nous-research globals (@layer base) paints code the dark page background, so
// .hc-prose must pin a light Control ink token explicitly, and code must clear
// the ≥13px floor against the 15px root (--theme-base-size, src/index.css).
const css = readFileSync(new URL("./control-tokens.css", import.meta.url), "utf8");
const ROOT_PX = 15; // --theme-base-size in src/index.css

/** Isolate a single rule body `<selector> { ... }` (first match). */
function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  if (!match) throw new Error(`selector not found: ${selector}`);
  return match[1];
}

function fontSizePx(body: string): number {
  const m = body.match(/font-size:\s*([0-9.]+)rem/);
  if (!m) throw new Error(`no rem font-size in: ${body}`);
  return parseFloat(m[1]) * ROOT_PX;
}

describe("Control .hc-prose code mobile-legibility guard", () => {
  const inline = ruleBody(".hc-prose code");
  const block = ruleBody(".hc-prose pre code");

  it("AC-1: inline AND block code override the dark global code colour with a light Control token", () => {
    expect(inline).toMatch(/color:\s*var\(--hc-text\)/);
    expect(block).toMatch(/color:\s*var\(--hc-text\)/);
  });

  it("AC-2: computed code font-size clears the 13px mobile floor at the 15px root", () => {
    expect(fontSizePx(inline)).toBeGreaterThanOrEqual(13);
    expect(fontSizePx(block)).toBeGreaterThanOrEqual(13);
  });

  it("AC-3: long inline paths wrap (overflow-wrap) so they cannot force horizontal dialog overflow", () => {
    expect(inline).toMatch(/overflow-wrap:\s*anywhere/);
  });
});
