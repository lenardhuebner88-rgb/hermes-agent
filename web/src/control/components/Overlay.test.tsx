// @vitest-environment jsdom
import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Overlay } from "./Overlay";

afterEach(cleanup);

describe("Overlay", () => {
  it("keeps the bottom-sheet base classes for <tab and adds the tab: right side-sheet tier (SHELL-SPEC.md W2-c)", () => {
    render(
      <Overlay onClose={() => {}} ariaLabel="Test-Overlay">
        <button type="button">Inner</button>
      </Overlay>,
    );
    const dialog = screen.getByRole("dialog", { name: "Test-Overlay" });
    // <tab bottom-sheet, unchanged: bottom-anchored, top-rounded only.
    expect(dialog.className).toContain("rounded-t-2xl");
    expect(dialog.className).toContain("rounded-b-none");
    // >=tab (600px) right side-sheet: full height, capped width, flush left border.
    expect(dialog.className).toContain("tab:h-dvh");
    expect(dialog.className).toContain("tab:w-[min(420px,60vw)]");
    expect(dialog.className).toContain("tab:border-l");
    expect(dialog.className).toContain("tab:border-line");
    expect(dialog.className).toContain("tab:rounded-none");
    expect(dialog.className).toContain("hc-side-sheet-in");

    const backdrop = dialog.parentElement;
    expect(backdrop?.className).toContain("items-end");
    expect(backdrop?.className).toContain("justify-center");
    expect(backdrop?.className).toContain("tab:items-stretch");
    expect(backdrop?.className).toContain("tab:justify-end");
  });

  it("focuses the first focusable element on open and restores focus to the trigger on close", () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <div>
          <button type="button" onClick={() => setOpen(true)}>Trigger</button>
          {open ? (
            <Overlay onClose={() => setOpen(false)} ariaLabel="Focus-Overlay">
              <button type="button">Inner action</button>
            </Overlay>
          ) : null}
        </div>
      );
    }
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Trigger" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Inner action" }));

    fireEvent.keyDown(window, { key: "Escape" });
    expect(document.activeElement).toBe(trigger);
  });

  it("closes on Escape and on backdrop click, not on inner click", () => {
    const onClose = vi.fn();
    render(
      <Overlay onClose={onClose} ariaLabel="Close-Overlay">
        <button type="button">Inner</button>
      </Overlay>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Inner" }));
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("presentation"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
