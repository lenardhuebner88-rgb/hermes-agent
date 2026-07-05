// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { AktivitaetTab } from "./NodeDetailDrawer";

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
