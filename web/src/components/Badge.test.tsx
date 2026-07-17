// @vitest-environment jsdom
import { createRef, forwardRef } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Badge, type BadgeTone } from "./Badge";

vi.mock("@nous-research/ui/ui/components/badge", () => ({
  Badge: forwardRef<HTMLSpanElement, React.ComponentPropsWithoutRef<"span">>(
    function MockNousBadge(props, ref) {
      return <span {...props} data-nous-default="true" ref={ref} />;
    },
  ),
}));

afterEach(cleanup);

const EXPECTED_TONE_CLASS: Record<BadgeTone, string> = {
  default: "data-nous-default",
  destructive: "text-destructive",
  outline: "text-midground/80",
  secondary: "bg-midground/8",
  success: "text-success",
  warning: "text-warning",
};

describe("Badge", () => {
  for (const tone of Object.keys(EXPECTED_TONE_CLASS) as BadgeTone[]) {
    it(`renders the ${tone} tone as a semantic span`, async () => {
      render(<Badge tone={tone}>{tone}</Badge>);
      const badge = await screen.findByText(tone);

      expect(badge.tagName).toBe("SPAN");
      if (tone === "default") {
        await waitFor(() => {
          expect(screen.getByText(tone).getAttribute(EXPECTED_TONE_CLASS[tone])).toBe(
            "true",
          );
        });
      } else {
        expect(badge.className).toContain(EXPECTED_TONE_CLASS[tone]);
      }
    });
  }

  it("forwards className, title, DOM props, a11y attributes, style, and ref", () => {
    const ref = createRef<HTMLSpanElement>();
    render(
      <Badge
        aria-label="Worker status"
        className="custom-badge"
        data-testid="badge"
        ref={ref}
        style={{ maxWidth: 120 }}
        title="Full worker status"
        tone="success"
      >
        Ready
      </Badge>,
    );

    const badge = screen.getByTestId("badge");
    expect(badge.getAttribute("aria-label")).toBe("Worker status");
    expect(badge.className).toContain("custom-badge");
    expect(badge.getAttribute("title")).toBe("Full worker status");
    expect(badge.style.maxWidth).toBe("120px");
    expect(ref.current).toBe(badge);
  });
});
