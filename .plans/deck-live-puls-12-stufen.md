# Hermes Deck — „Wer arbeitet gerade woran" · 12 Iterationsstufen

Stand 2026-08-05. Ziel: die Deck-App vom *Zähler* zum *Instrument*. Der Operator
soll am Telefon in unter drei Sekunden sehen, **welcher Agent gerade was tut**,
**woran gearbeitet wird**, und **eingreifen können**, ohne an den Rechner zu gehen.

---

## 0. Grounding — was heute wirklich da ist (gemessen, nicht erinnert)

### Was die App heute zeigt
`ControlSummary.Pulse` → `ControlCard.PulseBand` zeigt **Namen + Zählstand**:
„3 arbeiten", „12 Tool-Calls in 1 Stunde". Der Agents-Tab (`ui/Screens.kt:145`)
listet Agenten mit Heartbeat-Punkt und Modellwahl. Das ist alles.

### Warum sie nicht mehr zeigen *kann*
`hermes_cli/buzz_agent_heartbeat.py:104-132` ruft
`journalctl --user -u buzz-agent@<stem>.service -o json --output-fields=_SYSTEMD_USER_UNIT -g 'tool_call:'`
— es fragt `MESSAGE` **bewusst nicht ab** (`:113-118`, Begründung: ANSI-Escapes
kämen als Byte-Array). Gezählt wird die Zeilenmenge, `working = tool_calls_recent > 0`
(`:63-64`). Der Inhalt wird nie gelesen. Das ist die eine Ursache.

### Sonde: der Inhalt ist da und ist informativ
`journalctl --user -u 'buzz-agent@*' --since -24h | grep 'tool_call:'`, ANSI gestrippt,
Top-Werte:

```
767  tool_call: Terminal (execute)
187  tool_call: Bash (execute)
112  tool_call: Edit (edit)
 67  tool_call: Guardian Review (think)
 51  tool_call: Read (read)
 36  tool_call: terminal: /mnt/data/services/buzz/target/release/buzz canvas get --channel 7abf1e2a-662... (execute)
 26  tool_call: terminal: date -u +%Y-%m-%dT%H:%M:%SZ (execute)
 11  tool_call: Grep (search)
```

Format: `tool_call: <label> (<kind>)`, kind ∈ {execute, edit, read, search, think,
other, unknown}. Bei Terminal-Aufrufen steht das **echte Kommando** im Label,
serverseitig auf ~80 Zeichen gekürzt. Dazu `tool_call_update: <id> → completed`
als Abschluss-Signal. Live-Belegung 3 h: claude 121, codex 114, qwen 66, hermes 53,
kimi 44, terra 9, fable 7, grok 0 — bei 8 laufenden `buzz-agent@*`-Units.

**Das ist genau die Information, die Piet verlangt, und sie wird heute weggeworfen.**

### Was Hermes sonst noch hat, das die App nicht anfasst
Kanban-Plugin-Routen unter `/api/plugins/kanban/…`:

| Route | Inhalt | heute in der App |
|---|---|---|
| `GET /workers/active` | jeder laufende Run: run_id, task_id/-titel/-status, assignee, profile, worker_pid, Heartbeat-Note, Token-Zähler | **nein** |
| `GET /runs/live-events` | Cross-Worker-Ticker (claimed/heartbeat/completed/blocked), pollbar via `since_id` | **nein** |
| `GET /runs/{id}/timeline`, `GET /tasks/{id}/activity` | Ereignisse eines Runs/Tasks | **nein** |
| `GET /api/dispatch/holds` | welche Tasks dispatch-blockiert sind | **nein** |
| `POST /runs/{run_id}/terminate` | Run abbrechen | **nein** |
| `POST /tasks/{root_id}/cancel-chain` | Kette abbrechen | **nein** |
| `POST /tasks/{task_id}/flow-release` | Freigabe-Gate lösen | **nein** |

### Drittes Defizit: es gibt kein Polling
Kein Timer, kein WorkManager, keine `repeatOnLifecycle`-Schleife im ganzen
`main/kotlin`-Baum. `loadAgents()` läuft **nur** bei Screen-Eintritt
(`ui/HomeScreen.kt:89`, `ui/Screens.kt:150`). Wer auf dem Deck-Tab bleibt, sieht
beliebig alte Zahlen — und `ControlSummary` trägt für die Agentendaten **keinen
Zeitstempel**, kann das Alter also nicht einmal zugeben.

---

## 1. Die drei Sätze, die den Umbau tragen

1. **Ein Zähler ist kein Puls.** „12 Tool-Calls" beantwortet keine Frage, die ein
   Operator hat. „codex · seit 40 s · `rg -n dispatch_once kanban_db.py`" beantwortet sie.
