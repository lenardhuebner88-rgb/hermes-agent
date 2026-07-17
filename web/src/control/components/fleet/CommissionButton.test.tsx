import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { CommissionState } from "../../hooks/commissionCapture";
import { CommissionButton } from "./CommissionButton";

function renderButton(state?: CommissionState, variant: "pill" | "full" = "pill") {
  return renderToStaticMarkup(<CommissionButton state={state} variant={variant} onClick={() => undefined} />);
}

describe("CommissionButton canonical action vocabulary", () => {
  it.each([
    [undefined, "border-live/40 bg-live/10 text-bronze-hi"],
    ["done", "border-line bg-surface-2 text-ink-2"],
    ["error", "border-live/40 bg-live/10 text-bronze-hi"],
  ] as const)("renders state %s without status-color button classes", (state, expectedClasses) => {
    const html = renderButton(state);

    expect(html).toContain("min-h-12");
    for (const expectedClass of expectedClasses.split(" ")) expect(html).toContain(expectedClass);
    expect(html).not.toMatch(/(?:emerald|red)-/);
    expect(html).not.toMatch(/(?:status-ok|status-alert)/);
  });

  it("keeps the full variant at the same touch height and spans the container", () => {
    const html = renderButton(undefined, "full");
    expect(html).toContain("min-h-12");
    expect(html).toContain("w-full");
    expect(html).toContain("justify-center");
  });
});
