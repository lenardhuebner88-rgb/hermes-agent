# Hermes Deck — „Arbeitet er wirklich?" · Takt, Beweis und Session-Leiste

Stand 2026-08-05, Folgeplan zu `deck-live-puls-12-stufen.md`.

Zwei Aufträge:

1. **Wirklich live sehen, ob ein Agent arbeitet — und was.** Heute: 8-Sekunden-Poll
   und ein `looks_open`-Heuristikpunkt. Das ist eine *Behauptung*, kein Beweis.
2. **Eine kleine Leiste pro Agent:** wie viele Compacts, wie alt die Session, wie
   voll der Kontext.

---

## 0. Grounding — gemessen am 2026-08-05, nicht erinnert

### Was das Journal wirklich enthält (24 h, `buzz-agent@claude`, 4746 Zeilen)

| Target | n | Beispiel |
|---|---|---|
| `acp::tool:` | 4063 | `tool_call: Terminal (execute)`, `tool_call_update: <id> → in_progress` |
| `buzz_acp:` | 142 | `buzz-acp starting: …`, `agent initialized:`, `presence set`, `steer success` |
| `pool::model:` | 8 | `applied model opus[1m] via configOption` |
| `buzz_acp::queue:` | 8 | `extending in-flight deadline by 7200s` |
| `buzz_acp::acp:` | 8 | `hard turn timeout exceeded (silence …)` |

Drei Dinge daraus, die der bestehende Parser noch nicht nutzt:

- **`tool_call_update: <id> → in_progress` feuert alle ~30 s, solange ein Call
  läuft.** Das ist ein echtes Lebenszeichen *vom Agenten selbst* — kein
  Rückschluss. Genau das fehlt heute als sichtbares Signal.
- **`buzz-acp starting: …` markiert den Beginn einer Prozess-Session** (3× in
  24 h bei claude), begleitet von `agent_returned — respawning … outcome="hard_timeout"`.
  Die systemd-Unit läuft durch; die *Session* darunter startet neu. **Das
  Session-Alter ist also nicht `ActiveEnterTimestamp`** — dieser Irrtum wäre in
  einer Leiste stundenlang plausibel falsch.
- **Codex loggt Kompaktierung als Tool-Call:** `tool_call: Context compacting (other)`,
  8× in 24 h. Der bestehende Parser sieht diese Zeile bereits — er unterscheidet
  sie nur nicht von anderer Arbeit.

### Was auf Platte liegt (die genauen Zahlen)

`~/.claude/projects/-mnt-data-services-buzz-agent-workspaces-<stem>/<sessionId>.jsonl`
— das vollständige Claude-Code-Transkript des Agenten. Pro Assistant-Turn:

```json
"usage": {"input_tokens":2, "cache_creation_input_tokens":2617,
          "cache_read_input_tokens":528250, "output_tokens":1143}
```

**Kontextgröße = `input + cache_creation + cache_read` des letzten Turns.**
Kompaktierung ist ein Struktur-Marker, nicht Text:
`{"type":"system","subtype":"compact_boundary","compactMetadata":{"trigger":"manual","preTokens":325542,"postTokens":15587}}`
plus `isCompactSummary:true` auf der Zusammenfassung.

**Live gerechnet, gerade eben:**

| Stem | Session | Alter | Kontext | Compacts | Modell |
|---|---|---|---|---|---|
| claude | `ecb8f97c` | 8,8 h | 530.869 | 0 | claude-opus-5 |
| fable | `d5635067` | 2,8 h | 102.956 | 0 | claude-fable-5 |
| sonnet | `ae0acdd7` | 54,5 h | 60.940 | 0 | claude-sonnet-5 |

Codex hat dieselben Zahlen an anderer Stelle — aber **nicht** unter den Namen,
die hier zuerst standen. Nachgemessen nach Fables Einspruch:

| Feld | Wert im jüngsten Rollout | Bedeutung |
|---|---|---|
| `context_window` | `{"window_id": "019fd300-…"}` | **eine ID, keine Zahl** |
| `model_context_window` | `258400` | die echte Fenstergröße |
| `last_token_usage.input_tokens` | `121430` | der aktuelle Kontext |
| `total_token_usage.input_tokens` | `4512314` | **kumulativ über die ganze Session** |

Wer den ersten Entwurf wörtlich gebaut hätte, hätte `total_token_usage` gegen
`model_context_window` gerechnet: **1746 %**. Der Fehler wäre im Meter sofort
sichtbar gewesen — aber nur, weil er so grotesk ist; bei einer kürzeren Session
hätte er plausibel ausgesehen.

