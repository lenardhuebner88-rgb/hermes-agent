import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ChainNodeCard } from "./ChainNodeCard";
import { computeLevels } from "./dagLayout";
import type { ChainGraphEdge, ChainGraphNode } from "../../lib/types";

interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface KettenGraphProps {
  nodes: ChainGraphNode[];
  edges: ChainGraphEdge[];
  rootId: string;
  now: number;
}

export function KettenGraph({ nodes, edges, rootId, now }: KettenGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const nodeRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const [rects, setRects] = useState<Map<string, Rect>>(new Map());

  const levels = useMemo(() => computeLevels(nodes, edges), [nodes, edges]);

  const measure = () => {
    const container = containerRef.current;
    if (!container) return;
    const cRect = container.getBoundingClientRect();
    const next = new Map<string, Rect>();
    for (const [id, el] of nodeRefs.current.entries()) {
      const r = el.getBoundingClientRect();
      next.set(id, {
        x: r.left - cRect.left,
        y: r.top - cRect.top,
        width: r.width,
        height: r.height,
      });
    }
    setRects(next);
  };

  useLayoutEffect(() => {
    measure();
  }, [nodes, edges, levels]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(container);
    return () => ro.disconnect();
  }, []);

  const paths = useMemo(() => {
    const out: { key: string; d: string }[] = [];
    for (const e of edges) {
      const from = rects.get(e.from);
      const to = rects.get(e.to);
      if (!from || !to) continue;
      const x1 = from.x + from.width / 2;
      const y1 = from.y + from.height;
      const x2 = to.x + to.width / 2;
      const y2 = to.y;
      const dy = Math.max(16, (y2 - y1) / 2);
      const d = `M ${x1} ${y1} C ${x1} ${y1 + dy}, ${x2} ${y2 - dy}, ${x2} ${y2}`;
      out.push({ key: `${e.from}->${e.to}`, d });
    }
    return out;
  }, [rects, edges]);

  if (nodes.length === 0) return null;

  return (
    <div
      ref={containerRef}
      className="relative min-w-0 max-w-full overflow-hidden rounded-xl border border-[var(--hc-border)] bg-[var(--hc-panel)] p-4"
    >
      <svg
        className="pointer-events-none absolute inset-0 z-0"
        width="100%"
        height="100%"
        aria-hidden="true"
      >
        {paths.map((p) => (
          <path
            key={p.key}
            d={p.d}
            fill="none"
            stroke="var(--hc-border-strong)"
            strokeWidth={2}
            strokeLinecap="round"
          />
        ))}
        {paths.map((p) => {
          // small arrow head at the end of each path
          const parts = p.d.split(" ");
          const x = parseFloat(parts[parts.length - 2]);
          const y = parseFloat(parts[parts.length - 1]);
          return <circle key={`${p.key}-dot`} cx={x} cy={y} r={3} fill="var(--hc-border-strong)" />;
        })}
      </svg>

      <div
        className="relative z-10 grid gap-x-4 gap-y-6"
        style={{
          gridTemplateColumns:
            levels.length <= 1
              ? "minmax(0, 1fr)"
              : `repeat(${levels.length}, minmax(0, 1fr))`,
        }}
      >
        {levels.map((lvl) => (
          <div key={lvl.level} className="flex min-w-0 flex-col gap-4">
            {lvl.nodes.map((node) => (
              <ChainNodeCard
                key={node.id}
                ref={(el) => {
                  if (el) nodeRefs.current.set(node.id, el);
                  else nodeRefs.current.delete(node.id);
                }}
                node={node}
                isRoot={node.id === rootId}
                now={now}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
