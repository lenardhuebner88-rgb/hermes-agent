export type DesktopTerminalLayout = 1 | 2 | 4;

export interface TerminalTarget {
  session: string;
  window: string;
  window_id?: string | null;
}

export function normalizeDesktopLayout(value: unknown): DesktopTerminalLayout {
  const parsed = typeof value === "string" ? Number.parseInt(value, 10) : value;
  return parsed === 2 || parsed === 4 ? parsed : 1;
}

export function targetKey(target: TerminalTarget): string {
  return target.window_id || `${target.session}:${target.window}`;
}

export function resolvePaneTargets(
  available: TerminalTarget[],
  previous: Array<TerminalTarget | null>,
  visibleCount: DesktopTerminalLayout,
): Array<TerminalTarget | null> {
  const liveById = new Map(
    available
      .filter((target) => target.window_id)
      .map((target) => [target.window_id as string, target]),
  );
  const liveByName = new Map(
    available.map((target) => [`${target.session}:${target.window}`, target]),
  );
  const result: Array<TerminalTarget | null> = Array.from({ length: 4 }, () => null);
  const used = new Set<string>();
  let highestPreserved = -1;

  for (let index = 0; index < result.length; index += 1) {
    const candidate = previous[index];
    if (!candidate) continue;
    const canonical = candidate.window_id
      ? liveById.get(candidate.window_id)
      : liveByName.get(`${candidate.session}:${candidate.window}`);
    const key = canonical ? targetKey(canonical) : targetKey(candidate);
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
