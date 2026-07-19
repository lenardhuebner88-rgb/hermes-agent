/**
 * SessionsPanel — „SESSIONS" der Jarvis-Shell (Sprint 3, Karte S3.10): der
 * Spawn-Baum der Klassik (SessionsSection-Daten) als HUD-Strip (Offen/Aktiv-
 * Zähler) mit Overlay-Drawer.
 *
 * Daten über denselben Hook/Polling-Key wie die Klassik (useProjectSessions —
 * der pollingStore dedupliziert; ProjektePanel nutzt denselben Key bereits).
 * Filter, Spawn-Baum und Sortierung kommen aus der geteilten Ableitung
 * (filterSessions/buildSessionRows/countOpenSessions — Logik unverändert,
 * nur Präsentation im Jarvis-Look). Zeilen-Verhalten wie die Klassik:
 * Terminal-Deep-Link aus den strukturierten tmux-Feldern.
 *
 * Kill bleibt die bestehende Bestätigungs-Mechanik: das SessionKillSheet der
 * Klassik wird unverändert wiederverwendet. Killbar ist eine Zeile nur, wenn
 * sie sich über die strukturierten tmux-Felder (session+window, Fallback
 * window_name) einer Agent-Zeile aus useProjectAgents zuordnen lässt und
 * killTarget sie freigibt — nie ein Parsen des Anzeige-Labels. Quell-
 * Degradation (errors[]) wird dezent gezeigt, nie still (Klassik-Idiom).
 */
