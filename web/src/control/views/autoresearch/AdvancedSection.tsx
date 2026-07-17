import type { useDeepAudit, useTestFoundry } from "../../hooks/proposalsDeepAudit";
import { AUTORESEARCH_ADVANCED_GUIDE } from "../../lib/autoresearchAdvanced";
import type { getAdvancedRunChecklist, getDeepAuditGuidance, getTestFoundryGuidance } from "../../lib/autoresearchRunGuidance";
import type { TestFoundryResultSummary } from "../../lib/autoresearchTestFoundrySummary";
import { SignalChip, signalToneFromLegacy } from "../../components/leitstand";
import { Disclosure } from "../../components/primitives";
import { AdvancedGuidePanel } from "./panels";
import { LaneModelPanel } from "./LaneModelPanel";
import { DeepAuditPanel } from "./DeepAuditPanel";
import { TestFoundryPanel } from "./TestFoundryPanel";

type DeepAuditHook = ReturnType<typeof useDeepAudit>;
type TestFoundryHook = ReturnType<typeof useTestFoundry>;
type DeepAuditGuidance = ReturnType<typeof getDeepAuditGuidance>;
type TestFoundryGuidance = ReturnType<typeof getTestFoundryGuidance>;
type AdvancedRunChecklist = ReturnType<typeof getAdvancedRunChecklist>;

export function AdvancedSection({
  open,
  needsAttention,
  deepAudit,
  deepAuditRunning,
  effectiveDeepAuditSubsystem,
  deepAuditFocus,
  deepAuditMessage,
  deepAuditGuidance,
  deepAuditChecklist,
  testFoundry,
  testFoundryRunning,
  effectiveTestFoundryTarget,
  testFoundryApply,
  testFoundryMessage,
  testFoundryResultSummary,
  testFoundryGuidance,
  testFoundryChecklist,
  onToggle,
  onDeepAuditSubsystemChange,
  onDeepAuditFocusChange,
  onStartDeepAudit,
  onTestFoundryTargetChange,
  onTestFoundryApplyChange,
  onStartTestFoundry,
}: {
  open: boolean;
  needsAttention: boolean;
  deepAudit: DeepAuditHook;
  deepAuditRunning: boolean;
  effectiveDeepAuditSubsystem: string;
  deepAuditFocus: string;
  deepAuditMessage: string | null;
  deepAuditGuidance: DeepAuditGuidance;
  deepAuditChecklist: AdvancedRunChecklist;
  testFoundry: TestFoundryHook;
  testFoundryRunning: boolean;
  effectiveTestFoundryTarget: string;
  testFoundryApply: boolean;
  testFoundryMessage: string | null;
  testFoundryResultSummary: TestFoundryResultSummary | null;
  testFoundryGuidance: TestFoundryGuidance;
  testFoundryChecklist: AdvancedRunChecklist;
  onToggle: (open: boolean) => void;
  onDeepAuditSubsystemChange: (value: string) => void;
  onDeepAuditFocusChange: (value: string) => void;
  onStartDeepAudit: () => void;
  onTestFoundryTargetChange: (value: string) => void;
  onTestFoundryApplyChange: (value: boolean) => void;
  onStartTestFoundry: () => void;
}) {
  return (
    <Disclosure
      id="autoresearch-advanced"
      className="scroll-mt-6 space-y-4 border-t border-line pt-4"
      open={open}
      onToggle={onToggle}
      summary={
        <span className="min-h-12 flex w-full items-center justify-between gap-3 rounded-panel border border-line bg-surface-2 px-3 py-2 text-left">
          <span className="min-w-0">
            <span className="font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3">Erweitert</span>
            <span className="mt-1 block text-sm font-semibold text-ink">Modelle, Deep-Audit und Test-Foundry</span>
            <span className="mt-0.5 block text-xs leading-5 text-ink-2">Für gezielte Spezialläufe und Modellzuweisung. Der normale Ablauf bleibt oben: Entscheidungen prüfen, dann Probelauf starten.</span>
          </span>
          <SignalChip tone={signalToneFromLegacy(needsAttention ? "amber" : "zinc")} label={needsAttention ? "Aufmerksamkeit" : "Optional"} />
        </span>
      }
    >
      <div className="space-y-4">
        <AdvancedGuidePanel items={AUTORESEARCH_ADVANCED_GUIDE} />
        <LaneModelPanel />
        <DeepAuditPanel deepAudit={deepAudit} running={deepAuditRunning} effectiveSubsystem={effectiveDeepAuditSubsystem} focus={deepAuditFocus} message={deepAuditMessage} guidance={deepAuditGuidance} checklist={deepAuditChecklist} onSubsystemChange={onDeepAuditSubsystemChange} onFocusChange={onDeepAuditFocusChange} onStart={onStartDeepAudit} />
        <TestFoundryPanel testFoundry={testFoundry} running={testFoundryRunning} effectiveTarget={effectiveTestFoundryTarget} autoApply={testFoundryApply} message={testFoundryMessage} summary={testFoundryResultSummary} guidance={testFoundryGuidance} checklist={testFoundryChecklist} onTargetChange={onTestFoundryTargetChange} onAutoApplyChange={onTestFoundryApplyChange} onStart={onStartTestFoundry} />
      </div>
    </Disclosure>
  );
}
