// @vitest-environment jsdom

import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TONE_HEX } from "../../lib/tones";
import { ErrorNote, FleetEmptyState, FreshnessStrip, KpiTile, RoleChip, SectionHeader, SubtabChips, ViewHeader, type SubtabItem } from "./index";
import { nowSec } from "../../lib/derive";

afterEach(cleanup);

/**
 * Guards for the S1 Leitstand building blocks. These prove the shared idiom
 * behaves as the extracted views rely on it — especially SubtabChips, whose
 * tablist semantics and aria-label contract the FleetView tests
 * (`getByRole("tab", { name: "Subtab Plan" })`) pin.
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

    it("lifts the tile off its panel with the raised elevation step (rank-2)", () => {
      const { container } = render(<KpiTile label="Runs" value="12" />);
      const tile = container.firstElementChild;
      expect(tile?.className).toContain("shadow-raised");
      expect(tile?.className).toContain("transition-shadow");
    });
  });

  describe("RoleChip data identity", () => {
    it("uses the shared data token mapping and keeps a textual role marker", () => {
      const { container } = render(<RoleChip role={{ label: "Verifier", short: "V", tone: "sky" }} />);
      const chip = container.querySelector<HTMLElement>(".hc-role-chip");

      expect(chip?.style.getPropertyValue("--hc-role")).toBe("var(--color-data-4)");
      expect(container.textContent).toContain("V");
      expect(container.textContent).toContain("Verifier");
      expect(Object.values(TONE_HEX)).toEqual(expect.arrayContaining([
        "var(--color-data-1)",
        "var(--color-data-2)",
        "var(--color-data-3)",
        "var(--color-data-4)",
        "var(--color-data-5)",
        "var(--color-data-6)",
      ]));
      expect(Object.values(TONE_HEX).every((value) => /^var\(--color-data-[1-6]\)$/.test(value))).toBe(true);
    });
  });

  describe("ErrorNote", () => {
    it("renders the message with role=alert and a retry button that fires onRetry", () => {
      const onRetry = vi.fn();
      render(<ErrorNote message="Verlauf konnte nicht geladen werden." onRetry={onRetry} />);
      expect(screen.getByRole("alert").textContent).toContain("Verlauf konnte nicht geladen werden.");
      fireEvent.click(screen.getByRole("button", { name: /Erneut laden/ }));
      expect(onRetry).toHaveBeenCalledTimes(1);
    });

    it("shows the technical detail and keeps the alert tone by default", () => {
      render(<ErrorNote message="Laden fehlgeschlagen" detail="500: boom" onRetry={() => {}} />);
      const note = screen.getByRole("alert");
      expect(note.textContent).toContain("500: boom");
      expect(note.className).toContain("border-status-alert/30");
    });

    it("supports the degraded warn tone and omits the button without onRetry", () => {
      render(<ErrorNote tone="warn" message="Quelle degradiert." />);
      const note = screen.getByRole("alert");
      expect(note.className).toContain("border-status-warn/30");
      expect(screen.queryByRole("button")).toBeNull();
    });

    it("keeps the retry button neutral — no status-coloured affordance", () => {
      render(<ErrorNote message="Fehler" onRetry={() => {}} />);
      const button = screen.getByRole("button", { name: /Erneut laden/ });
      expect(button.className).toContain("border-line");
      expect(button.className).not.toContain("status-alert");
      expect(button.className).not.toContain("status-warn");
    });
  });

  describe("FleetEmptyState action slot", () => {
    it("renders the optional next-action row under the description", () => {
      const onOpen = vi.fn();
      render(
        <FleetEmptyState
          title="Noch keine Karten."
          desc="Der Bereich ist unbestückt."
          action={<button type="button" onClick={onOpen}>Neue Karte anlegen</button>}
        />,
      );
      const action = screen.getByRole("button", { name: "Neue Karte anlegen" });
      fireEvent.click(action);
      expect(onOpen).toHaveBeenCalledTimes(1);
    });

    it("renders no action row when the prop is absent", () => {
      const { container } = render(<FleetEmptyState title="Leer." desc="Nichts zu tun." />);
      expect(container.querySelector("button")).toBeNull();
    });
  });

  describe("SubtabChips", () => {
    const items: SubtabItem[] = [
      { id: "heute", label: "Heute" },
      { id: "plan", label: "Plan", count: 2 },
      { id: "risiko", label: "Risiko", warn: true },
    ];

    it("exposes tablist semantics: role=tab, aria-selected, aria-pressed mirror, FleetView aria-label contract", () => {
      render(<SubtabChips items={items} active="heute" onSelect={() => {}} ariaLabelPrefix="Subtab" />);
      expect(screen.getByRole("tablist", { name: "Subtab" })).toBeTruthy();
      const heute = screen.getByRole("tab", { name: "Subtab Heute" });
      expect(heute.getAttribute("aria-selected")).toBe("true");
      // aria-pressed mirror: locked FleetView code queries [aria-pressed="true"].
      expect(heute.getAttribute("aria-pressed")).toBe("true");
      expect(screen.getByRole("tab", { name: "Subtab Plan" }).getAttribute("aria-selected")).toBe("false");
      // A warn chip appends the warning suffix to its aria-label.
      expect(screen.getByRole("tab", { name: "Subtab Risiko — enthält Warnungen" })).toBeTruthy();
    });

    it("keeps a roving tabindex: only the active tab is tabbable by default", () => {
      render(<SubtabChips items={items} active="plan" onSelect={() => {}} ariaLabelPrefix="Subtab" />);
      expect(screen.getByRole("tab", { name: "Subtab Plan" }).tabIndex).toBe(0);
      expect(screen.getByRole("tab", { name: "Subtab Heute" }).tabIndex).toBe(-1);
      expect(screen.getByRole("tab", { name: "Subtab Risiko — enthält Warnungen" }).tabIndex).toBe(-1);
    });

    it("moves focus with ArrowRight/ArrowLeft (wrapping) and Home/End without selecting", () => {
      const onSelect = vi.fn();
      render(<SubtabChips items={items} active="heute" onSelect={onSelect} ariaLabelPrefix="Subtab" />);
      const heute = screen.getByRole("tab", { name: "Subtab Heute" });
      const plan = screen.getByRole("tab", { name: "Subtab Plan" });
      const risiko = screen.getByRole("tab", { name: "Subtab Risiko — enthält Warnungen" });

      heute.focus();
      fireEvent.keyDown(heute, { key: "ArrowRight" });
      expect(document.activeElement).toBe(plan);
      expect(plan.tabIndex).toBe(0);
      expect(heute.tabIndex).toBe(-1);
      expect(onSelect).not.toHaveBeenCalled();

      fireEvent.keyDown(plan, { key: "ArrowRight" });
      expect(document.activeElement).toBe(risiko);
      fireEvent.keyDown(risiko, { key: "ArrowRight" });
      expect(document.activeElement).toBe(heute);
      fireEvent.keyDown(heute, { key: "ArrowLeft" });
      expect(document.activeElement).toBe(risiko);

      fireEvent.keyDown(risiko, { key: "Home" });
      expect(document.activeElement).toBe(heute);
      fireEvent.keyDown(heute, { key: "End" });
      expect(document.activeElement).toBe(risiko);
      expect(onSelect).not.toHaveBeenCalled();
    });

    it("selects via Enter/Space on the focused tab and re-anchors the roving tabindex on the new active tab", () => {
      function Harness() {
        const [active, setActive] = useState("heute");
        return <SubtabChips items={items} active={active} onSelect={setActive} ariaLabelPrefix="Subtab" />;
      }
      render(<Harness />);
      const heute = screen.getByRole("tab", { name: "Subtab Heute" });
      const risiko = screen.getByRole("tab", { name: "Subtab Risiko — enthält Warnungen" });

      heute.focus();
      fireEvent.keyDown(heute, { key: "End" });
      fireEvent.keyDown(risiko, { key: "Enter" });
      // jsdom does not synthesize click from Enter on keyDown — activate directly.
      fireEvent.click(risiko);

      expect(risiko.getAttribute("aria-selected")).toBe("true");
      expect(risiko.tabIndex).toBe(0);
      expect(heute.tabIndex).toBe(-1);
    });

    it("renders the count superscript and fires onSelect with the chip id", () => {
      const onSelect = vi.fn();
      render(<SubtabChips items={items} active="heute" onSelect={onSelect} ariaLabelPrefix="Subtab" />);
      expect(screen.getByText("2").tagName.toLowerCase()).toBe("sup");
      fireEvent.click(screen.getByRole("tab", { name: "Subtab Plan" }));
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
      const active = screen.getByRole("tab", { name: "Subtab Plan" });
      expect(active.className).toContain("fleet-chip");
      expect(active.className).toContain("fleet-chip-on");
    });
  });

  describe("ViewHeader", () => {
    it("renders eyebrow, h1 title and description in the display/type-token idiom", () => {
      render(<ViewHeader eyebrow="QUALITÄT · 7 TAGE" title="Worker Scorecard" description="Eine Entscheidung, dann ihre Begründung." />);
      const eyebrow = screen.getByText("QUALITÄT · 7 TAGE");
      expect(eyebrow.className).toContain("font-display");
      expect(eyebrow.className).toContain("text-micro");
      expect(eyebrow.className).toContain("text-ink-3");
      const title = screen.getByRole("heading", { level: 1, name: "Worker Scorecard" });
      expect(title.className).toContain("font-display");
      expect(title.className).toContain("text-h1");
      const description = screen.getByText("Eine Entscheidung, dann ihre Begründung.");
      expect(description.className).toContain("text-ink-2");
    });

    it("renders the right-aligned actions slot and omits it (and the description) when absent", () => {
      const { container } = render(
        <ViewHeader eyebrow="FEHLER · 30 TAGE" title="Issues" actions={<button type="button">← Statistik</button>} />,
      );
      expect(screen.getByRole("button", { name: "← Statistik" })).toBeTruthy();
      expect(screen.queryByRole("heading", { level: 2 })).toBeNull();
      // Ohne description steht keine leere Erklärzeile im DOM.
      expect(container.querySelectorAll("p").length).toBe(1);
    });
  });

  describe("FreshnessStrip", () => {
    it("shows the muted age line in font-data micro and fires the refresh handler", () => {
      const onRefresh = vi.fn();
      render(<FreshnessStrip lastUpdated={nowSec() - 65} onRefresh={onRefresh} />);
      const line = screen.getByText(/^aktualisiert vor /);
      expect(line.textContent).toBe("aktualisiert vor 1m");
      expect(line.parentElement?.className).toContain("font-data");
      expect(line.parentElement?.className).toContain("tabular-nums");
      fireEvent.click(screen.getByRole("button", { name: "Jetzt aktualisieren" }));
      expect(onRefresh).toHaveBeenCalledTimes(1);
    });

    it("renders an honest dash until the first successful load — no invented timestamp", () => {
      render(<FreshnessStrip lastUpdated={null} />);
      expect(screen.getByText("aktualisiert —")).toBeTruthy();
      expect(screen.queryByRole("button")).toBeNull();
    });

    it("spins the icon only while the refetch is actually in flight", async () => {
      let settle: () => void = () => {};
      const onRefresh = vi.fn(() => new Promise<void>((resolve) => { settle = resolve; }));
      render(<FreshnessStrip lastUpdated={nowSec()} onRefresh={onRefresh} />);
      const button = screen.getByRole("button", { name: "Jetzt aktualisieren" });
      expect(button.querySelector("svg")?.className.baseVal ?? "").not.toContain("animate-spin");
      fireEvent.click(button);
      const spinning = button.querySelector("svg");
      expect(spinning?.getAttribute("class")).toContain("animate-spin");
      settle();
      await screen.findByRole("button", { name: "Jetzt aktualisieren" });
      expect(button.querySelector("svg")?.getAttribute("class")).not.toContain("animate-spin");
    });
  });
});
