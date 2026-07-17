# Projekte-Tab — Live-Board, Spawn-Baum, offene Sessions, Commit-Feed

**Datum:** 2026-07-17 · **Autor:** kimi (Worktree `kimi-projekte-liveboard`, Branch `kimi/projekte-liveboard`)
**Status:** Plan (vor Implementierung). Datenstand: Code-Lektüre main @ `5fdddd6e1`.

## Auftrag (Operator)

Den Projekte-Tab auf die höchste Design- und Informations-Ebene heben:
**wer arbeitet gerade wirklich woran**, **welche Agents hat wer gespawnt**,
**welche Sessions sind noch offen**, **saubere Übersicht aller Commits** —
alles mit echten Daten, keine Demo-Werte.

## Ist-Zustand (main)

- Karten-Grid (`ProjectCard`) mit letztem Commit (ohne Autor), Kanban-Zählern,
  Loop-Footer, SESSIONS-Sektion (tmux, killbar) und CHECK-INS-Sektion (Vault-Claims).
- `AgentsRail` gruppiert ALLE Agents nach **Kind** (Claude/Codex/…), nicht nach
  Projekt — beantwortet „wer arbeitet woran" nur indirekt.
- Backend `hermes_cli/projects_overview.py`: `/api/projects`, `/api/projects/agents`,
  `/api/projects/{slug}` — quellen-isoliert (git/kanban/loops/tmux/coordination),
  nie 500, TTL-Cache 10 s.

## Lücken (belegt durch Code-Lektüre)

1. `tasks.assignee` wird im SQL selektiert, aber im Payload **verworfen**
   (`projects_overview.py` `_kanban_running_agents`).
2. Coordination-Notes tragen `operator:` — **wird nicht geparst**.
3. Commits: nur Hash/Betreff/Zeit — **kein Autor**, kein projektübergreifender Feed.
4. `state.db` (`sessions.parent_session_id`, `model_config._delegate_from`) kennt
   den **Spawn-Baum** — der Tab nutzt ihn gar nicht.
5. „Session noch offen" = `sessions.ended_at IS NULL` + `is_active`-Heuristik
   (300 s, siehe `web_server.py` `/api/sessions`) — im Tab nicht sichtbar.

## Design (Leitstand-Sprache, DESIGN.md-konform)

Reihenfolge im Tab (Operator-Fragen von oben nach unten):

