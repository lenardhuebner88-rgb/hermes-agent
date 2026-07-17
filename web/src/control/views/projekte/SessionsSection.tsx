import { useState } from "react";
import { SquareTerminal } from "lucide-react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";
import { Eyebrow } from "../../components/primitives";
import { SubtabChips } from "../../components/leitstand";
import { Led } from "../../components/atoms";
import { fmtRelativeTime, fmtTokens } from "../../lib/derive";
import type { ProjectSession } from "../../lib/schemas";
import { de } from "../../i18n/de";
import { buildSessionRows, countOpenSessions, filterSessions, terminalDeepLink, type SessionsFilter } from "./derive";

const t = de.projekte;

export interface SessionsSectionProps {
  sessions: ReadonlyArray<ProjectSession>;
  /** slug → display name for the project tag; missing slugs fall back to the raw slug. */
  projectNames: Readonly<Record<string, string>>;
  now: number;
}

/** "Offene Sessions" — the spawn tree out of state.db: which session spawned
 *  which (children indented under their parent), which ones are still open,
 *  and which are actually alive right now (300s window, live LED). Default
 *  filter is "Offen" — open AND not stale; the never-closed zombie rows
 *  (≥24h idle, real live-host pattern) sit in their own "Verwaist" bucket so
 *  the operator sees the data-quality reality instead of a 150-row wall. */
export function SessionsSection({ sessions, projectNames, now }: SessionsSectionProps) {
  const [filter, setFilter] = useState<SessionsFilter>("open");

  const openCount = countOpenSessions(sessions);
  const activeCount = sessions.filter((session) => session.is_active).length;
  const staleCount = countOpenSessions(sessions, { includeStale: true }) - openCount;
  const visible = filterSessions(sessions, filter);
  const rows = buildSessionRows(visible);

  return (
    <section aria-label={t.sessionsTitle} className="space-y-3">
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <Eyebrow>{t.sessionsEyebrow}</Eyebrow>
          <h3 className="mt-1 font-display text-sec font-semibold text-ink">{t.sessionsTitle}</h3>
        </div>
        <SubtabChips
          items={[
            { id: "open" as const, label: t.sessionsFilterOpen, count: openCount },
            { id: "active" as const, label: t.sessionsFilterActive, count: activeCount },
            { id: "stale" as const, label: t.sessionsFilterStale, count: staleCount },
            { id: "all" as const, label: t.sessionsFilterAll, count: sessions.length },
          ]}
          active={filter}
          onSelect={setFilter}
          ariaLabelPrefix="Sessions-Filter"
        />
      </header>

      {rows.length === 0 ? (
        <p className="text-sec text-ink-3">{t.sessionsEmpty(filter)}</p>
      ) : (
        <ul className="space-y-1">
          {rows.map(({ session, depth, childCount }) => (
            <SessionRow
              key={session.id}
              session={session}
              depth={depth}
              childCount={childCount}
              projectNames={projectNames}
              now={now}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function SessionRow({
  session,
  depth,
  childCount,
  projectNames,
  now,
}: {
  session: ProjectSession;
  depth: number;
  childCount: number;
  projectNames: Readonly<Record<string, string>>;
  now: number;
}) {
  const ended = !session.is_open;
  const projectTag =
    session.project == null ? null : (projectNames[session.project] ?? session.project);

  return (
    <li
      className={cn(
        "flex min-w-0 items-center gap-2 rounded-card border px-2.5 py-1.5",
        ended
          ? "border-line-soft bg-surface-1"
          : "border-line-soft bg-surface-2",
      )}
      // Spawn-tree indent: layout only (color stays on tokens); one step per depth.
      style={depth > 0 ? { marginLeft: Math.min(depth, 4) * 18 } : undefined}
    >
      {session.is_active ? (
        <Led kind="live" size={7} />
      ) : (
        <span
          aria-hidden
          className={cn(
            "size-1.5 shrink-0 rounded-full",
            session.is_open ? "border border-ink-3" : "bg-ink-3/40",
          )}
        />
      )}
      <div className="min-w-0 flex-1">
        <p
          className={cn("truncate text-micro", ended ? "text-ink-3" : "text-ink")}
          title={session.label}
        >
          {session.label || session.id}
          {childCount > 0 ? (
            <span className="font-data text-ink-3"> {t.spawnedCount(childCount)}</span>
          ) : null}
        </p>
        <p className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 text-micro text-ink-3">
          <span>{session.source || "—"}</span>
          {session.model ? (
            <>
              <span aria-hidden>·</span>
              <span className="truncate font-data" title={session.model}>
                {session.model}
              </span>
            </>
          ) : null}
          {session.spawn_kind != null ? (
            <>
              <span aria-hidden>·</span>
              <span className="truncate">
                {session.spawned_by_label
                  ? `${t.spawnedBy(session.spawned_by_label)} · ${t.spawnKindLabel(session.spawn_kind)}`
                  : t.spawnKindLabel(session.spawn_kind)}
              </span>
            </>
          ) : null}
          {projectTag ? (
            <>
              <span aria-hidden>·</span>
              <span className="truncate">{projectTag}</span>
            </>
          ) : null}
          <span aria-hidden>·</span>
          <span className="font-data tabular-nums">
            {t.sessionsMeta(session.message_count, fmtTokens(session.tokens))}
          </span>
          {session.last_active != null && Number.isFinite(session.last_active) ? (
            <>
              <span aria-hidden>·</span>
              <span className="font-data tabular-nums">
                {fmtRelativeTime(session.last_active, now)}
              </span>
            </>
          ) : null}
          {ended ? (
            <>
              <span aria-hidden>·</span>
              <span>{t.sessionEnded}</span>
            </>
          ) : null}
        </p>
      </div>
      {session.tmux_session?.trim() ? (
        // Terminal-Deep-Link (Stage 12): nur Zeilen mit strukturierter tmux-
        // Adresse (additive Backend-Annotation); Fenster optional. 44px mobil,
        // kompakt ab tab — dasselbe Idiom wie die LiveBoard-Affordance.
        <Link
          to={terminalDeepLink(session.tmux_session, session.tmux_window_name ?? session.tmux_window)}
          aria-label={t.terminalOpenAria(session.label || session.id)}
          title={t.terminalOpenAria(session.label || session.id)}
          className="grid size-11 shrink-0 place-items-center rounded-card border border-line text-ink-3 hover:border-bronze/40 hover:bg-bronze/10 hover:text-bronze-hi focus-visible:outline-2 focus-visible:outline-bronze tab:size-7"
        >
          <SquareTerminal className="h-3.5 w-3.5" aria-hidden />
        </Link>
      ) : null}
    </li>
  );
}
