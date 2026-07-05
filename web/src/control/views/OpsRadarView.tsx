import { Bot, GitBranch, Radar, ShieldCheck, Wrench } from "lucide-react";
import { Led, StaleBadge, StatusPill, ToneCallout } from "../components/atoms";
import { Card, Panel, SkeletonCard, Stat, Text } from "../components/primitives";
import { StatusChip } from "../components/StatusChip";
import { useOperatorInventory } from "../hooks/useControlData";
import type { OperatorInventoryActor, OperatorInventoryResponse, OperatorInventoryWorktree, OperatorInventoryWorktreeState, ToneName } from "../lib/types";

function fmtNumber(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "-";
  return String(Math.round(value));
}

function fmtPercent(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "-";
  return `${Math.round(value)}%`;
}

function fmtMb(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "-";
  return value >= 1024 ? `${(value / 1024).toFixed(1)}GB` : `${Math.round(value)}MB`;
}

function fmtAge(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "-";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function stateTone(state: OperatorInventoryWorktreeState, orphaned: boolean): ToneName {
  if (orphaned) return "rose";
  if (state === "dirty" || state === "prunable") return "amber";
  if (state === "locked") return "cyan";
  if (state === "clean") return "emerald";
  return "zinc";
}

function leverDot(tone: ToneName) {
  if (tone === "red" || tone === "rose") return "error";
  if (tone === "amber") return "warn";
  if (tone === "emerald") return "ready";
  return "idle";
}

function worktreeRank(item: OperatorInventoryWorktree): number {
  if (item.orphaned) return 0;
  if (item.state === "dirty") return 1;
  if (item.state === "prunable") return 2;
  if (item.state === "locked") return 3;
  if (item.state === "unknown") return 4;
  return 5;
}

function WorktreeRow({ item }: { item: OperatorInventoryWorktree }) {
  const tone = stateTone(item.state, item.orphaned);
  return (
    <div className="grid min-w-0 gap-2 rounded-lg border border-white/10 bg-white/[.03] px-3 py-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <StatusPill tone={tone} label={item.orphaned ? "orphan" : item.state} dot={leverDot(tone)} />
          <span className="truncate text-sm font-medium text-white">{item.path_label}</span>
          <span className="rounded-full border border-white/10 px-2 py-0.5 text-xs hc-soft">{item.relation}</span>
        </div>
        <p className="mt-1 truncate hc-mono text-xs hc-soft">{item.branch}{item.task_hint ? ` - ${item.task_hint}` : ""}</p>
        {item.orphaned ? <p className="mt-1 text-xs text-rose-200">Kein aktiver Worker-Match fuer diesen Kanban-Worktree.</p> : null}
      </div>
      <div className="grid grid-cols-3 gap-2 lg:w-72">
        <Stat label="Dirty" value={item.status_checked ? fmtNumber(item.dirty_count) : "?"} tone={item.dirty_count ? "amber" : "zinc"} />
        <Stat label="Untracked" value={item.status_checked ? fmtNumber(item.untracked_count) : "?"} />
        <Stat label="Lock" value={item.locked ? "ja" : "nein"} tone={item.locked ? "cyan" : "zinc"} />
      </div>
    </div>
  );
}

function ActorRow({ actor }: { actor: OperatorInventoryActor }) {
  const tone: ToneName = actor.stale_count ? "red" : actor.source === "canonical" ? "emerald" : "cyan";
  return (
    <div className="grid min-w-0 gap-2 rounded-lg border border-white/10 bg-white/[.03] px-3 py-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Led kind={actor.stale_count ? "error" : actor.source === "canonical" ? "ready" : "idle"} />
          <span className="truncate text-sm font-medium text-white">{actor.label}</span>
          <span className="rounded-full border border-white/10 px-2 py-0.5 text-xs hc-soft">{actor.source === "canonical" ? "kanonisch" : "prozess"}</span>
          <span className="rounded-full border border-white/10 px-2 py-0.5 text-xs hc-soft">read-only</span>
        </div>
        <p className="mt-1 truncate text-xs hc-soft">{actor.confidence} confidence{actor.stale_count ? ` - ${actor.stale_count} stale` : ""}</p>
      </div>
      <div className="grid grid-cols-4 gap-2 lg:w-80">
        <Stat label="Anzahl" value={actor.count} tone={tone} />
        <Stat label="CPU" value={fmtPercent(actor.cpu_percent)} />
        <Stat label="RAM" value={fmtMb(actor.rss_mb)} />
        <Stat label="Aeltester" value={fmtAge(actor.oldest_age_seconds)} />
      </div>
    </div>
  );
}

export function OpsRadarContent({ data, lastUpdated, isStale, error, embedded }: {
  data: OperatorInventoryResponse | null;
  lastUpdated: number | null;
  isStale?: boolean;
  error?: string | null;
  /** In der System-Fusion (S1) trägt der geteilte Kopf die Status-Zeile + Hebel —
   *  eingebettet bleiben nur die Sektionen Worktrees + Akteure. */
  embedded?: boolean;
}) {
  if (!data) {
    return (
      <div className="space-y-4">
        {error ? <ToneCallout tone="amber">Ops Radar konnte nicht geladen werden: {error}</ToneCallout> : null}
        <SkeletonCard rows={6} />
      </div>
    );
  }

  const summary = data.summary;
  const mismatchCount = summary.worktrees_dirty + summary.worktrees_orphaned + summary.worktrees_status_unknown;
  const worktrees = [...data.worktrees].sort((a, b) => worktreeRank(a) - worktreeRank(b) || a.path_label.localeCompare(b.path_label)).slice(0, 24);
  const actors = [...data.actors].sort((a, b) => (b.source === "canonical" ? 1 : 0) - (a.source === "canonical" ? 1 : 0) || b.count - a.count || a.label.localeCompare(b.label));
  const next = data.next_lever;

  return (
    <div className="space-y-4">
      {embedded ? null : (
      <>
      <Card surface="raised" tone={next.tone} className="overflow-hidden p-0" ariaLabel="Ops Radar">
        <div className="flex flex-col gap-3 border-b border-[var(--hc-border)] px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <p className="hc-eyebrow">Ops Radar</p>
            <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
              <StatusPill tone={next.tone} label={next.label} dot={leverDot(next.tone)} size="md" />
              <Text as="h2" variant="subtitle" className="truncate text-white">Worktrees und Akteure</Text>
            </div>
          </div>
          <StaleBadge isStale={isStale} lastUpdated={lastUpdated} />
        </div>

        <div className="grid grid-cols-2 gap-2 p-3 lg:grid-cols-4">
          <StatusChip icon={GitBranch} label="Worktrees" value={`${summary.worktrees_total} total`} hint={`${summary.worktrees_locked} locked - ${summary.worktrees_dirty} dirty`} tone={summary.worktrees_dirty || summary.worktrees_orphaned ? "amber" : "zinc"} />
          <StatusChip icon={Bot} label="Akteure" value={String(summary.actors_total)} hint={`${summary.actors_canonical} kanonisch`} tone={summary.actors_total ? "cyan" : "zinc"} />
          <StatusChip icon={Radar} label="Mismatch" value={String(mismatchCount)} hint={`${summary.worktrees_orphaned} orphan - ${summary.worktrees_status_unknown} unklar`} tone={mismatchCount ? "rose" : "emerald"} />
          <StatusChip icon={Wrench} label="Top-Hebel" value={next.label} hint={next.detail} tone={next.tone} />
        </div>
      </Card>

      <Panel title="Echte Hebel" eyebrow="read-only">
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          {data.levers.map((lever) => (
            <a key={lever.action} href={lever.target} className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-3 hover:bg-white/[.05]">
              <div className="flex items-center justify-between gap-3">
                <StatusPill tone={lever.tone} label={lever.label} dot={leverDot(lever.tone)} />
                <span className="hc-mono text-sm text-white">{lever.count}</span>
              </div>
              <p className="mt-2 line-clamp-2 text-sm hc-soft">{lever.detail}</p>
              <p className="mt-2 hc-type-label hc-dim">read-only - keine Runtime-Mutation</p>
            </a>
          ))}
        </div>
      </Panel>
      </>
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,.85fr)]">
        <Panel title="Worktree-Ledger" eyebrow="Git Inventar">
          {worktrees.length === 0 ? (
            <div className="flex min-h-20 items-center gap-3 rounded-lg border border-white/10 bg-white/[.03] px-3 py-3">
              <ShieldCheck className="h-5 w-5 text-emerald-300" />
              <div>
                <p className="text-sm font-medium text-white">Keine Worktrees gemeldet</p>
                <p className="text-xs hc-soft">Die Inventarquelle lieferte keine Worktree-Zeilen.</p>
              </div>
            </div>
          ) : (
            <div className="grid gap-2">{worktrees.map((item, idx) => <WorktreeRow key={`${item.id}:${item.branch}:${idx}`} item={item} />)}</div>
          )}
        </Panel>

        <Panel title="Actor Map" eyebrow="Worker, Agents, Daemons">
          {actors.length === 0 ? (
            <div className="flex min-h-20 items-center gap-3 rounded-lg border border-white/10 bg-white/[.03] px-3 py-3">
              <ShieldCheck className="h-5 w-5 text-emerald-300" />
              <div>
                <p className="text-sm font-medium text-white">Keine Akteure aktiv</p>
                <p className="text-xs hc-soft">Keine Worker-, Agent- oder Testprozesse erkannt.</p>
              </div>
            </div>
          ) : (
            <div className="grid gap-2">{actors.map((actor) => <ActorRow key={`${actor.role}-${actor.source}`} actor={actor} />)}</div>
          )}
        </Panel>
      </div>

      {data.errors.length > 0 ? (
        <ToneCallout tone="amber">Einige Inventarwerte konnten nicht gelesen werden. Die Anzeige bleibt read-only und zeigt keine Rohpfade oder Cmdlines.</ToneCallout>
      ) : null}
    </div>
  );
}

export function OpsRadarView() {
  const inventory = useOperatorInventory();
  return (
    <OpsRadarContent
      data={inventory.data}
      lastUpdated={inventory.lastUpdated}
      isStale={inventory.isStale}
      error={inventory.error}
    />
  );
}
