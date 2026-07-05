import { useState } from "react";
import { Activity, AlertTriangle, Bot, Check, ClipboardCopy, FlaskConical, Inbox } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { useAutoresearchStatus, useDecisionInbox, useHermesWorkers, useMetricsLite, useSystemHealth } from "../hooks/useControlData";
import { isActionable } from "../lib/autoresearch";
import { buildOverview, freshness, nowSec, workerHealth } from "../lib/derive";
import { de } from "../i18n/de";
import type { ToneName, Proposal } from "../lib/types";
import { StatusPill } from "../components/atoms";
import { SystemHealthStrip } from "../components/SystemHealthStrip";

import { FleetPod, FleetEmptyState } from "../components/fleet/atoms";
import { toneClasses } from "../lib/tones";
import { Hero } from "../components/Hero";
import { Card, Disclosure, Panel, Skeleton, SkeletonCard, Stat, Text } from "../components/primitives";

type Focus = "hermes" | "proposals" | "warnings" | null;

// OpenClaw (Mission Control, :3000) wurde 2026-06-01 abgeschaltet — die Übersicht
// zeigt nur noch die lebenden Systeme (Hermes + Autoresearch). Die frei gewordene
// Kachel ist jetzt der Autoresearch-Loop-Status.
const arStateLabel: Record<"idle" | "running" | "stopping" | "crashed", string> = {
  idle: "Inaktiv", running: "Läuft", stopping: "Stoppt", crashed: "Abgestürzt",
};

interface Props {
  proposals: Proposal[];
  proposalsLoading?: boolean;
  proposalsError?: string | null;
  proposalsLastUpdated?: number | null;
}

