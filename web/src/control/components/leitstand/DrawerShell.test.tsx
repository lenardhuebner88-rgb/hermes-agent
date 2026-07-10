// @vitest-environment jsdom
import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DrawerShell } from "./DrawerShell";

afterEach(cleanup);

describe("DrawerShell", () => {
  it("keeps the bottom-sheet base classes for <tab and adds the tab: flush right side-sheet tier (SHELL-SPEC.md W2-c)", () => {
    render(
      <DrawerShell title="Test-Drawer" onClose={() => {}} ariaLabel="Test-Drawer">
        <button type="button">Inner</button>
      </DrawerShell>,
    );
    const dialog = screen.getByRole("dialog", { name: "Test-Drawer" });
    // <tab bottom-sheet, unchanged: bottom-anchored, top-rounded, shadow.
    expect(dialog.className).toContain("rounded-t-2xl");
    expect(dialog.className).toContain("shadow-2xl");
    // >=tab (600px) flush right side-sheet: full height, no rounding/shadow,
    // border-l is the only seam, default width min(420px, 60vw).
    expect(dialog.className).toContain("tab:h-full");
    expect(dialog.className).toContain("tab:rounded-none");
    expect(dialog.className).toContain("tab:border-l");
    expect(dialog.className).toContain("tab:border-line");
    expect(dialog.className).toContain("tab:shadow-none");
    expect(dialog.className).toContain("tab:w-[min(420px,60vw)]");
    expect(dialog.className).toContain("hc-side-sheet-in");

    const backdrop = dialog.parentElement;
    expect(backdrop?.className).toContain("items-end");
    expect(backdrop?.className).toContain("justify-end");
    expect(backdrop?.className).toContain("tab:items-stretch");
  });

  it("still lets a caller override the default width (e.g. wider Bibliothek/Modelle drawers)", () => {
    render(
      <DrawerShell title="Wide" onClose={() => {}} ariaLabel="Wide-Drawer" widthClassName="tab:w-[min(900px,calc(100vw-2rem))]">
        <button type="button">Inner</button>
      </DrawerShell>,
    );
    const dialog = screen.getByRole("dialog", { name: "Wide-Drawer" });
    expect(dialog.className).toContain("tab:w-[min(900px,calc(100vw-2rem))]");
    expect(dialog.className).not.toContain("tab:w-[min(420px,60vw)]");
  });

  it("focuses the first focusable element on open and restores focus to the trigger on close", () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <div>
          <button type="button" onClick={() => setOpen(true)}>Trigger</button>
          {open ? (
            <DrawerShell title="Focus" onClose={() => setOpen(false)} ariaLabel="Focus-Drawer">
              <button type="button">Inner action</button>
            </DrawerShell>
          ) : null}
        </div>
      );
    }
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Trigger" });
    trigger.focus();
    fireEvent.click(trigger);
    // First focusable inside the dialog is the header close button.
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Schließen" }));

    fireEvent.keyDown(window, { key: "Escape" });
    expect(document.activeElement).toBe(trigger);
  });

  it("closes on Escape and calls onClose on backdrop click, not on inner click", () => {
    const onClose = vi.fn();
    render(
      <DrawerShell title="Close" onClose={onClose} ariaLabel="Close-Drawer">
        <button type="button">Inner</button>
      </DrawerShell>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Inner" }));
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("presentation"));
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