2. **Ein Live-View ohne Poll ist ein Screenshot.** Jede Zahl bekommt ein Alter,
   und das Alter wird angezeigt, nicht verschwiegen.
3. **Sehen ohne Eingreifen frustriert.** Wer am Telefon einen Run entgleisen
   sieht und ihn nicht stoppen kann, hätte auch nicht hinsehen brauchen.

---

## 2. Was dem Operator sonst fehlt (mein Urteil, nicht die Auftragszeile)

Gereiht nach dem, was am Telefon wirklich passiert:

| # | Lücke | Warum sie beißt | Stufe |
|---|---|---|---|
| A | **Kein Kanban-Blick** — die App kennt Buzz-Agenten, aber nicht die Hermes-Läufe | „welcher Agent arbeitet" heißt für Piet *auch* „welcher Worker hängt an welcher Karte" | I8 |
| B | **Kein Not-Aus** | Runaway-Loops und Token-Verbrennung sind belegte Vorfälle; der Rechner ist nicht immer in Reichweite | I9 |
| C | **Keine Frische** | Jede Zahl auf dem Schirm behauptet, jetzt zu gelten. Das ist bei Screen-Entry-Laden schlicht falsch | I5/I10 |
| D | **Kein Blocker-Radar** | Ketten stehen still, weil etwas auf *Piet* wartet — die App zeigt Freigaben aus Buzz, aber nicht die Dispatch-Holds | I8 |
| E | **Budget als Balken statt als Warnung** | „87 %" ist keine Handlung. „In ~50 min ist Opus zu" wäre eine | I10 |
| F | **Keine Historie** | „arbeitet gerade" ohne „seit wann / wie lange schon dasselbe" verbirgt genau den Hänger, den man sucht | I6/I7 |
| G | **Kein Push** | bewusst **out of scope** — braucht FCM-Projekt und Serverschlüssel; wird nicht in dieser Runde gebaut, nur nicht verbaut |

---

## 2b. Fable-Review (2026-08-05) — was sie am Plan geändert hat

Fable hat den Plan gegen den Code und gegen ein eigenes Live-Journal geprüft.
Drei Befunde haben den Plan wirklich verändert:

1. **Tool-Calls überlappen — eine Dauer wäre erfunden.** Drei
   `tool_call_update … → completed` in derselben Sekunde, während weitere Calls
   dazwischen öffneten. Start- und Abschlusszeile teilen keine ID. Konsequenz:
   die Route behauptet nie „läuft seit 40 s", sondern nur *„zuletzt: X · vor
   40 s"* plus ein ausdrücklich schwaches `looks_open` („seither hat nichts
   Abschluss gemeldet"). Umgesetzt in I1.
2. **Reihenfolge nach Wert, nicht nach Baubequemlichkeit.** Kanban-Läufe und
   Not-Aus (`workers/active`, `terminate`) brauchen **keinen** neuen Parser —
   die Routen existieren. Sie hinter sieben Parser-Stufen zu stellen war falsch.
3. **Puls gehört *nicht* auf Tab 1.** Er verdrängt „Wartet auf dich", also genau
   die Stelle, an der Piet handeln muss. Puls nimmt den Platz der alten
   Agenten-Liste (Tab 3), die er ohnehin ersetzt; die PulseBand auf Tab 1 wird
   tappbarer Einstieg dorthin.

Dazu ihre stärkste Empfehlung, die den Plan neu zentriert:

> **Ein einziger, gereihter Handlungsstapel.** Der Plan baut vier Sichten und
> lässt den Operator selbst zusammensetzen, was davon Handlung verlangt. Das
> Instrument ist die Umkehrung: Freigaben, Holds, überzogene Runs, gescheiterte
> Agenten und knappes Budget in **einer** priorisierten Liste, je Zeile
> *Was · seit wann · [Aktion]*. Nicht mehr Sichtbarkeit — weniger
> Entscheidungsarbeit.

Ebenfalls übernommen: lokale Benachrichtigungen per WorkManager statt „Push ist
out of scope" (braucht kein FCM); ein Nacht-Digest „was ist passiert"; Stillstand
als eigener Zustand, der nach **oben** sortiert statt ans Ende. Gestrichen: der
60-Minuten-Aktivitätsstreifen in jeder Listenzeile — Dekoration, die pro Zeile
Aggregation kostet; Verlauf gibt es einmal im Detail-Sheet.

## 3. Die 12 Iterationsstufen

Jede Stufe ist für sich lauffähig und abnehmbar. Reihenfolge nach Operator-Wert
(Fable-Befund 2): was ohne neuen Parser geht, kommt zuerst.