export function OverviewView({ proposals, proposalsLoading, proposalsError, proposalsLastUpdated }: Props) {
  const navigate = useNavigate();
  const workers = useHermesWorkers();
  const health = useSystemHealth();
  const metrics = useMetricsLite();
  const autoresearch = useAutoresearchStatus();
  const now = nowSec();
  const overview = buildOverview(workers.data?.workers ?? [], [], proposals, now);
  const [focus, setFocus] = useState<Focus>(null);
  const [copied, setCopied] = useState(false);

  // The SAME deduped decision inbox the Postfach landing uses (useDecisionInbox)
  // — one source of truth for "Was braucht mich?", so the Übersicht count can't
  // drift from the inbox. "Ruhig" only when nothing waits AND workers are healthy.
  const inboxCounts = useDecisionInbox().summary;

  const allCalm = overview.allHealthy && inboxCounts.total === 0;
  const title = allCalm ? de.overview.healthyTitle : de.overview.warnTitle(overview.warnings.length + inboxCounts.total);
  const proposalValue = proposalsError ? "Nicht geladen" : proposalsLoading && proposals.length === 0 ? "unbekannt" : String(overview.openProposals);

  const arState = autoresearch.data?.state ?? "idle";
  const arValue = arState === "running" ? `Iter ${autoresearch.data?.iteration ?? 0}` : arStateLabel[arState];

  // E1: per-source freshness. usePolling pausiert bei document.hidden → "stale"
  // ist hier eine Warnung (pausiert/veraltet), kein Fehler, kein stilles 0.
  const sources = [
    { label: de.overview.sourceHermes, fresh: freshness(workers.lastUpdated, 5000, now), err: workers.error },
    { label: de.overview.sourceAutoresearch, fresh: freshness(proposalsLastUpdated ?? null, 6000, now), err: proposalsError },
  ];

  const openProposals = proposals.filter(isActionable);
  // Nach dem OpenClaw-Rückbau enthält warnings nur noch Hermes-Einträge; flatMap
  // engt den Union-Typ auf die hermes-Variante ein (mit .worker/.health).
  const hermesWarnings = overview.warnings.flatMap((w) => w.kind === "hermes" ? [w] : []);

  // Initiale Lade-Phase je Quelle: erst füllen, wenn noch keine Daten da sind —
  // ersetzt das Leer→Voll-Flackern durch ruhige Skeletons (Verhalten unverändert).
  const healthLoading = health.loading && !health.data;
  const workersLoading = workers.loading && !workers.data;
  const proposalsInitialLoading = Boolean(proposalsLoading) && proposals.length === 0 && !proposalsError;

  const toggle = (next: Focus) => setFocus((cur) => (cur === next ? null : next));

  const copyDiagnostics = async () => {
    const lines = [
      `Hermes Control — Diagnose (${freshness(now, 0, now).label})`,
      `${de.overview.sourceHermes}: ${overview.hermesRunning}/${overview.hermesTotal} laufen · ${sources[0].fresh.stale ? "stale" : "frisch"} (${sources[0].fresh.label})`,
      `Autoresearch: ${arValue} · ${overview.openProposals} offen · ${sources[1].fresh.stale ? "stale" : "frisch"} (${sources[1].fresh.label})`,
      `Warnungen (${hermesWarnings.length}):`,
      ...hermesWarnings.map((w) => `- [hermes] ${w.worker.task_title} — ${workerHealth(w.worker, now).label}`),
    ];
    try {
      await navigator.clipboard.writeText(lines.join("\n"));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked (e.g. headless) — silent, non-critical */
    }
  };

  return (
    <div className="space-y-5">
      {healthLoading ? (
        <SkeletonCard rows={3} />
      ) : (
        <SystemHealthStrip data={health.data} error={health.error} now={now} metrics={metrics.data} />
      )}
      {/* Hero: ein-Satz-Lagebild + Diagnose-Kopie, auf dem geteilten Primitive. */}
      <Hero
        eyebrow={de.tabs.overview}
        title={title}
        subtitle="Hermes-Worker und Autoresearch laufen hier in einer Sicht zusammen."
        tone={allCalm ? "emerald" : "amber"}
        status={{ label: allCalm ? "Ruhig" : "Aufmerksamkeit", tone: allCalm ? "emerald" : "amber", dot: allCalm ? "live" : "warn" }}
        action={
          <Button outlined size="sm" onClick={copyDiagnostics} className="gap-2">{copied ? <Check className="h-4 w-4" /> : <ClipboardCopy className="h-4 w-4" />}{copied ? de.overview.copied : de.overview.copyDiagnostics}</Button>
        }
      />

      {inboxCounts.total > 0 ? (
        <button type="button" onClick={() => navigate("/control/inbox")} className="flex w-full items-center justify-between gap-3 rounded-lg border border-amber-500/30 bg-amber-500/[.07] px-4 py-3 text-left transition hover:bg-amber-500/[.12]">
          <span className="flex min-w-0 items-center gap-3">
            <Inbox className="h-5 w-5 shrink-0 text-amber-200" />
            <span className="min-w-0">
              <span className="block text-sm font-semibold text-white">{de.overview.inboxWaiting(inboxCounts.total)}</span>
              <span className="block truncate text-xs hc-soft">{de.overview.inboxBreakdown(inboxCounts.autoresearch, inboxCounts.family, inboxCounts.orchestrator)}</span>
            </span>
          </span>
          <span className="shrink-0 text-xs font-medium text-amber-200">{de.overview.inboxOpen} →</span>
        </button>
      ) : null}

      {/* E1: Datenfrische je Quelle */}
      <Panel eyebrow="Datenfrische" title={de.overview.sources}>
        <div className="grid gap-2 sm:grid-cols-2">
          {sources.map((s) => {
            // Conditional tone: Fehler oder stale → amber-Tönung; sonst neutral.
            // Bedingung unverändert ggü. der alten Inline-Logik, nur auf Stat tone= abgebildet.
            const toned: ToneName | undefined = s.err || s.fresh.stale ? "amber" : undefined;
            const hint = s.err ?? (s.fresh.stale ? de.overview.stalePaused : undefined);
            return (
              <Stat
                key={s.label}
                tone={toned}
                label={s.label}
                value={s.err ? "Nicht geladen" : s.fresh.stale ? de.overview.staleWarn(s.fresh.label.replace("vor ", "")) : s.fresh.label}
                hint={hint}
              />
            );
          })}
        </div>
      </Panel>

      {/* E2: Kacheln sind Drilldown-Toggles (Autoresearch-Kachel navigiert direkt) */}
      <Panel eyebrow="Systeme" title="Auf einen Blick">
        <div className="grid gap-3 md:grid-cols-4">
          <MetricTile icon={<Bot />} label="Hermes laufen" value={`${overview.hermesRunning}/${overview.hermesTotal}`} active={focus === "hermes"} loading={workersLoading} onClick={() => toggle("hermes")} />
          <MetricTile icon={<Activity />} label="Autoresearch" value={arValue} onClick={() => navigate("/control/autoresearch")} />
          <MetricTile icon={<FlaskConical />} label={de.overview.proposals} value={proposalValue} active={focus === "proposals"} loading={proposalsInitialLoading} onClick={() => toggle("proposals")} />
          <MetricTile icon={<AlertTriangle />} label={de.overview.warnings} value={String(hermesWarnings.length)} tone={hermesWarnings.length > 0 ? "amber" : undefined} active={focus === "warnings"} loading={workersLoading} onClick={() => toggle("warnings")} />
        </div>
      </Panel>

      {focus === "hermes" ? (
        <Drilldown
          id="dd-hermes"
          title="Hermes-Worker"
          tab="/control/flow"
          navigate={navigate}
          empty={de.overview.noProblemWorkers}
          loading={workersLoading}
          items={hermesWarnings.map((w) => ({ key: w.worker.run_id, label: w.worker.task_title, pill: <StatusPill tone={workerHealth(w.worker, now).tone} label={workerHealth(w.worker, now).label} dot={workerHealth(w.worker, now).dot} /> }))}
        />
      ) : null}
      {focus === "proposals" ? (
        <Drilldown
          id="dd-proposals"
          title={de.overview.proposals}
          tab="/control/autoresearch"
          navigate={navigate}
          empty={de.overview.noOpenProposals}
          loading={proposalsInitialLoading}
          items={openProposals.map((p) => ({ key: p.id, label: p.title ?? p.target, pill: <StatusPill tone={p.mode === "code" ? "violet" : "cyan"} label={p.mode === "code" ? "Code" : "Skill"} /> }))}
        />
      ) : null}

      {/* Default-Panel: Braucht-Aufmerksamkeit (auch sichtbar bei focus="warnings") */}
      {focus === null || focus === "warnings" ? (
        <Panel eyebrow={de.overview.warnings} title={de.overview.needsAttention}>
          {workersLoading ? (
            <div className="grid gap-3 lg:grid-cols-2"><Skeleton className="h-11 w-full" /><Skeleton className="h-11 w-full" /></div>
          ) : hermesWarnings.length === 0 ? (
            <FleetEmptyState ok title={de.overview.nothingUrgent} desc="Keine Hermes-Worker brauchen gerade Aufmerksamkeit." />
          ) : (
            <div className="grid gap-3 lg:grid-cols-2">{hermesWarnings.map((warning) => <button key={warning.worker.run_id} type="button" onClick={() => navigate("/control/flow")} className="hc-surface-card hc-hit flex min-h-11 w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm transition hover:border-white/15"><span className="min-w-0 truncate">{warning.worker.task_title}</span><StatusPill tone={workerHealth(warning.worker, now).tone} label={workerHealth(warning.worker, now).label} dot={workerHealth(warning.worker, now).dot} /></button>)}</div>
          )}
        </Panel>
      ) : null}
    </div>
  );
}

