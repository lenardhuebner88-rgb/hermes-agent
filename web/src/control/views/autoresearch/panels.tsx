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
import { StatusPill } from "../../components/atoms";
import { Card, Disclosure, SkeletonCard, Stat, Stagger, StaggerItem, Text } from "../../components/primitives";
import type { AutoresearchActionHint } from "../../lib/autoresearchActionPlan";
import { reviewStepToneClass } from "./panels.helpers";

export function Metric({ label, value }: { label: string; value: string }) {
  return <Stat label={label} value={value} />;
}

export function OperatorActionCard({ icon, eyebrow, hint, title, body, button }: { icon: ReactNode; eyebrow: string; hint: AutoresearchActionHint; title: string; body: string; button: ReactNode }) {
  return (
    <Card surface="card" className="flex min-h-[188px] flex-col justify-between p-3">
      <div>
        <div className="mb-3 flex items-center justify-between gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-md border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]">
            {icon}
          </span>
          <span className="flex flex-wrap justify-end gap-1.5">
            <span className="rounded-full border border-white/10 px-2 py-0.5 text-[11px] font-medium hc-soft">{eyebrow}</span>
            <StatusPill tone={hint.tone} label={hint.label} />
          </span>
        </div>
        <Text as="h3" variant="label" className="text-white">{title}</Text>
        <p className="mt-1 text-xs leading-5 hc-soft">{body}</p>
        <div className="mt-3 rounded-md border border-white/10 bg-black/20 px-2 py-1.5 text-xs leading-5 hc-soft">
          <p><span className="font-semibold text-white">Warum:</span> {hint.reason}</p>
          <p className="mt-1"><span className="font-semibold text-white">Danach:</span> {hint.after}</p>
        </div>
      </div>
      <div className="mt-3">{button}</div>
    </Card>
  );
}

