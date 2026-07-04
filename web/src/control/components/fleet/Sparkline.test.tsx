// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Sparkline } from "./Sparkline";

describe("Sparkline", () => {
  it("renders 7 bars deterministically for a given state (no Math.random)", () => {
    const { container: first } = render(<Sparkline state="laeuft" />);
    const { container: second } = render(<Sparkline state="laeuft" />);
    expect(first.querySelectorAll("span").length).toBe(7);
    expect(first.innerHTML).toBe(second.innerHTML);
  });

  it("renders lively (animated, live-toned) bars for laeuft", () => {
    const { container } = render(<Sparkline state="laeuft" />);
    const bar = container.querySelector("span");
    expect(bar?.className).toContain("bg-live");
    expect(bar?.className).toContain("motion-safe:animate-pulse");
  });

  it("renders flat, quiet bars for dead/idle (no animation)", () => {
    const { container: dead } = render(<Sparkline state="dead" />);
    const { container: idle } = render(<Sparkline state="idle" />);
    for (const el of [dead, idle]) {
      const bar = el.querySelector("span");
      expect(bar?.className).toContain("bg-ink-3/60");
      expect(bar?.className).not.toContain("animate-pulse");
    }
  });

  it("falls back to idle bars for an unknown state defensively", () => {
    const { container } = render(<Sparkline state={"unknown" as never} />);
    expect(container.querySelectorAll("span").length).toBe(7);
  });
});
