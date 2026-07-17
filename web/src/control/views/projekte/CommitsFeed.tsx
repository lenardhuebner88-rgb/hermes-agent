import { Eyebrow } from "../../components/primitives";
import { fmtRelativeTime } from "../../lib/derive";
import type { ProjectCommitFeedEntry } from "../../lib/schemas";
import { de } from "../../i18n/de";

const t = de.projekte;

export interface CommitsFeedProps {
  commits: ReadonlyArray<ProjectCommitFeedEntry>;
  now: number;
}

/** "Alle Commits" — one cross-project timeline: every row carries its project
 *  tag, subject, short hash, author and relative age. Newest first (backend
 *  merge), capped server-side. Read-only by design. */
export function CommitsFeed({ commits, now }: CommitsFeedProps) {
  return (
    <section aria-label={t.commitsTitle} className="space-y-3">
      <header>
        <Eyebrow>{t.commitsEyebrow}</Eyebrow>
        <h3 className="mt-1 font-display text-sec font-semibold text-ink">{t.commitsTitle}</h3>
      </header>

      {commits.length === 0 ? (
        <p className="text-sec text-ink-3">{t.commitsEmpty}</p>
      ) : (
        <ul className="min-w-0 space-y-1">
          {commits.map((commit) => (
            <li
              key={`${commit.project}:${commit.hash}:${commit.committed_at}`}
              className="flex min-w-0 items-center gap-2 rounded-card border border-line-soft bg-surface-2 px-2.5 py-1.5"
            >
              <span className="inline-flex max-w-full shrink-0 items-center rounded-card border border-line bg-surface-1 px-1.5 py-0.5 text-micro text-ink-3">
                <span className="truncate">{commit.project_name || commit.project}</span>
              </span>
              <p className="min-w-0 flex-1 truncate text-micro text-ink" title={commit.message}>
                {commit.message || t.noCommitMessage}
              </p>
              <span className="shrink-0 font-data text-micro text-ink-3">{commit.hash}</span>
              {commit.author ? (
                <span
                  className="max-w-28 shrink-0 truncate text-micro text-ink-2"
                  title={commit.author}
                >
                  {commit.author}
                </span>
              ) : null}
              <span className="shrink-0 font-data text-micro tabular-nums text-ink-3">
                {fmtRelativeTime(commit.committed_at, now)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
