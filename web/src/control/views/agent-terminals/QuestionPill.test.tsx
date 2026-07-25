// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QuestionPill } from "./QuestionPill";

afterEach(cleanup);

describe("QuestionPill", () => {
  it("keeps the warning signal separate from the neutral 44px button affordance", () => {
    const onClick = vi.fn();
    const { rerender } = render(
      <QuestionPill
        count={3}
        standingSinceTs={new Date(Date.now() - 5_000).toISOString()}
        onClick={onClick}
      />,
    );

    const button = screen.getByTestId("frage-pill");
    expect(button.tagName).toBe("BUTTON");
    expect(button.className).not.toMatch(/(?:text|bg|border)-status-/u);
    expect(button.className).not.toContain("rounded-full");
    expect(button.className).toContain("min-h-[44px]");
    expect(button.outerHTML).not.toContain("text-[");
    expect(button.querySelector(".bg-status-warn")).not.toBeNull();
    expect(button.textContent).toContain("3 Fragen");
    expect(button.getAttribute("aria-label")).toMatch(/^3 Fragen · steht seit/u);

    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);

    rerender(<QuestionPill count={0} onClick={onClick} />);
    expect(screen.queryByTestId("frage-pill")).toBeNull();
  });
});
