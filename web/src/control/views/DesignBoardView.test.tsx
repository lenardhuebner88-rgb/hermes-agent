import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { DesignBoardView } from "./DesignBoardView";

describe("DesignBoardView", () => {
  it("renders the heading without crashing", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter><DesignBoardView /></MemoryRouter>
    );
    expect(html).toContain("Design Board");
  });

  // Raw-hex / arbitrary-color absence is enforced project-wide by the
  // gate-frontend.sh token ratchet; asserting it here with the ratchet's own
  // literal search strings would itself trip that grep-based ratchet. Assert
  // token usage positively instead.
  it("uses Leitstand surface tokens", () => {
    const src = readFileSync(new URL("./DesignBoardView.tsx", import.meta.url), "utf8");
    expect(src).toContain("bg-surface-0");
  });
});
