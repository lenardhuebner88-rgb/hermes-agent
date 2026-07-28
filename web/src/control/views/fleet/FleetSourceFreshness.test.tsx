// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { de } from "../../i18n/de";
import { FleetSourceFreshness } from "./FleetSourceFreshness";

const NOW = Math.floor(Date.now() / 1000);
// Weit jenseits des Stale-Schwellwerts (3× Poll-Intervall, mind. 30 s).
const STALE_AT = NOW - 20 * 60;

afterEach(cleanup);

describe("FleetSourceFreshness", () => {
  it("bleibt unsichtbar, solange alle Quellen frisch sind", () => {
    const { container } = render(
      <FleetSourceFreshness sources={[{ label: "PlanSpecs", lastUpdated: NOW, isStale: false }]} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("bietet bei veralteten Quellen eine Neu-laden-Aktion und nennt die Hintergrund-Tab-Ursache", () => {
    const reload = vi.fn();
    render(
      <FleetSourceFreshness sources={[
        { label: "PlanSpecs", lastUpdated: STALE_AT, reload },
        { label: "Kosten", lastUpdated: NOW },
      ]} />,
    );

    // Alter sichtbar + Ursache offengelegt (Hintergrund-Tab pausiert by design).
    expect(screen.getByText(/Daten von vor/)).toBeTruthy();
    expect(screen.getByText(de.fleet.freshnessPausedNote)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Neu laden/ }));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("zeigt bei Fehler keinen Pausen-Hinweis, aber weiterhin die Aktion", () => {
    const reload = vi.fn();
    render(
      <FleetSourceFreshness sources={[
        { label: "Worker", error: "500: boom", lastUpdated: NOW, reload },
      ]} />,
    );

    expect(screen.getByText(de.staleBadge.error)).toBeTruthy();
    expect(screen.queryByText(de.fleet.freshnessPausedNote)).toBeNull();
    expect(screen.getByRole("button", { name: /Neu laden/ })).toBeTruthy();
  });

  it("ohne reload-Fähigkeit keine tote Aktion", () => {
    render(
      <FleetSourceFreshness sources={[{ label: "PlanSpecs", lastUpdated: STALE_AT }]} />,
    );
    expect(screen.getByText(/Daten von vor/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Neu laden/ })).toBeNull();
  });
});