| # | Stufe | Kern | Zustand |
|---|---|---|---|
| I1 | Tool-Call-Parser | `hermes_cli/buzz_agent_tool_calls.py` | **fertig** — 33 Tests, echte Journal-Fixture |
| I2 | Aktivitäts-Route | `/api/buzz/agents/activity` | **fertig** |
| I3 | Mobiler Sammel-Payload | `/api/deck/pulse` | **fertig** — 17 Tests, live gegen echtes Board |
| I4 | Wire-Layer der App | Modelle + Parser + Fixtures | **fertig** — 11 Tests gegen echte Antwort |
| I5 | Polling, Frische, Tokens | Lifecycle-Loop + `Theme.kt` | **fertig** — 6 Tests |
| I6 | Handlungsstapel | `ActionStack` | **fertig** — 13 Tests |
| I7 | Puls-Screen (Tab 3) | ARBEITET · LÄUFT · STEHT · STILL | **fertig** — 8 Compose-Tests am Emulator |
| I8 | Agent-Detail-Sheet | Timeline der letzten Calls | **fertig** |
| I9 | Eingriff | Run beenden · Kette · Freigabe | **fertig** (Verdrahtung; live ungeprüft) |
| I10 | Nacht-Digest & Stillstand | „Zuletzt passiert" + STILL-Sektion | **fertig** |
| I11 | Lokale Benachrichtigungen | WorkManager | **offen** |
| I12 | Design-Audit, Gates, APK | DESIGN.md, Gate, Release | **teilweise** — DESIGN.md + Gate grün, APK offen |

### Was noch aussteht, ausdrücklich

- **I11 gar nicht gebaut.** Die App meldet sich nicht von selbst; sie muss
  geöffnet werden.
- **I9 ist verdrahtet, aber nie gegen einen echten Lauf ausgelöst worden.** Ein
  `terminate` auf einen laufenden Worker im Vorbeigehen zu testen wäre ein
  Eingriff ins Produktivsystem gewesen. Der Pfad ist tot-getestet bis zur
  HTTP-Grenze, nicht darüber hinaus.
- **Der Handlungsstapel öffnet Sheets, hat aber keinen Knopf pro Zeile.**
- **Nicht in die Live-Branch gemergt und kein APK gebaut** — das ist ein
  Operator-Entscheid, kein Bau-Schritt.

**I1 — Tool-Call-Parser** ✅
`hermes_cli/buzz_agent_tool_calls.py`. Liest das Journal **mit** `MESSAGE`,
dekodiert die Byte-Array-Form, strippt ANSI, parst `tool_call: <label> (<kind>)`
und `tool_call_update: <id> → <status>`. Maskiert Zugangsdaten vor der Ausgabe,
klemmt Labels auf 160 Zeichen, cached 5 s. `Diagnostics(lines_seen,
lines_understood)` macht einen Formatwechsel sichtbar, statt ihn als stille
Flotte auszugeben. `looks_open` statt „läuft seit" — siehe Fable-Befund 1.
*Abgenommen:* 33 Tests grün gegen eine **echte** Journal-Fixture (72 Zeilen, sechs
Units, ANSI intakt, `MESSAGE` als Byte-Array); Live-Lauf gegen das echte Journal:
231 Zeilen gesehen, 231 verstanden, 0,06 s.

**I2 — Aktivitäts-Route**
`GET /api/buzz/agents/activity?limit=20` in fork-eigenem Modul. Je Agent:
`stem, display_name, state, latest{label,kind,seconds_ago}, looks_open,
open_for_seconds, last_signal_seconds_ago, recent[]`, dazu `diagnostics` und
`window_seconds` top-level.
*Done when:* Route-Contract-Test grün, 401 ohne Session, Payload gegen echten
Journal-Stand geprüft.

**I3 — Ein Request für das Telefon**
`GET /api/deck/pulse` bündelt: Buzz-Agenten samt Aktivität,
`/api/plugins/kanban/workers/active`, `dispatch/holds`, `account-usage` und den
`runs/live-events`-Ticker. Jeder Teil mit eigenem `as_of` und eigenem `error`;
ein kaputter Teil darf die anderen nicht mitreißen. Board-Frage entschieden:
der Payload nennt das abgefragte Board explizit, statt Vollständigkeit zu
suggerieren (Fable, Nebenbefund b).
*Done when:* Test beweist Teil-Degradation; Contract-Test „Session-Cookie reicht
für `/api/plugins/kanban/*`" (Fable, Nebenbefund c); < 1,5 s auf dem echten Host.

**I4 — Wire-Layer**
Modelle `ToolCall`, `AgentPulse`, `WorkerRun`, `DispatchHold`, `ActionItem`,
`DeckPulse` + `HermesPayloads`-Parser. Tests gegen **eingecheckte reale
Payload-Fixtures**, nicht gegen erfundenes JSON.

