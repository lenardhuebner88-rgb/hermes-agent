// @vitest-environment jsdom
//
// W3 regression: claude-cli reasoning is steered via `claude_effort`/`--effort`,
// NOT this control (which persists agent.reasoning_effort — a no-op for
// claude-cli). So an empty support set must render the HONEST empty state — and
// when the backend supplies a hint (the claude_effort pointer) that hint shows
// instead of the generic "no Reasoning-Knopf" text or, worse, greyed segments
// that imply a disabled hermes-style control.
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ReasoningControl } from "./ReasoningControl";

afterEach(() => {
  cleanup();
});

describe("ReasoningControl honest empty-support state", () => {
  it("shows the generic no-Knopf text when support is empty and no hint is given", () => {
    render(<ReasoningControl value={null} support={[]} ariaLabel="x" onChange={() => {}} />);
    expect(screen.getByText("Modell hat keinen Reasoning-Knopf")).toBeTruthy();
  });

  it("shows an explicit hint (claude-cli claude_effort pointer) for empty support", () => {
    const hint = "claude -p: Reasoning via Profil-Config „claude_effort“ (--effort) — hier nicht schaltbar";
    render(
      <ReasoningControl value={null} support={[]} hint={hint} ariaLabel="x" onChange={() => {}} />,
    );
    expect(screen.getByText(hint)).toBeTruthy();
    expect(screen.queryByText("Modell hat keinen Reasoning-Knopf")).toBeNull();
  });

  it("renders the active segments (not the empty-state hint) when support is non-empty", () => {
    render(
      <ReasoningControl
        value={null}
        support={["low", "medium", "high"]}
        hint="must not leak into the active control"
        ariaLabel="x"
        onChange={() => {}}
      />,
    );
    expect(screen.queryByText(/keinen Reasoning-Knopf/)).toBeNull();
    expect(screen.queryByText("must not leak into the active control")).toBeNull();
    expect(screen.getByTitle("high")).toBeTruthy();
  });
});
