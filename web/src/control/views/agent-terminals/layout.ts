export type DesktopTerminalLayout = 1 | 2 | 4;

export interface TerminalTarget {
  session: string;
  window: string;
}

export function normalizeDesktopLayout(value: unknown): DesktopTerminalLayout {
  const parsed = typeof value === "string" ? Number.parseInt(value, 10) : value;
  return parsed === 2 || parsed === 4 ? parsed : 1;
}

export function targetKey(target: TerminalTarget): string {
  return `${target.session}:${target.window}`;
}

export function resolvePaneTargets(
  available: TerminalTarget[],
  previous: Array<TerminalTarget | null>,
  visibleCount: DesktopTerminalLayout,
): Array<TerminalTarget | null> {
  const live = new Map(available.map((target) => [targetKey(target), target]));
  const result: Array<TerminalTarget | null> = Array.from({ length: 4 }, () => null);
  const used = new Set<string>();
  let highestPreserved = -1;

  for (let index = 0; index < result.length; index += 1) {
    const candidate = previous[index];
    if (!candidate) continue;
    const key = targetKey(candidate);
    const canonical = live.get(key);
    if (!canonical || used.has(key)) continue;
    result[index] = canonical;
    used.add(key);
    highestPreserved = index;
  }

  const desired = Math.max(visibleCount, highestPreserved + 1);
  for (let index = 0; index < desired && index < result.length; index += 1) {
    if (result[index]) continue;
    const candidate = available.find((target) => !used.has(targetKey(target)));
    if (!candidate) break;
    result[index] = candidate;
    used.add(targetKey(candidate));
  }

  return result;
}