export function ReadinessPanel({ summary }: { summary: AutoresearchReadinessSummary }) {
  return (
    <section className={cn("rounded-lg border p-3", reviewStepToneClass(summary.tone))} aria-label="Autoresearch Betriebsstatus">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="hc-eyebrow">Betriebsstatus</p>
            <StatusPill tone={summary.tone} label={summary.label} />
          </div>
          <Text as="h2" variant="subtitle" className="mt-2 text-white">{summary.title}</Text>
          <p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">{summary.detail}</p>
          <p className="mt-2 text-sm text-white"><span className="font-semibold">Jetzt sinnvoll:</span> {summary.next}</p>
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
    <section className={cn("rounded-lg border p-3", reviewStepToneClass(summary.tone))} aria-label="Autoresearch Abschluss und Aufräumen">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="hc-eyebrow">Abschluss & Aufräumen</p>
            <StatusPill tone={summary.tone} label={summary.label} />
          </div>
          <Text as="h2" variant="subtitle" className="mt-2 text-white">{summary.title}</Text>
          <p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">{summary.detail}</p>
          <p className="mt-2 text-sm text-white"><span className="font-semibold">Jetzt sinnvoll:</span> {summary.next}</p>
        </div>
        <div className="grid shrink-0 gap-2 sm:grid-cols-3 lg:min-w-[360px]">
          {summary.facts.map((fact) => <FactTile key={fact.label} label={fact.label} value={fact.value} tone={fact.tone} />)}
          {summary.archiveLabel ? (
            <Button outlined className="hc-hit sm:col-span-3" onClick={onArchiveReverted} disabled={archiveDisabled} prefix={archiveBusy ? <Spinner /> : <Archive className="h-4 w-4" />}>
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
        <button key={item.id} type="button" onClick={() => onJump(item.id)} className="hc-hit rounded-lg border border-white/10 bg-white/[.03] px-3 py-2 text-left transition hover:border-[var(--hc-accent-border)] hover:bg-[var(--hc-accent-wash)]">
          <span className="flex items-center gap-2 text-sm font-semibold text-white">{iconFor(item.kind)}{item.label}</span>
          <span className="mt-0.5 block text-xs leading-5 hc-soft">{item.detail}</span>
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
    <section className="rounded-lg border border-white/10 bg-white/[.025] p-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="hc-eyebrow">Vor Erweitert</p>
          <Text as="h2" variant="subtitle" className="text-white">Nur öffnen, wenn der normale Probelauf nicht reicht.</Text>
        </div>
        <StatusPill tone="zinc" label="Optional" />
      </div>
      <Stagger className="grid gap-3 lg:grid-cols-3">
        {items.map((item) => (
          <StaggerItem key={item.kind}>
            <article className={cn("rounded-lg border p-3", reviewStepToneClass(item.tone))}>
              <div className="flex items-start justify-between gap-3">
                <span className="grid h-8 w-8 shrink-0 place-items-center rounded-md border border-white/10 bg-black/20 text-white">{iconFor(item.kind)}</span>
                <StatusPill tone={item.tone} label={item.label} />
              </div>
              <Text as="h3" variant="label" className="mt-3 text-white">{item.title}</Text>
              <div className="mt-2 space-y-1 text-xs leading-5 hc-soft">
                <p><span className="font-semibold text-white">Wann:</span> {item.when}</p>
                <p><span className="font-semibold text-white">Aufwand:</span> {item.cost}</p>
                <p><span className="font-semibold text-white">Sicherheit:</span> {item.safety}</p>
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
    <article className={cn("rounded-lg border px-3 py-2", reviewStepToneClass(card.tone))}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="hc-mono text-xs hc-dim">{fmtClock(at)}</span>
            <StatusPill tone={card.tone} label={card.label} />
          </div>
          <Text as="h3" variant="label" className="mt-1 text-white">{card.title}</Text>
          <p className="mt-1 text-sm leading-6 hc-soft">{card.detail}</p>
        </div>
        <p className="max-w-sm text-xs leading-5 hc-dim"><span className="font-semibold text-white/80">Danach:</span> {card.next}</p>
      </div>
    </article>
  );
}

export function LatestActivityPanel({ at, card }: { at: number; card: AutoresearchActivityCard }) {
  return (
    <section className={cn("rounded-lg border p-3", reviewStepToneClass(card.tone))} aria-label="Letzte Autoresearch Aktion">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="hc-eyebrow">Letzte Aktion</p>
            <span className="hc-mono text-xs hc-dim">{fmtClock(at)}</span>
            <StatusPill tone={card.tone} label={card.label} />
          </div>
          <Text as="h2" variant="label" className="mt-2 text-white">{card.title}</Text>
          <p className="mt-1 text-sm leading-6 hc-soft">{card.detail}</p>
        </div>
        <p className="rounded-md border border-white/10 bg-black/20 px-3 py-2 text-xs leading-5 hc-soft sm:max-w-sm"><span className="font-semibold text-white">Jetzt sinnvoll:</span> {card.next}</p>
      </div>
    </section>
  );
}

export function ReviewFlowPanel({ flow, busy, onPrimary }: { flow: AutoresearchReviewFlow; busy: boolean; onPrimary: () => void }) {
  const icon = flow.primaryAction === "confirm-selection" ? <CheckCheck className="h-4 w-4" /> : flow.primaryAction === "select-visible" ? <ListChecks className="h-4 w-4" /> : flow.primaryAction === "clear-selection" ? <X className="h-4 w-4" /> : flow.primaryAction === "archive-reverted" ? <Archive className="h-4 w-4" /> : flow.primaryAction === "generate" ? <Sparkles className="h-4 w-4" /> : <ClipboardCheck className="h-4 w-4" />;

  return (
    <div className="rounded-lg border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] p-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="hc-eyebrow text-[var(--hc-accent-text)]">Review-Flow</p>
            <StatusPill tone={flow.tone} label={flow.progressLabel} />
          </div>
          <Text as="h3" variant="subtitle" className="mt-2 text-white">{flow.title}</Text>
          <p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">{flow.detail}</p>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-black/30"><div className="h-full rounded-full bg-[var(--hc-accent)]" style={{ width: `${flow.progressPercent}%` }} /></div>
        </div>
        <div className="grid shrink-0 gap-2 sm:grid-cols-3 lg:min-w-[360px]">
          {flow.steps.map((step) => <FactTile key={step.label} label={step.label} value={step.value} tone={step.tone} />)}
          <Button className="hc-hit sm:col-span-3" onClick={onPrimary} disabled={busy} prefix={busy ? <Spinner /> : icon}>{flow.primaryLabel}</Button>
        </div>
      </div>
    </div>
  );
}

export function QueueActionSummaryPanel({ summary }: { summary: AutoresearchQueueActionSummary }) {
  return (
    <div className={cn("max-w-xl rounded-lg border p-3 text-left", reviewStepToneClass(summary.tone))}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="hc-eyebrow">Auswahlwirkung</p>
          <Text as="h3" variant="label" className="mt-1 text-white">{summary.title}</Text>
          <div className="mt-2 space-y-1 text-xs leading-5 hc-soft"><p>{summary.batchLine}</p><p>{summary.manualLine}</p><p className="text-white/90">{summary.confirmLine}</p></div>
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
    <div className={cn("sticky bottom-[calc(4.75rem+env(safe-area-inset-bottom,0px))] z-30 rounded-lg border p-3 shadow-2xl shadow-black/40 backdrop-blur lg:bottom-3", reviewStepToneClass(summary.tone))}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2"><p className="hc-eyebrow">Auswahl bereit</p><StatusPill tone={summary.tone} label={`${selectedCount} markiert`} /></div>
          <Text as="h3" variant="label" className="mt-1 text-white">{summary.title}</Text>
          <p className="mt-1 max-w-3xl text-xs leading-5 hc-soft">{summary.confirmLine}</p>
        </div>
        <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
          <Button outlined className="hc-hit justify-center" onClick={onClear} disabled={busy} prefix={<X className="h-4 w-4" />}>Auswahl leeren</Button>
          <Button className="hc-hit justify-center" onClick={onConfirm} disabled={!canConfirm} title={canConfirm ? "Übernimmt nur die markierten sammelsicheren Karten." : "Riskante Auswahl erst leeren oder einzeln prüfen."} prefix={busy ? <Spinner /> : <CheckCheck className="h-4 w-4" />}>Auswahl übernehmen</Button>
        </div>
      </div>
    </div>
  );
}

export function QueueModePicker({ summary, activeMode, onChange }: { summary: ReturnType<typeof getAutoresearchQueueModeSummary>; activeMode: AutoresearchQueueMode; onChange: (mode: AutoresearchQueueMode) => void }) {
  return (
    <div className="max-w-3xl rounded-lg border border-white/10 bg-white/[.025] p-2 text-left">
      <div className="grid gap-1.5 sm:grid-cols-4">
        {summary.options.map((option) => {
          const active = option.id === activeMode;
          return (
            <button key={option.id} type="button" onClick={() => onChange(option.id)} aria-pressed={active} className={cn("hc-hit rounded-md border px-2 py-1.5 text-left transition", active ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]" : "border-white/10 bg-black/20 hover:bg-white/[.04]")}>
              <span className="flex items-center justify-between gap-2"><span className="text-xs font-semibold text-white">{option.label}</span><StatusPill tone={option.tone} label={String(option.count)} /></span>
            </button>
          );
        })}
      </div>
      <p className="mt-2 text-xs leading-5 hc-soft"><span className="font-semibold text-white">{summary.active.label}:</span> {summary.active.detail}</p>
    </div>
  );
}

export function EmptyQueueModePanel({ guidance, onChangeMode }: { guidance: AutoresearchEmptyQueueModeGuidance; onChangeMode: (mode: AutoresearchQueueMode) => void }) {
  return (
    <div className={cn("rounded-lg border p-3", reviewStepToneClass(guidance.tone))}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2"><p className="hc-eyebrow">Filter leer</p><StatusPill tone={guidance.tone} label={guidance.label} /></div>
          <Text as="h3" variant="subtitle" className="mt-2 text-white">{guidance.title}</Text>
          <p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">{guidance.detail}</p>
        </div>
        <Button outlined className="hc-hit shrink-0 justify-center" onClick={() => onChangeMode(guidance.primaryMode)} prefix={<ListChecks className="h-4 w-4" />}>{guidance.primaryLabel}</Button>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-4">{guidance.facts.map((fact) => <FactTile key={fact.label} label={fact.label} value={fact.value} tone={fact.tone} />)}</div>
    </div>
  );
}

export function DecisionGuidePanel({ guide }: { guide: AutoresearchDecisionGuide }) {
  const icon = guide.tone === "emerald" ? <ShieldCheck className="h-4 w-4" /> : guide.tone === "cyan" ? <ClipboardCheck className="h-4 w-4" /> : guide.tone === "amber" ? <Target className="h-4 w-4" /> : <X className="h-4 w-4" />;
  return (
    <div className={cn("rounded-lg border p-3", reviewStepToneClass(guide.tone))}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2"><span className="grid h-8 w-8 place-items-center rounded-md border border-white/10 bg-black/20 text-white">{icon}</span><p className="hc-eyebrow">Heute tun</p><StatusPill tone={guide.tone} label={guide.primaryLabel} /></div>
          <Text as="h3" variant="subtitle" className="mt-2 text-white">{guide.headline}</Text>
          <p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">{guide.summary}</p>
          <p className="mt-2 text-sm text-white"><span className="font-semibold">Nächster sicherer Schritt:</span> {guide.next}</p>
        </div>
        <div className="grid shrink-0 gap-2 sm:grid-cols-3 lg:min-w-[360px]">{guide.facts.map((fact) => <FactTile key={fact.label} label={fact.label} value={fact.value} tone={fact.tone} />)}</div>
      </div>
    </div>
  );
}

export function RunGuidanceCard({ guidance }: { guidance: AutoresearchRunGuidance }) {
  return (
    <div className="rounded-lg border border-white/10 bg-black/20 p-3">
      <div className="mb-2 flex items-center justify-between gap-2"><p className="hc-eyebrow">Vor dem Start</p><StatusPill tone={guidance.tone} label={guidance.label} /></div>
      <div className="grid gap-2 text-xs leading-5 hc-soft"><p><span className="font-semibold text-white">Wofür:</span> {guidance.outcome}</p><p><span className="font-semibold text-white">Kosten:</span> {guidance.cost}</p><p><span className="font-semibold text-white">Sicherheit:</span> {guidance.safety}</p></div>
    </div>
  );
}

export function LoopPresetPicker({ selectedId, disabled, onSelect }: { selectedId: ResearchLoopPresetId | null; disabled: boolean; onSelect: (presetId: ResearchLoopPresetId) => void }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2"><div><p className="hc-eyebrow">Start-Preset</p><p className="mt-0.5 text-xs hc-soft">Wähle zuerst den Zweck, nicht die Technik.</p></div><StatusPill tone={selectedId ? "emerald" : "amber"} label={selectedId ? "Preset aktiv" : "Eigene Werte"} /></div>
      <div className="grid gap-2">
        {RESEARCH_LOOP_PRESETS.map((preset) => {
          const selected = preset.id === selectedId;
          return (
            <button key={preset.id} type="button" onClick={() => onSelect(preset.id)} disabled={disabled} title={preset.title} aria-label={`${preset.label}: ${preset.summary} ${preset.cost}.`} aria-pressed={selected} className={cn("hc-hit min-h-[118px] rounded-lg border px-3 py-2.5 text-left transition disabled:cursor-not-allowed disabled:opacity-60", selected ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]" : "border-white/10 bg-black/20 hover:bg-white/[.04]")}>
              <span className="flex items-start justify-between gap-3"><span className="min-w-0"><span className="block text-sm font-semibold text-white">{preset.label}</span><span className="mt-0.5 block text-xs font-medium text-white/85">{preset.operatorTitle}</span></span><span className={cn("shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium", selected ? "border-[var(--hc-accent-border)] text-[var(--hc-accent-text)]" : "border-white/10 hc-soft")}>{preset.badge}</span></span>
              <span className="mt-2 block text-xs leading-5 hc-soft">{preset.operatorFit}</span><span className="mt-1 block text-xs leading-5 hc-dim">{preset.operatorResult}</span><span className="mt-2 block hc-mono text-[11px] hc-dim">{preset.cost}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}


export function TargetingPreview({ summary }: { summary: ResearchLoopStartSummary }) {
  return (
    <div className="rounded-lg border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 py-2 text-xs text-[var(--hc-accent-text)]">
      <div className="flex items-start gap-2"><Target className="mt-0.5 h-3.5 w-3.5 shrink-0" /><div className="min-w-0"><p className="font-semibold">{summary.title}</p><p className="mt-1.5 leading-5">{summary.scope}</p><p className="mt-0.5 leading-5">{summary.detail}</p><p className="mt-1.5 leading-5"><span className="font-semibold">Aufwand:</span> {summary.cost}</p><p className="mt-0.5 leading-5"><span className="font-semibold">Sicherheit:</span> {summary.safety}</p><p className="mt-1 hc-mono text-[11px] opacity-75">{summary.technicalLabel}</p></div></div>
    </div>
  );
}

export function StartChecklistPanel({ checklist }: { checklist: ResearchLoopStartChecklist }) {
  return <ChecklistPanel eyebrow="Start-Check" title={checklist.title} detail={checklist.detail} label={checklist.label} tone={checklist.tone} items={checklist.items} className="p-3" />;
}

export function AdvancedRunChecklistPanel({ checklist }: { checklist: AutoresearchAdvancedRunChecklist }) {
  return <ChecklistPanel eyebrow="Start-Check" title={checklist.title} detail={checklist.detail} label={checklist.label} tone={checklist.tone} items={checklist.items} className="p-2.5" compact />;
}

export function TestFoundryResultPanel({ summary, raw }: { summary: TestFoundryResultSummary; raw: unknown }) {
  const rawPayload = formatLastRunRawPayload(raw);
  return (
    <div className={cn("rounded-lg border p-3", reviewStepToneClass(summary.tone))}>
      <div className="flex flex-col gap-3">
        <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><p className="hc-eyebrow">Letzter Test-Foundry-Lauf</p><StatusPill tone={summary.tone} label={summary.label} /></div><Text as="h3" variant="label" className="mt-2 text-white">{summary.title}</Text><p className="mt-1 text-sm leading-6 hc-soft">{summary.detail}</p><p className="mt-2 text-sm text-white"><span className="font-semibold">Jetzt sinnvoll:</span> {summary.next}</p></div>
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">{summary.facts.map((fact) => <FactTile key={fact.label} label={fact.label} value={fact.value} tone={fact.tone} compact title={fact.value} />)}</div>
      </div>
      {rawPayload ? <Disclosure summary={<span className="font-semibold text-white">{summary.rawLabel}</span>} className="mt-3 rounded-md border border-white/10 bg-black/20 px-3 py-2 text-xs hc-soft"><pre className="max-h-40 overflow-auto rounded-md border border-white/10 bg-black/30 p-2 hc-mono text-[11px] leading-5 text-white/80">{rawPayload}</pre></Disclosure> : null}
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
    <div className={cn("rounded-lg border p-3", reviewStepToneClass(brief.tone))}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><p className="hc-eyebrow">Letzter Lauf</p><StatusPill tone={brief.tone} label={brief.label} /></div><Text as="h3" variant="label" className="mt-2 text-white">{brief.title}</Text><p className="mt-1 text-sm leading-6 hc-soft">{brief.detail}</p><p className="mt-2 text-sm text-white"><span className="font-semibold">Nächster Schritt:</span> {brief.next}</p></div>
        <div className="grid shrink-0 grid-cols-2 gap-1.5 sm:grid-cols-4 lg:min-w-[360px]">{brief.facts.map((fact) => <FactTile key={fact.label} label={fact.label} value={fact.value} tone={fact.tone} compact />)}</div>
      </div>
      {brief.rawLine || rawPayload || proposed !== null || kept !== null || reverted !== null || showCounters || stopped || receipt || note ? (
        <Disclosure summary={<span className="font-semibold text-white">Technische Details</span>} className="mt-3 rounded-md border border-white/10 bg-black/20 px-3 py-2 text-xs hc-soft">
          {brief.rawLine ? <p><span className="text-white">Rohstatus:</span> {brief.rawLine}</p> : null}
          {rawPayload ? <pre className="mt-2 max-h-40 overflow-auto rounded-md border border-white/10 bg-black/30 p-2 hc-mono text-[11px] leading-5 text-white/80">{rawPayload}</pre> : null}
          {proposed !== null || kept !== null || reverted !== null ? <p className="mt-1 hc-mono">proposed={proposed ?? "?"} · übernommen={kept ?? "?"} · zurückgerollt={reverted ?? "?"}</p> : null}
          {showCounters ? <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 hc-mono"><span>{de.autoresearch.skillsResearched}={counters.skillsResearched ?? "?"} · {de.autoresearch.researchErrors}={counters.researchErrors ?? "?"} · {de.autoresearch.skillsWithFindings}={counters.skillsWithFindings ?? "?"}</span>{showErrorBadge ? <span className="rounded-full border border-red-500/40 bg-red-500/15 px-2 py-0.5 text-xs text-red-200">{de.autoresearch.researchErrorBadge}</span> : null}</p> : null}
          {showCounters ? <p className="mt-1 hc-mono">{de.autoresearch.researchTokens}: {formatResearchTokens(counters.researchTokens)}</p> : null}
          {showCounters ? <p className="mt-1 text-xs hc-dim">{de.autoresearch.counterLegend}</p> : null}
          {stopped ? <p className="mt-1">Stop: {stopped}</p> : null}
          {receipt ? <p className="mt-1 truncate text-xs hc-dim" title={receipt}>Receipt: {receipt}</p> : null}
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
  return <Card surface="card" className="flex items-center gap-3 p-4 text-sm hc-soft">{icon}<span>{text}</span></Card>;
}

export function EmptySkeleton({ rows = 3 }: { rows?: number }) {
  return <SkeletonCard rows={rows} />;
}

export function DeepAuditFindings({ findings, proposals }: { findings: DeepAuditFinding[]; proposals: string[] }) {
  const proposalCardLabel = `${proposals.length} ${proposals.length === 1 ? "Karte" : "Karten"}`;
  if (findings.length === 0) {
    return (
      <div className="mt-4 rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm hc-soft">
        <div className="flex flex-wrap items-center gap-2"><span>Noch keine Deep-Audit-Findings.</span>{proposals.length > 0 ? <StatusPill tone="amber" label={proposalCardLabel} /> : null}</div>
        <p className="mt-1">{proposals.length > 0 ? "Der Lauf hat bereits Review-Karten gemeldet; die Detail-Findings sind in dieser Antwort nicht enthalten." : "Wenn der Lauf fertig ist, erscheinen hier die prüfbaren Risiken und die daraus erzeugten Review-Karten."}</p>
      </div>
    );
  }
  const severityCounts = findings.reduce<Record<DeepAuditFinding["severity"], number>>((counts, finding) => { counts[finding.severity] += 1; return counts; }, { critical: 0, high: 0, medium: 0, low: 0 });
  const topSeverity = (["critical", "high", "medium", "low"] as const).find((severity) => severityCounts[severity] > 0) ?? "low";
  const topFinding = findings.find((finding) => finding.severity === topSeverity) ?? findings[0];

  return (
    <div className="mt-4 space-y-3">
      <section className={cn("rounded-lg border p-3", reviewStepToneClass(severityTone(topSeverity)))}>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><p className="hc-eyebrow">Audit-Ergebnis</p><StatusPill tone={severityTone(topSeverity)} label={deepAuditSeverityLabel(topSeverity)} />{proposals.length > 0 ? <StatusPill tone="amber" label={proposalCardLabel} /> : null}</div><Text as="h3" variant="subtitle" className="mt-2 text-white">{findings.length === 1 ? "1 prüfbares Risiko gefunden." : `${findings.length} prüfbare Risiken gefunden.`}</Text><p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">Wichtigster Punkt: {topFinding.title}. Der Deep-Audit schreibt keinen Code; daraus entstehen nur Review-Karten.</p><p className="mt-2 text-sm text-white"><span className="font-semibold">Jetzt sinnvoll:</span> Review-Karten prüfen, dann nur belegte Fixes übernehmen.</p></div>
          <div className="grid shrink-0 grid-cols-2 gap-1.5 sm:grid-cols-4 lg:min-w-[360px]">{(["critical", "high", "medium", "low"] as const).map((severity) => <FactTile key={severity} label={deepAuditSeverityLabel(severity)} value={String(severityCounts[severity])} tone={severityTone(severity)} compact />)}</div>
        </div>
      </section>
      <Stagger className="grid gap-3">
        {findings.map((finding, index) => (
          <StaggerItem key={`${finding.fileline}-${index}`}>
            <article className={cn("rounded-lg border p-3", reviewStepToneClass(severityTone(finding.severity)))}>
              <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><StatusPill tone={severityTone(finding.severity)} label={deepAuditSeverityLabel(finding.severity)} /><StatusPill tone="cyan" label={deepAuditCategoryLabel(finding.category)} /></div><Text as="h3" variant="label" className="mt-2 text-white">{finding.title}</Text><p className="mt-1 text-sm leading-6 hc-soft">{finding.problem}</p><p className="mt-2 text-sm text-white"><span className="font-semibold">Fix-Hinweis:</span> {finding.fix_hint}</p></div>
                <div className="shrink-0 rounded-md border border-white/10 bg-black/20 px-2.5 py-2 lg:max-w-sm"><p className="text-[10px] font-semibold uppercase tracking-[.12em] hc-dim">Technische Spur</p><p className="mt-1 break-all hc-mono text-xs hc-soft">{finding.fileline}</p></div>
              </div>
              <span className="sr-only">{finding.evidence}</span>
              <Disclosure summary={<span className="font-semibold text-white">Evidence anzeigen</span>} className="mt-3 rounded-md border border-white/10 bg-black/20 px-3 py-2 text-xs hc-soft"><blockquote className="whitespace-pre-wrap rounded border border-white/10 bg-white/[.03] px-3 py-2 text-xs text-zinc-100">{finding.evidence}</blockquote></Disclosure>
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
    <div className={cn("rounded-lg border", reviewStepToneClass(tone), className)}>
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0"><p className="hc-eyebrow">{eyebrow}</p><Text as="h3" variant="label" className="mt-1 text-white">{title}</Text><p className="mt-1 text-xs leading-5 hc-soft">{detail}</p></div>
        <StatusPill tone={tone} label={label} />
      </div>
      <div className={cn("grid", compact ? "gap-1.5" : "gap-2")}>
        {items.map((item) => (
          <div key={item.label} className={cn("rounded-md border border-white/10 bg-black/20", compact ? "px-2 py-1.5" : "px-2.5 py-2")}>
            <div className="flex items-center justify-between gap-2"><p className={cn("font-semibold uppercase tracking-[.12em] hc-dim", compact ? "text-[9px]" : "text-[10px]")}>{item.label}</p><StatusPill tone={item.tone} label={item.value} /></div>
            <p className="mt-1 text-xs leading-5 hc-soft">{item.detail}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function FactTile({ label, value, tone, compact, title }: { label: string; value: string; tone: ToneName; compact?: boolean; title?: string }) {
  return (
    <div className={cn("rounded-md border", compact ? "px-2 py-1.5" : "px-3 py-2", reviewStepToneClass(tone))}>
      <p className={cn("font-semibold uppercase tracking-[.12em] hc-dim", compact ? "text-[9px]" : "text-[10px]")}>{label}</p>
      <p className="mt-1 truncate text-sm font-semibold text-white" title={title}>{value}</p>
    </div>
  );
}