**I5 — Polling, Frische, Tokens**
`repeatOnLifecycle(STARTED)`: 8 s im Vordergrund, sofortiger Tick bei Rückkehr,
Backoff 8→16→32→60 s nach Fehlern, harter Stopp im Hintergrund. Jeder Wert trägt
`asOf`; `Freshness.of(asOf, now)` → frisch / alt / stale. Neue Tokens in
`Theme.kt` (Fable, Design-Urteil): `monoStyle` (12 sp, Monospace,
`textSecondary`), Tabular-Ziffern für alle „seit"-Timer — sonst zappeln die
Zahlen bei jedem Tick — und die Stale-Dimmung als benanntes Muster.

**I6 — Handlungsstapel „Jetzt dran"**
Eine gereihte Liste aus fünf Quellen: Buzz-Freigaben, Dispatch-Holds, Runs über
Zeitbudget, gescheiterte Agenten, Budget-Reichweite unter einer Stunde. Je Zeile
*Was · seit wann · [eine Aktion]*. Die Reihung ist reine, getestete Logik
(`ActionStack.of(...)`) — nicht in der Composable, damit sie sich nicht selbst
widerspricht. Leerzustand ist ein Erfolg und sagt das auch.

**I7 — Puls-Screen (Tab 2)**
Kopfzeile `PULS` + Frische (tappbar zum Neuladen), dann vier Sektionen:
**ARBEITET** (Punkt mit der einen erlaubten Atmung, Name, Kind-Chip, aktueller
Tool-Call einzeilig monospace mit Ellipse), **LÄUFT** (Kanban-Runs mit
Profil-Kante, Laufzeit, Heartbeat-Note, Fortschritt in CapacityBand-Geometrie),
**STEHT** (Holds mit Grund), **STILL** (Name + „still seit 14 h"; über der
Schwelle mit `warning`-Punkt nach oben). `accent` erscheint auf diesem Screen
fast nicht — die Semantikfarben tragen die Zustände.

**I8 — Agent-Detail-Sheet**
Timeline der letzten 30 Calls, nach Minute gruppiert, Kind-Icon, Kommandos
monospace auf 2 Zeilen geklemmt. Kopf: Zustand, Modell (bestehende Modellwahl),
Unit-Status, letzter Start. Hier — und nur hier — der Aktivitätsverlauf.

**I9 — Eingriff**
Aus dem Run-Sheet: **Run beenden** (`POST /runs/{id}/terminate`), **Kette
abbrechen** (`cancel-chain`), **Freigabe lösen** (`flow-release`). Bestätigung
nennt den Titel des Ziels; Ergebnis kommt vom Server, kein optimistisches UI.
Der Knopf lebt ausschließlich im Sheet, nie als Icon in der Listenzeile.

**I10 — Nacht-Digest & Stillstand**
„Seit gestern Abend": fünf Zeilen fertig/gescheitert aus `live-events`.
Stillstand wird ein First-Class-Zustand mit Dauer — der belegte Vorfall (acht
Units `active`, vier stundenlang stumm) ist genau das, was die Sortierung
„arbeitend zuerst" verstecken würde.

**I11 — Lokale Benachrichtigungen**
WorkManager alle 15 min, zieht `deck/pulse`, meldet **nur bei Zustandswechsel**:
neuer Hold, Run blockiert, Budget-Reichweite unter 30 min, Agent gescheitert.
Kein FCM, kein Serverschlüssel. Abschaltbar je Klasse in den Einstellungen.

**I12 — Design-Audit, Gates, Auslieferung**
Design ist Definition-of-Done **jeder** UI-Stufe (Fable: I11-als-Sammelstufe ist
ein Anti-Pattern), diese Stufe ist nur noch Audit + Fortschreibung von
`DESIGN.md`: Motion-Doktrin, Stale-Muster, `edgeFor()` auf Profile erweitert,
Monospace-Token. Dann `scripts/gate-android.sh hermes-deck`,
`scripts/run-affected.sh` + `ruff`, Release-APK via `scripts/release-deck-apk.sh`.

## 4. Bekannte Risiken

1. **Journal-Format ist kein Vertrag.** Ändert der ACP-Agent sein Log-Format,
   liefert der Parser leere Listen. Gegenmittel: der Parser meldet
   „N Zeilen gesehen, 0 geparst" als eigenen Fehlerzustand — stille Leere ist
   verboten (Kontrollprobe eingebaut statt nachträglich).
2. **`journalctl` kostet.** 8 Units × alle 8 s ist zu viel. Deshalb serverseitig
   ein Cache mit TTL und **einem** Journal-Aufruf über alle Units.
3. **Kommandozeilen im Log können Geheimnisse tragen.** Der Parser maskiert
   Muster wie `token=`, `password=`, `Authorization:`, `sk-…` vor der Ausgabe.
4. **Upstream-Merge-Fähigkeit.** Keine Zeile in `kanban_db.py` oder anderen
   upstream-eigenen Dateien; neue Routen leben in eigenen Modulen.
