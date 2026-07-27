// @vitest-environment jsdom
//
// Regression coverage for FleetTaskActions' `review`-status contract. The
// backend deliberately rejects direct PATCH review→done and review→blocked;
// no UI context may opt back into those deterministic 409 actions.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { FleetTaskActions } from "./TaskActions";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const FIXTURE_TASK_ID = "t_review123";

describe("FleetTaskActions — review-status stage buttons (default vs. allowReviewStage)", () => {
  it("status=review, default props: no Ship/Rework stage buttons", () => {
    render(<FleetTaskActions taskId={FIXTURE_TASK_ID} status="review" />);

    expect(screen.queryByRole("button", { name: "Ausliefern" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Nacharbeit" })).toBeNull();
  });

  it("status=review + legacy opt-in: Ship and Rework still stay absent", () => {
    // @ts-expect-error The removed escape hatch must not re-enter the public contract.
    render(<FleetTaskActions taskId={FIXTURE_TASK_ID} status="review" allowReviewStage />);

    expect(screen.queryByRole("button", { name: "Ausliefern" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Nacharbeit" })).toBeNull();
    expect(fetchJSONMock).not.toHaveBeenCalled();
  });

  it("withholds Starten and explains known unsatisfied parents before submission", () => {
    render(
      <FleetTaskActions
        taskId="t_child"
        status="todo"
        stageBlockReason="Starten nicht verfügbar — Vorgänger Blocking parent (running) ist nicht fertig."
      />,
    );

    expect(screen.queryByRole("button", { name: "Starten" })).toBeNull();
    expect(screen.getByText(/Blocking parent.*running/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Abbrechen" })).toBeTruthy();
  });
});

// KF-2: Start-Aktion am Kettenglied gibt sichtbar die ganze Kette frei.
const HELD_CHAIN = { rootId: "t_root9", memberCount: 3 };

describe("FleetTaskActions — gehaltene Kette (KF-2)", () => {
  it("AC-1+AC-2: Start-Aktion wird zur Kettenfreigabe (Root-ID + Gliederzahl), kein Einzelstart", async () => {
    const onReleaseChain = vi.fn().mockResolvedValue({ ok: true, released: 3 });
    render(
      <FleetTaskActions
        taskId="t_child2"
        status="scheduled"
        heldChain={HELD_CHAIN}
        onReleaseChain={onReleaseChain}
      />,
    );

    // Benennt Kette + Gliederzahl statt nur "Starten" (AC-1).
    const releaseBtn = screen.getByRole("button", { name: "Kette t_root9 freigeben (3 Glieder)" });
    expect(screen.queryByRole("button", { name: "Starten" })).toBeNull();

    // Zwei-Klick-Scharfstellung: erst Bestätigen, dann Freigabe der Root-ID (AC-2).
    fireEvent.click(releaseBtn);
    expect(onReleaseChain).not.toHaveBeenCalled();
    expect(screen.getByText(/Kette t_root9 mit 3 Gliedern freigeben/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));
    await waitFor(() => expect(onReleaseChain).toHaveBeenCalledWith("t_root9"));

    // Erfolg wird quittiert; der Status der einzelnen Karte wird nicht angefasst.
    await screen.findByText(/Kette freigegeben — 3 Glieder startbereit/);
    expect(fetchJSONMock).not.toHaveBeenCalled();
  });

  it("AC-3: Karte ohne Kettenzugehörigkeit behält Starten unverändert", () => {
    render(<FleetTaskActions taskId="t_free" status="scheduled" />);

    expect(screen.getByRole("button", { name: "Starten" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /freigeben/ })).toBeNull();
  });

  it("AC-4: abgelehnte Freigabe zeigt den Grund, kein stiller Fehlschlag", () => {
    render(
      <FleetTaskActions
        taskId="t_child2"
        status="scheduled"
        heldChain={HELD_CHAIN}
        onReleaseChain={vi.fn()}
        releaseChainError="404: chain root t_root9 is not operator-held (nothing to release)"
      />,
    );

    expect(screen.getByText(/not operator-held/)).toBeTruthy();
    // Die Aktion bleibt für einen erneuten Versuch sichtbar.
    expect(screen.getByRole("button", { name: "Kette t_root9 freigeben (3 Glieder)" })).toBeTruthy();
  });

  it("AC-5: Vorgänger-Hinweis erscheint weiterhin und verdeckt die Freigabe-Aktion nicht", () => {
    render(
      <FleetTaskActions
        taskId="t_child2"
        status="scheduled"
        heldChain={HELD_CHAIN}
        onReleaseChain={vi.fn()}
        stageBlockReason="Starten nicht verfügbar — Vorgänger Parent A (scheduled) ist nicht fertig."
      />,
    );

    expect(screen.getByText(/Parent A.*scheduled/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Kette t_root9 freigeben (3 Glieder)" })).toBeTruthy();
  });
});
