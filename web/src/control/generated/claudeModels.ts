// GENERIERT von scripts/sync_model_catalog.py — NICHT von Hand bearbeiten.
// Quelle: loops/models.yaml → engines.claude.models (Single Source of Truth für
// die Claude-Max-Abo-Modelle). Ein neues Modell wird DORT eingetragen; danach
// `python3 scripts/sync_model_catalog.py` laufen lassen. Drift ist gate-rot.

/** Ein Modell der Max-Abo-Lane (`claude -p --model <id>`). */
export interface GeneratedClaudeModel {
  id: string;
  label: string;
  runtime: "claude-cli";
  group: string;
  provider: null;
  locked: false;
}

export const CLAUDE_CLI_MODELS: GeneratedClaudeModel[] = [
  { id: "claude-fable-5", label: "Claude Fable 5", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: false },
  { id: "claude-opus-5", label: "Claude Opus 5", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: false },
  { id: "claude-opus-4-8", label: "Claude Opus 4.8", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: false },
  { id: "claude-sonnet-5", label: "Claude Sonnet 5", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: false },
  { id: "claude-sonnet-4-6", label: "Claude Sonnet 4.6", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: false },
  { id: "claude-haiku-4-5", label: "Claude Haiku 4.5", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: false },
];

/** Nur die IDs — für Freitext-Vorschlagslisten (datalist). */
export const CLAUDE_CLI_MODEL_IDS: string[] = CLAUDE_CLI_MODELS.map(
  (model) => model.id,
);
