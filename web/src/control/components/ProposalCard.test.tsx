// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProposalCard } from "./ProposalCard";
import { de } from "../i18n/de";
import { getProposalOperatorBrief } from "../lib/autoresearchProposalBrief";
import { formatProposalCategory } from "../lib/autoresearchProposalLabels";
import type { Proposal } from "../lib/types";

function proposal(overrides: Partial<Proposal> & Pick<Proposal, "id" | "target">): Proposal {
  return {
    section: null,
    title: null,
    rationale_plain: "weil es die Arbeitsweise verlässlicher macht",
    diff_before_after: " Kontext, der nicht geändert wurde\n- alte Zeile\n+ neue Zeile",
    mode: "skill",
    status: "proposed",
    ...overrides,
  };
}

const noop = () => {};

afterEach(cleanup);

describe("ProposalCard decision surface", () => {
  it("shows only the four plain-language fields and decision buttons by default", () => {
    const realProposal = proposal({
      id: "decision-1",
      target: "web/src/control/views/AutoresearchView.tsx",
      section: "focusProposal",
      title: "Fix focusProposal in web/src/control/views/AutoresearchView.tsx",
      mode: "code",
      category: "bug_risk",
      severity: "high",
      evidence: "SHA deadbeef and raw path web/src/control/views/AutoresearchView.tsx",
      expected_benefit: "The CTA scrolls after render.",
      risk_summary: "May affect document.getElementById.",
      test_plan: "scripts/run-affected.sh web/src/control/views/AutoresearchView.test.tsx",
      recommendation: "Apply after reviewing the raw diff.",
      diff_before_after: " context from a foreign skill\n- document.getElementById(id)\n+ setActiveFocusId(id)",
      target_sha256: "deadbeef",
    });

    const html = renderToStaticMarkup(<ProposalCard proposal={realProposal} density="airy" onApply={noop} onSkip={noop} />);

    for (const label of [
      de.autoresearch.decisionWhat,
      de.autoresearch.decisionBenefit,
      de.autoresearch.decisionRecommendation,
      de.autoresearch.decisionEffortRisk,
      de.autoresearch.accept,
      de.autoresearch.reject,
      de.autoresearch.technicalExpand,
    ]) expect(html).toContain(label);

    for (const technicalText of [
      realProposal.target,
      realProposal.title!,
      realProposal.test_plan!,
      "document.getElementById",
      "deadbeef",
      "raw path",
    ]) expect(html).not.toContain(technicalText);
  });

  it("reveals paths, evidence and only changed diff lines after the technical disclosure opens", async () => {
    const technical = proposal({
      id: "technical-1",
      target: "hermes_cli/web_server.py",
      mode: "code",
      category: "bug_risk",
      evidence: "Beleg aus Zeile 42",
      diff_before_after: " unrelated skill markdown\n- risky()\n+ guarded()",
    });
    render(<ProposalCard proposal={technical} density="airy" onApply={noop} onSkip={noop} />);

    expect(screen.queryByText("hermes_cli/web_server.py")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: de.autoresearch.technicalExpand }));

    await waitFor(() => expect(screen.getByText(/Ziel: hermes_cli\/web_server.py/)).toBeTruthy());
    expect(screen.getByText("Beleg aus Zeile 42")).toBeTruthy();
    expect(screen.getByText("risky()")).toBeTruthy();
    expect(screen.getByText("guarded()")).toBeTruthy();
    expect(screen.queryByText("unrelated skill markdown")).toBeNull();
  });

  it("lets the operator accept or reject a manual code proposal without opening the diff", () => {
    const onApply = vi.fn();
    const onSkip = vi.fn();
    const code = proposal({ id: "manual-code", target: "hermes_cli/foo.py", mode: "code", severity: "high" });
    render(<ProposalCard proposal={code} density="airy" selectable batchSelectable={false} onApply={onApply} onSkip={onSkip} />);

    const accept = screen.getByRole("button", { name: de.autoresearch.accept });
    const reject = screen.getByRole("button", { name: de.autoresearch.reject });
    expect(accept.hasAttribute("disabled")).toBe(false);
    expect(reject.hasAttribute("disabled")).toBe(false);
    fireEvent.click(accept);
    fireEvent.click(reject);
    expect(onApply).toHaveBeenCalledWith(code);
    expect(onSkip).toHaveBeenCalledWith(code);
  });

  it("keeps batch selection available inside the technical disclosure", async () => {
    render(<ProposalCard proposal={proposal({ id: "batch-1", target: "skills/foo/SKILL.md" })} density="airy" selectable batchSelectable onApply={noop} onSkip={noop} />);
    expect(screen.queryByRole("checkbox", { name: de.autoresearch.selectProposal })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: de.autoresearch.technicalExpand }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: de.autoresearch.selectProposal })).toBeTruthy());
  });

  it("renders a stable anchor for focus after the enclosing group opens", () => {
    const html = renderToStaticMarkup(<ProposalCard proposal={proposal({ id: "focus-1", target: "skill/foo" })} density="airy" onApply={noop} onSkip={noop} />);
    expect(html).toContain('id="autoresearch-proposal-focus-1"');
  });

  it("uses translated category labels without leaking backend keys into the default fields", () => {
    expect(formatProposalCategory("info_leak")?.label).toBe("Geheimnis sichtbar");
    const html = renderToStaticMarkup(<ProposalCard proposal={proposal({ id: "category-1", target: "skill/foo", category: "info_leak" })} density="airy" onApply={noop} onSkip={noop} />);
    expect(html).toContain("Geheimnis sichtbar");
    expect(html).not.toContain("info_leak");
  });

  it("keeps the operator brief status-focused for completed proposals", () => {
    const brief = getProposalOperatorBrief(proposal({
      id: "brief-done",
      target: "hermes_cli/foo.py",
      mode: "code",
      status: "applied",
      result: "Gate passed and patch applied.",
    }));
    expect(brief.label).toBe("Erledigt");
    expect(brief.summary).toContain("Gate passed");
    expect(brief.facts.some((fact) => fact.label === "Klick")).toBe(false);
  });
});