1. **Header + Summary-Strip** — wie bisher, plus Chip „N offene Sessions".
2. **„Wer arbeitet gerade" (LiveBoard, ersetzt AgentsRail)** — EINE Sektion,
   gruppiert nach **Projekt** (Namen aufgelöst, „Unzugeordnet" zuletzt).
   Pro Zeile: Kind-Icon+Name (Data-Palette), Quellen-Chip
   (Prozess / Kanban / Loop / Check-in), Task oder Label, Meta
   (**Lane/Assignee** bei Kanban, **Operator** bei Claims), Laufzeit/Alter,
   Kill-Button nur bei tmux (unverändert SessionKillSheet).
   Sortierung: Prozesse → Kanban → Loops → Check-ins, dann älteste zuerst.
3. **Projekt-Karten** (bestehend, gehoben): Commit-Zeile mit **Autor**,
   Claim-Zeilen mit **Operator**.
4. **„Offene Sessions" (SessionsSection, neu)** — Spawn-Baum aus `state.db`:
   Kinder eingerückt unter dem Elternteil („gespawnt von X · Subagent/Branch/
   Fortsetzung"), Live-LED bei `is_active`, Modell, Nachrichten/Tokens mono,
   Projekt-Tag. Filter-Chips: **Offen** (Default: offen UND frisch) ·
   **Aktiv** (300 s) · **Verwaist** (offen, aber ≥24 h still — der
   Datenqualitäts-Eimer) · **Alle** (36 h Fenster).

   **Real-Daten-Befund (2026-07-17, live `state.db`):** nahezu alle
   Session-Zeilen behalten ewig `ended_at IS NULL` (Gateway-/CLI-Zeilen werden
   selten geschlossen) — ein naiver „Offen"-Filter würde 150+ Zombie-Zeilen
   zeigen. Deshalb markiert das Backend `stale_open` (offen + ≥24 h ohne
   Aktivität) und der Default zeigt nur frische Offene; der verwaiste Rest
   bleibt als eigener Chip mit Zähler sichtbar.
5. **„Alle Commits" (CommitsFeed, neu)** — projektübergreifende Timeline:
   Projekt-Chip, Betreff, Hash, **Autor**, relative Zeit (Top 30, je Projekt 6).
6. Detail-Drawer (bestehend): Commits mit Autor, Agents mit Assignee/Operator.

## Backend (alles additiv, Vertrag bricht nie)

`hermes_cli/projects_overview.py`:

- `_git_log_commits(entry, limit, now)` — gemeinsamer Helper für
  `_project_last_commit`/`_project_recent_commits`/Feed; Format um `%an`
  erweitert → Feld `author`. Fehlertexte unverändert.
- `_kanban_running_agents`: Zeile + `assignee`.
- `_parse_coordination_note`: + `operator`.
- `build_project_detail`: Agent-Re-Map reicht `assignee`/`operator` durch.
- **Neu** `build_sessions_payload(registry, *, state_db_path, now)`:
  read-only SQLite auf `state.db`; Fenster `started_at >= now-36h OR ended_at IS NULL`,
  Limit 150, **open-first sortiert** (damit alt-aber-offen nie dem Cap zum Opfer
  fällt); `last_active` via `MAX(messages.timestamp)`; Marker
  `_delegate_from`/`_branched_from` via `json_extract`; Eltern-Labels per
  einem IN-Query aufgelöst; `spawn_kind ∈ delegate|branch|compression|child`;
  `is_open`/`is_active` (300 s)/`stale_open` (≥24 h); Projekt-Attribution über
  `cwd`/`git_repo_root`.
- **Neu** `build_commits_payload(registry, *, now)`: je Projekt 6 Commits,
  gemergt, absteigend, Cap 30; Fehler pro Projekt isoliert.
- Routen `/api/projects/sessions` + `/api/projects/commits` VOR `/{slug}`
  registrieren; `_RESERVED_SLUGS` + `sessions`, `commits`; TTL-Cache wie bisher.

## Frontend

- `schemas/projekte.ts`: `assignee`/`operator` (nullable), `author` (catch ""),
  neue Schemas `ProjectSessionsResponse`, `ProjectsCommitsResponse` — alle
  Felder defensiv `catch()` wie bisher.
- `hooks/projekte.ts`: `useProjectSessions` (12 s), `useProjectCommits` (30 s).
- `views/projekte/derive.ts`: `liveBoardGroups`, `agentSourceRank`,
  `buildSessionRows` (Baum → flache Liste mit `depth`), `filterSessions`,
  `countOpenSessions`. `groupAgentsByKind` entfällt mit AgentsRail.
- Neu: `LiveBoard.tsx`, `SessionsSection.tsx`, `CommitsFeed.tsx`;
  `AgentsRail.tsx` entfällt (durch LiveBoard ersetzt).
- `ProjekteView.tsx`, `ProjectCard.tsx`, `ProjectDetailDrawer.tsx`: Komposition
  + Autor/Assignee/Operator.
- `i18n/de.ts`: neue Strings (ruhiges Operator-Deutsch, Empty-State-Doktrin).

## Tests

- Backend (`tests/hermes_cli/test_projects_overview.py`): Autor in
  last/recent/feed; Feed-Merge-Ordnung + Cap + Isolation; Assignee; Operator;
  Sessions-Builder (temp state.db: open/active, spawn_kind, Eltern-Auflösung,
  Attribution, Limit); reserved slugs `sessions`/`commits`; Routen erreichbar.
- Frontend: `derive.test.ts` (neue Pure-Functions), Render-Tests für
  LiveBoard/SessionsSection/CommitsFeed, `ProjekteView.test.tsx` angepasst
  (neue Hooks gemockt).

## Gates

- `scripts/run-affected.sh` + `ruff` auf geänderte `.py` (nie Vollsuite interaktiv).
- `scripts/gate-frontend.sh` (web/ im Diff; inkl. Token-Ratchet, tsc -b).
- Visuelle Abnahme nach `hermes-ui-preview` (390 / 820 / Desktop, Konsole 0).

## Explizit NICHT im Scope

- Kein Push/Commit/Deploy (Worktree-Lieferung zur Review).
- Keine Kill-Aktion für state.db-Sessions (nur tmux bleibt killbar).
- Keine `/api/agent-questions`-Anbindung (eigener Endpunkt existiert bereits;
  bewusst nicht doppelt verdrahtet).
