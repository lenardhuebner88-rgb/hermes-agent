import type { ChainGraphEdge, ChainGraphNode } from "../../lib/types";

export interface DagLevel {
  level: number;
  nodes: ChainGraphNode[];
}

/** Compute DAG levels (longest path from any source). Roots = level 0. */
export function computeLevels(nodes: ChainGraphNode[], edges: ChainGraphEdge[]): DagLevel[] {
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));
  const childrenByParent = new Map<string, string[]>();
  const parentsByChild = new Map<string, string[]>();
  for (const e of edges) {
    childrenByParent.set(e.from, [...(childrenByParent.get(e.from) ?? []), e.to]);
    parentsByChild.set(e.to, [...(parentsByChild.get(e.to) ?? []), e.from]);
  }

  const levels = new Map<string, number>();
  const visited = new Set<string>();

  function walk(id: string): number {
    if (levels.has(id)) return levels.get(id)!;
    if (visited.has(id)) return 0; // cycle guard
    visited.add(id);
    const parents = parentsByChild.get(id) ?? [];
    const parentLevel = parents.length ? Math.max(...parents.map(walk)) + 1 : 0;
    levels.set(id, parentLevel);
    return parentLevel;
  }

  for (const n of nodes) walk(n.id);

  const byLevel = new Map<number, ChainGraphNode[]>();
  for (const [id, lvl] of levels) {
    const node = nodeMap.get(id);
    if (!node) continue;
    const list = byLevel.get(lvl) ?? [];
    list.push(node);
    byLevel.set(lvl, list);
  }

  // Deterministic order within a level: stable by id.
  for (const list of byLevel.values()) list.sort((a, b) => a.id.localeCompare(b.id));

  return [...byLevel.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([level, levelNodes]) => ({ level, nodes: levelNodes }));
}

/**
 * Linearize the DAG into a single vertical-pipeline order: by level ascending,
 * then by id within a level (stable). `computeLevels` already returns levels in
 * ascending order with nodes sorted by id, so a flat concat preserves it. The
 * pipeline view renders nodes top-to-bottom in this order.
 */
export function linearizeNodes(nodes: ChainGraphNode[], edges: ChainGraphEdge[]): ChainGraphNode[] {
  return computeLevels(nodes, edges).flatMap((lvl) => lvl.nodes);
}

export function statusTone(status: string): "emerald" | "cyan" | "amber" | "red" | "violet" | "zinc" {
  if (status === "done") return "emerald";
  if (status === "running") return "cyan";
  if (status === "review") return "amber";
  if (status === "blocked") return "red";
  // B5: scheduled looks like a planned/upcoming chain → violet (same as Flow);
  // old default "zinc" made geplante Ketten look dead (grey=idle).
  if (status === "scheduled") return "violet";
  return "zinc";
}

export function statusDot(status: string): "live" | "warn" | "error" | "ready" | "idle" {
  if (status === "running") return "live";
  if (status === "blocked") return "error";
  if (status === "review") return "warn";
  if (status === "done") return "ready";
  return "idle";
}
