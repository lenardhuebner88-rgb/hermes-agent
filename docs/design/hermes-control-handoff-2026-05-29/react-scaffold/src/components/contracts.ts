/**
 * Komponenten-Kontrakte — die geteilten Bausteine.
 *
 * KERNREGEL der Architektur: A (luftig) und B (kompakt) rendern DIESELBEN
 * Komponenten mit DENSELBEN Props. Die Dichte kommt aus dem Context (useDensity),
 * NICHT aus unterschiedlichen Komponenten. So bleibt „eine App, drei Stufen" wartbar.
 *
 * Diese Datei definiert nur die Props (keine Implementierung) — sie ist der
 * Vertrag zwischen Views und UI-Bausteinen.
 */
import type {
  Worker, AgentLive, Proposal, ToneName, DiffLine, WorkerHealth,
} from '../lib/types';
import type { ReactNode } from 'react';
import type { Density } from '../hooks/useDensity';
import type { DotKind } from '../lib/tones';

/* ── Atome ─────────────────────────────────────────────────────────────── */

export interface StatusPillProps {
  tone: ToneName;
  label: string;
  dot?: DotKind;          // optionaler pulsierender LED-Punkt
  size?: 'sm' | 'md';
}

export interface LedProps {
  kind: DotKind;
  pulse?: boolean;        // respektiert global prefers-reduced-motion
  size?: number;          // px, Default 8
}

export interface DiffViewProps {
  lines: DiffLine[];
  /** B zeigt Zeilennummern, A/C nicht. */
  showLineNumbers?: boolean;
  /** mobil einklappbar; Default-Zustand eingeklappt auf < md. */
  collapsible?: boolean;
  defaultCollapsed?: boolean;
}

/* ── Karten (in beiden Dichten identische Props) ───────────────────────── */

export interface WorkerCardProps {
  worker: Worker;
  health: WorkerHealth;   // vorab via workerHealth() abgeleitet
  density: Density;
  now: number;            // injizierte Referenzzeit
  onInspect?: (runId: string) => void;
  onPrimaryAction?: (worker: Worker, action: WorkerAction) => void;
}
export type WorkerAction = 'dispatch' | 'nudge' | 'unlock' | 'restart';

export interface AgentCardProps {
  agent: AgentLive;
  density: Density;
  now: number;
}

export interface ProposalCardProps {
  proposal: Proposal;
  density: Density;
  onApply: (id: string, mode: Proposal['mode']) => void;
  onSkip: (id: string) => void;
}

/* ── Shell ─────────────────────────────────────────────────────────────── */

export type TabId = 'overview' | 'hermes' | 'openclaw' | 'autoresearch';

export interface ShellProps {
  active: TabId;
  onNavigate: (tab: TabId) => void;
  openProposals: number;  // Badge auf dem Autoresearch-Tab
  density: Density;
  children: ReactNode;
}

export interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onNavigate: (tab: TabId) => void;
  onAction: (action: 'fetchMore' | 'applyAll') => void;
}

/* Empfohlene Komponentenliste (Implementierungs-Checkliste):
   Atome:   <Led> <StatusPill> <DiffView> <MeterBar> <Eyebrow> <ToneCallout>
   Karten:  <WorkerCard> <AgentCard> <ProposalCard> <StatTile> <QueueCounters>
   Shell:   <ShellAiry> (Bottom-Tabs)  <ShellCompact> (Rail)  <CommandPalette>
   Views:   <OverviewView> <HermesFleet> <OpenClawFleet> <AutoresearchView>
*/
