// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SignalChip, SignalLabel, signalToneFromLegacy } from "./StatusSignal";

afterEach(cleanup);

describe("StatusSignal", () => {
  it("SignalLabel trägt immer das Wort + aria-hidden Punkt (nie farb-only)", () => {
    const { container } = render(<SignalLabel tone="alert" label="blockiert" />);
    expect(screen.getByText("blockiert")).toBeTruthy();
    const dot = container.querySelector("[aria-hidden]");
    expect(dot).not.toBeNull();
    expect(dot!.className).toContain("bg-status-alert");
  });

  it("SignalChip rendert Chip-Körper mit Ton-Tint", () => {
    const { container } = render(<SignalChip tone="warn" label="drift" />);
    expect(screen.getByText("drift")).toBeTruthy();
    expect((container.firstChild as HTMLElement).className).toContain("border-status-warn/30");
  });

  it("SignalChip hält lange Labels per Ellipsis + title vollständig erreichbar", () => {
    render(<SignalChip tone="neutral" label="sehr langer PlanSpec-Status" title="sehr langer PlanSpec-Status" className="max-w-24" />);
    const label = screen.getByText("sehr langer PlanSpec-Status");
    expect(label.className).toContain("truncate");
    expect(label.getAttribute("title")).toBe("sehr langer PlanSpec-Status");
  });

  it("neutral nutzt Ink-Vokabular, keine Statusfarbe und kein Bronze", () => {
    const { container } = render(<SignalChip tone="neutral" label="später" />);
    const cls = (container.firstChild as HTMLElement).className;
    expect(cls).toContain("border-line");
    expect(cls).not.toMatch(/status-(ok|warn|alert)/);
    expect(cls).not.toMatch(/live|bronze/);
  });

  it("signalToneFromLegacy übersetzt das alte ToneName-Vokabular", () => {
    expect(signalToneFromLegacy("red")).toBe("alert");
    expect(signalToneFromLegacy("rose")).toBe("alert");
    expect(signalToneFromLegacy("amber")).toBe("warn");
    expect(signalToneFromLegacy("emerald")).toBe("ok");
    expect(signalToneFromLegacy("zinc")).toBe("neutral");
    expect(signalToneFromLegacy(undefined)).toBe("neutral");
  });
});
