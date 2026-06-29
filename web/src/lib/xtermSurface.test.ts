/* @vitest-environment jsdom */
import { describe, expect, it, vi } from "vitest";
import { hardenTerminalTextInput } from "./xtermSurface";

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
