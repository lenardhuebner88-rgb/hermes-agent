// Ausgelagert aus ResearchView.tsx (react-refresh/only-export-components).
interface TaskComment { author: string | null; body: string; created_at: number }

export interface ResearchDetail {
  task: { id: string; title: string; body?: string | null; result?: string | null; status: string } | null;
  comments?: TaskComment[];
}

export function buildResearchIdempotencyKey(): string {
  const maybeCrypto = typeof crypto !== "undefined" ? crypto : null;
  const randomPart = typeof maybeCrypto?.randomUUID === "function"
    ? maybeCrypto.randomUUID()
    : `${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`;
  return `research-${randomPart}`;
}

/** Antwort = letzter Kommentar (Receipt-Muster), sonst result. Exportiert für den Test. */
export function pickAnswer(detail: ResearchDetail): { body: string; author: string | null; at: number | null } | null {
  const comments = detail.comments ?? [];
  if (comments.length > 0) {
    const last = comments[comments.length - 1];
    return { body: last.body, author: last.author, at: last.created_at };
  }
  const result = detail.task?.result?.trim();
  if (result) return { body: result, author: null, at: null };
  return null;
}
