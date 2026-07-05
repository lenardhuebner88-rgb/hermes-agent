// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { KpiTile, SectionHeader, SubtabChips, type SubtabItem } from "./index";

afterEach(cleanup);

/**
 * Guards for the S1 Leitstand building blocks. These prove the shared idiom
 * behaves as the extracted views rely on it — especially SubtabChips, which
 * FleetView drives through and whose aria-label contract the FleetView tests
 * (`getByRole("button", { name: "Subtab Plan" })`) pin.
 */
describe("leitstand building blocks", () => {
  describe("SectionHeader", () => {
    it("renders the label and right-aligned meta on a top hairline by default", () => {
      const { container } = render(<SectionHeader label="Druck" meta="OK" />);
      expect(screen.getByText("Druck")).toBeTruthy();
      expect(screen.getByText("OK")).toBeTruthy();
      expect(container.firstElementChild?.className).toContain("border-t");
    });

    it("drops the hairline when rule=false and omits meta when absent", () => {
      const { container } = render(<SectionHeader label="Nur Label" rule={false} />);
      expect(container.firstElementChild?.className).not.toContain("border-t");
      expect(screen.queryByText("OK")).toBeNull();
    });
  });

  describe("KpiTile", () => {
    it("renders value, suffix and a coloured up-delta", () => {
      render(<KpiTile label="Akzeptanz" value="92" suffix="%" delta="▲ 3" deltaTone="up" />);
      expect(screen.getByText("Akzeptanz")).toBeTruthy();
      expect(screen.getByText("92")).toBeTruthy();
      expect(screen.getByText("%")).toBeTruthy();
      const delta = screen.getByText("▲ 3");
      expect(delta.className).toContain("text-status-ok");
    });
  });

  describe("SubtabChips", () => {
    const items: SubtabItem[] = [
      { id: "heute", label: "Heute" },
      { id: "plan", label: "Plan", count: 2 },
      { id: "risiko", label: "Risiko", warn: true },
    ];

    it("marks the active chip via aria-pressed and exposes the FleetView aria-label contract", () => {
      render(<SubtabChips items={items} active="heute" onSelect={() => {}} ariaLabelPrefix="Subtab" />);
      const heute = screen.getByRole("button", { name: "Subtab Heute" });
      expect(heute.getAttribute("aria-pressed")).toBe("true");
      // Plain-name lookup mirrors FleetView.planspec-drawer.test.tsx.
      expect(screen.getByRole("button", { name: "Subtab Plan" })).toBeTruthy();
      // A warn chip appends the warning suffix to its aria-label.
      expect(screen.getByRole("button", { name: "Subtab Risiko — enthält Warnungen" })).toBeTruthy();
    });

    it("renders the count superscript and fires onSelect with the chip id", () => {
      const onSelect = vi.fn();
      render(<SubtabChips items={items} active="heute" onSelect={onSelect} ariaLabelPrefix="Subtab" />);
      expect(screen.getByText("2").tagName.toLowerCase()).toBe("sup");
      fireEvent.click(screen.getByRole("button", { name: "Subtab Plan" }));
      expect(onSelect).toHaveBeenCalledWith("plan");
    });

    it("applies the caller's chip skin classes (Fleet-theme preservation)", () => {
      render(
        <SubtabChips
          items={items}
          active="plan"
          onSelect={() => {}}
          ariaLabelPrefix="Subtab"
          classes={{ chip: "fleet-chip", chipActive: "fleet-chip-on", warnDot: "fleet-warn-dot" }}
        />,
      );
      const active = screen.getByRole("button", { name: "Subtab Plan" });
      expect(active.className).toContain("fleet-chip");
      expect(active.className).toContain("fleet-chip-on");
    });
  });
});
