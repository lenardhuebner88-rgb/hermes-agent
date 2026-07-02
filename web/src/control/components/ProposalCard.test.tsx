import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ProposalCard } from "./ProposalCard";
import { de } from "../i18n/de";
import { getProposalOperatorBrief } from "../lib/autoresearchProposalBrief";
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

  it("renders a stable anchor for review-flow focus", () => {
    const html = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "focus-1", target: "skill/foo" })} density="airy" onApply={noop} onSkip={noop} />,
    );
    expect(html).toContain('id="autoresearch-proposal-focus-1"');
  });

  it("shows manual-review cards as not batch-selectable", () => {
    const safe = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "p6-safe", target: "skill/foo" })} density="airy" selectable batchSelectable onApply={noop} onSkip={noop} />,
    );
    expect(safe).toContain('type="checkbox"');
    expect(safe).not.toContain("Einzelreview");

    const manual = renderToStaticMarkup(
      <ProposalCard proposal={proposal({ id: "p6-manual", target: "hermes_cli/foo.py", mode: "code" })} density="airy" selectable batchSelectable={false} onApply={noop} onSkip={noop} />,
    );
    expect(manual).not.toContain(`aria-label="${de.autoresearch.selectProposal}"`);
    expect(manual).toContain("Einzelreview");
  });

  it("opens the diff by default for actionable manual-review cards", () => {
    const manual = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({ id: "manual-diff", target: "hermes_cli/foo.py", mode: "code", diff_before_after: "- risky()\n+ guarded()" })}
        density="airy"
        selectable
        batchSelectable={false}
        onApply={noop}
        onSkip={noop}
      />,
    );

    expect(manual).toContain("Diese Änderung ist geöffnet");
    expect(manual).not.toContain("hidden md:block");
    expect(manual).toContain("guarded()");
  });

  it("requires explicit confirmation before applying actionable manual-review cards", () => {
    const manual = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({ id: "manual-confirm", target: "hermes_cli/foo.py", mode: "code", diff_before_after: "- risky()\n+ guarded()" })}
        density="airy"
        selectable
        batchSelectable={false}
        onApply={noop}
        onSkip={noop}
      />,
    );

    expect(manual).toContain("Diff geprüft");
    expect(manual).toContain("Erst Diff geprüft bestätigen.");
    expect(manual).toContain(de.autoresearch.batchManualReviewHint);
    expect(manual).toContain("disabled");
    expect(manual).toContain(de.autoresearch.skip);
  });

  it("keeps batch-selectable card diffs collapsed by default", () => {
    const safe = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({ id: "safe-diff", target: "skill/foo", diff_before_after: "- alt\n+ neu" })}
        density="airy"
        selectable
        batchSelectable
        onApply={noop}
        onSkip={noop}
      />,
    );

    expect(safe).not.toContain("Diese Änderung ist geöffnet");
    expect(safe).not.toContain("Diff geprüft");
    expect(safe).toContain("hidden md:block");
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

  it("shows click outcomes before the operator has to read diff details", () => {
    const html = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({
          id: "outcome-1",
          target: "hermes_cli/foo.py",
          mode: "code",
          diff_before_after: "- risky()\n+ guarded()",
        })}
        density="airy"
        onApply={noop}
        onSkip={noop}
      />,
    );

    expect(html).toContain("Was passiert beim Klick?");
    expect(html).toContain("Schreibt Code und startet danach das Gate.");
    expect(html).toContain("Keine Datei wird geändert; die Karte ist erledigt.");
    expect(html).toContain("Roter Lauf wird automatisch zurückgerollt.");
    expect(html.indexOf("Was passiert beim Klick?")).toBeLessThan(html.indexOf("Kurzbriefing"));
    expect(html.indexOf("Was passiert beim Klick?")).toBeLessThan(html.indexOf(de.autoresearch.fixDiff));
  });

  it("adapts click outcomes for reverted and completed cards", () => {
    const revertedHtml = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({
          id: "outcome-reverted",
          target: "skill/foo",
          last_outcome: "reverted_no_improvement",
        })}
        density="airy"
        onApply={noop}
        onSkip={noop}
      />,
    );
    expect(revertedHtml).toContain("Archivieren");
    expect(revertedHtml).toContain("Räumt die Karte aus den offenen Entscheidungen.");
    expect(revertedHtml).toContain("Startet genau diesen Kandidaten bewusst neu.");

    const doneHtml = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({
          id: "outcome-done",
          target: "skill/foo",
          status: "applied",
          result: "Gate passed.",
        })}
        density="airy"
        onApply={noop}
        onSkip={noop}
      />,
    );
    expect(doneHtml).toContain("Schon übernommen.");
    expect(doneHtml).toContain("Keine Entscheidung offen.");
    expect(doneHtml).toContain("Gate passed.");
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

  it("renders a compact operator brief before the detailed decision sections", () => {
    const html = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({
          id: "brief-1",
          target: "hermes_cli/web_server.py",
          mode: "code",
          title: "Deep-Audit in hermes_cli/web_server.py:6420: subprocess.Popen with shell-built command",
          category: "bug_risk",
          severity: "high",
        })}
        density="airy"
        onApply={noop}
        onSkip={noop}
      />,
    );

    expect(html).toContain("Kurzbriefing");
    expect(html).toContain("Code mit Gate");
    expect(html).toContain("Betroffen");
    expect(html).toContain("Code: hermes_cli/web_server.py");
    expect(html).toContain("Übernehmen startet Code-Änderung plus Test-Gate.");
  });

  it("keeps skill proposal briefs plain and non-code-specific", () => {
    const brief = getProposalOperatorBrief(proposal({
      id: "brief-2",
      target: "skills/family/SKILL.md",
      section: "examples",
      category: "missing_section",
      severity: "low",
    }));

    expect(brief.label).toBe("Skill-Polish");
    expect(brief.title).toContain("ohne Code-Lauf");
    expect(brief.facts.find((fact) => fact.label === "Betroffen")?.value).toBe("Skill: skills/family/SKILL.md · examples");
    expect(brief.facts.find((fact) => fact.label === "Klick")?.value).toContain("Skill-Text");
  });

  it("keeps completed proposal briefs status-focused instead of action-focused", () => {
    const brief = getProposalOperatorBrief(proposal({
      id: "brief-done",
      target: "hermes_cli/foo.py",
      mode: "code",
      status: "applied",
      result: "Gate passed and patch applied.",
    }));

    expect(brief.label).toBe("Erledigt");
    expect(brief.summary).toContain("Gate passed");
    expect(brief.facts.find((fact) => fact.label === "Stand")?.value).toBe("Keine Aktion offen.");
    expect(brief.facts.some((fact) => fact.label === "Klick")).toBe(false);
  });

  it("keeps testing cards focused on the running gate instead of unavailable actions", () => {
    const html = renderToStaticMarkup(
      <ProposalCard
        proposal={proposal({
          id: "brief-testing",
          target: "hermes_cli/foo.py",
          mode: "code",
          status: "testing",
          result: "Gate is still running.",
        })}
        density="airy"
        onApply={noop}
        onSkip={noop}
      />,
    );

    expect(html).toContain("Gate läuft");
    expect(html).toContain("Auf Gate-Ergebnis warten.");
    expect(html).not.toContain("Entscheidungshilfe");
    expect(html).not.toContain("Übernehmen schreibt");
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
