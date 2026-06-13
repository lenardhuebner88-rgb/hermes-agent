export type CheckStatus = "pass" | "fail" | "na";

export interface CheckResult {
  id: string;
  label: string;
  status: CheckStatus;
  rationale: string;
}

export interface HeuristicResult {
  score: number;
  max: number;
  checks: CheckResult[];
}

interface Detector {
  id: string;
  label: string;
  appliesTo: string[];
  rationale: string;
  test: (p: string) => boolean;
}

// ids + labels MUST stay in sync with the catalog `heuristic[]` rows (the
// catalog provides documentation; the predicates below cannot live in JSON).
const DETECTORS: Detector[] = [
  { id: "done-when", label: "Hat Done-When", appliesTo: ["*"], rationale: "größter Einzel-Hebel", test: (p) => /done[- ]when|completion condition|finished when|done:/i.test(p) },
  { id: "stop-condition", label: "Hat Stop-Bedingung", appliesTo: ["*"], rationale: "verhindert Drift/stilles Falsch-Handeln", test: (p) => /\bstop\b|\bhalt\b|ask before|wait for (explicit )?confirmation|stop after/i.test(p) },
  { id: "scope-limited", label: "Scope begrenzt (Datei/Verzeichnis)", appliesTo: ["*"], rationale: "verhindert Scope-Creep", test: (p) => /scope:|only (make|touch|modify|change)|do not touch|outside|within .*(dir|directory|src)|files? (in|recently)/i.test(p) },
  { id: "plan-first", label: "Plan-First vor Code", appliesTo: ["feature", "bugfix"], rationale: "Reasoning-first hebt Patch-Qualität", test: (p) => /reason first|before (writing )?code|propose a (written )?plan|wait for approval|\(no code\)/i.test(p) },
  { id: "output-format", label: "Output-Format spezifiziert", appliesTo: ["*"], rationale: "maschinen-verarbeitbar", test: (p) => /output:|output format|numbered list|\bjson\b|\byaml\b|\bxml\b|frontmatter|format:/i.test(p) },
  { id: "read-only", label: "Read-Only-Pledge", appliesTo: ["audit"], rationale: "sonst wird Audit zum ungewollten Fix", test: (p) => /do not modify|read[- ]only|don'?t (change|edit|modify)|no file changes/i.test(p) },
  { id: "behavior-preservation", label: "Behavior-Preservation-Pledge", appliesTo: ["refactor"], rationale: "sonst stilles Verhaltens-Drift", test: (p) => /without changing behavior|behavior[- ]preserv|characterization test|same observable behavior/i.test(p) },
  { id: "regression-test", label: "Regression-Test verlangt", appliesTo: ["bugfix"], rationale: "sonst kehrt der Bug zurück", test: (p) => /regression test|add a .{0,15}test|previously failing test/i.test(p) },
  { id: "clarification-gate", label: "Clarification-Gate", appliesTo: ["feature", "research"], rationale: "gegen stille Fehlinterpretation", test: (p) => /clarifying question|ask .{0,20}question|if .{0,30}ambiguous|surface it|surface the/i.test(p) },
  { id: "severity-label", label: "Severity-Label", appliesTo: ["audit"], rationale: "sonst unpriorisierte Findings", test: (p) => /severity|\[critical|critical\s*\|\s*high|priorit/i.test(p) },
];

export function score(promptText: string, taskTypeId: string): HeuristicResult {
  const checks: CheckResult[] = DETECTORS.map((d) => {
    const applies = d.appliesTo.includes("*") || d.appliesTo.includes(taskTypeId);
    if (!applies) return { id: d.id, label: d.label, status: "na" as const, rationale: d.rationale };
    return { id: d.id, label: d.label, status: d.test(promptText) ? "pass" : ("fail" as const), rationale: d.rationale };
  });
  const passed = checks.filter((c) => c.status !== "fail").length;
  return { score: passed, max: DETECTORS.length, checks };
}
