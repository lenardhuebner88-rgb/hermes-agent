import type { ToneName } from "./types";

export interface TestFoundryResultFact {
  label: string;
  value: string;
  tone: ToneName;
}

export interface TestFoundryResultSummary {
  tone: ToneName;
  label: string;
  title: string;
  detail: string;
  next: string;
  rawLabel: string;
  facts: TestFoundryResultFact[];
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function arrayCount(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

function applyFailureDetail(value: unknown): string | null {
  const result = asRecord(value);
  if (!result || result.ok !== false) return null;
  return stringValue(result.detail)
    ?? stringValue(result.reason)
    ?? stringValue(result.error)
    ?? stringValue(result.result)
    ?? "Der Apply-Schritt hat den separaten Branch nicht freigegeben.";
}

function formatCount(value: number | null): string {
  return value === null ? "n/v" : value.toLocaleString("de-DE");
}

function formatValidatedTitle(testsKept: number | null): string {
  if (testsKept === null) return "Test-Foundry-Lauf erfolgreich.";
  return `${formatCount(testsKept)} ${testsKept === 1 ? "Test wurde" : "Tests wurden"} validiert.`;
}

export function getTestFoundryResultSummary(lastRun: unknown): TestFoundryResultSummary | null {
  const run = asRecord(lastRun);
  if (!run) return null;

  const ok = run.ok === true;
  const target = stringValue(run.target) ?? "n/v";
  const testsKept = numberValue(run.tests_kept);
  const proposals = arrayCount(run.proposals);
  const mutantsRun = numberValue(run.mutants_run);
  const survivors = arrayCount(run.survivors);
  const tokens = numberValue(run.tokens);
  const model = stringValue(run.model);
  const reason = stringValue(run.reason);
  const applyBranch = stringValue(run.apply_branch);
  const applyCommit = stringValue(run.apply_commit);
  const applyFailure = applyFailureDetail(run.apply_result);
  const branchWritten = ok && Boolean(applyBranch || applyCommit);

  const facts: TestFoundryResultFact[] = [
    { label: "Target", value: target, tone: "zinc" },
    { label: "Tests", value: formatCount(testsKept), tone: ok && (testsKept ?? 0) > 0 ? "emerald" : "zinc" },
    { label: "Queue", value: formatCount(proposals), tone: proposals > 0 ? "amber" : "zinc" },
    { label: "Mutanten", value: formatCount(mutantsRun), tone: survivors > 0 ? "cyan" : "zinc" },
    { label: "Tokens", value: formatCount(tokens), tone: tokens && tokens > 0 ? "violet" : "zinc" },
  ];
  if (model) {
    facts.push({ label: "Modell", value: model, tone: "zinc" });
  }

  if (applyFailure) {
    return {
      tone: "amber",
      label: "Branch-Gate fehlgeschlagen",
      title: formatValidatedTitle(testsKept),
      detail: applyFailure,
      next: "Apply-Branch und Gate-Ausgabe prüfen; Tests nicht übernehmen, bevor das Gate grün ist.",
      rawLabel: "Technische Test-Foundry-Details",
      facts,
    };
  }

  if (branchWritten) {
    return {
      tone: "emerald",
      label: "Branch bereit",
      title: formatValidatedTitle(testsKept),
      detail: `Test-Foundry hat die Ergebnisse auf ${applyBranch ?? "dem Apply-Branch"} abgelegt${applyCommit ? ` (${applyCommit.slice(0, 10)})` : ""}.`,
      next: "Branch prüfen und erst danach in main übernehmen.",
      rawLabel: "Technische Test-Foundry-Details",
      facts,
    };
  }

  if (ok) {
    return {
      tone: "emerald",
      label: "Queue gefüllt",
      title: formatValidatedTitle(testsKept),
      detail: proposals > 0
        ? `${proposals} ${proposals === 1 ? "Queue-Karte wartet" : "Queue-Karten warten"} auf Review.`
        : "Der Lauf war erfolgreich, hat aber keine Queue-Karte in dieser Antwort gemeldet.",
      next: "Queue-Karten prüfen; Tests nur mit sichtbarem Nutzen übernehmen.",
      rawLabel: "Technische Test-Foundry-Details",
      facts,
    };
  }

  return {
    tone: reason ? "amber" : "zinc",
    label: reason ? "Nichts behalten" : "Rohstatus",
    title: reason ? "Lauf hat keinen sicheren Test behalten." : "Test-Foundry-Ergebnis liegt vor.",
    detail: reason ?? "Die Antwort enthält keinen klaren Erfolgshinweis. Technische Details prüfen.",
    next: reason ? "Target, betroffene Tests oder Baseline prüfen und danach kleiner neu starten." : "Details öffnen, bevor ein weiterer Lauf gestartet wird.",
    rawLabel: "Technische Test-Foundry-Details",
    facts,
  };
}