function Drilldown({ id, title, tab, navigate, items, empty, loading }: { id: string; title: string; tab: string; navigate: (path: string) => void; items: Array<{ key: string; label: string; pill: React.ReactNode }>; empty: string; loading?: boolean }) {
  // Animierter Drilldown: Disclosure (auto↔0 Höhe) statt hartem Conditional —
  // der Titel ist die Summary, die "Tab öffnen"-Aktion sitzt rechts daneben.
  return (
    <Card surface="card" className="p-4">
      <div className="flex items-start justify-between gap-3">
        <Disclosure
          id={id}
          defaultOpen
          className="flex-1"
          summary={<span className="hc-type-subtitle text-[var(--hc-text)]">{title}</span>}
        >
          {loading ? (
            <div className="space-y-2"><Skeleton className="h-10 w-full" /><Skeleton className="h-10 w-3/4" /></div>
          ) : items.length === 0 ? (
            <Text className="hc-soft">{empty}</Text>
          ) : (
            <div className="space-y-2">
              {items.map((it) => (
                <button key={it.key} type="button" onClick={() => navigate(tab)} className="flex w-full items-center justify-between rounded-lg border border-white/10 px-3 py-2 text-left text-sm hover:bg-white/5">
                  <span className="min-w-0 truncate">{it.label}</span>{it.pill}
                </button>
              ))}
            </div>
          )}
        </Disclosure>
        <Button ghost size="xs" className="shrink-0" onClick={() => navigate(tab)}>{de.overview.openTab}</Button>
      </div>
    </Card>
  );
}

function MetricTile({ icon, label, value, active, tone, loading, onClick }: { icon: React.ReactNode; label: string; value: string; active?: boolean; tone?: ToneName; loading?: boolean; onClick: () => void }) {
  // Conditional tone: a non-zero warnings/problem count tints the tile amber/red
  // so "good vs bad" is pre-attentive instead of read-and-reason. The condition
  // (caller-supplied tone) is unchanged — only the rendering now wraps a FleetPod
  // (bigger, calmer KPI) and maps the tone onto the shared wash (toneClasses) + the
  // pod's status dot. Stays a real <button> for keyboard + aria-pressed.
  const iconColor = tone === "red" ? "text-red-300" : tone === "amber" ? "text-amber-300" : "text-[var(--hc-accent-text)]";
  const dot = tone === "red" ? "error" : tone === "amber" ? "warn" : undefined;
  const podLabel = (
    <span className="flex items-center gap-1.5">
      <span aria-hidden className={iconColor}>{icon}</span>
      {label}
    </span>
  );
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "hc-hit text-left transition",
        tone ? cn("rounded-xl border", toneClasses(tone)) : "",
        active && "rounded-xl ring-1 ring-[var(--hc-accent-border)]",
      )}
    >
      {loading ? (
        <div className="hc-fleet-pod">
          <span className="block text-sm hc-soft">{podLabel}</span>
          <Skeleton className="mt-2 h-7 w-16" />
        </div>
      ) : (
        <FleetPod label={podLabel} value={value} dot={dot} />
      )}
    </button>
  );
}
