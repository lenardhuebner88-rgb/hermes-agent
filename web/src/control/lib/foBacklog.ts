import type { BacklogItem, BacklogDetail } from "./schemas";

export type FoSortKey = "risk" | "age" | "status";

export type FoFilterOptions = {
  owner?: string;
  risk?: string;
  area?: string;
  status?: string;
  stale?: boolean;
};

const RISK_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 };
const STATUS_ORDER: Record<string, number> = {
  now: 0,
  next: 1,
  in_progress: 2,
  blocked: 3,
  later: 4,
  done: 5,
};

export function computeNextFoTaskId(items: BacklogItem[]): string | null {
  const pick = (status: string) => {
    const candidates = items.filter((it) => it.status === status && !it.stale);
    if (candidates.length === 0) {
      // fall back to stale items of this status if none non-stale
      const stale = items.filter((it) => it.status === status);
      if (stale.length === 0) return null;
      stale.sort((a, b) => a.updated.localeCompare(b.updated) || a.id.localeCompare(b.id));
      return stale[0].id;
    }
    candidates.sort((a, b) => a.updated.localeCompare(b.updated) || a.id.localeCompare(b.id));
    return candidates[0].id;
  };
  return pick("now") ?? pick("next") ?? null;
}

export function buildFoCommissionPrompt(detail: BacklogDetail): string {
  return `Du bist eine Orchestrator-Session auf dem Homeserver mit vollem Zugriff. Arbeite GENAU EINEN FO-Backlog-Task ab.
TASK: ${detail.title}   (id: ${detail.id})
SPEC: ~/projects/family-organizer/backlog/items/${detail.id}.md  ← ZUERST vollständig lesen (status, owner, area, risk, Akzeptanzkriterien)
ROOT: ~/projects/family-organizer   GATE: npm run gate:e2e
1) Preflight: cd ~/projects/family-organizer + \`git status\` (FO-Tab liest origin/main → committen, damit Fortschritt sichtbar wird).
2) Task umsetzen (Next.js/Vitest; orchestrate-Skill / Workflow-Harness erlaubt).
3) Gate fahren: npm run gate:e2e — WIRKLICH grün (Mocks = Regressions-Wächter, kein Erstbeweis).
4) NUR bei grün: Item-\`.md\` status→done/in_progress + \`result\`-Zeile aktualisieren; commit + (FO-Repo) push.
5) Discord-Report (nie nur Telegram): Status + Commit + Ergebnis.
ABBRUCH (stop & melde, NICHT loopen/raten): Gate 2–3× rot · DB-Migration/destruktiv · Spec mehrdeutig · etwas außerhalb des Task-Scopes müsste geändert werden.`;
}

export function filterFoItems(
  items: BacklogItem[],
  q: string,
  filters: FoFilterOptions,
): BacklogItem[] {
  let result = items;

  if (q.trim()) {
    const lower = q.toLowerCase();
    result = result.filter(
      (it) =>
        it.title.toLowerCase().includes(lower) ||
        it.id.toLowerCase().includes(lower) ||
        it.area.toLowerCase().includes(lower) ||
        it.owner.toLowerCase().includes(lower) ||
        (it.excerpt ?? "").toLowerCase().includes(lower),
    );
  }

  if (filters.owner) {
    result = result.filter((it) => it.owner === filters.owner);
  }
  if (filters.risk) {
    result = result.filter((it) => it.risk === filters.risk);
  }
  if (filters.area) {
    result = result.filter((it) => it.area === filters.area);
  }
  if (filters.status) {
    result = result.filter((it) => it.status === filters.status);
  }
  if (filters.stale === true) {
    result = result.filter((it) => it.stale);
  }

  return result;
}

export function sortFoItems(items: BacklogItem[], key: FoSortKey): BacklogItem[] {
  const arr = [...items];
  switch (key) {
    case "risk":
      arr.sort(
        (a, b) =>
          (RISK_ORDER[a.risk] ?? 9) - (RISK_ORDER[b.risk] ?? 9) ||
          a.updated.localeCompare(b.updated) ||
          a.id.localeCompare(b.id),
      );
      break;
    case "age":
      arr.sort(
        (a, b) =>
          a.updated.localeCompare(b.updated) ||
          a.id.localeCompare(b.id),
      );
      break;
    case "status":
      arr.sort(
        (a, b) =>
          (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) ||
          a.updated.localeCompare(b.updated) ||
          a.id.localeCompare(b.id),
      );
      break;
  }
  return arr;
}
