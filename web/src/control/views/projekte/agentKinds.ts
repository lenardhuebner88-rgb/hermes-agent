// Kind → display metadata for Projekte-Tab agent chips + Agents-Rail (Stufe 5).
// Identity colors use the DATA palette (DESIGN.md W6-4) — never status/bronze.
import type { LucideIcon } from "lucide-react";
import {
  Bot,
  CircleHelp,
  KanbanSquare,
  RefreshCw,
  Server,
  Sparkles,
  Terminal,
  Zap,
} from "lucide-react";
import type { ProjectAgent, ProjectAgentKind } from "../../lib/schemas";
import { de } from "../../i18n/de";

const labels = de.projekte.agentKinds;

export type AgentKindStyle = {
  /** German display name (from de.projekte.agentKinds). */
  label: string;
  icon: LucideIcon;
  /** Existing token text-color class (data identity or ink hierarchy). */
  tone: string;
};

/** Canonical kind order for the Alle-Agents rail (and tests). */
export const PROJECT_AGENT_KIND_ORDER: readonly ProjectAgentKind[] = [
  "claude",
  "codex",
  "kimi",
  "grok",
  "hermes",
  "kanban",
  "loop",
  "unknown",
] as const;

/**
 * Per-kind styling. Engine colors align with LoopsView ENGINE_COLOR where the
 * kinds overlap (claude/codex/kimi/hermes); remaining kinds take free data-N
 * slots or ink hierarchy for unknown.
 */
export const AGENT_KIND_STYLES: Record<ProjectAgentKind, AgentKindStyle> = {
  claude: { label: labels.claude, icon: Sparkles, tone: "text-data-1" },
  codex: { label: labels.codex, icon: Terminal, tone: "text-data-4" },
  kimi: { label: labels.kimi, icon: Bot, tone: "text-data-5" },
  grok: { label: labels.grok, icon: Zap, tone: "text-data-3" },
  hermes: { label: labels.hermes, icon: Server, tone: "text-data-2" },
  kanban: { label: labels.kanban, icon: KanbanSquare, tone: "text-data-6" },
  loop: { label: labels.loop, icon: RefreshCw, tone: "text-ink-2" },
  unknown: { label: labels.unknown, icon: CircleHelp, tone: "text-ink-3" },
};

/** Max agent chips visible on a project card before "+N" overflow. */
export const AGENTS_CHIP_MAX_VISIBLE = 4;

/** Compact chip label: kanban/loop show the free-text label; coding CLIs show the kind name. */
export function agentChipText(agent: Pick<ProjectAgent, "kind" | "label">): string {
  if (agent.kind === "kanban" || agent.kind === "loop") {
    const text = agent.label.trim();
    return text || AGENT_KIND_STYLES[agent.kind].label;
  }
  return AGENT_KIND_STYLES[agent.kind]?.label ?? labels.unknown;
}
