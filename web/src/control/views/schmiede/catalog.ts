export type BlockCategory = "core" | "long-run" | "optional";
export type WrapMode = "completion-condition" | "interval-loop" | "full-auto" | "system-prompt";

export interface Block {
  id: string;
  letter: string;
  label: string;
  description: string;
  body: string;
  source: string;
  category: BlockCategory;
}

export interface TaskType {
  id: string;
  label: string;
  blockIds: string[];
  typeBody: string;
  defaultDoneWhen: string;
  checklist: string[];
  rawTemplate: string;
  source: string;
}

export interface ModeOverrides {
  reversibilityGate?: string;
  escalation?: string;
  persistence?: string;
}

export interface Mode {
  id: string;
  label: string;
  description: string;
  overrides: ModeOverrides;
  rawPreset: string;
  source: string;
}

export interface Target {
  id: string;
  label: string;
  mechanicNote: string;
  wrapMode: WrapMode;
  source: string;
}

export interface HeuristicCheck {
  id: string;
  label: string;
  appliesTo: string[];
  weight: number;
  rationale: string;
}

export interface EvalEvidence {
  name: string;
  measures: string;
  keyNumber: string;
  lesson: string;
  source: string;
}

export interface PromptForgeCatalog {
  version: number;
  blocks: Block[];
  taskTypes: TaskType[];
  modes: Mode[];
  targets: Target[];
  heuristic: HeuristicCheck[];
  evalEvidence: EvalEvidence[];
}

/** The Konfigurator's current selection — drives compose(). */
export interface ForgeSelection {
  targetId: string;
  taskTypeId: string;
  modeId: string;
  modelId: string;
  slots: {
    task: string;
    scope: string;
    /** /loop only: suggested interval in minutes (undefined = self-paced). */
    intervalMinutes?: number;
    /** completion-condition / interval-loop: max turns/rounds before stop. */
    maxTurns?: number;
  };
}
