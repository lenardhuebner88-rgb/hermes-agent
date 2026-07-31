// @vitest-environment jsdom
//
// 2026-07-31 regression: the operator reported the Lanes tab as broken — "die
// Modelle lassen sich nicht auswählen, es ist nur grau, nicht anpassbar". The
// controls WERE hard-disabled, and the backend even shipped the reason
// (`locked_reason`), but nothing rendered it. A disabled control without a
// visible reason is indistinguishable from a dead one, so a correct lock and a
// bug look identical. FastControl already shows its own hint; ModelSelect must
// do the same.
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ModelSelect } from "./ModelSelect";
import type { EditorRow, LaneModelOption } from "./api";

afterEach(() => {
  cleanup();
});

const MODELS: LaneModelOption[] = [
  { id: "gpt-5.5", label: "GPT-5.5", runtime: "hermes", group: "OpenAI Codex", provider: "openai-codex" },
];

function row(over: Partial<EditorRow> = {}): EditorRow {
  return {
    profile: "reviewer",
    description: "",
    defaultLabel: "Claude Opus 5",
    defaultRuntime: "claude-cli",
    defaultProvider: null,
    defaultModel: "claude-opus-5",
    defaultFallbackProviders: [],
    worker_runtime: "claude-cli",
    provider: null,
    model: null,
    fallbackProviders: [],
    locked: false,
    lockedReason: null,
    choice: "",
    ...over,
  };
}

describe("ModelSelect lock transparency", () => {
  const reason = "Claude-CLI / claude -p excluded from this slice";

  it("renders the backend lock reason when the select is locked shut", () => {
    render(
      <ModelSelect row={row({ locked: true, lockedReason: reason })} models={MODELS} disabled onChange={() => {}} />,
    );
    expect(screen.getByText(reason)).toBeTruthy();
    expect(screen.getByTitle(reason)).toBeTruthy();
    expect(screen.getByLabelText("Modell für reviewer").hasAttribute("disabled")).toBe(true);
  });

  it("stays silent and enabled for an unlocked row", () => {
    render(<ModelSelect row={row()} models={MODELS} onChange={() => {}} />);
    expect(screen.queryByText(reason)).toBeNull();
    expect(screen.getByLabelText("Modell für reviewer").hasAttribute("disabled")).toBe(false);
  });

  it("does not claim a lock reason when the row is merely busy-disabled", () => {
    // `disabled` also covers the transient busy state. A busy control is not a
    // locked one — inventing a lock note there would be a different lie.
    render(<ModelSelect row={row()} models={MODELS} disabled onChange={() => {}} />);
    expect(screen.queryByText(reason)).toBeNull();
  });
});
