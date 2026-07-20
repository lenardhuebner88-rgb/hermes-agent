// @vitest-environment jsdom
/**
 * EngineSwitcher — S2.2 Modell-Switcher (seit S5-Design im Orb-Header des
 * Chats; das Shell-Emblem ist entfallen):
 *  1. Optionen kommen aus dem Roster (GET /api/pa/engines) mit den Brief-
 *     Labels („Opus 4.8", „Fable 5", „gpt-5.6-sol", „Kimi K3"); Vorauswahl =
 *     Server-Default, solange keine Wahl getroffen wurde.
 *  2. Eine Wahl landet im engineSelection-Store (gilt für den nächsten Turn —
 *     der POST-Payload-Teil ist in JarvisChat.test.tsx belegt).
 *  3. Roster nicht ladbar/leer → statisches S1-Badge als Fallback (kein
 *     Crash, kein leeres Dropdown).
 *  4. Eine Wahl, die im aktuellen Roster nicht mehr existiert, fällt auf den
 *     Server-Default zurück.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen } from "@testing-library/react";

import type { PaEnginesResponse } from "@/lib/api";
import { _resetPollingStore } from "../hooks/pollingStore";
import { _resetEngineChoice, getEngineChoice, setEngineChoice } from "./engineSelection";

configure({ asyncUtilTimeout: 5000 });

const getPaEnginesMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      getPaEngines: getPaEnginesMock,
    },
  };
});

import { EngineSwitcher } from "./EngineSwitcher";

const ROSTER: PaEnginesResponse = {
  default_engine: "sol",
  engines: [
    { engine: "sol", models: ["gpt-5.6-sol"], default_model: "gpt-5.6-sol", supports_images: true },
    {
      engine: "claude",
      models: ["opus-4.8", "fable-5"],
      default_model: "opus-4.8",
      supports_images: false,
    },
    { engine: "kimi", models: ["k3"], default_model: "k3", supports_images: false },
  ],
};

beforeEach(() => {
  _resetPollingStore();
  _resetEngineChoice();
  getPaEnginesMock.mockResolvedValue(ROSTER);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetPollingStore();
  _resetEngineChoice();
});

describe("EngineSwitcher (Roster /api/pa/engines)", () => {
  it("rendert die Roster-Optionen mit Brief-Labels, Vorauswahl = Server-Default", async () => {
    render(<EngineSwitcher />);

    const select = (await screen.findByLabelText(
      "Modell für den nächsten Turn wählen",
    )) as HTMLSelectElement;
    const labels = Array.from(select.options).map((option) => option.textContent);
    expect(labels).toEqual(["gpt-5.6-sol", "Opus 4.8", "Fable 5", "Kimi K3"]);
    // Keine Wahl getroffen → der Server-Default (sol/gpt-5.6-sol) ist aktiv.
    expect(getEngineChoice()).toBeNull();
    expect(select.value).toBe("sol:gpt-5.6-sol");
  });

  it("eine Wahl landet im Store (gilt für den nächsten Turn)", async () => {
    render(<EngineSwitcher />);

    const select = (await screen.findByLabelText(
      "Modell für den nächsten Turn wählen",
    )) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "claude:opus-4.8" } });

    expect(getEngineChoice()).toEqual({ engine: "claude", model: "opus-4.8" });
    expect(select.value).toBe("claude:opus-4.8");

    fireEvent.change(select, { target: { value: "kimi:k3" } });
    expect(getEngineChoice()).toEqual({ engine: "kimi", model: "k3" });
  });

  it("Roster-Fehler → statisches S1-Badge, kein leeres Dropdown", async () => {
    getPaEnginesMock.mockRejectedValue(new Error("network down"));
    render(<EngineSwitcher />);

    expect(await screen.findByText(/GPT-5\.6-SOL/)).toBeTruthy();
    expect(screen.queryByLabelText("Modell für den nächsten Turn wählen")).toBeNull();
  });

  it("Wahl außerhalb des Rosters fällt auf den Server-Default zurück", async () => {
    setEngineChoice({ engine: "ghost", model: "x-1" });
    render(<EngineSwitcher />);

    const select = (await screen.findByLabelText(
      "Modell für den nächsten Turn wählen",
    )) as HTMLSelectElement;
    expect(select.value).toBe("sol:gpt-5.6-sol");
  });

  it("S4-Härtung: die Wahl überlebt einen Reload (localStorage)", async () => {
    setEngineChoice({ engine: "claude", model: "opus-4.8" });
    expect(window.localStorage.getItem("hermes.jarvis.engine")).toBe(
      JSON.stringify({ engine: "claude", model: "opus-4.8" }),
    );

    // Reload simulieren: Modul frisch importieren — der Store restauriert die
    // persistierte Wahl beim Load (Muster des Vorlese-Toggles).
    vi.resetModules();
    const fresh = await import("./engineSelection");
    expect(fresh.getEngineChoice()).toEqual({ engine: "claude", model: "opus-4.8" });
    fresh._resetEngineChoice();
  });

  it("S4-Härtung: eine restaurierte Wahl ist die Vorauswahl des Switchers", async () => {
    window.localStorage.setItem(
      "hermes.jarvis.engine",
      JSON.stringify({ engine: "claude", model: "opus-4.8" }),
    );
    vi.resetModules();
    // Komponente UND Store aus derselben frischen Modul-Instanz (der statisch
    // importierte Switcher hängt am alten Store ohne restaurierte Wahl).
    const fresh = await import("./engineSelection");
    const { EngineSwitcher: FreshSwitcher } = await import("./EngineSwitcher");
    expect(fresh.getEngineChoice()).toEqual({ engine: "claude", model: "opus-4.8" });

    render(<FreshSwitcher />);
    const select = (await screen.findByLabelText(
      "Modell für den nächsten Turn wählen",
    )) as HTMLSelectElement;
    expect(select.value).toBe("claude:opus-4.8");
    fresh._resetEngineChoice();
  });
});
