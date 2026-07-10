import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(new URL("./control-tokens.css", import.meta.url), "utf8");

describe("Control reduced-motion kill switch", () => {
  it("stops CSS animation and transition motion while excluding xterm internals", () => {
    const reducedBlock = css.match(/@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?animation: none !important;[\s\S]*?transition: none !important;[\s\S]*?\n\}/)?.[0];

    expect(reducedBlock).toBeTruthy();
    expect(reducedBlock).toContain("[data-control]");
    expect(reducedBlock).toContain(":not(.xterm, .xterm *)");
  });
});
