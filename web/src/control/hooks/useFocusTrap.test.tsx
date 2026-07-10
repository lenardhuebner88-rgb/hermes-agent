// @vitest-environment jsdom
import { useRef } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { useFocusTrap } from "./useFocusTrap";

function Dialog({ active }: { active: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useFocusTrap(ref, active);
  return (
    <div ref={ref} data-testid="dialog">
      <button type="button">First</button>
      <button type="button">Second</button>
      <button type="button">Last</button>
    </div>
  );
}

function DialogWithHiddenFirst({ active }: { active: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useFocusTrap(ref, active);
  return (
    <div ref={ref} data-testid="dialog">
      <button type="button" hidden>Hidden first</button>
      <button type="button">Visible first</button>
      <button type="button">Visible last</button>
    </div>
  );
}

function Harness({ mounted }: { mounted: boolean }) {
  return (
    <div>
      <button type="button">Outside trigger</button>
      {mounted ? <Dialog active /> : null}
    </div>
  );
}

afterEach(cleanup);

describe("useFocusTrap", () => {
  it("focuses the first focusable element inside the container on mount", () => {
    render(<Dialog active />);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "First" }));
  });

  it("traps Tab: wraps last -> first and Shift+Tab: first -> last", () => {
    render(<Dialog active />);
    const last = screen.getByRole("button", { name: "Last" });
    last.focus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "First" }));

    const first = screen.getByRole("button", { name: "First" });
    first.focus();
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
  });

  it("skips a hidden focusable child when choosing and wrapping focus", () => {
    render(<DialogWithHiddenFirst active />);
    const visibleFirst = screen.getByRole("button", { name: "Visible first" });
    const visibleLast = screen.getByRole("button", { name: "Visible last" });

    expect(document.activeElement).toBe(visibleFirst);
    visibleLast.focus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(visibleFirst);
  });

  it("restores focus to the previously-focused element once the trap unmounts", () => {
    const { rerender } = render(<Harness mounted={false} />);
    const trigger = screen.getByRole("button", { name: "Outside trigger" });
    trigger.focus();
    expect(document.activeElement).toBe(trigger);

    rerender(<Harness mounted />);
    // Focus moved into the dialog on mount.
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "First" }));

    rerender(<Harness mounted={false} />);
    expect(document.activeElement).toBe(trigger);
  });

  it("does not restore focus to an element that was removed from the document", () => {
    const { rerender, unmount } = render(<Harness mounted={false} />);
    const trigger = screen.getByRole("button", { name: "Outside trigger" });
    trigger.focus();

    rerender(<Harness mounted />);
    trigger.remove();
    // Unmounting must not throw even though the previously-focused element is gone.
    expect(() => unmount()).not.toThrow();
  });
});
