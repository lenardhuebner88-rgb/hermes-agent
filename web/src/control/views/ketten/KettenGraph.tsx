import { useMemo } from "react";
import { cn } from "@/lib/utils";
import { FleetEmptyState } from "../../components/fleet/atoms";
import { ChainNodeCard } from "./ChainNodeCard";
import { linearizeNodes } from "./dagLayout";
import type { ChainGraphEdge, ChainGraphNode } from "../../lib/types";
import { de } from "../../i18n/de";

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

  if (nodes.length === 0)
    return <FleetEmptyState title={de.ketten.emptyGraphTitle} desc={de.ketten.emptyGraphDesc} />;

  return (
    <div className="relative min-w-0 max-w-full rounded-xl border border-[var(--hc-border)] bg-[var(--hc-panel)] p-4">
      {/* Vertical pipeline line — spec: left:13px top:14px bottom:14px width:3px radius:3px
          Gradient: emerald 0% → cyan 30% → border-strong 56% → border-strong 100%
          (cyanPct is the dynamic hand — emerald/cyan section tracks done+running nodes).
          We use the spec gradient directly; the live-progress tint rides the top band. */}
      <div
        aria-hidden="true"
        data-pipeline-line
        className="pointer-events-none absolute"
        style={{
          left: 13,
          top: 14,
          bottom: 14,
          width: 3,
          borderRadius: 3,
          background: `linear-gradient(180deg, var(--hc-emerald) 0%, var(--hc-cyan) 30%, var(--hc-border-strong) ${Math.max(30, cyanPct)}%, var(--hc-border-strong) 100%)`,
        }}
      />

      {/* Node list — padding-left:38px gives room for the 13px-left line + 16px dot + gap */}
      <div className="relative flex flex-col gap-[18px]" style={{ paddingLeft: 38 }}>
        {ordered.map((node) => {
          const isRoot = node.id === rootId;
          const isRunning = node.status === "running";
          const dotKind = isRunning ? "running" : isRoot ? "root" : "open";

          // Node dot: 16×16, centred on the pipeline line at left:13px (line centre = 14.5px).
          // Position: left = -(38 - 13 + 8) = -33px from content edge → aligns with line centre.
          // Box-shadow ring: 0 0 0 4px var(--hc-bg) isolates the dot from the line colour.
          return (
            <div key={node.id} className="relative">
              <span
                aria-hidden="true"
                data-node-dot={dotKind}
                className={cn(
                  "absolute z-[2] h-4 w-4 rounded-full border-[3px]",
                  // Vertical centre of the card; line sits at left:13px (0-indexed from container).
                  // The dot is 16px wide; its left edge = 13 - 8 = 5px from container left.
                  // We're inside the padded flex child, so offset from child left = -(38-5) = -33px.
                  "-translate-y-1/2",
                  dotKind === "running"
                    ? "border-cyan-400 bg-cyan-400 motion-safe:animate-pulse"
                    : dotKind === "root"
                      ? "border-indigo-500 bg-indigo-500"
                      : "border-[var(--hc-border-strong)] bg-[var(--hc-bg)]",
                )}
                style={{
                  top: "50%",
                  left: -33,
                  boxShadow: isRunning
                    ? "0 0 0 4px var(--hc-bg), 0 0 8px 2px rgba(34,211,238,.45)"
                    : "0 0 0 4px var(--hc-bg)",
                }}
              />
              <ChainNodeCard node={node} isRoot={isRoot} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
