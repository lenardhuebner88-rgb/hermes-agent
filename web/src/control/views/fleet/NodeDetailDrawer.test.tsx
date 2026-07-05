// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it } from "vitest";

import { AktivitaetTab, UebersichtTab } from "./NodeDetailDrawer";

// Events im echten activity-Format (GET /tasks/{id}/activity → {events: [{id, kind, note, at}]}).
const baseEvents = [
  { id: 1, kind: "review_skipped_deterministic", note: null, at: 1782508000 },
  { id: 2, kind: "review_deferred_to_tip", note: "Kette läuft weiter", at: 1782507900 },
  { id: 3, kind: "claimed", note: null, at: 1782507800 },
];

describe("AktivitaetTab (NodeDetailDrawer)", () => {
  afterEach(() => cleanup());

  it("maps review_skipped_deterministic to a positive-toned, human-readable chip", () => {
    render(<AktivitaetTab events={baseEvents} now={1782508100} loading={false} />);

    expect(screen.getByText("Gates-verifiziert (Review übersprungen)")).toBeTruthy();
    expect(screen.queryByText("review_skipped_deterministic")).toBeNull();
  });

  it("maps review_deferred_to_tip to a neutral-toned, human-readable chip", () => {
    render(<AktivitaetTab events={baseEvents} now={1782508100} loading={false} />);

    expect(screen.getByText("Urteil am Kettenende")).toBeTruthy();
    expect(screen.queryByText("review_deferred_to_tip")).toBeNull();
  });

  it("still renders unmapped kinds raw, unaffected by the new mapping", () => {
    render(<AktivitaetTab events={baseEvents} now={1782508100} loading={false} />);

    expect(screen.getByText("claimed")).toBeTruthy();
  });
});


describe("UebersichtTab mobile Lesbarkeit und Runtime-Semantik", () => {
  it("beschriftet Task-Lane und Laufprofil getrennt", () => {
    const html = renderToStaticMarkup(
      <UebersichtTab
        task={{ id: "t1", title: "T", status: "running", assignee: "premium", body: null }}
        latestRun={{ profile: "premium", status: "running", runtime_seconds: 60 }}
        elapsedSec={60}
        deliverables={[]}
      />,
    );

    expect(html).toContain("Task-Lane");
    expect(html).toContain("Laufprofil");
    expect(html).toContain("premium");
    expect(html).not.toContain("Assignee");
    expect(html).not.toContain("Modell");
  });

  it("rendert lange Taskbeschreibungen mit Wortumbruch und eigener Scrollfläche statt hartem Abschneiden", () => {
    const body = `## Auftrag\n${"x".repeat(500)}ENDE`;
    const html = renderToStaticMarkup(
      <UebersichtTab
        task={{ id: "t1", title: "T", status: "running", assignee: "coder", body }}
        latestRun={{ profile: "coder", status: "running", runtime_seconds: 60 }}
        elapsedSec={60}
        deliverables={[]}
      />,
    );

    expect(html).toContain("overflow-y:auto");
    expect(html).toContain("overflow-wrap:anywhere");
    expect(html).toContain("white-space:pre-wrap");
    expect(html).toContain("ENDE");
    expect(html).not.toContain("mask-image");
  });
});