import { useEffect, useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import { SquareTerminal, X } from "lucide-react";

import { de } from "../i18n/de";
import { useProjectAgents, useProjectSessions } from "../hooks/useControlData";
import { fmtRelativeTime, fmtTokens, nowSec } from "../lib/derive";
import type { ProjectAgent, ProjectSession } from "../lib/schemas";
import {
  buildSessionRows,
  countOpenSessions,
  filterSessions,
  killTarget,
  terminalDeepLink,
  type SessionsFilter,
} from "../views/projekte/derive";
import { SessionKillSheet } from "../views/projekte/SessionKillSheet";
import { useProjectNames } from "./useProjectNames";

const t = de.jarvis;
const tp = de.projekte;

export interface SessionsPanelProps {
  open: boolean;
  onToggle: () => void;
}

/** Ordnet eine Session-Zeile ihrer Agent-Zeile zu — ausschließlich über die
 *  strukturierten tmux-Felder (session + window, Fallback window_name), nie
 *  über das Anzeige-Label (killTarget-Doktrin: destructive Aktionen hängen
 *  nicht an Präsentations-Strings). Kein Match → Zeile ist nicht killbar. */
function matchKillAgent(
  session: ProjectSession,
  agents: ReadonlyArray<ProjectAgent>,
): ProjectAgent | null {
  const tmuxSession = session.tmux_session?.trim();
  if (!tmuxSession) return null;
  const window = session.tmux_window?.trim();
  const windowName = session.tmux_window_name?.trim();
  for (const agent of agents) {
    if (agent.tmux_session?.trim() !== tmuxSession) continue;
    if (window && agent.tmux_window?.trim() === window) return agent;
    if (!window && windowName && agent.tmux_window_name?.trim() === windowName) return agent;
  }
  return null;
}

/** Eine Session-Zeile — Inhalt wie die Klassik (SessionRow): Live-LED,
 *  Label + Spawn-Zähler, Meta (Quelle · Modell · Spawn · Projekt ·
 *  Nachrichten/Tokens · Alter · Beendet-Marker), Spawn-Baum-Einzug. Rechts
 *  Terminal-Deep-Link und — nur bei eindeutig zugeordnetem, killbarem
 *  Agent — der Kill-Button (öffnet das Klassik-Sheet). */
function SessionJvRow({
  session,
  depth,
  childCount,
  projectNames,
  now,
  killAgent,
  onKill,
}: {
  session: ProjectSession;
  depth: number;
  childCount: number;
  projectNames: Readonly<Record<string, string>>;
  now: number;
  killAgent: ProjectAgent | null;
  onKill: (agent: ProjectAgent) => void;
}) {
  const ended = !session.is_open;
  const headline = session.label || session.id;
  const projectTag =
    session.project == null ? null : (projectNames[session.project] ?? session.project);
  const killable = killAgent != null && killTarget(killAgent) != null;

  const meta: string[] = [];
  meta.push(session.source || "—");
  if (session.model) meta.push(session.model);
  if (session.spawn_kind != null) {
    meta.push(
      session.spawned_by_label
        ? `${tp.spawnedBy(session.spawned_by_label)} · ${tp.spawnKindLabel(session.spawn_kind)}`
        : tp.spawnKindLabel(session.spawn_kind),
    );
  }
  if (projectTag) meta.push(projectTag);
  meta.push(tp.sessionsMeta(session.message_count, fmtTokens(session.tokens)));
  if (session.last_active != null && Number.isFinite(session.last_active)) {
    meta.push(fmtRelativeTime(session.last_active, now));
  }
  if (ended) meta.push(tp.sessionEnded);

  return (
    <div
      className={ended ? "jv-srow jv-ended" : "jv-srow"}
      style={
        depth > 0
          ? ({ "--tree-depth": Math.min(depth, 4), marginLeft: "calc(var(--tree-depth, 0) * 14px)" } as CSSProperties)
          : undefined
      }
    >
      {session.is_active ? (
        <span className="jv-led" aria-hidden="true" />
      ) : (
        <span className={session.is_open ? "jv-dot-open" : "jv-dot-ended"} aria-hidden="true" />
      )}
      <div className="jv-srow-main">
        <span className="jv-srow-label" title={headline}>
          {headline}
          {childCount > 0 ? <span className="jv-n"> {tp.spawnedCount(childCount)}</span> : null}
        </span>
        <span className="jv-srow-meta">{meta.join(" · ")}</span>
      </div>
      {session.tmux_session?.trim() ? (
        <Link
          className="jv-iconbtn"
          to={terminalDeepLink(session.tmux_session, session.tmux_window_name ?? session.tmux_window)}
          aria-label={tp.terminalOpenAria(headline)}
          title={tp.terminalOpenAria(headline)}
        >
          <SquareTerminal className="h-3.5 w-3.5" aria-hidden />
        </Link>
      ) : null}
      {killable && killAgent ? (
        <button
          type="button"
          className="jv-iconbtn jv-kill"
          aria-label={tp.killSessionAria(headline)}
          title={tp.killSessionAria(headline)}
          onClick={() => onKill(killAgent)}
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      ) : null}
    </div>
  );
}

export function SessionsPanel({ open, onToggle }: SessionsPanelProps) {
  const sessions = useProjectSessions();
  const agents = useProjectAgents();
  const projectNames = useProjectNames();
  const now = nowSec();
  const [filter, setFilter] = useState<SessionsFilter>("open");
  const [killAgent, setKillAgent] = useState<ProjectAgent | null>(null);

  const sessionList = sessions.data?.sessions ?? [];
  const agentList = agents.data?.agents ?? [];
  const sourceErrors = sessions.data?.errors ?? [];

  const openCount = countOpenSessions(sessionList);
  const activeCount = sessionList.filter((session) => session.is_active).length;
  const staleCount = countOpenSessions(sessionList, { includeStale: true }) - openCount;
  const rows = buildSessionRows(filterSessions(sessionList, filter));

  // ESC schließt den Drawer (InboxPanel-Idiom der Shell).
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onToggle();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onToggle]);

  return (
    <>
      <div className="jv-float jv-strip">
        <span className="jv-strip-title">{t.sessionsPanelTitle}</span>
        {sessions.error ? (
          <span className="jv-strip-tease jv-warn" title={tp.sessionsError}>
            !
          </span>
        ) : sessions.data === null ? (
          <span className="jv-strip-tease jv-dim">…</span>
        ) : (
          <span className="jv-strip-tease jv-dim">
            {t.sessionsStripCounts(openCount, activeCount)}
          </span>
        )}
        <button
          type="button"
          className="jv-strip-toggle"
          aria-expanded={open}
          aria-controls="jv-sessions-sheet"
          aria-label={t.sessionsExpandAria}
          onClick={onToggle}
        >
          {open ? "▾" : "▸"}
        </button>
      </div>

      {open ? (
        <div
          className="jv-float jv-sheet"
          id="jv-sessions-sheet"
          role="region"
          aria-label={t.sessionsPanelTitle}
        >
          <div className="jv-ptitle jv-fragen-head">
            {t.sessionsPanelTitle}
            <button
              type="button"
              className="jv-fclose"
              onClick={onToggle}
              aria-label={t.sessionsClose}
            >
              ×
            </button>
          </div>

          <div className="jv-chips" role="group" aria-label={t.sessionsPanelTitle}>
            <button
              type="button"
              className="jv-chip"
              aria-pressed={filter === "open"}
              onClick={() => setFilter("open")}
            >
              {tp.sessionsFilterOpen} <span className="jv-n">{openCount}</span>
            </button>
            <button
              type="button"
              className="jv-chip"
              aria-pressed={filter === "active"}
              onClick={() => setFilter("active")}
            >
              {tp.sessionsFilterActive} <span className="jv-n">{activeCount}</span>
            </button>
            <button
              type="button"
              className="jv-chip"
              aria-pressed={filter === "stale"}
              onClick={() => setFilter("stale")}
            >
              {tp.sessionsFilterStale} <span className="jv-n">{staleCount}</span>
            </button>
            <button
              type="button"
              className="jv-chip"
              aria-pressed={filter === "all"}
              onClick={() => setFilter("all")}
            >
              {tp.sessionsFilterAll} <span className="jv-n">{sessionList.length}</span>
            </button>
          </div>

          <div className="jv-fbody">
            {sessions.error ? (
              <p className="jv-qerror" role="alert">
                {tp.sessionsError}
              </p>
            ) : null}

            {!sessions.error && sessions.data === null ? (
              <p className="jv-qloading">{t.sessionsLoading}</p>
            ) : null}

            {/* Quell-Degradation (z.B. tmux-Scan) dezent — der Rest der
                Zeilen gilt weiter (WartetPanel-Idiom), nie ein Crash. */}
            {sourceErrors.map((message, index) => (
              <p className="jv-srcerr" key={index} title={message}>
                {message}
              </p>
            ))}

            {!sessions.error && sessions.data !== null && rows.length === 0 ? (
              <p className="jv-qloading">{tp.sessionsEmpty(filter)}</p>
            ) : null}

            {rows.map(({ session, depth, childCount }) => (
              <SessionJvRow
                key={session.id}
                session={session}
                depth={depth}
                childCount={childCount}
                projectNames={projectNames}
                now={now}
                killAgent={matchKillAgent(session, agentList)}
                onKill={setKillAgent}
              />
            ))}
          </div>
        </div>
      ) : null}

      {/* Kill-Bestätigung der Klassik, unverändert wiederverwendet (gleicher
          Endpoint, gleiche Mechanik; nach Erfolg Reload beider Polls). */}
      {killAgent ? (
        <SessionKillSheet
          agent={killAgent}
          projectName={
            killAgent.project ? (projectNames[killAgent.project] ?? killAgent.project) : null
          }
          now={now}
          onClose={() => setKillAgent(null)}
          onKilled={() => {
            void agents.reload();
            void sessions.reload();
          }}
        />
      ) : null}
    </>
  );
}
