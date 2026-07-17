# Projekte-Tab Mobile — Sessions sichtbar & killbar (Design-Mockups)

**Datum:** 2026-07-17 · **Autor:** kimi (Session `work:2`, Worktree `kimi/projects-mobile-mockups`)
**Status:** Design-Vorschlag, keine Implementierung. Datenstand: Live-GET `/api/projects` + `/api/projects/agents` (07:44 CEST).

## Ausgangslage (Ist)

Screenshot: `ist-zustand-390.png` (Live `/control/projekte`, 390×844). Probleme mobil:

- Agent-Chips im Karten-Footer zeigen nur Kind-Icons — **kein Unterschied zwischen echtem
  laufendem Prozess (tmux) und Vault-Check-in**, kein Task-Text (nur Tooltip), keine Laufzeit.
- **Keine Kill-Aktion** — Sessions schließen geht nur über den Terminal-Tab, nicht vom Projekte-Tab.
- Agents-Rail gruppiert nach Kind statt nach Projekt; weit weg von den Karten.

## Datenfundament (Data-first, nur vorhandene Felder)

Pro Agent (`GET /api/projects/agents`): `kind`, `label` (`"work:2 kimi"` = tmux session:window_index +
window_name), `task` (nur Coordination), `project`, `since`, `source` (`tmux` | `coordination` | `kanban` | `loop`).

- **"Läuft tatsächlich"** = `source == "tmux"` (echter Prozess, `tmux list-panes`).
- **Check-in** = `source == "coordination"` (Vault-Claim; hat `task`-Text; **nicht killbar**).
- **Kill-Mapping** (existiert schon): `POST /api/agent-terminals/terminate` mit
  `{session, window, external: true}` — Session/Fenster direkt aus `label` parsbar
  (`"work:5 claude-agent-2"` → `session="work"`, `window="5"`). `external: true`, weil
  beliebige tmux-Fenster nicht dashboard-managed sind (`agent_terminals.py:terminate_live`).
- **"Was noch offen ist"** = `kanban.{open,blocked,needs_input,review,done_7d}` pro Projekt
  (aus `GET /api/projects`), plus Check-in-Zeilen mit `task`.

Kein einziges Mockup-Element braucht Backend-Neubau — außer dem dünnen UI-Wiring auf den
bestehenden Terminate-Endpunkt.

## Variante A — Projektkarten + Bottom-Sheet-Kill

`mockup-a-projektkarten.html/.png` (Zwei-Zustände-Deck).

- Karten bleiben die Grundeinheit (Evolution, keine Revolution); bekommen eine eigene
  **SESSIONS-Sektion mit echten Reihen**: Kind-Icon (Data-Palette), mono Label, Quelle,
  Laufzeit (`now - since`), ✕-Kill-Button (nur tmux-Zeilen).
- **Check-ins als eigene, ruhigere Reihen** (gestrichelter Punkt, `Check-in`-Tag, Task-Text
  1-zeilig) — visuell klar getrennt von Live-Prozessen.
- **Kill = Bottom Sheet** (mobile-native): nennt Opfer exakt (`work:5 · claude-agent-2`,
  Projekt-Chip, `tmux work:5`, Laufzeit), Warnzeile, `Abbrechen` / `Session beenden`.
  Hinweiszeile erklärt, warum Check-ins nicht killbar sind.
- Trade-off: Kill braucht 2 Taps (Sheet + Bestätigen) — sicherer, aber langsamer; Karte
  wird bei vielen Sessions lang.

## Variante B — Sessions zuerst (Betriebsmodus) + Inline-Kill

`mockup-b-sessions-first.html/.png`.

- **Umgekehrte Hierarchie**: oben KPI-Strip (6 live / 7 Check-ins / 1 blockiert), dann die
  flache Liste **aller** laufenden Prozesse mit Projekt-Chip (`Unzugeordnet` gestrichelt),
  Laufzeit, ✕ — die Frage „was läuft gerade?" ohne Scrollen.
- **Kill = Inline-2-Tap**: ✕ klappt die Zeile zur Bestätigung um (`Nein` / `Ja, beenden`).
  Schnellster Weg: 2 Taps ohne Kontextwechsel.
- „Offene Arbeit" als 2-Spalten-Mini-Grid (Kanban-Zahlen + Blockiert/Input-Chips),
  Check-ins kompakt darunter („alle 7 anzeigen").
- Nebenbei-Vorschlag: `Projekte` rückt in die Primär-Nav (ersetzt `Mehr`-Slot im Mock).
- Trade-off: Projekt-Details (Commit, Loops) rücken nach unten/aus dem Fokus; Inline-Confirm
  ist weniger feierlich als das Sheet (Fehltip-Risiko minimal höher).

## Offene Implementierungsnotizen (für später)

- Optimistic UI: nach Kill-Zeile kurz als „wird beendet…" ausgrauen, bei Fehler (`HTTPException`
  aus `_agent_terminal_error`) Zeile + Toast restaurieren.
- `external: true` ist die scharfe Variante — alternativ Kill-Button nur bei
  dashboard-managed Fenstern zeigen (Capabilities-Endpunkt liefert `managed`).
- Terminate braucht `session` + **window name oder index**: `label` parsen oder Backend
  liefert künftig strukturierte Felder mit.
- Beide Varianten nutzen ausschließlich `theme.css`-Tokens (Bronze auf Graphit) und die
  Data-Palette für Kind-Identität (Kanaltrennung: Status ≠ Identität).

## Dateien

- `mockup-a-projektkarten.html` / `.png` — Variante A (Deck, 2 Zustände)
- `mockup-b-sessions-first.html` / `.png` — Variante B
- `ist-zustand-390.png` — Ist-Screenshot (Live, 390×844)
- `render.py` — `venv/bin/python render.py` rendert beide PNGs neu (390×844, 2× Scale)
