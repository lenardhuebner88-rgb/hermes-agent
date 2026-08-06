import { describe, expect, it } from "vitest";
import { AGENT_KIND_STYLES, PROJECT_AGENT_KIND_ORDER } from "./agentKinds";

describe("AGENT_KIND_STYLES", () => {
  it("defines icon and tone for every kind in PROJECT_AGENT_KIND_ORDER", () => {
    for (const kind of PROJECT_AGENT_KIND_ORDER) {
      const style = AGENT_KIND_STYLES[kind];
      expect(style, kind).toBeDefined();
      expect(style.icon, kind).toBeDefined();
      expect(style.tone, kind).toBeTruthy();
    }
  });

  it("assigns a distinct icon to every kind", () => {
    const icons = PROJECT_AGENT_KIND_ORDER.map(
      (kind) => AGENT_KIND_STYLES[kind].icon,
    );
    expect(new Set(icons).size).toBe(PROJECT_AGENT_KIND_ORDER.length);
  });
});