### Was der Fork dafür schon hat (und was er bewusst *nicht* tut)

Ein zweiter Grounding-Lauf durch die Hermes-Seite hat drei Dinge ergeben, die
den Plan verändern:

1. **`hermes_cli/claude_code_harvester.py` liest diese Transkripte bereits** und
   schreibt `session_id`, `input_tokens`, `cache_read_tokens` nach
   `run_usage_facts`. Die Identität Workspace-Pfad → `buzz-agent@<stem>` ist
   ebenfalls schon gebaut: `usage_facts_buzz_attribution.py:33-69`. Ich baue
   also **keine** neue Zuordnung, ich benutze diese.
2. **Der Harvester lässt `context_window_used` absichtlich NULL** —
   `claude_code_harvester.py:651-652`: *"Unavailable timings and context-window
   observations stay absent (NULL) — they are never estimated"*. Das ist eine
   Entscheidung, keine Lücke, und ich stelle mich nicht stillschweigend dagegen.
   Der Unterschied, auf den es ankommt: `input + cache_creation + cache_read`
   des letzten Turns ist **keine Schätzung**, sondern die Prompt-Größe, die die
   API in Rechnung gestellt hat. Sie wird deshalb als eigenes, anders benanntes
   Feld geführt (`prompt_tokens_last_turn`) und **nicht** in
   `context_window_used` geschrieben — sonst kippt eine gepflegte Doktrin durch
   die Hintertür.
3. **Modell → Kontextfenster existiert schon**: `hermes_cli/model_switch.py:859-895`
   (`model_info.context_window`). Schritt S4 entfällt als Neubau und wird zum
   Nachschlagen in dieser Quelle.

Nicht verwechseln: `trajectory_compressor.py` (Trainingsdaten-Batch, für
Live-Agenten ungenutzt), die Hermes-native Kompressionskette
(`sessions.end_reason='compression'`, gezählt als `compressionDepth` in
`acp_adapter/provenance.py`) und Claude Codes `compact_boundary`. Drei
verschiedene Dinge mit demselben Wort. Die Leiste meint das dritte.

Und die ehrlichste Einordnung des Ganzen: `acp_adapter/server.py:880-909`
berechnet Kontextfüllstand und Kompressionstiefe bereits **live** — aber nur für
ACP-über-stdio, und die Buzz-Agenten laufen in einer anderen Welt. Es gibt
keinen HTTP-Endpunkt, der das spiegelt. Genau diese Brücke ist der Auftrag.

### Die Abdeckungslücke, ehrlich benannt

Korrigiert nach Fables Gegenprüfung — die Lücke ist kleiner als zuerst notiert:

| Stem | Kontext exakt | Quelle |
|---|---|---|
| claude, fable, sonnet | **ja** | Claude-Code-Transkript, `usage` je Turn |
| codex, terra | **ja** | Codex-Rollout, `last_token_usage` / `model_context_window` |
| qwen | **ja** | `~/.qwen/projects/…-qwen/chats/*.jsonl`, `usageMetadata.promptTokenCount` (live: 179.842) |
| kimi, grok, hermes | **nein** | kein bekannter Session-Store |

**Die Korrektheitsbedingung für Codex:** Rollouts liegen alle in *einem*
Verzeichnisbaum, gemischt über Loops, Worktrees und Buzz. Der jüngste Rollout
gehörte bei der Messung `/home/piet/.hermes/loops/dashboard-experience/wt` —
einem Loop-Worker. „Jüngste Datei nehmen" hätte dessen Kontext dem Buzz-Codex
zugeschrieben. Es muss über `cwd` auf den Workspace gefiltert werden; das ist
kein Feinschliff, das ist die Bedingung dafür, dass die Zahl überhaupt stimmt.

Für kimi/grok/hermes bleibt nur das Journal: Session-Start, kein Kontextstand. **Die Leiste muss also je Agent verschieden viel wissen dürfen** —
und das sagen, statt eine leere Leiste als „0 %" zu rendern.

### Zwei Fallen, an denen ich beim Messen selbst hängengeblieben bin

1. **Die Zeitstempel im Transkript sind UTC (`…Z`).** Naiv lokal geparst ergab
   „letzte Zeile vor 164 min", real waren es 44. Ein zwei Stunden falsches
   Session-Alter sieht völlig plausibel aus.
