// @vitest-environment jsdom
/**
 * PeripheryStrip — S5-Design: eine Zeile Maschinenraum über dem Gespräch.
 * Zähler + letzter deduplizierter Stand sichtbar, Tap/Enter öffnet das Log,
 * Leerzustand rendert nichts.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PeripheryStrip } from "./PeripheryStrip";
import type { WatcherDigest } from "./watcherDigest";

function digestOf(overrides: Partial<WatcherDigest>): WatcherDigest {
  return {
    latest: [],
    completedToday: 0,
    attentionOpen: 0,
    blockedOpen: 0,
    lastEvent: null,
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("PeripheryStrip", () => {
  it("zeigt Zähler und letzten Stand (Kurztitel + Uhrzeit)", () => {
    const digest = digestOf({
      latest: [
        { taskId: "t_492864de", state: "completed", title: "Jarvis Mobile: APK paketieren", ts: 1700000000 },
      ],
      completedToday: 3,
      attentionOpen: 1,
      blockedOpen: 1,
      lastEvent: {
        taskId: "t_492864de",
        state: "completed",
        title: "Jarvis Mobile: APK paketieren",
        ts: 1700000000,
      },
    });
    render(<PeripheryStrip digest={digest} onOpenLog={() => undefined} />);

    const strip = screen.getByRole("button", { name: /Wächter-Zusammenfassung/ });
    expect(strip.textContent).toContain("✓ 3 · 👁 1 · ⚠ 1");
    expect(strip.textContent).toContain("zuletzt: ✓ Jarvis Mobile: APK paketieren,");
    expect(strip.textContent).toMatch(/\d{2}:\d{2}/);
  });

  it("kürzt lange Titel auf ~60 Zeichen", () => {
    const long = `Sehr langer Task-Titel ${"x".repeat(80)}`;
    const digest = digestOf({
      latest: [{ taskId: null, state: "info", title: long, ts: 1700000000 }],
      lastEvent: { taskId: null, state: "info", title: long, ts: 1700000000 },
    });
    render(<PeripheryStrip digest={digest} onOpenLog={() => undefined} />);

    const strip = screen.getByRole("button", { name: /Wächter-Zusammenfassung/ });
    expect(strip.textContent).not.toContain(long);
    expect(strip.textContent).toContain("…");
  });

  it("Tap und Enter öffnen das Log (onOpenLog)", () => {
    const onOpenLog = vi.fn();
    const digest = digestOf({
      latest: [{ taskId: null, state: "session", title: "Agenten-Session beendet: x", ts: 1700000000 }],
    });
    render(<PeripheryStrip digest={digest} onOpenLog={onOpenLog} />);

    const strip = screen.getByRole("button", { name: /Wächter-Zusammenfassung/ });
    fireEvent.click(strip);
    fireEvent.keyDown(strip, { key: "Enter" });
    expect(onOpenLog).toHaveBeenCalledTimes(2);
  });

  it("Leerzustand: ohne Events rendert der Strip nichts", () => {
    const { container } = render(
      <PeripheryStrip digest={digestOf({})} onOpenLog={() => undefined} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("zeigt offene Approvals als Badge, auch wenn kein Wächter-Event vorliegt", () => {
    render(
      <PeripheryStrip
        digest={digestOf({})}
        inboxCount={3}
        onOpenLog={() => undefined}
      />,
    );

    expect(screen.getByText(/3 offene Freigaben/)).toBeTruthy();
  });
});
