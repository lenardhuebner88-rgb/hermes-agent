import { describe, expect, it } from "vitest";

import {
  normalizeDesktopLayout,
  resolvePaneTargets,
  targetKey,
  type TerminalTarget,
} from "./layout";

const windows: TerminalTarget[] = [
  { session: "work", window: "hermes" },
  { session: "work", window: "claude" },
  { session: "work", window: "codex" },
  { session: "work", window: "kimi" },
];

describe("terminal multiview layout", () => {
  it("accepts only 1, 2, and 4 pane layouts", () => {
    expect(normalizeDesktopLayout(1)).toBe(1);
    expect(normalizeDesktopLayout("2")).toBe(2);
    expect(normalizeDesktopLayout(4)).toBe(4);
    expect(normalizeDesktopLayout(3)).toBe(1);
    expect(normalizeDesktopLayout(null)).toBe(1);
  });

  it("preserves unique live targets in stable pane order and fills gaps", () => {
    const prior: Array<TerminalTarget | null> = [windows[0], windows[2], null, windows[1]];
    expect(resolvePaneTargets(windows, prior, 4)).toEqual([windows[0], windows[2], windows[3], windows[1]]);
  });

  it("deduplicates targets so one tmux window cannot be attached twice", () => {
    const prior = [windows[0], windows[0], windows[1], windows[1]];
    const result = resolvePaneTargets(windows, prior, 4);
    expect(result.filter(Boolean).map((item) => targetKey(item!))).toEqual([
      "work:hermes",
      "work:codex",
      "work:claude",
      "work:kimi",
    ]);
  });

  it("keeps hidden pane assignments when the visible count shrinks", () => {
    const prior = [...windows];
    expect(resolvePaneTargets(windows, prior, 2)).toEqual(prior);
    expect(resolvePaneTargets(windows, prior, 1)).toEqual(prior);
  });
});
