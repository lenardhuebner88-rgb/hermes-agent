import { useMemo } from "react";
import { GitBranch, RefreshCw } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { cn } from "@/lib/utils";
import { useBoard, useChainGraph } from "../hooks/useControlData";
import { fmtDur } from "../lib/derive";
import { buildChains } from "../lib/fleet";
import { taskStatusLabel } from "../lib/tones";
import type { BoardTask, ChainGraphNode, TaskStatus, ToneName } from "../lib/types";
import { StatusPill, ToneCallout } from "../components/atoms";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import { Eyebrow, SkeletonCard } from "../components/primitives";

function statusTone(status: TaskStatus): ToneName {
  if (status === "done") return "emerald";
  if (status === "running") return "amber";
  if (status === "review") return "cyan";
  if (status === "blocked") return "red";
  if (status === "ready") return "sky";
  if (status === "scheduled") return "violet";
  return "zinc";
}

function nodeRuntime(node: ChainGraphNode): string | null {
  const seconds = node.latest_run?.runtime_seconds ?? node.runtime_seconds;
  return seconds == null ? null : fmtDur(seconds);
}

function nodeHeartbeat(node: ChainGraphNode): string | null {
  const seconds = node.latest_run?.heartbeat_age_seconds;
  return seconds == null ? null : fmtDur(seconds);
}

function ChainNodeCard({ node, outgoing }: { node: ChainGraphNode; outgoing: string[] }) {
  const runtime = nodeRuntime(node);
  const heartbeat = nodeHeartbeat(node);
  return (
    <article className="min-w-0 rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-2.5">
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <span className="hc-mono min-w-0 max-w-full truncate hc-type-label hc-dim">{node.id}</span>
        <span className="ml-auto shrink-0"><StatusPill tone={statusTone(node.status)} label={taskStatusLabel[node.status] ?? node.status} /></span>
      </div>
      <h3 className="mt-1.5 line-clamp-2 text-sm font-semibold leading-snug text-white">{node.title}</h3>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {node.assignee ? <span className="rounded-full border border-white/10 px-2 py-0.5 hc-type-label hc-soft">{node.assignee}</span> : null}
        {runtime ? <span className="rounded-full border border-amber-400/25 bg-amber-400/10 px-2 py-0.5 hc-type-label text-amber-100">Run {runtime}</span> : null}
        {heartbeat ? <span className="rounded-full border border-cyan-400/25 bg-cyan-400/10 px-2 py-0.5 hc-type-label text-cyan-100">HB {heartbeat}</span> : null}
      </div>
      {outgoing.length ? (
        <div className="mt-2 flex flex-wrap gap-1">
          {outgoing.map((to) => (
            <span key={to} className="max-w-full rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-mono hc-type-label hc-dim">
              -&gt; {to}
            </span>
          ))}
        </div>
      ) : null}
    </article>
  );
}

