import { useMemo } from "react";
import { cn } from "@/lib/utils";
import { ChainNodeCard } from "./ChainNodeCard";
import { linearizeNodes } from "./dagLayout";
import type { ChainGraphEdge, ChainGraphNode } from "../../lib/types";

interface KettenGraphProps {
  nodes: ChainGraphNode[];
  edges: ChainGraphEdge[];
  rootId: string;
}

export function KettenGraph({ nodes, edges, rootId }: KettenGraphProps) {
  const ordered = useMemo(() => linearizeNodes(nodes, edges), [nodes, edges]);

  // The pipeline line is cyan up to (and including) the last running/done node,
  // grey below — a glanceable "how far has the chain progressed" cue.
  const cyanPct = useMemo(() => {
    if (ordered.length === 0) return 0;
    let progressed = 0;
    ordered.forEach((n, i) => {
      if (n.status === "done" || n.status === "running") progressed = i + 1;
    });
    return Math.round((progressed / ordered.length) * 100);
  }, [ordered]);

  if (nodes.length === 0) return null;

  return (
    <div className="relative min-w-0 max-w-full rounded-xl border border-[var(--hc-border)] bg-[var(--hc-panel)] p-4">
      {/* Vertical pipeline line — replaces the old SVG bézier edge layer. */}
      <div
        aria-hidden="true"
        data-pipeline-line
        className="pointer-events-none absolute bottom-[32px] left-[24px] top-[32px] w-[3px] rounded-full"
        style={{
          background: `linear-gradient(to bottom, #22d3ee 0%, #22d3ee ${cyanPct}%, var(--hc-border-strong) ${cyanPct}%, var(--hc-border-strong) 100%)`,
        }}
      />

      <div className="relative ml-[44px] flex flex-col gap-3">
        {ordered.map((node) => {
          const isRoot = node.id === rootId;
          const dotKind = node.status === "running" ? "running" : isRoot ? "root" : "open";
          return (
            <div key={node.id} className="relative">
              {/* Knoten-Punkt auf der Pipeline-Linie. */}
              <span
                aria-hidden="true"
                data-node-dot={dotKind}
                className={cn(
                  "absolute -left-[36px] top-1/2 z-[2] h-4 w-4 -translate-y-1/2 rounded-full border-[3px] bg-[var(--hc-panel)]",
                  dotKind === "running"
                    ? "animate-pulse border-cyan-400 bg-cyan-400"
                    : dotKind === "root"
                      ? "border-indigo-500 bg-indigo-500"
                      : "border-[var(--hc-border-strong)]",
                )}
              />
              <ChainNodeCard node={node} isRoot={isRoot} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
