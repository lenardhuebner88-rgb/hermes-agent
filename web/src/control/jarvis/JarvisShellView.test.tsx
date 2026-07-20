// @vitest-environment jsdom
/**
 * JarvisShellView — S4-Härtung: die statischen A4-Mock-Panels (Brain-Stats/
 * Top-Hubs, Filter, KI-LAGE, System-Sparklines) tragen denselben sichtbaren
 * Mock-Tag wie der Graph-Fallback (JARVIS_BRAIN_MOCKTAG) — Mock-Inhalte
 * gehen nie als live durch. S5-Design: die Mock-Panels stehen hinter dem
 * HUD-Toggle (jv-hud-off am Stage-Root, Default aus, localStorage
 * hermes.jarvis.hud) und das Periphery-Event des Chats öffnet den
 * Aktivitaet-Drawer. Die Live-Kinder (Graph, Panels, Chat) sind hier
 * bewusst gestubbt: getestet wird ausschließlich das Shell-Markup.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const aktivitaetProps = vi.hoisted(() => ({ open: undefined as boolean | undefined }));

vi.mock("./useOfflineBannerHeight", () => ({ useOfflineBannerHeight: () => undefined }));
vi.mock("./JarvisGraph", () => ({
  JarvisGraph: () => <div data-testid="graph" />,
  JarvisGraphStatsTag: () => null,
  JarvisGraphTag: () => null,
}));
vi.mock("./JarvisChat", async () => {
  const actual = await vi.importActual<typeof import("./JarvisChat")>("./JarvisChat");
  return { ...actual, JarvisChat: () => null };
});
vi.mock("./ProjektePanel", () => ({ ProjektePanel: () => null }));
vi.mock("./WartetPanel", () => ({ WartetPanel: () => null }));
vi.mock("./AktivitaetPanel", () => ({
  AktivitaetPanel: (props: { open: boolean }) => {
    aktivitaetProps.open = props.open;
    return null;
  },
}));
vi.mock("./SessionsPanel", () => ({ SessionsPanel: () => null }));

import { JarvisShellView } from "./JarvisShellView";
import { JARVIS_OPEN_AKTIVITAET_EVENT } from "./JarvisChat";
import { JARVIS_MOCK_TAG } from "./mockContent";

function renderShell() {
  return render(
    <MemoryRouter>
      <JarvisShellView />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  aktivitaetProps.open = undefined;
});

afterEach(() => cleanup());

describe("JarvisShellView — Mock-Tags an den statischen A4-Panels", () => {
  it("markiert Brain-Panel, Filter, KI-LAGE und Sparklines sichtbar als Mock", () => {
    const { container } = render(
      <MemoryRouter>
        <JarvisShellView />
      </MemoryRouter>,
    );

    const tags = Array.from(container.querySelectorAll(".jv-panelmock"));
    expect(tags).toHaveLength(4);
    for (const tag of tags) {
      expect(tag.textContent).toBe(JARVIS_MOCK_TAG);
      // Dasselbe Label-Muster wie der Graph-Fallback-Tag.
      expect(tag.className).toContain("jv-mocktag");
    }

    // Brain-Panel (Stats + Top-Hubs): Tag am Panel-Titel.
    expect(container.querySelector(".jv-brainpanel h1 .jv-panelmock")).toBeTruthy();
    // Filter-Panel.
    expect(container.querySelector(".jv-filter .jv-ptitle .jv-panelmock")).toBeTruthy();
    // KI-LAGE-Panel.
    expect(container.querySelector(".jv-news .jv-ptitle .jv-panelmock")).toBeTruthy();
    // System-Sparklines im Wartet-Panel-Float.
    expect(container.querySelector(".jv-sys .jv-panelmock")).toBeTruthy();
  });
});

describe("JarvisShellView — S5-Design (HUD-Toggle + Periphery-Event)", () => {
  it("rendert das alte S1-Emblem nicht mehr (Engine-Wahl nur am Orb-Header)", () => {
    const { container } = renderShell();

    // Kein zweiter Orb/Switcher neben dem JarvisOrb im Chat — das schwebende
    // Emblem rechts unten ist mit S5 entfallen.
    expect(container.querySelector(".jv-emblem")).toBeNull();
    expect(container.querySelector(".jv-ering")).toBeNull();
  });

  it("HUD ist Default aus (jv-hud-off am Stage-Root), Panels bleiben im DOM", () => {
    const { container } = renderShell();

    expect(container.querySelector(".jv-stage.jv-hud-off")).toBeTruthy();
    // Die Mock-Panels werden per CSS ausgeblendet, nicht aus dem DOM gerissen
    // (Mock-Tags der S4-Härtung bleiben am Code).
    expect(container.querySelectorAll(".jv-panelmock")).toHaveLength(4);
  });

  it("HUD-Toggle blendet die Panels ein und persistiert in localStorage", () => {
    const { container, getByRole } = renderShell();

    const toggle = getByRole("button", { name: /HUD-Panels/ });
    expect(toggle.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(toggle);
    expect(container.querySelector(".jv-stage.jv-hud-off")).toBeNull();
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
    expect(window.localStorage.getItem("hermes.jarvis.hud")).toBe("1");

    fireEvent.click(toggle);
    expect(container.querySelector(".jv-stage.jv-hud-off")).toBeTruthy();
    expect(window.localStorage.getItem("hermes.jarvis.hud")).toBe("0");
  });

  it("persistierter HUD-Stand wird beim Mount gelesen", () => {
    window.localStorage.setItem("hermes.jarvis.hud", "1");
    const { container } = renderShell();

    expect(container.querySelector(".jv-stage.jv-hud-off")).toBeNull();
  });

  it("Periphery-Event des Chats öffnet den Aktivitaet-Drawer", () => {
    renderShell();
    expect(aktivitaetProps.open).toBe(false);

    fireEvent(window, new CustomEvent(JARVIS_OPEN_AKTIVITAET_EVENT));
    expect(aktivitaetProps.open).toBe(true);
  });
});
