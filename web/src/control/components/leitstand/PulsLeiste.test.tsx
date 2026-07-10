// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PulsLeiste } from "./PulsLeiste";

afterEach(cleanup);

const baseGateway = { status: "healthy" as const, stale: false, title: "Zuletzt aktuell vor 3s" };

/**
 * Guards the W2-b shared instrument strip (SHELL-SPEC.md "Puls-Leiste data
 * contract" / DESIGN.md "Puls-Leiste contract"): null-safety (never a 0-fake),
 * the Fragen "never color-only" rule, tabular/mono value styling, and the
 * Gateway LED+label pairing.
 */
describe("PulsLeiste", () => {
  it("renders the masthead label and dashes every instrument when its source is null", () => {
    render(
      <PulsLeiste label="Crons" workers={null} fragen={null} kostenUsd={null} gateway={{ ...baseGateway, status: "unknown" }} />,
    );
    expect(screen.getByText("Crons")).toBeTruthy();
    // Worker, Fragen, Kosten all fall back to "—" — never a fake 0.
    expect(screen.getAllByText("—").length).toBe(3);
  });

  it("shows an optional subtitle line under the label", () => {
    render(<PulsLeiste label="Fleet" subtitle="Leitstand · 10. Juli" workers={0} fragen={0} kostenUsd={null} gateway={baseGateway} />);
    expect(screen.getByText("Leitstand · 10. Juli")).toBeTruthy();
  });

  it("dims the Worker value to ink-3 and skips the ok-LED when the count is 0", () => {
    // Gateway defaults to "healthy" (its own hc-led-live) in these fixtures, so
    // the LED check must be scoped to the Worker label, not a document-wide query.
    render(<PulsLeiste label="Fleet" workers={0} fragen={null} kostenUsd={null} gateway={baseGateway} />);
    const value = screen.getByText("0");
    expect(value.className).toContain("text-ink-3");
    const workerLed = screen.getByText("Worker").querySelector(".hc-led");
    expect(workerLed?.className).toContain("hc-led-idle");
    expect(workerLed?.className).not.toContain("hc-led-live");
  });

  it("shows an ok-LED and un-dimmed value when Worker count is > 0", () => {
    render(<PulsLeiste label="Fleet" workers={3} fragen={null} kostenUsd={null} gateway={baseGateway} />);
    const value = screen.getByText("3");
    expect(value.className).not.toContain("text-ink-3");
    const workerLed = screen.getByText("Worker").querySelector(".hc-led");
    expect(workerLed?.className).toContain("hc-led-live");
  });

  it("renders Fragen as a plain dashed instrument (no icon) when the count is 0", () => {
    render(<PulsLeiste label="Fleet" workers={0} fragen={0} kostenUsd={null} gateway={baseGateway} />);
    expect(document.querySelector("svg")).toBeNull();
  });

  it("pairs the Fragen count with a warn-LED + AlertTriangle icon when > 0 — never color-only", () => {
    render(<PulsLeiste label="Fleet" workers={0} fragen={2} fragenTone="amber" kostenUsd={null} gateway={baseGateway} />);
    // Count + label text is present regardless of the icon (never color-only).
    expect(screen.getByText("2")).toBeTruthy();
    expect(screen.getByText("Fragen")).toBeTruthy();
    // The icon itself carries the warn signal alongside the LED.
    expect(document.querySelector("svg")).not.toBeNull();
    expect(document.querySelector(".hc-led-warn")).not.toBeNull();
  });

  it("escalates the Fragen LED/icon to the alert tone when the worst tone is red/rose", () => {
    render(<PulsLeiste label="Fleet" workers={0} fragen={1} fragenTone="red" kostenUsd={null} gateway={baseGateway} />);
    expect(document.querySelector(".hc-led-error")).not.toBeNull();
    expect(document.querySelector("svg.text-status-alert")).not.toBeNull();
  });

  it("formats Kosten as USD with a de-comma via fmtUsd, not €", () => {
    render(<PulsLeiste label="Fleet" workers={0} fragen={0} kostenUsd={4.1} gateway={baseGateway} />);
    expect(screen.getByText("$4,10")).toBeTruthy();
    expect(screen.queryByText(/€/)).toBeNull();
  });

  it("renders every instrument value in font-data/tabular-nums (data, not chrome)", () => {
    render(<PulsLeiste label="Fleet" workers={3} fragen={2} fragenTone="amber" kostenUsd={4.1} gateway={baseGateway} />);
    for (const text of ["3", "2", "$4,10"]) {
      const value = screen.getByText(text);
      expect(value.className).toContain("font-data");
      expect(value.className).toContain("tabular-nums");
    }
  });

  it("shows the Gateway LED class + a short state label, with the full status as a tooltip", () => {
    render(<PulsLeiste label="Fleet" workers={0} fragen={0} kostenUsd={null} gateway={{ status: "degraded", stale: false, title: "Gateway hakt" }} />);
    expect(screen.getByText("degraded")).toBeTruthy();
    expect(document.querySelector(".hc-led-warn")).not.toBeNull();
    expect(screen.getByText("degraded").closest("[title]")?.getAttribute("title")).toBe("Gateway hakt");
  });

  it("labels a stale Gateway reading distinctly from its live status", () => {
    render(<PulsLeiste label="Fleet" workers={0} fragen={0} kostenUsd={null} gateway={{ status: "healthy", stale: true, title: "stale" }} />);
    expect(screen.getByText("stale")).toBeTruthy();
  });

  it("renders the right-side children slot (NotificationBridge/StatusDots/⌘K in the real shell)", () => {
    render(
      <PulsLeiste label="Fleet" workers={0} fragen={0} kostenUsd={null} gateway={baseGateway}>
        <span>utility-slot</span>
      </PulsLeiste>,
    );
    expect(screen.getByText("utility-slot")).toBeTruthy();
  });

  it("wires an optional embedded ⌘K trigger for standalone adopters", () => {
    const onOpenCommand = vi.fn();
    render(<PulsLeiste label="Fleet" workers={0} fragen={0} kostenUsd={null} gateway={baseGateway} onOpenCommand={onOpenCommand} />);
    fireEvent.click(screen.getByRole("button", { name: "Command Palette" }));
    expect(onOpenCommand).toHaveBeenCalledTimes(1);
  });

  it("omits the ⌘K trigger entirely when no onOpenCommand is given", () => {
    render(<PulsLeiste label="Fleet" workers={0} fragen={0} kostenUsd={null} gateway={baseGateway} />);
    expect(screen.queryByRole("button", { name: "Command Palette" })).toBeNull();
  });

  it("marks Kosten with the äquiv. suffix when kostenIsEquivalent is set (honesty marker, s. Fleet's HeuteTab)", () => {
    render(<PulsLeiste label="Fleet" workers={0} fragen={0} kostenUsd={4.1} kostenIsEquivalent gateway={baseGateway} />);
    expect(screen.getByText(/äquiv\./)).toBeTruthy();
  });

  it("omits the äquiv. suffix when kostenIsEquivalent is not set", () => {
    render(<PulsLeiste label="Fleet" workers={0} fragen={0} kostenUsd={4.1} gateway={baseGateway} />);
    expect(screen.queryByText(/äquiv\./)).toBeNull();
  });
});
