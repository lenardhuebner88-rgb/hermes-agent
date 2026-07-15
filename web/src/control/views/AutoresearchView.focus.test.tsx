// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const hooks = vi.hoisted(() => ({
  useAutoresearchStatus: vi.fn(() => ({ data: null, loading: false, error: null })),
  useAutoresearchRuns: vi.fn(() => ({ data: null, loading: false, error: null })),
  useDeepAudit: vi.fn(() => ({ busy: false, trigger: vi.fn(), subsystems: [] })),
  useTestFoundry: vi.fn(() => ({ busy: false, trigger: vi.fn(), targets: [] })),
}));

vi.mock("../hooks/useControlData", () => hooks);
vi.mock("./autoresearch/ProposalQueue", () => ({
  ProposalQueue: ({ focusId, onClearFocus }: { focusId: string | null; onClearFocus: () => void }) => (
    <section aria-label="Entscheidungs-Inbox">
      <output data-testid="focused-proposal">{focusId ?? "keine Entscheidung"}</output>
      <button type="button" onClick={onClearFocus}>Weiter</button>
    </section>
  ),
}));
vi.mock("./autoresearch/LoopControls", () => ({ LoopControls: () => null }));
vi.mock("./autoresearch/AdvancedSection", () => ({ AdvancedSection: () => null }));
vi.mock("./autoresearch/ResolvedQueues", () => ({ ResolvedQueues: () => null }));
vi.mock("./autoresearch/RunsList", () => ({ RunsList: () => null }));
vi.mock("./autoresearch/OutcomePanel", () => ({ OutcomePanel: () => null }));
vi.mock("./autoresearch/panels", () => ({ ActivityTimelineItem: () => null, LatestActivityPanel: () => null, DeepAuditFindings: () => null }));

import { AutoresearchView } from "./AutoresearchView";

afterEach(cleanup);

const proposal = {
  id: "proposal-a",
  target: "web/src/control/views/AutoresearchView.tsx",
  section: "focusProposal",
  title: "Proposal A",
  category: "bug_risk",
  severity: "high",
  evidence: "A focused decision must survive browser history.",
  rationale_plain: "Browser Back should replay the deep link.",
  diff_before_after: "- replace\n+ push",
  mode: "code",
  status: "proposed",
  finding_state: "verified",
  decision_state: "needs_operator",
  delivery_state: "none",
  operator_action_required: true,
  expected_benefit: "The request can be replayed.",
  risk_summary: "The focused decision could be lost.",
  test_plan: "Navigate back after consuming the focus.",
  recommendation: "Use browser history.",
};

const store = {
  proposals: [proposal],
  data: null,
  loading: false,
  error: null,
  busy: null,
  openSkillProposals: [],
  activity: [],
  apply: vi.fn(),
  skip: vi.fn(),
  generate: vi.fn(),
  confirmBatch: vi.fn(),
  skipBatch: vi.fn(),
  reload: vi.fn(),
};

describe("AutoresearchView deep-link history", () => {
  it("replays a consumed focus query when browser Back restores the route", async () => {
    window.history.replaceState({}, "", "/control/autoresearch?focus=proposal-a");
    render(<BrowserRouter><AutoresearchView density="airy" store={store as never} /></BrowserRouter>);

    expect(screen.getByTestId("focused-proposal").textContent).toBe("proposal-a");
    fireEvent.click(screen.getByRole("button", { name: "Weiter" }));

    await waitFor(() => expect(window.location.search).toBe(""));
    expect(screen.getByTestId("focused-proposal").textContent).toBe("keine Entscheidung");

    await act(async () => { window.history.back(); });
    await waitFor(() => {
      expect(window.location.search).toBe("?focus=proposal-a");
      expect(screen.getByTestId("focused-proposal").textContent).toBe("proposal-a");
    });
  });

  it("keeps Browser Back free after consuming queryless keyboard focus", async () => {
    window.history.replaceState({}, "", "/control/previous");
    window.history.pushState({}, "", "/control/autoresearch");
    render(<BrowserRouter><AutoresearchView density="airy" store={store as never} /></BrowserRouter>);

    fireEvent.keyDown(document.body, { key: "t" });
    await waitFor(() => expect(screen.getByTestId("focused-proposal").textContent).toBe("proposal-a"));
    fireEvent.click(screen.getByRole("button", { name: "Weiter" }));

    await waitFor(() => expect(screen.getByTestId("focused-proposal").textContent).toBe("keine Entscheidung"));
    await act(async () => { window.history.back(); });
    await waitFor(() => expect(window.location.pathname).toBe("/control/previous"));
  });

  it("keeps Browser Back free after consuming keyboard focus over a stale query", async () => {
    window.history.replaceState({}, "", "/control/previous");
    window.history.pushState({}, "", "/control/autoresearch?focus=removed-proposal");
    render(<BrowserRouter><AutoresearchView density="airy" store={store as never} /></BrowserRouter>);

    fireEvent.keyDown(document.body, { key: "t" });
    await waitFor(() => expect(screen.getByTestId("focused-proposal").textContent).toBe("proposal-a"));
    fireEvent.click(screen.getByRole("button", { name: "Weiter" }));

    await waitFor(() => expect(screen.getByTestId("focused-proposal").textContent).toBe("keine Entscheidung"));
    await act(async () => { window.history.back(); });
    await waitFor(() => expect(window.location.pathname).toBe("/control/previous"));
  });
});