export function ChainVizView(_props: { density?: unknown }) {
  const [params, setParams] = useSearchParams();
  const board = useBoard();
  const allTasks: BoardTask[] = useMemo(() => board.data?.columns.flatMap((c) => c.tasks) ?? [], [board.data]);
  const chainBoard = useMemo(() => buildChains(allTasks), [allTasks]);
  const options = useMemo(() => {
    const rows: Array<{ rootId: string; label: string; count: number; active: boolean }> = [];
    for (const chain of chainBoard.active) {
      rows.push({
        rootId: chain.rootId,
        label: chain.root?.title ?? chain.members[0]?.title ?? chain.rootId,
        count: chain.members.length,
        active: true,
      });
    }
    for (const chain of chainBoard.done.slice(0, 20)) {
      rows.push({
        rootId: chain.rootId,
        label: chain.root?.title ?? chain.members[0]?.title ?? chain.rootId,
        count: chain.members.length,
        active: false,
      });
    }
    for (const task of [...chainBoard.singles, ...chainBoard.doneSingles].slice(0, 20)) {
      rows.push({ rootId: task.id, label: task.title, count: 1, active: task.status !== "done" });
    }
    return rows;
  }, [chainBoard]);
  const requestedRoot = params.get("root")?.trim() || null;
  const selectedRoot = options.some((row) => row.rootId === requestedRoot)
    ? requestedRoot
    : options[0]?.rootId ?? null;
  const graph = useChainGraph(selectedRoot);
  const levels = useMemo(() => {
    const map = new Map<number, ChainGraphNode[]>();
    for (const node of graph.data?.nodes ?? []) {
      const list = map.get(node.level);
      if (list) list.push(node);
      else map.set(node.level, [node]);
    }
    return [...map.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([level, nodes]) => ({ level, nodes: nodes.sort((a, b) => a.id.localeCompare(b.id)) }));
  }, [graph.data]);
  const outgoingByNode = useMemo(() => {
    const out = new Map<string, string[]>();
    for (const edge of graph.data?.edges ?? []) {
      const list = out.get(edge.from);
      if (list) list.push(edge.to);
      else out.set(edge.from, [edge.to]);
    }
    return out;
  }, [graph.data]);

  return (
    <div className="space-y-4">
      <header className="flex min-w-0 flex-wrap items-center gap-3">
        <div className="min-w-0">
          <Eyebrow>Ketten-Viz</Eyebrow>
          <h2 className="mt-1 text-xl font-semibold tracking-normal text-white">DAG</h2>
        </div>
        <select
          value={selectedRoot ?? ""}
          onChange={(event) => setParams(event.target.value ? { root: event.target.value } : {}, { replace: true })}
          className="ml-auto min-h-10 min-w-0 max-w-full rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-3 text-sm text-white sm:max-w-md"
        >
          {options.map((row) => (
            <option key={row.rootId} value={row.rootId}>
              {row.active ? "live" : "done"} · {row.count} · {row.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={!selectedRoot || graph.loading}
          onClick={() => void graph.reload()}
          className="inline-flex min-h-10 items-center gap-1.5 rounded-md border border-[var(--hc-border-strong)] px-3 text-sm hc-soft disabled:opacity-40"
        >
          <RefreshCw className="h-4 w-4" />Reload
        </button>
      </header>

      {board.error ? <ToneCallout tone="red">{board.error}</ToneCallout> : null}
      {graph.error ? <ToneCallout tone="red">{graph.error}</ToneCallout> : null}

      {!selectedRoot && !board.loading ? (
        <FleetEmptyState title="Keine Ketten" desc="Noch keine Root-Ketten im aktuellen Board." />
      ) : graph.loading && graph.data == null ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"><SkeletonCard rows={4} /><SkeletonCard rows={4} /><SkeletonCard rows={4} /></div>
      ) : (
        <FleetPanel eyebrow="Live-DAG" meta={`${graph.data?.nodes.length ?? 0} Nodes · ${graph.data?.edges.length ?? 0} Edges`}>
          <div className="overflow-x-auto pb-2 [scrollbar-width:thin]">
            <div
              className="grid items-start gap-3"
              style={{
                minWidth: `${Math.max(1, levels.length) * 230}px`,
                gridTemplateColumns: `repeat(${Math.max(1, levels.length)}, minmax(210px, 1fr))`,
              }}
            >
              {levels.map((level, index) => (
                <section key={level.level} className="min-w-0">
                  <div className="mb-2 flex items-center gap-2">
                    <span className={cn("inline-flex h-7 w-7 items-center justify-center rounded-full border hc-mono hc-type-label", index === levels.length - 1 ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]" : "border-[var(--hc-border)] hc-soft")}>{level.level}</span>
                    <span className="hc-type-label hc-dim">{index === levels.length - 1 ? "Root" : "Level"}</span>
                  </div>
                  <div className="space-y-2">
                    {level.nodes.map((node) => (
                      <ChainNodeCard key={node.id} node={node} outgoing={outgoingByNode.get(node.id) ?? []} />
                    ))}
                  </div>
                </section>
              ))}
            </div>
          </div>
          {graph.data?.edges.length ? (
            <div className="mt-3 flex flex-wrap gap-1.5">
              <GitBranch className="h-4 w-4 text-[var(--hc-accent-text)]" />
              {graph.data.edges.map((edge) => (
                <span key={`${edge.from}:${edge.to}`} className="max-w-full rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-mono hc-type-label hc-dim">
                  {edge.from} -&gt; {edge.to}
                </span>
              ))}
            </div>
          ) : null}
        </FleetPanel>
      )}
    </div>
  );
}