2. **Die `sessionId` im Transkript und das `channel=<uuid>` im Journal haben
   nichts miteinander zu tun.** Die Verbindung läuft ausschließlich über den
   Workspace-Pfad (`…workspaces-<stem>`) und die Startzeit. Wer die IDs
   korreliert, korreliert Zufall.

---

## 1. Die Design-Frage: wie beweist man „arbeitet gerade"?

Ein Punkt ist eine Behauptung. Eine Farbe ist eine Behauptung. Der Operator hat
keine Möglichkeit, sie zu prüfen — und genau deshalb war der alte grüne Punkt
wertlos, als vier von acht Units stundenlang stumm waren.

**Das Prinzip: Beweis zeigen, nicht Urteil.** Drei Schichten, von grob nach fein:

### Schicht 1 — Der Takt (das eigentlich Neue)

Eine schmale Leiste über die letzten 60 Sekunden, ein Strich pro Journal-Signal
(Call-Start, `in_progress`-Tick, Abschluss), neueste rechts. Sie **wandert,
während man hinsieht**. Wenn sie steht, arbeitet niemand — das ist keine
Interpretation, das ist die Rohmessung.

Das ist die eine Stelle, an der ich Fables früheres Urteil („Aktivitätsstreifen
= Dekoration, raus") bewusst nicht übernehme, und der Unterschied ist begründbar:
damals sollte der Streifen in **jeder Zeile jedes Agenten** stehen und einen
60-Minuten-Verlauf zeigen — Historie, die das Detail-Sheet besser beantwortet.
Hier steht er **nur bei arbeitenden Agenten**, zeigt **60 Sekunden**, und ist
nicht Verlauf, sondern *Gegenwart*. Er beantwortet die eine Frage, die dieser
Auftrag stellt.

### Schicht 2 — Die Zeile (was)

Der aktuelle Tool-Call in Monospace, einzeilig, daneben ein Zähler „seit 12 s",
der **bei jedem neuen Call auf null springt**. Ein Zähler, den man zurückspringen
sieht, ist der billigste Fortschrittsbeweis, den es gibt — er braucht keine
Animation und keine Farbe.

Dazu ein zweites, schwächeres Signal, das heute schon auf der Leitung liegt und
nicht gerendert wird: `last_signal_seconds_ago`. Ist es jünger als der Call, hat
der Agent seit dem Start des Calls noch gelebt. Ist es genauso alt wie der Call
und der Call ist zehn Minuten alt, **hängt** er. Das ist der Unterschied zwischen
„arbeitet lange" und „steht", und heute kann die App ihn nicht zeigen.

### Schicht 3 — Die Session-Leiste

Eine Zeile unter dem Namen, drei Fakten, 11 sp, Tabellenziffern:

```
▓▓▓▓▓▓░░░░  53 %   ·   8,8 h   ·   0 ⇲
Kontext            Session       Compacts
```

- **Kontext** als 3-dp-Meter in `accentSoft`/`accent`; ab 80 % `warning`, ab
  92 % `danger`. Der Prozentwert steht daneben, weil ein Balken allein keine Zahl
  ist.
- **Session-Alter** aus dem *ersten Transkript-Zeitstempel*, nicht aus systemd.
- **Compacts** als Zähler mit dem Kompressions-Glyph; 0 ist ein guter Wert und
  wird nicht versteckt.

Fehlt eine Zahl (kimi, qwen, grok …), steht dort ein `—` mit Tooltip-Text
„für diesen Agenten nicht messbar" — **nie** eine 0.

---

## 2. Die Transportfrage: SSE oder schneller pollen?

„Wirklich live" heißt unter einer Sekunde. Der 8-Sekunden-Poll schafft das nicht.
Zwei Wege:

**A · SSE-Stream** `GET /api/deck/stream` — der Server hängt an `journalctl -f`
und schiebt jede `tool_call`-Zeile sofort raus. Echt live, ein Request statt
achtzig pro Zehnminutenfenster, und der Takt aus Schicht 1 wird trivial.
Kosten: gehaltene Verbindung, Reconnect-Logik, Doze-Modus.

**B · Cursor-Poll, 2 s** — `since_seq` im bestehenden Payload, Antwort nur mit
Neuem. Einfach, keine neue Infrastruktur, aber achtmal mehr Requests und die
Journal-Abfrage kostet auch mit Cache etwas.

**Meine Empfehlung: A**, mit B als Rückfallebene im selben Endpunkt (fällt der
Stream aus, pollt der Client weiter). Die Dashboard-Seite hat bereits sieben
WebSocket-Routen, SSE über `StreamingResponse` braucht keine neue Abhängigkeit,
und OkHttp kann beides. **Diese Entscheidung will ich von Fable geprüft haben** —
sie ist die einzige im Plan, die schwer zurückzunehmen ist.

---

## 3. Die Schritte

| # | Schritt | Kern |
|---|---|---|
| S1 | **Session-Leser** | `hermes_cli/agent_session_facts.py`: liest das jüngste Transkript je Stem (Claude-Form **und** Codex-Rollout-Form) über die bestehende Pfad→Unit-Zuordnung aus `usage_facts_buzz_attribution.py`. Liefert `session_id, started_at, prompt_tokens_last_turn, context_window, compacts, measured_at`. Tests gegen echte, kopierte Transkript-Ausschnitte inkl. UTC-Falle. |
| S2 | **Compacts aus dem Journal** | Der bestehende Parser erkennt `Context compacting` als eigene Art statt als Tool-Call — deckt die Stems ohne Transkript teilweise ab. |
| S3 | **Session-Start aus dem Journal** | `buzz-acp starting:` / `respawning` als Session-Grenze; für Stems ohne Transkript die einzige Quelle für das Alter. |
| S4 | **Kontextfenster nachschlagen** | Kein Neubau: `model_switch.py` → `model_info.context_window`. Unbekanntes Modell ⇒ `null` und **kein** Prozentwert, statt eines geratenen Defaults. Codex liefert `context_window` im eigenen Rollout mit — dann gilt seiner. |
| S5 | **Payload** | `session`-Block je Agent in `/api/deck/pulse`, mit eigenem `error` je Stem (Abdeckungslücke ist ein Zustand, kein Fehler). |
| S6 | **Takt-Stream** | `GET /api/deck/stream` (SSE) über `journalctl -f`, plus Cursor-Fallback. |
| S7 | **App: Wire + Takt-Puffer** | `SessionFacts`-Modell; ein Ringpuffer der letzten 60 s Signale, gefüllt aus Stream *oder* Poll — die UI merkt den Unterschied nicht. |
| S8 | **App: Takt-Leiste** | Die 60-Sekunden-Leiste auf der Karte des arbeitenden Agenten. |
| S9 | **App: Session-Leiste** | Kontext-Meter + Alter + Compacts, auf der Karte kompakt, im Sheet vollständig. |
| S10 | **App: Hänger-Erkennung** | „steht seit 11 min" statt „arbeitet", wenn der Call alt ist und kein `in_progress` nachkam. |
| S11 | **Design-Doku + Gates** | DESIGN.md: Takt-Doktrin, Meter-Muster, `—`-statt-0-Regel; Gate + Compose-Tests. |

---

## 4. Bekannte Risiken

1. **Transkript-Dateien sind privat (`600`) und gehören dem Nutzer, nicht dem
   Dashboard-Prozess** — läuft der Dienst als derselbe Nutzer? Muss vor S1
   geprüft werden, sonst liest der Server nichts und meldet leer.
2. **66 MB je Stem-Verzeichnis, 70 Dateien.** Die jüngste Datei zu finden ist
   billig (`mtime`), sie ganz zu lesen nicht (2,1 MB). Nur das **Ende** lesen —
   der letzte `usage`-Block genügt für den Kontext; die Compacts brauchen aber
   die ganze Datei. Also: Compact-Zähler cachen und inkrementell fortschreiben.
3. **Der Kontextwert ist der Stand des letzten Turns, nicht „jetzt".** Zwischen
   zwei Turns wächst nichts. Die Leiste muss das Alter dieses Werts kennen.
4. **SSE über Tailscale + Doze.** Ein Stream, der im Hintergrund stirbt und beim
   Aufwachen nicht neu verbindet, sieht aus wie ein Fleet, der nichts tut.


---

## 5. Fable-Review, Runde 2 (2026-08-05) — was sie geändert hat

Vier Blocker, zwei davon gegen meine eigenen Messungen — beide nachgeprüft und
bestätigt:

- **B1 · Codex-Feldnamen falsch.** Siehe oben; hätte ein Meter bei 1746 % ergeben.
- **B2 · Abdeckungstabelle in beide Richtungen falsch.** qwen *hat* einen Store,
  terra läuft über Codex — und der cwd-Filter ist Korrektheitsbedingung, nicht Kür.
- **B3 · `compact_boundary` ist in keinem Buzz-Transkript belegt.** Die
  „0 Compacts"-Spalte oben ist damit exakt das, was ein kaputter Detektor auch
  liefern würde. Die Test-Fixture muss eine echte Boundary-Zeile aus Piets
  eigenen Sessions kopieren, und `isCompactSummary` zählt als zweites Signal mit.
- **B4 · Verzeichnis `…workspaces-claude--scratch-a2-fable/` existiert.** Wer den
  Stem per Suffix auflöst, bucht Claudes Scratch-Session auf fable. Exakter
  Verzeichnisname, kein `endsWith`.

Entwarnt: Leserechte (der Dashboard-Prozess läuft als `piet`, Risiko 1 entfällt),
Kosten des Compact-Zählens (3 ms über die 2,1-MB-Datei — der geplante
inkrementelle Cache war Overengineering und ist gestrichen). Neu aufgenommen:
Filter auf `isSidechain:false`, sonst schiebt ein Task-Subagent seinen
Mini-Kontext als Flotten-Wahrheit unter.

**Transport: sie widerspricht, und sie hat recht.** Ihr stärkstes Argument ist,
dass der Ringpuffer aus S7 die Entscheidung selbst umkehrbar macht — damit fällt
das Hauptargument für „jetzt SSE". Dazu: Doze ist beim Poll strukturell gelöst
(jeder Tick ist sein eigener Reconnect), die Takt-Striche werden nach
*Zeitstempel* positioniert und rendern bei 2 s Latenz identisch, und ein
`journalctl -f` pro Verbindung bleibt bei einem verschwundenen Tailscale-Client
als Zombie stehen — ein Leck, das mein Plan nicht genannt hat.
**Entschieden: 2-Sekunden-Cursor-Poll, solange der Puls-Tab sichtbar ist.**

**Ihre wichtigste Design-Korrektur:** die Atmung des Punktes hängt heute an
`looksOpen` — einer eingestandenen Heuristik. Sie wird an das echte Lebenszeichen
gebunden (`last_signal_seconds_ago < 45`; der `in_progress`-Tick feuert alle
~30 s). Call offen, aber Signal älter als 90 s ⇒ Punkt statisch `warning`,
Statuswort „steht seit X". Damit wechselt die einzige erlaubte Animation der App
vom Behaupten aufs Beweisen — und die Taktleiste ist Zugabe statt Träger.

**Zur Session-Leiste:** Compacts sind auf der Karte die falsche Zahl. Die
Operator-Frage ist nicht „wie oft komprimiert", sondern „wie viel hat diese
Session vergessen, und wann" — also im Sheet `preTokens → postTokens` der letzten
Kompaktierung („325k → 16k, vor 2 h"). Auf der Karte erscheint der Zähler nur
ab 1. Und für nicht messbare Agenten: die Zeile erscheint **gar nicht** — eine
Karte ohne Leiste liest sich als „hat keine", ein `—` liest sich als kaputt.

### Verbindliche Reihenfolge (Fables Schnitt)

**N0 → S1+S5+S9 → S6′+S7+S8 → S3 → Gates.**

| # | Schritt | Warum dort |
|---|---|---|
| **N0** | Lebenszeichen, Hänger-Erkennung, Atmung an Evidenz | **reine App-Arbeit, null Backend** — die Felder liegen seit I2 ungenutzt im Payload. Kleinster Schnitt, größter Teil des Nutzens |
| S1 | Session-Leser (4 Formate, cwd-Filter, Sidechain-Filter, exakter Verzeichnisname) | S4 darin gefaltet — Fenstergröße ist ein Aufruf in `model_switch`, kein Schritt |
| S5 | `session`-Block im Payload, per-Stem-Zustand | |
| S9 | Kontext-Meter + Alter auf der Karte, volle Messreihe im Sheet | |
| S6′ | 2-s-Cursor-Poll statt SSE | |
| S7 | Ringpuffer der letzten 60 s | Transport bleibt austauschbar |
| S8 | Taktleiste, mit benanntem Regelbruch gegen die Motion-Doktrin | |
| S3 | Session-Start aus dem Journal — **nur noch** für kimi/grok/hermes | |
| S2 | **gestrichen, vorbehaltlich einer 5-Minuten-Probe:** „Context compacting" ist bisher nur von Codex belegt, und Codex hat exakte Rollouts. Deckt womöglich niemanden ab, der es braucht |
| S11 | schrumpft auf Gate + APK — DESIGN.md-Fortschreibung gehört in den Commit der jeweiligen UI-Stufe |
