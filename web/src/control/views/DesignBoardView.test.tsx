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

  it("uses token classes, no raw hex", () => {
    const src = readFileSync(new URL("./DesignBoardView.tsx", import.meta.url), "utf8");
    expect(src).not.toMatch(/#[0-9a-fA-F]{3,6}\b/);
    expect(src).not.toMatch(/\[#|\[rgb/);
  });
});
