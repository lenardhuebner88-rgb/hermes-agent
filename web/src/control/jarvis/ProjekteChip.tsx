/**
 * ProjekteChip — G3 (Produktreife E1): der eingeklappte Projekte-Chip der
 * Jarvis-Shell, ersetzt das große Interims-ProjektePanel.
 *
 * Daten über EXAKT denselben Pfad wie die Klassik (useProjects/useProjectAgents/
 * useProjectSessions — der pollingStore dedupliziert über die Keys, kein zweiter
 * Fetch) und dieselbe Ableitung (buildProjectsOverview + computeAttention aus
 * views/projekte/derive): der ⚠-Zähler ist dieselbe Ampel wie im Klassik-Grid
 * (alert = Eingriff/blockiert/stale/loop-rot), keine eigene Ableitung.
 *
 * Chip „Projekte · n · ⚠ k" (⚠-Segment nur bei Alarmen). Das Popover (Klick/
 * Tap, aria-expanded + aria-haspopup=dialog) zeigt NUR Alarm-Projekte — Name,
 * deutsche Gründe, Drilldown auf die Klassik wie bisher — plus „alle zeigen"
 * → /control/projekte-klassisch. Tastatur: Chip ist <button>, ESC und
 * Outside-Click schließen, Fokus zurück an den Chip, Fokus sichtbar.
 * Degraded-Mode bei Fetch-Fehler: „Projekte · –" bleibt bedienbar (Fehlerzeile
 * im Popover statt Crash).
 */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { de } from "../i18n/de";
import {
  useProjectAgents,
  useProjects,
  useProjectSessions,
} from "../hooks/useControlData";
import {
  buildProjectsOverview,
  computeAttention,
  type AttentionReason,
} from "../views/projekte/derive";

const t = de.jarvis;
const tp = de.projekte;

/** Grund-Label — identisch zur Klassik (reasonChipLabel) bzw. dem früheren
 *  ProjektePanel: dieselben deutschen reason-Strings, keine eigene Ableitung. */
function reasonLabel(reason: AttentionReason): string {
  switch (reason.kind) {
    case "needs_input":
      return tp.reasonNeedsInput(reason.count);
    case "blocked":
      return tp.reasonBlocked(reason.count);
    case "stale_sessions":
      return tp.reasonStale(reason.count);
    case "loop_red":
      return tp.reasonLoopRed;
  }
}

export function ProjekteChip() {
  const projects = useProjects();
  const agents = useProjectAgents();
  const sessions = useProjectSessions();

  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);

  const list = projects.data?.projects ?? [];
  const overview = buildProjectsOverview(
    list,
    agents.data?.agents ?? [],
    sessions.data?.sessions ?? [],
  );
  const alerts = overview.sortedList
    .map((project) => ({
      project,
      attention: computeAttention(
        project,
        overview.agentCountBySlug[project.slug] ?? 0,
        overview.staleBySlug[project.slug] ?? 0,
      ),
    }))
    .filter(({ attention }) => attention.level === "alert");

  const degraded = projects.error !== null;
  const ready = projects.data !== null;
  const total = list.length;
  const alertCount = alerts.length;

  // ESC + Outside-Click schließen das Popover; ESC gibt den Fokus an den Chip.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setOpen(false);
      buttonRef.current?.focus();
    };
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [open]);

  // Fokus in das Popover, sobald es öffnet — Tab erreicht dann die Links.
  useEffect(() => {
    if (open) popoverRef.current?.focus();
  }, [open]);

  return (
    <div className="jv-chip-root" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className="jv-chip"
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls={open ? "jv-chip-popover" : undefined}
        onClick={() => setOpen((value) => !value)}
      >
        <span>{t.projekteChip.label(ready ? total : "–")}</span>
        {!degraded && ready && alertCount > 0 ? (
          <span className="jv-chip-warn">{t.projekteChip.warn(alertCount)}</span>
        ) : null}
      </button>

      {open ? (
        <div
          ref={popoverRef}
          id="jv-chip-popover"
          className="jv-chip-popover"
          role="dialog"
          aria-label={t.projekteChip.popoverLabel}
          tabIndex={-1}
        >
          {degraded ? (
            <p className="jv-chip-note jv-chip-note-error" role="alert">
              {t.projekteError}
            </p>
          ) : !ready ? (
            <p className="jv-chip-note">{t.projekteLoading}</p>
          ) : alerts.length === 0 ? (
            <p className="jv-chip-note jv-chip-note-empty">{t.projekteChip.empty}</p>
          ) : (
            <ul className="jv-chip-list">
              {alerts.map(({ project, attention }) => (
                <li key={project.slug}>
                  <Link
                    to="/control/projekte-klassisch"
                    className="jv-chip-row"
                    aria-label={t.projekteOpenAria(project.name)}
                  >
                    <span className="jv-chip-row-name">{project.name}</span>
                    <span className="jv-chip-row-reasons">
                      {attention.reasons.map((reason) => (
                        <span
                          className="jv-chip-reason"
                          key={reason.kind}
                          data-reason={reason.kind}
                        >
                          {reasonLabel(reason)}
                        </span>
                      ))}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}

          <Link to="/control/projekte-klassisch" className="jv-chip-showall">
            {t.projekteChip.showAll}
          </Link>
        </div>
      ) : null}
    </div>
  );
}
