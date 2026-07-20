// @vitest-environment jsdom
/**
 * JarvisOrb — S5-Design: der Zustand landet als `.jv-orb--<state>`-Klasse am
 * Orb (reine CSS-Animation), die aria-Zeile trägt Zustand + Modell, und ein
 * Tap fokussiert den Engine-Switcher (Engine-Wahl bleibt am Orb). Der echte
 * Switcher (Roster-Fetch) ist hier gestubbt — getestet wird nur das Orb-
 * Markup/Verhalten.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./EngineSwitcher", () => ({
  EngineSwitcher: () => (
    <label className="jv-model jv-switch">
      <select aria-label="Modell für den nächsten Turn wählen" />
    </label>
  ),
}));

import { JarvisOrb, type JarvisOrbState } from "./JarvisOrb";

afterEach(() => cleanup());

describe("JarvisOrb", () => {
  it("bildet jeden Zustand auf die .jv-orb--<state>-Klasse ab", () => {
    const states: JarvisOrbState[] = ["idle", "listening", "thinking", "speaking", "error"];
    for (const state of states) {
      const { container, unmount } = render(<JarvisOrb state={state} engineLabel="GPT-5.6-SOL" />);
      expect(container.querySelector(`.jv-orb--${state}`)).toBeTruthy();
      unmount();
    }
  });

  it("trägt Zustand + Modell in der aria-Zeile", () => {
    render(<JarvisOrb state="thinking" engineLabel="OPUS-4.8" />);
    const orb = document.querySelector(".jv-orb") as HTMLButtonElement;
    expect(orb.getAttribute("aria-label")).toContain("DENKT");
    expect(orb.getAttribute("aria-label")).toContain("OPUS-4.8");
  });

  it("Tap fokussiert den Engine-Switcher und ruft onEngineClick", () => {
    const onEngineClick = vi.fn();
    render(<JarvisOrb state="idle" engineLabel="GPT-5.6-SOL" onEngineClick={onEngineClick} />);

    const orb = document.querySelector(".jv-orb") as HTMLButtonElement;
    fireEvent.click(orb);

    const select = document.querySelector(".jv-orbswitch select") as HTMLSelectElement;
    expect(document.activeElement).toBe(select);
    expect(onEngineClick).toHaveBeenCalledTimes(1);
  });
});
