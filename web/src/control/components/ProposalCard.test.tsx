import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ProposalCard } from "./ProposalCard";
import { de } from "../i18n/de";
import { formatProposalCategory } from "../lib/autoresearchProposalLabels";
import type { Proposal } from "../lib/types";

function proposal(overrides: Partial<Proposal> & Pick<Proposal, "id" | "target">): Proposal {
  return {
    section: null,
    title: null,
    rationale_plain: "weil es besser ist",
    diff_before_after: "- alt\n+ neu",
    mode: "skill",
    status: "proposed",
    ...overrides,
  };
}

const noop = () => {};

describe("ProposalCard", () => {
  it("renders the category badge with the German label when a category is present", () => {
    const html = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "p1", target: "skill/foo", category: "Sicherheit" })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(html).toContain(`${de.autoresearch.category}: Sicherheit`);
  });

  it("turns backend category keys into plain operator labels", () => {
    const html = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "p1b", target: "skill/foo", category: "info_leak" })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(html).toContain(`${de.autoresearch.category}: Geheimnis sichtbar`);
    expect(html).toContain("Token, Zugangsdaten oder interne Details");
    expect(html).not.toContain(`${de.autoresearch.category}: info_leak`);
  });

  it("formats unknown category keys while preserving the backend key for correlation", () => {
    expect(formatProposalCategory("custom_backend_signal")?.label).toBe("Custom Backend Signal");
    const html = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "p1c", target: "skill/foo", category: "custom_backend_signal" })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(html).toContain(`${de.autoresearch.category}: Custom Backend Signal`);
    expect(html).toContain("Backend-Kategorie: custom_backend_signal");
  });

  it("keeps missing and unclear trigger explanations distinct", () => {
    expect(formatProposalCategory("missing_trigger")?.help).toContain("überhaupt starten");
    expect(formatProposalCategory("unclear_trigger")?.help).toContain("nicht eindeutig genug");
  });

  it("omits the category badge when the category is empty or whitespace", () => {
    const html = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "p2", target: "skill/foo", category: "   " })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(html).not.toContain(`${de.autoresearch.category}:`);
  });

  it("renders the verbatim evidence block when evidence is present", () => {
    const html = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "p3", target: "skill/foo", evidence: "Zeile 42: token im Log geleakt" })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(html).toContain(de.autoresearch.evidence);
    expect(html).toContain("Zeile 42: token im Log geleakt");
    expect(html).toContain("<blockquote");
  });

  it("omits the evidence block when evidence is null or blank", () => {
    const htmlNull = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "p4", target: "skill/foo", evidence: null })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(htmlNull).not.toContain("<blockquote");

    const htmlBlank = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "p4b", target: "skill/foo", evidence: "  " })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(htmlBlank).not.toContain("<blockquote");
  });

  it("renders the clearly labelled fix-diff section with the before/after content", () => {
    const html = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({ id: "p5", target: "agent/bar.py", mode: "code", diff_before_after: "- x = 1\n+ x = 2" })}
        density="airy"
        onApply={noop}
        onSkip={noop}
      />,
    );
    expect(html).toContain(de.autoresearch.fixDiff);
    expect(html).toContain("x = 1");
    expect(html).toContain("x = 2");
  });

  it("renders the selection checkbox only when selectable", () => {
    const selectable = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "p6", target: "skill/foo" })} density="airy" selectable onApply={noop} onSkip={noop} />,
    );
    expect(selectable).toContain('type="checkbox"');

    const plain = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "p7", target: "skill/foo" })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(plain).not.toContain('type="checkbox"');
  });

  it("renders the model-assigned severity badge", () => {
    const html = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "ps1", target: "skill/foo", severity: "critical" })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(html).toContain(`${de.autoresearch.severity}: ${de.autoresearch.severityCritical}`);
  });

  it("falls back to the category-derived severity badge when severity is absent", () => {
    const html = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "ps2", target: "skill/foo", category: "missing_section" })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(html).toContain(`${de.autoresearch.severity}: ${de.autoresearch.severityLow}`);
  });

  it("renders all three Track-C elements together (badge + evidence + fix-diff)", () => {
    const html = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({
          id: "p8",
          target: "agent/baz.py",
          mode: "code",
          category: "Sicherheit",
          evidence: "Beleg: Secret im Klartext",
          diff_before_after: "- secret = leak()\n+ secret = vault()",
        })}
        density="airy"
        onApply={noop}
        onSkip={noop}
      />,
    );
    expect(html).toContain(`${de.autoresearch.category}: Sicherheit`);
    expect(html).toContain("Beleg: Secret im Klartext");
    expect(html).toContain(de.autoresearch.fixDiff);
    expect(html).toContain("secret = vault()");
  });

  it("labels mutation-test proposals as test hardening", () => {
    const html = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({
          id: "tf1",
          target: "hermes_cli/kanban_db.py",
          mode: "test",
          proposal_type: "mutation_test",
          category: "mutation_survivor",
        })}
        density="airy"
        onApply={noop}
        onSkip={noop}
      />,
    );
    expect(html).toContain("Test-Härtung");
  });

  it("explains the apply/skip consequence in plain language", () => {
    const skillHtml = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "plain-1", target: "skill/foo" })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(skillHtml).toContain("Entscheidung:");
    expect(skillHtml).toContain("schreibt den Skill-Vorschlag direkt");

    const codeHtml = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "plain-2", target: "hermes_cli/foo.py", mode: "code" })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(codeHtml).toContain("startet direkt die Test-Suite");
    expect(codeHtml).toContain("automatisch zurückgerollt");
  });

  it("renders a structured decision guide for non-technical review", () => {
    const html = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({ id: "guide-1", target: "hermes_cli/foo.py", mode: "code", severity: "high" })}
        density="airy"
        onApply={noop}
        onSkip={noop}
      />,
    );

    expect(html).toContain("Entscheidungshilfe");
    expect(html).toContain("Nutzen");
    expect(html).toContain("Risiko");
    expect(html).toContain("Empfohlen");
    expect(html).toContain("Einzeln prüfen");
    expect(html).toContain("Test-Suite");
  });

  it("shows reverted proposals as archive-first decisions", () => {
    const html = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({ id: "guide-2", target: "skill/foo", last_outcome: "reverted_no_improvement" })}
        density="airy"
        onApply={noop}
        onSkip={noop}
      />,
    );

    expect(html).toContain("Archivieren empfohlen");
    expect(html).toContain("automatisch ohne Verbesserung zurückgerollt");
  });
});
