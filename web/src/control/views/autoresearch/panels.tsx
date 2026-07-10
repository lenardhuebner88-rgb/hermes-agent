import type { ReactNode } from "react";
import { Archive, CheckCheck, ClipboardCheck, FlaskConical, ListChecks, Radar, SearchCode, Settings2, ShieldCheck, Sparkles, Target, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import type { AutoresearchActivityCard } from "../../lib/autoresearchActivity";
import type { AutoresearchAdvancedGuideItem } from "../../lib/autoresearchAdvanced";
import { fmtClock } from "../../lib/derive";
import { formatResearchTokens, hasResearchCounters, readLastRunCounters, severityTone, shouldShowResearchErrorBadge } from "../../lib/autoresearch";
import { getAutoresearchLastRunBrief } from "../../lib/autoresearchRunSummary";
import type { AutoresearchSectionNavItem } from "../../lib/autoresearchNavigation";
import type { AutoresearchReadinessSummary } from "../../lib/autoresearchReadiness";
import type { AutoresearchResolvedSummary } from "../../lib/autoresearchResolvedSummary";
import { getAutoresearchQueueModeSummary, type AutoresearchEmptyQueueModeGuidance, type AutoresearchQueueMode } from "../../lib/autoresearchQueueMode";
import type { AutoresearchDecisionGuide, AutoresearchQueueActionSummary } from "../../lib/autoresearchDecisionGuide";
import type { AutoresearchReviewFlow } from "../../lib/autoresearchReviewFlow";
import { RESEARCH_LOOP_PRESETS, type AutoresearchAdvancedRunChecklist, type AutoresearchRunGuidance, type ResearchLoopPresetId, type ResearchLoopStartChecklist, type ResearchLoopStartSummary } from "../../lib/autoresearchRunGuidance";
import type { TestFoundryResultSummary } from "../../lib/autoresearchTestFoundrySummary";
import { de } from "../../i18n/de";
import type { DeepAuditFinding, useAutoresearchStatus } from "../../hooks/useControlData";
import type { AutoresearchRun, ToneName } from "../../lib/types";
import { KpiTile, SignalChip, signalToneFromLegacy } from "../../components/leitstand";
import { Card, Disclosure, Eyebrow, SkeletonCard, Stagger, StaggerItem, Text } from "../../components/primitives";
import type { AutoresearchActionHint } from "../../lib/autoresearchActionPlan";
import { reviewStepToneClass } from "./panels.helpers";

export function Metric({ label, value }: { label: string; value: string }) {
  return <KpiTile label={label} value={value} />;
}

export function OperatorActionCard({ icon, eyebrow, hint, title, body, button }: { icon: ReactNode; eyebrow: string; hint: AutoresearchActionHint; title: string; body: string; button: ReactNode }) {
  return (
    <Card surface="card" className="flex min-h-[188px] flex-col justify-between p-3">
      <div>
        <div className="mb-3 flex items-center justify-between gap-3">
          <span className="grid size-10 place-items-center rounded-card border border-line bg-surface-2 text-brand">
            {icon}
          </span>
          <span className="flex flex-wrap justify-end gap-1.5">
            <Eyebrow>{eyebrow}</Eyebrow>
            <SignalChip tone={signalToneFromLegacy(hint.tone)} label={hint.label} />
          </span>
        </div>
        <Text as="h3" variant="label" className="text-ink">{title}</Text>
        <p className="mt-1 text-xs leading-5 text-ink-2">{body}</p>
        <div className="mt-3 rounded-card border border-line bg-surface-2 px-2 py-1.5 text-xs leading-5 text-ink-2">
          <p><span className="font-semibold text-ink">Warum:</span> {hint.reason}</p>
          <p className="mt-1"><span className="font-semibold text-ink">Danach:</span> {hint.after}</p>
        </div>
      </div>
      <div className="mt-3">{button}</div>
    </Card>
  );
}

export function ReadinessPanel({ summary }: { summary: AutoresearchReadinessSummary }) {
  return (
    <section className={cn("rounded-panel border p-3", reviewStepToneClass(summary.tone))} aria-label="Autoresearch Betriebsstatus">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Eyebrow>Betriebsstatus</Eyebrow>
            <SignalChip tone={signalToneFromLegacy(summary.tone)} label={summary.label} />
          </div>
          <Text as="h2" variant="subtitle" className="mt-2 text-ink">{summary.title}</Text>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-ink-2">{summary.detail}</p>
          <p className="mt-2 text-sm text-ink"><span className="font-semibold">Jetzt sinnvoll:</span> {summary.next}</p>
        </div>
        <div className="grid shrink-0 grid-cols-2 gap-1.5 sm:grid-cols-5 lg:min-w-[420px]">
          {summary.facts.map((fact) => <FactTile key={fact.label} label={fact.label} value={fact.value} tone={fact.tone} compact />)}
        </div>
      </div>
    </section>
  );
}

export function ResolvedQueueSummaryPanel({ summary, archiveBusy, archiveDisabled, onArchiveReverted }: { summary: AutoresearchResolvedSummary; archiveBusy: boolean; archiveDisabled: boolean; onArchiveReverted: () => void }) {
  return (
    <section className={cn("rounded-panel border p-3", reviewStepToneClass(summary.tone))} aria-label="Autoresearch Abschluss und Aufräumen">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Eyebrow>Abschluss & Aufräumen</Eyebrow>
            <SignalChip tone={signalToneFromLegacy(summary.tone)} label={summary.label} />
          </div>
          <Text as="h2" variant="subtitle" className="mt-2 text-ink">{summary.title}</Text>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-ink-2">{summary.detail}</p>
          <p className="mt-2 text-sm text-ink"><span className="font-semibold">Jetzt sinnvoll:</span> {summary.next}</p>
        </div>
        <div className="grid shrink-0 gap-2 sm:grid-cols-3 lg:min-w-[360px]">
          {summary.facts.map((fact) => <FactTile key={fact.label} label={fact.label} value={fact.value} tone={fact.tone} />)}
          {summary.archiveLabel ? (
            <Button outlined className="min-h-12 sm:col-span-3" onClick={onArchiveReverted} disabled={archiveDisabled} prefix={archiveBusy ? <Spinner /> : <Archive className="h-4 w-4" />}>
              {summary.archiveLabel}
            </Button>
          ) : null}
        </div>
      </div>
    </section>
  );
}

export function CockpitSectionNav({ items, onJump }: { items: readonly AutoresearchSectionNavItem[]; onJump: (id: string) => void }) {
  const iconFor = (kind: AutoresearchSectionNavItem["kind"]) => {
    switch (kind) {
      case "review": return <ClipboardCheck className="h-4 w-4" />;
      case "run": return <Radar className="h-4 w-4" />;
      case "history": return <ListChecks className="h-4 w-4" />;
      case "advanced": return <Settings2 className="h-4 w-4" />;
    }
  };

  return (
    <nav aria-label="Autoresearch Bereiche" className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
      {items.map((item) => (
        <button key={item.id} type="button" onClick={() => onJump(item.id)} className="min-h-12 rounded-panel border border-line bg-surface-2 px-3 py-2 text-left transition hover:border-live hover:bg-live/10">
          <span className="flex items-center gap-2 text-sm font-semibold text-ink">{iconFor(item.kind)}{item.label}</span>
          <span className="mt-0.5 block text-xs leading-5 text-ink-2">{item.detail}</span>
        </button>
      ))}
    </nav>
  );
}

export function AdvancedGuidePanel({ items }: { items: readonly AutoresearchAdvancedGuideItem[] }) {
  const iconFor = (kind: AutoresearchAdvancedGuideItem["kind"]) => {
    switch (kind) {
      case "models": return <Settings2 className="h-4 w-4" />;
      case "deep-audit": return <SearchCode className="h-4 w-4" />;
      case "test-foundry": return <FlaskConical className="h-4 w-4" />;
    }
  };

  return (
    <section className="rounded-panel border border-line bg-surface-1 p-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <Eyebrow>Vor Erweitert</Eyebrow>
          <Text as="h2" variant="subtitle" className="text-ink">Nur öffnen, wenn der normale Probelauf nicht reicht.</Text>
        </div>
        <SignalChip tone={signalToneFromLegacy("zinc")} label="Optional" />
      </div>
      <Stagger className="grid gap-3 lg:grid-cols-3">
        {items.map((item) => (
          <StaggerItem key={item.kind}>
            <article className={cn("rounded-panel border p-3", reviewStepToneClass(item.tone))}>
              <div className="flex items-start justify-between gap-3">
                <span className="grid h-8 w-8 shrink-0 place-items-center rounded-card border border-line bg-surface-2 text-ink">{iconFor(item.kind)}</span>
                <SignalChip tone={signalToneFromLegacy(item.tone)} label={item.label} />
              </div>
              <Text as="h3" variant="label" className="mt-3 text-ink">{item.title}</Text>
              <div className="mt-2 space-y-1 text-xs leading-5 text-ink-2">
                <p><span className="font-semibold text-ink">Wann:</span> {item.when}</p>
                <p><span className="font-semibold text-ink">Aufwand:</span> {item.cost}</p>
                <p><span className="font-semibold text-ink">Sicherheit:</span> {item.safety}</p>
              </div>
            </article>
          </StaggerItem>
        ))}
      </Stagger>
    </section>
  );
}

export function ActivityTimelineItem({ at, card }: { at: number; card: AutoresearchActivityCard }) {
  return (
    <article className={cn("rounded-panel border px-3 py-2", reviewStepToneClass(card.tone))}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-data tabular-nums text-xs text-ink-3">{fmtClock(at)}</span>
            <SignalChip tone={signalToneFromLegacy(card.tone)} label={card.label} />
          </div>
          <Text as="h3" variant="label" className="mt-1 text-ink">{card.title}</Text>
          <p className="mt-1 text-sm leading-6 text-ink-2">{card.detail}</p>
        </div>
        <p className="max-w-sm text-xs leading-5 text-ink-3"><span className="font-semibold text-ink-2">Danach:</span> {card.next}</p>
      </div>
    </article>
  );
}

export function LatestActivityPanel({ at, card }: { at: number; card: AutoresearchActivityCard }) {
  return (
    <section className={cn("rounded-panel border p-3", reviewStepToneClass(card.tone))} aria-label="Letzte Autoresearch Aktion">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Eyebrow>Letzte Aktion</Eyebrow>
            <span className="font-data tabular-nums text-xs text-ink-3">{fmtClock(at)}</span>
            <SignalChip tone={signalToneFromLegacy(card.tone)} label={card.label} />
          </div>
          <Text as="h2" variant="label" className="mt-2 text-ink">{card.title}</Text>
          <p className="mt-1 text-sm leading-6 text-ink-2">{card.detail}</p>
        </div>
        <p className="rounded-card border border-line bg-surface-2 px-3 py-2 text-xs leading-5 text-ink-2 sm:max-w-sm"><span className="font-semibold text-ink">Jetzt sinnvoll:</span> {card.next}</p>
      </div>
    </section>
  );
}

export function ReviewFlowPanel({ flow, busy, onPrimary }: { flow: AutoresearchReviewFlow; busy: boolean; onPrimary: () => void }) {
  const icon = flow.primaryAction === "confirm-selection" ? <CheckCheck className="h-4 w-4" /> : flow.primaryAction === "select-visible" ? <ListChecks className="h-4 w-4" /> : flow.primaryAction === "clear-selection" ? <X className="h-4 w-4" /> : flow.primaryAction === "archive-reverted" ? <Archive className="h-4 w-4" /> : flow.primaryAction === "generate" ? <Sparkles className="h-4 w-4" /> : <ClipboardCheck className="h-4 w-4" />;

  return (
    <div className="rounded-panel border border-live bg-live/10 p-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Eyebrow className="text-live">Prüfablauf</Eyebrow>
            <SignalChip tone={signalToneFromLegacy(flow.tone)} label={flow.progressLabel} />
          </div>
          <Text as="h3" variant="subtitle" className="mt-2 text-ink">{flow.title}</Text>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-ink-2">{flow.detail}</p>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-surface-0"><div className="h-full rounded-full bg-live" style={{ width: `${flow.progressPercent}%` }} /></div>
        </div>
        <div className="grid shrink-0 gap-2 sm:grid-cols-3 lg:min-w-[360px]">
          {flow.steps.map((step) => <FactTile key={step.label} label={step.label} value={step.value} tone={step.tone} />)}
          <Button className="min-h-12 sm:col-span-3" onClick={onPrimary} disabled={busy} prefix={busy ? <Spinner /> : icon}>{flow.primaryLabel}</Button>
        </div>
      </div>
    </div>
  );
}

export function QueueActionSummaryPanel({ summary }: { summary: AutoresearchQueueActionSummary }) {
  return (
    <div className={cn("max-w-xl rounded-panel border p-3 text-left", reviewStepToneClass(summary.tone))}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <Eyebrow>Auswahlwirkung</Eyebrow>
          <Text as="h3" variant="label" className="mt-1 text-ink">{summary.title}</Text>
          <div className="mt-2 space-y-1 text-xs leading-5 text-ink-2"><p>{summary.batchLine}</p><p>{summary.manualLine}</p><p className="text-ink">{summary.confirmLine}</p></div>
        </div>
        <div className="grid shrink-0 grid-cols-3 gap-1.5 sm:min-w-64">
          {summary.facts.map((fact) => <FactTile key={fact.label} label={fact.label} value={fact.value} tone={fact.tone} compact />)}
        </div>
      </div>
    </div>
  );
}

export function SelectionActionBar({ summary, selectedCount, canConfirm, busy, onConfirm, onClear }: { summary: AutoresearchQueueActionSummary; selectedCount: number; canConfirm: boolean; busy: boolean; onConfirm: () => void; onClear: () => void }) {
  return (
    <div className={cn("sticky bottom-[calc(4.75rem+env(safe-area-inset-bottom,0px))] z-30 rounded-panel border p-3 shadow-2xl shadow-black/40 backdrop-blur lg:bottom-3", reviewStepToneClass(summary.tone))}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2"><Eyebrow>Auswahl bereit</Eyebrow><SignalChip tone={signalToneFromLegacy(summary.tone)} label={`${selectedCount} markiert`} /></div>
          <Text as="h3" variant="label" className="mt-1 text-ink">{summary.title}</Text>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-ink-2">{summary.confirmLine}</p>
        </div>
        <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
          <Button outlined className="min-h-12 justify-center" onClick={onClear} disabled={busy} prefix={<X className="h-4 w-4" />}>Auswahl leeren</Button>
          <Button className="min-h-12 justify-center" onClick={onConfirm} disabled={!canConfirm} title={canConfirm ? "Übernimmt nur die markierten sammelsicheren Karten." : "Riskante Auswahl erst leeren oder einzeln prüfen."} prefix={busy ? <Spinner /> : <CheckCheck className="h-4 w-4" />}>Auswahl übernehmen</Button>
        </div>
      </div>
    </div>
  );
}

export function QueueModePicker({ summary, activeMode, onChange }: { summary: ReturnType<typeof getAutoresearchQueueModeSummary>; activeMode: AutoresearchQueueMode; onChange: (mode: AutoresearchQueueMode) => void }) {
  return (
    <div className="max-w-3xl rounded-panel border border-line bg-surface-1 p-2 text-left">
      <div className="grid gap-1.5 sm:grid-cols-4">
        {summary.options.map((option) => {
          const active = option.id === activeMode;
          return (
            <button key={option.id} type="button" onClick={() => onChange(option.id)} aria-pressed={active} className={cn("min-h-12 rounded-card border px-2 py-1.5 text-left transition", active ? "border-live bg-live/10" : "border-line bg-surface-2 hover:bg-surface-3")}>
              <span className="flex items-center justify-between gap-2"><span className="text-xs font-semibold text-ink">{option.label}</span><SignalChip tone={signalToneFromLegacy(option.tone)} label={String(option.count)} /></span>
            </button>
          );
        })}
      </div>
      <p className="mt-2 text-xs leading-5 text-ink-2"><span className="font-semibold text-ink">{summary.active.label}:</span> {summary.active.detail}</p>
    </div>
  );
}

export function EmptyQueueModePanel({ guidance, onChangeMode }: { guidance: AutoresearchEmptyQueueModeGuidance; onChangeMode: (mode: AutoresearchQueueMode) => void }) {
  return (
    <div className={cn("rounded-panel border p-3", reviewStepToneClass(guidance.tone))}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2"><Eyebrow>Filter leer</Eyebrow><SignalChip tone={signalToneFromLegacy(guidance.tone)} label={guidance.label} /></div>
          <Text as="h3" variant="subtitle" className="mt-2 text-ink">{guidance.title}</Text>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-ink-2">{guidance.detail}</p>
        </div>
        <Button outlined className="min-h-12 shrink-0 justify-center" onClick={() => onChangeMode(guidance.primaryMode)} prefix={<ListChecks className="h-4 w-4" />}>{guidance.primaryLabel}</Button>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-4">{guidance.facts.map((fact) => <FactTile key={fact.label} label={fact.label} value={fact.value} tone={fact.tone} />)}</div>
    </div>
  );
}

export function DecisionGuidePanel({ guide }: { guide: AutoresearchDecisionGuide }) {
  const icon = guide.tone === "emerald" ? <ShieldCheck className="h-4 w-4" /> : guide.tone === "cyan" ? <ClipboardCheck className="h-4 w-4" /> : guide.tone === "amber" ? <Target className="h-4 w-4" /> : <X className="h-4 w-4" />;
  return (
    <div className={cn("rounded-panel border p-3", reviewStepToneClass(guide.tone))}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2"><span className="grid h-8 w-8 place-items-center rounded-card border border-line bg-surface-2 text-ink">{icon}</span><Eyebrow>Heute tun</Eyebrow><SignalChip tone={signalToneFromLegacy(guide.tone)} label={guide.primaryLabel} /></div>
          <Text as="h3" variant="subtitle" className="mt-2 text-ink">{guide.headline}</Text>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-ink-2">{guide.summary}</p>
          <p className="mt-2 text-sm text-ink"><span className="font-semibold">Nächster sicherer Schritt:</span> {guide.next}</p>
        </div>
        <div className="grid shrink-0 gap-2 sm:grid-cols-3 lg:min-w-[360px]">{guide.facts.map((fact) => <FactTile key={fact.label} label={fact.label} value={fact.value} tone={fact.tone} />)}</div>
      </div>
    </div>
  );
}

export function RunGuidanceCard({ guidance }: { guidance: AutoresearchRunGuidance }) {
  return (
    <div className="rounded-panel border border-line bg-surface-2 p-3">
      <div className="mb-2 flex items-center justify-between gap-2"><Eyebrow>Vor dem Start</Eyebrow><SignalChip tone={signalToneFromLegacy(guidance.tone)} label={guidance.label} /></div>
      <div className="grid gap-2 text-xs leading-5 text-ink-2"><p><span className="font-semibold text-ink">Wofür:</span> {guidance.outcome}</p><p><span className="font-semibold text-ink">Kosten:</span> {guidance.cost}</p><p><span className="font-semibold text-ink">Sicherheit:</span> {guidance.safety}</p></div>
    </div>
  );
}

export function LoopPresetPicker({ selectedId, disabled, onSelect }: { selectedId: ResearchLoopPresetId | null; disabled: boolean; onSelect: (presetId: ResearchLoopPresetId) => void }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2"><div><Eyebrow>Startvorlage</Eyebrow><p className="mt-0.5 text-xs text-ink-2">Wähle zuerst den Zweck, nicht die Technik.</p></div><SignalChip tone={signalToneFromLegacy(selectedId ? "emerald" : "amber")} label={selectedId ? "Vorlage aktiv" : "Eigene Werte"} /></div>
      <div className="grid gap-2">
        {RESEARCH_LOOP_PRESETS.map((preset) => {
          const selected = preset.id === selectedId;
          return (
            <button key={preset.id} type="button" onClick={() => onSelect(preset.id)} disabled={disabled} title={preset.title} aria-label={`${preset.label}: ${preset.summary} ${preset.cost}.`} aria-pressed={selected} className={cn("min-h-12 min-h-[118px] rounded-panel border px-3 py-2.5 text-left transition disabled:cursor-not-allowed disabled:opacity-60", selected ? "border-live bg-live/10" : "border-line bg-surface-2 hover:bg-surface-3")}>
              <span className="flex items-start justify-between gap-3"><span className="min-w-0"><span className="block text-sm font-semibold text-ink">{preset.label}</span><span className="mt-0.5 block text-xs font-medium text-ink-2">{preset.operatorTitle}</span></span><span className={cn("shrink-0 text-micro font-medium", selected ? "text-live" : "text-ink-2")}>{preset.badge}</span></span>
              <span className="mt-2 block text-xs leading-5 text-ink-2">{preset.operatorFit}</span><span className="mt-1 block text-xs leading-5 text-ink-3">{preset.operatorResult}</span><span className="mt-2 block font-data tabular-nums text-micro text-ink-3">{preset.cost}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}


export function TargetingPreview({ summary }: { summary: ResearchLoopStartSummary }) {
  return (
    <div className="rounded-panel border border-line bg-surface-2 px-3 py-2 text-xs text-ink-2">
      <div className="flex items-start gap-2"><Target className="mt-0.5 h-3.5 w-3.5 shrink-0" /><div className="min-w-0"><p className="font-semibold">{summary.title}</p><p className="mt-1.5 leading-5">{summary.scope}</p><p className="mt-0.5 leading-5">{summary.detail}</p><p className="mt-1.5 leading-5"><span className="font-semibold">Aufwand:</span> {summary.cost}</p><p className="mt-0.5 leading-5"><span className="font-semibold">Sicherheit:</span> {summary.safety}</p><p className="mt-1 font-data tabular-nums text-micro opacity-75">{summary.technicalLabel}</p></div></div>
    </div>
  );
}

export function StartChecklistPanel({ checklist }: { checklist: ResearchLoopStartChecklist }) {
  return <ChecklistPanel eyebrow="Startprüfung" title={checklist.title} detail={checklist.detail} label={checklist.label} tone={checklist.tone} items={checklist.items} className="p-3" />;
}

export function AdvancedRunChecklistPanel({ checklist }: { checklist: AutoresearchAdvancedRunChecklist }) {
  return <ChecklistPanel eyebrow="Startprüfung" title={checklist.title} detail={checklist.detail} label={checklist.label} tone={checklist.tone} items={checklist.items} className="p-2.5" compact />;
}

export function TestFoundryResultPanel({ summary, raw }: { summary: TestFoundryResultSummary; raw: unknown }) {
  const rawPayload = formatLastRunRawPayload(raw);
  return (
    <div className={cn("rounded-panel border p-3", reviewStepToneClass(summary.tone))}>
      <div className="flex flex-col gap-3">
        <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><Eyebrow>Letzter Test-Foundry-Lauf</Eyebrow><SignalChip tone={signalToneFromLegacy(summary.tone)} label={summary.label} /></div><Text as="h3" variant="label" className="mt-2 text-ink">{summary.title}</Text><p className="mt-1 text-sm leading-6 text-ink-2">{summary.detail}</p><p className="mt-2 text-sm text-ink"><span className="font-semibold">Jetzt sinnvoll:</span> {summary.next}</p></div>
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">{summary.facts.map((fact) => <FactTile key={fact.label} label={fact.label} value={fact.value} tone={fact.tone} compact title={fact.value} />)}</div>
      </div>
      {rawPayload ? <Disclosure summary={<span className="font-semibold text-ink">{summary.rawLabel}</span>} className="mt-3 rounded-card border border-line bg-surface-2 px-3 py-2 text-xs text-ink-2"><pre className="max-h-40 overflow-auto rounded-card border border-line bg-surface-0 p-2 font-data tabular-nums text-micro leading-5 text-ink-2">{rawPayload}</pre></Disclosure> : null}
    </div>
  );
}

export function LastRun({ status, latestRun }: { status: ReturnType<typeof useAutoresearchStatus>["data"]; latestRun: AutoresearchRun | null }) {
  const receipt = status?.last_receipt;
  const note = status?.note;
  const lastRun = status?.last_run;
  const objectRun = lastRun && typeof lastRun === "object" ? lastRun as Record<string, unknown> : null;
  const proposed = typeof objectRun?.proposed === "number" ? objectRun.proposed : null;
  const kept = typeof objectRun?.kept === "number" ? objectRun.kept : null;
  const reverted = typeof objectRun?.reverted === "number" ? objectRun.reverted : null;
  const stopped = objectRun?.stopped === true ? "Signal erhalten" : null;
  const brief = getAutoresearchLastRunBrief({ lastRun, latestRun, receipt, note });
  const rawPayload = formatLastRunRawPayload(lastRun);
  const counters = readLastRunCounters(lastRun);
  const showCounters = hasResearchCounters(counters);
  const showErrorBadge = shouldShowResearchErrorBadge(counters.researchErrors);

  return (
    <div className={cn("rounded-panel border p-3", reviewStepToneClass(brief.tone))}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><Eyebrow>Letzter Lauf</Eyebrow><SignalChip tone={signalToneFromLegacy(brief.tone)} label={brief.label} /></div><Text as="h3" variant="label" className="mt-2 text-ink">{brief.title}</Text><p className="mt-1 text-sm leading-6 text-ink-2">{brief.detail}</p><p className="mt-2 text-sm text-ink"><span className="font-semibold">Nächster Schritt:</span> {brief.next}</p></div>
        <div className="grid shrink-0 grid-cols-2 gap-1.5 sm:grid-cols-4 lg:min-w-[360px]">{brief.facts.map((fact) => <FactTile key={fact.label} label={fact.label} value={fact.value} tone={fact.tone} compact />)}</div>
      </div>
      {brief.rawLine || rawPayload || proposed !== null || kept !== null || reverted !== null || showCounters || stopped || receipt || note ? (
        <Disclosure summary={<span className="font-semibold text-ink">Technische Details</span>} className="mt-3 rounded-card border border-line bg-surface-2 px-3 py-2 text-xs text-ink-2">
          {brief.rawLine ? <p><span className="text-ink">Rohstatus:</span> {brief.rawLine}</p> : null}
          {rawPayload ? <pre className="mt-2 max-h-40 overflow-auto rounded-card border border-line bg-surface-0 p-2 font-data tabular-nums text-micro leading-5 text-ink-2">{rawPayload}</pre> : null}
          {proposed !== null || kept !== null || reverted !== null ? <p className="mt-1 text-ink-2"><span>Vorgeschlagen=</span><span className="font-data tabular-nums text-ink">{proposed ?? "?"}</span> · <span>übernommen=</span><span className="font-data tabular-nums text-ink">{kept ?? "?"}</span> · <span>zurückgerollt=</span><span className="font-data tabular-nums text-ink">{reverted ?? "?"}</span></p> : null}
          {showCounters ? <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-ink-2"><p><span>{de.autoresearch.skillsResearched}=</span><span className="font-data tabular-nums text-ink">{counters.skillsResearched ?? "?"}</span> · <span>{de.autoresearch.researchErrors}=</span><span className="font-data tabular-nums text-ink">{counters.researchErrors ?? "?"}</span> · <span>{de.autoresearch.skillsWithFindings}=</span><span className="font-data tabular-nums text-ink">{counters.skillsWithFindings ?? "?"}</span></p>{showErrorBadge ? <SignalChip tone="alert" label={de.autoresearch.researchErrorBadge} /> : null}</div> : null}
          {showCounters ? <p className="mt-1 text-ink-2"><span>{de.autoresearch.researchTokens}: </span><span className="font-data tabular-nums text-ink">{formatResearchTokens(counters.researchTokens)}</span></p> : null}
          {showCounters ? <p className="mt-1 text-xs text-ink-3">{de.autoresearch.counterLegend}</p> : null}
          {stopped ? <p className="mt-1">Stop: {stopped}</p> : null}
          {receipt ? <p className="mt-1 truncate text-xs text-ink-3" title={receipt}>Beleg: <span className="font-data">{receipt}</span></p> : null}
          {note ? <p className="mt-1">{note}</p> : null}
        </Disclosure>
      ) : null}
    </div>
  );
}

function formatLastRunRawPayload(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  try { return JSON.stringify(value, null, 2); } catch { return null; }
}

export function Empty({ icon, text }: { icon: ReactNode; text: string }) {
  return <Card surface="card" className="flex items-center gap-3 p-4 text-sm text-ink-2">{icon}<span>{text}</span></Card>;
}

export function EmptySkeleton({ rows = 3 }: { rows?: number }) {
  return <SkeletonCard rows={rows} />;
}

export function DeepAuditFindings({ findings, proposals }: { findings: DeepAuditFinding[]; proposals: string[] }) {
  const proposalCardLabel = `${proposals.length} ${proposals.length === 1 ? "Karte" : "Karten"}`;
  if (findings.length === 0) {
    return (
      <div className="mt-4 rounded-panel border border-line bg-surface-2 px-3 py-2 text-sm text-ink-2">
        <div className="flex flex-wrap items-center gap-2"><span>Noch keine Deep-Audit-Findings.</span>{proposals.length > 0 ? <SignalChip tone={signalToneFromLegacy("amber")} label={proposalCardLabel} /> : null}</div>
        <p className="mt-1">{proposals.length > 0 ? "Der Lauf hat bereits Review-Karten gemeldet; die Detail-Findings sind in dieser Antwort nicht enthalten." : "Wenn der Lauf fertig ist, erscheinen hier die prüfbaren Risiken und die daraus erzeugten Review-Karten."}</p>
      </div>
    );
  }
  const severityCounts = findings.reduce<Record<DeepAuditFinding["severity"], number>>((counts, finding) => { counts[finding.severity] += 1; return counts; }, { critical: 0, high: 0, medium: 0, low: 0 });
  const topSeverity = (["critical", "high", "medium", "low"] as const).find((severity) => severityCounts[severity] > 0) ?? "low";
  const topFinding = findings.find((finding) => finding.severity === topSeverity) ?? findings[0];

  return (
    <div className="mt-4 space-y-3">
      <section className={cn("rounded-panel border p-3", reviewStepToneClass(severityTone(topSeverity)))}>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><Eyebrow>Audit-Ergebnis</Eyebrow><SignalChip tone={signalToneFromLegacy(severityTone(topSeverity))} label={deepAuditSeverityLabel(topSeverity)} />{proposals.length > 0 ? <SignalChip tone={signalToneFromLegacy("amber")} label={proposalCardLabel} /> : null}</div><Text as="h3" variant="subtitle" className="mt-2 text-ink">{findings.length === 1 ? "1 prüfbares Risiko gefunden." : `${findings.length} prüfbare Risiken gefunden.`}</Text><p className="mt-1 max-w-3xl text-sm leading-6 text-ink-2">Wichtigster Punkt: {topFinding.title}. Der Deep-Audit schreibt keinen Code; daraus entstehen nur Review-Karten.</p><p className="mt-2 text-sm text-ink"><span className="font-semibold">Jetzt sinnvoll:</span> Review-Karten prüfen, dann nur belegte Fixes übernehmen.</p></div>
          <div className="grid shrink-0 grid-cols-2 gap-1.5 sm:grid-cols-4 lg:min-w-[360px]">{(["critical", "high", "medium", "low"] as const).map((severity) => <FactTile key={severity} label={deepAuditSeverityLabel(severity)} value={String(severityCounts[severity])} tone={severityTone(severity)} compact />)}</div>
        </div>
      </section>
      <Stagger className="grid gap-3">
        {findings.map((finding, index) => (
          <StaggerItem key={`${finding.fileline}-${index}`}>
            <article className={cn("rounded-panel border p-3", reviewStepToneClass(severityTone(finding.severity)))}>
              <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><SignalChip tone={signalToneFromLegacy(severityTone(finding.severity))} label={deepAuditSeverityLabel(finding.severity)} /><SignalChip tone={signalToneFromLegacy("cyan")} label={deepAuditCategoryLabel(finding.category)} /></div><Text as="h3" variant="label" className="mt-2 text-ink">{finding.title}</Text><p className="mt-1 text-sm leading-6 text-ink-2">{finding.problem}</p><p className="mt-2 text-sm text-ink"><span className="font-semibold">Fix-Hinweis:</span> {finding.fix_hint}</p></div>
                <div className="shrink-0 rounded-card border border-line bg-surface-2 px-2.5 py-2 lg:max-w-sm"><p className="text-micro font-semibold uppercase tracking-[.12em] text-ink-3">Technische Spur</p><p className="mt-1 break-all font-data tabular-nums text-xs text-ink-2">{finding.fileline}</p></div>
              </div>
              <span className="sr-only">{finding.evidence}</span>
              <Disclosure summary={<span className="font-semibold text-ink">Beleg anzeigen</span>} className="mt-3 rounded-card border border-line bg-surface-2 px-3 py-2 text-xs text-ink-2"><blockquote className="whitespace-pre-wrap rounded border border-line bg-surface-2 px-3 py-2 text-xs text-ink">{finding.evidence}</blockquote></Disclosure>
            </article>
          </StaggerItem>
        ))}
      </Stagger>
    </div>
  );
}

function deepAuditSeverityLabel(severity: DeepAuditFinding["severity"]): string {
  switch (severity) { case "critical": return "Kritisch"; case "high": return "Hoch"; case "medium": return "Mittel"; case "low": return "Niedrig"; }
}

function deepAuditCategoryLabel(category: string): string {
  switch (category) { case "bug_risk": return "Bug-Risiko"; case "security": return "Sicherheit"; case "contradiction": return "Widerspruch"; case "missing_section": return "Lücke"; case "operational_risk": return "Betriebsrisiko"; default: return category || "Audit"; }
}

function ChecklistPanel({ eyebrow, title, detail, label, tone, items, className, compact }: { eyebrow: string; title: string; detail: string; label: string; tone: ToneName; items: Array<{ label: string; value: string; detail: string; tone: ToneName }>; className: string; compact?: boolean }) {
  return (
    <div className={cn("rounded-panel border", reviewStepToneClass(tone), className)}>
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0"><Eyebrow>{eyebrow}</Eyebrow><Text as="h3" variant="label" className="mt-1 text-ink">{title}</Text><p className="mt-1 text-xs leading-5 text-ink-2">{detail}</p></div>
        <SignalChip tone={signalToneFromLegacy(tone)} label={label} />
      </div>
      <div className={cn("grid", compact ? "gap-1.5" : "gap-2")}>
        {items.map((item) => (
          <div key={item.label} className={cn("rounded-card border border-line bg-surface-2", compact ? "px-2 py-1.5" : "px-2.5 py-2")}>
            <div className="flex items-center justify-between gap-2"><p className={cn("font-semibold uppercase tracking-[.12em] text-ink-3", compact ? "text-micro" : "text-micro")}>{item.label}</p><SignalChip tone={signalToneFromLegacy(item.tone)} label={item.value} /></div>
            <p className="mt-1 text-xs leading-5 text-ink-2">{item.detail}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Ton→LED, damit Severity-/Status-Kacheln (z. B. Deep-Audit critical/high)
 *  ihre Scan-Marke behalten — Wort bleibt der Träger, der Punkt ist Beiwerk;
 *  neutrale/informationale Töne bleiben bewusst punktlos (kein LED-Rauschen). */
function factDot(tone: ToneName): "ready" | "warn" | "error" | undefined {
  if (tone === "emerald") return "ready";
  if (tone === "amber") return "warn";
  if (tone === "red" || tone === "rose") return "error";
  return undefined;
}

function FactTile({ label, value, tone, compact, title }: { label: string; value: string; tone: ToneName; compact?: boolean; title?: string }) {
  return <KpiTile label={label} value={<span title={title}>{value}</span>} dot={factDot(tone)} className={compact ? "px-2 py-1.5" : "px-3 py-2"} />;
}
