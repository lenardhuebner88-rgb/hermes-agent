import type { ForgeSelection, PromptForgeCatalog, Target, TaskType } from "./catalog";

function blockBody(catalog: PromptForgeCatalog, id: string): string {
  return catalog.blocks.find((b) => b.id === id)?.body ?? "";
}

/** Deterministic best-practice assembly (Spec §5), then target-adapter wrap. */
export function compose(selection: ForgeSelection, catalog: PromptForgeCatalog): string {
  const taskType = catalog.taskTypes.find((t) => t.id === selection.taskTypeId);
  const mode = catalog.modes.find((m) => m.id === selection.modeId);
  const target = catalog.targets.find((t) => t.id === selection.targetId);
  if (!taskType || !mode || !target) return "";

  const task = selection.slots.task.trim() || "[describe the task: file + symptom + outcome]";
  const scope = selection.slots.scope.trim() || "[scope: file / directory boundary]";

  // Blocks the catalog flags for this task type but that aren't slots/mode-driven
  // are woven in at their canonical position (grounding early, output-format last).
  const inBlocks = (id: string) => taskType.blockIds.includes(id);

  const parts: string[] = [];
  parts.push(blockBody(catalog, "role")); // A
  parts.push(`Goal: ${task}`); // B (slot)
  parts.push(`Scope: ${scope}`); // G (slot)
  if (inBlocks("grounding")) parts.push(blockBody(catalog, "grounding")); // C
  parts.push(taskType.typeBody); // type-specific core
  parts.push(mode.overrides.persistence ?? blockBody(catalog, "persistence")); // E (mode wins)
  parts.push(blockBody(catalog, "verification")); // I
  parts.push(`Done-when: ${taskType.defaultDoneWhen}`); // F
  if (mode.overrides.reversibilityGate) parts.push(mode.overrides.reversibilityGate); // H
  if (mode.overrides.escalation) parts.push(mode.overrides.escalation); // J
  if (inBlocks("output-format")) parts.push(blockBody(catalog, "output-format")); // L

  const core = parts.filter((p) => p && p.trim()).join("\n\n");
  return wrapForTarget(core, target, selection, taskType);
}

function wrapForTarget(core: string, target: Target, selection: ForgeSelection, taskType: TaskType): string {
  const modelHint = selection.modelId
    ? `# Model: ${selection.modelId} (set via your CLI's model flag)`
    : "";
  // Prepend the model hint only when set; keep the inner "" entries — they are
  // intentional blank-line spacers in the wrap arrays below (a blanket
  // filter(Boolean) would glue the directive line straight onto the body).
  const head = (lines: string[]) => (modelHint ? [modelHint, ...lines] : lines).join("\n");

  switch (target.wrapMode) {
    case "completion-condition": {
      const maxTurns = selection.slots.maxTurns ?? 20;
      return head([
        `/goal Completion condition (provable from the transcript): ${taskType.defaultDoneWhen} — or stop after ${maxTurns} turns.`,
        `Note: the evaluator sees only your transcript output, not the filesystem. Explicitly print the proof (test exit code, \`git status\`) in your messages.`,
        "",
        core,
      ]);
    }
    case "interval-loop": {
      const cadence = selection.slots.intervalMinutes
        ? `/loop ${selection.slots.intervalMinutes}m`
        : "/loop (self-paced)";
      const rounds = selection.slots.maxTurns ?? 5;
      return head([
        cadence,
        "",
        core,
        "",
        `Each round: state [DONE] or [CONTINUE: <reason>]. Stop after ${rounds} rounds or when [DONE]. Never proceed if a round made no measurable progress.`,
      ]);
    }
    case "full-auto": {
      return head([
        `# codex /goal — --approval-mode full-auto. AGENTS.md is your operating manual.`,
        "",
        core,
        "",
        `Bias to action: deliver working code. Deny unconditionally: force-push over history, mass-delete, writing credentials to unrelated files, exfiltration.`,
      ]);
    }
    case "system-prompt":
    default:
      return head(["<system_prompt>", core, "</system_prompt>"]);
  }
}
