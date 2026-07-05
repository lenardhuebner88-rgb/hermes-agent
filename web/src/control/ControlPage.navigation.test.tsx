import { describe, expect, it } from "vitest";
import { legacyControlRedirectTarget } from "./navigation";

describe("legacy control redirects", () => {
  it("preserves drawer and filter query params when old fleet routes redirect", () => {
    expect(legacyControlRedirectTarget("/control/fleet", "?task=t_42&filter=blocked")).toBe("/control/fleet?task=t_42&filter=blocked");
    expect(legacyControlRedirectTarget("/control/system", "?filter=ops&task=t_99")).toBe("/control/system?filter=ops&task=t_99");
  });

  it("keeps clean targets clean when no query was present", () => {
    expect(legacyControlRedirectTarget("/control/bibliothek", "")).toBe("/control/bibliothek");
  });
});
