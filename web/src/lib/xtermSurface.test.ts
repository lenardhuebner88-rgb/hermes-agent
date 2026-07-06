/* @vitest-environment jsdom */
import { describe, expect, it, vi } from "vitest";
import {
  hardenTerminalTextInput,
  SGR_WHEEL_DOWN,
  SGR_WHEEL_UP,
  shouldDeferWheelToTerminal,
  touchScrollSteps,
} from "./xtermSurface";

describe("shouldDeferWheelToTerminal", () => {
  it("defers to xterm on the alternate buffer (fullscreen TUIs)", () => {
    expect(shouldDeferWheelToTerminal("alternate")).toBe(true);
  });

  it("does not defer on the normal buffer (local scrollback)", () => {
    expect(shouldDeferWheelToTerminal("normal")).toBe(false);
  });
});

describe("SGR wheel report constants", () => {
  it("wheel-up/down sequences match tmux's SGR mouse button codes (tmux.h: MOUSE_WHEEL_UP == 64)", () => {
    expect(SGR_WHEEL_UP).toBe("\x1b[<64;1;1M");
    expect(SGR_WHEEL_DOWN).toBe("\x1b[<65;1;1M");
  });
});

describe("touchScrollSteps", () => {
  it("accumulates sub-step drag deltas across calls before emitting a step", () => {
    const first = touchScrollSteps(15, 20);
    expect(first).toEqual({ steps: 0, remainder: 15 });

    const second = touchScrollSteps(first.remainder + 10, 20);
    expect(second).toEqual({ steps: 1, remainder: 5 });
  });

  it("is symmetric for negative (opposite-direction) deltas", () => {
    const first = touchScrollSteps(-15, 20);
    expect(first).toEqual({ steps: 0, remainder: -15 });

    const second = touchScrollSteps(first.remainder - 10, 20);
    expect(second).toEqual({ steps: -1, remainder: -5 });
  });

  it("emits multiple steps at once for a large single delta", () => {
    expect(touchScrollSteps(45, 20)).toEqual({ steps: 2, remainder: 5 });
  });

  it("returns no steps and passes the delta through unchanged for a non-positive step size", () => {
    expect(touchScrollSteps(100, 0)).toEqual({ steps: 0, remainder: 100 });
  });
});

describe("hardenTerminalTextInput", () => {
  it("disables mobile keyboard autocorrect/prediction on xterm helper textareas", () => {
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      cb(0);
      return 0;
    });
    const host = document.createElement("div");
    const textarea = document.createElement("textarea");
    textarea.className = "xterm-helper-textarea";
    host.appendChild(textarea);

    hardenTerminalTextInput(host);

    expect(textarea.getAttribute("autocomplete")).toBe("off");
    expect(textarea.getAttribute("autocorrect")).toBe("off");
    expect(textarea.getAttribute("autocapitalize")).toBe("off");
    expect(textarea.getAttribute("spellcheck")).toBe("false");
    expect(textarea.getAttribute("aria-autocomplete")).toBe("none");
    expect(textarea.getAttribute("enterkeyhint")).toBe("send");
    expect(textarea.getAttribute("data-ms-editor")).toBe("false");
    expect(textarea.spellcheck).toBe(false);
  });
});
