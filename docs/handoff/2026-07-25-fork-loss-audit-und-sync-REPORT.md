---
title: Fork-Loss-Audit, Upstream-Sync 187, Autoland-Entkopplung
date: 2026-07-25
agent: Claude (Opus 5, 1M)
status: abgeschlossen
repo: /home/piet/.hermes/hermes-agent
---

# Bericht: Audit → Sync → zwei Nebenstränge

`main` von `67eb20348` auf **`43dfc232d`**, alles nach `piet-fork` gepusht,
0 Commits offen. Vier Merges, jeder einzeln gegatet.

## Ausgangslage

Der Sync vom 24.07. (1962 Commits) wurde gelandet, obwohl sein Receipt
`status: blocked` trug — Begründung: ein „blob-based fork-loss selftest" melde
**56 Dateien**. Neun Minuten später lag er trotzdem auf `main`; 42 rote Tests
folgten. Das Werkzeug lag in einem Wegwerf-Worktree, `MERGE-REPORT.md` wurde nie
committet. **Beides ist weg**, im Repo, auf allen Branches und im Vault
nachgesucht. Die 56 sind nicht nachrechenbar und werden es nie wieder sein.

Daraus die Reihenfolge dieses Blocks: erst das Messinstrument neu bauen und den
letzten Sync nachträglich vermessen, dann entscheiden, ob die nächsten 187
Commits kommen.

## 1 — Das Messwerkzeug (`scripts/refactor/fork_loss_check.py`)

Committet, mit Tests, im Repo. Es kann nicht mehr verdampfen — das war der
eigentliche Auftrag, nicht die Zahl.

**Verlust-Definition, zweistufig:**

1. *Risikofläche* = Dateien, die **Fork und Upstream** gegen die alte Merge-Base
   geändert haben. Nur-Fork-Dateien bekommen eine Existenzprüfung. Pfade, die der
   Fork **selbst** gelöscht hat, fliegen raus — das ist eine Fork-Entscheidung,
   kein Verlust. Für den 24.07.-Sync: Fork 1967, Upstream 2597, **Schnitt 277**.
2. *Zeilen-Sweep* über fork-hinzugefügte Zeilen, dann *Symbol-Bestätigung* per
   `ast` → `SYMBOL_GONE` / `SYMBOL_CHANGED` / `LINE_ONLY` / `FILE_GONE`.
   Verschobene, umindentierte und umgebrochene Zeilen dürfen **nicht** zählen.

**Tragender Test:** neun synthetische Git-Fixtures mit per Konstruktion bekanntem
Verlust. Der wichtigste ist der Reflow-Fall — ein Werkzeug, das jede
Umformatierung als Verlust meldet, produziert genau die unüberprüfbare Zahl, die
es ersetzen soll.

**Zwei Bugs, die erst der Echtlauf fand:** lokale Zuweisungen galten als Symbole
(eine verlorene Funktion zersplitterte in einen Fund pro Zeile); und
fork-gelöschte Dateien wurden als Verlust gemeldet — **alle drei** `FILE_GONE`
des ersten Laufs waren solche Phantome.

## 2 — Das Audit des 24.07.-Syncs

**46 Funde in 35 Dateien** gegen den heutigen Stand (38 gegen den Merge-Commit).
Jeder Fund klassifiziert, mit Beleg:

| Kategorie | Zahl |
|---|---|
| A — Upstream-Ersatz, Funktion erhalten | 21 |
| B — bewusste Fork-Entscheidung (eigener Post-Merge-Refactor) | 4 |
| D — Rauschen (Kommentar, Format, generierte Datei, Upstream-eigener Baum) | 21 |
| **C — Kollateralschaden** | **0** |
| **UNKLAR** | **0** |

Stichproben selbst nachgeprüft statt übernommen: die drei `SYMBOL_GONE` in
`tests/hermes_cli/test_backup.py` (Upstream ersetzte die Testklasse: 6 einfache
Fork-Tests → 11 mit Modus-Matrix), die Exit-Semantik in `hermes_cli/main.py`
(`sys.exit(0)` vs. Durchfallen — beweisbar identisch, weil `main()` danach endet
und nackt aufgerufen wird) und `_set_task_model_override_in_txn`.

**Damit war die Operator-Schwelle erfüllt** (null unerklärte Verluste), und der
Sync war vorab freigegeben.

### Altlast, kein Verlust

Der 24.07.-Sync hat unseren Desktop-Godfile-Split (`e123a2e73`, 17.07.)
zurückgedreht: `apps/desktop/electron/main.ts` ist wieder ein 10.894-Zeilen-
Monolith. Die fünf extrahierten Module liegen verwaist (~70 KB, **null**
Importeure, nachgezählt). Kein fehlendes Verhalten, aber duplizierter toter Code
— und ein Lehrstück für die Standing Rule „Fork-Code nie in Upstream-eigene
Dateien".

## 3 — Der Sync (187 Commits, `0cb41891c`)

317 Dateien, +22.999/−1.724. **12 Konflikt-Hunks in 8 Dateien**, `kanban_db.py`
nicht darunter — wie vorhergesagt (0 von 187 Commits fassen es an).

**Kein einziger Konflikt wurde als *ours* oder *theirs* abgeräumt.** Jeder hatte
beidseitig Substanz:

- `gateway/delivery_ledger.py` — unsere `reply_to`/`metadata_json`-Migration lebt
  in Upstreams umgebautem `_initialize_schema()` weiter; alle Schreibpfade auf
  deren FD-Leak-Fix `_transaction()`.
- `agent/credential_pool.py` — Runtime-Key-Herkunft (Fork) und
  `_unmatched_rotation_streak` (Upstream), orthogonal, beide behalten.
- `cron/scheduler.py` — `HERMES_HOME`-Anker (Fork) + `workdir`-cwd (Upstream).
- `hermes_cli/config.py` — `restart_drain_timeout` bleibt bei 180s, Upstreams
  Gegenargument daneben dokumentiert; deren `build_wait_timeout` übernommen.
- `hermes_cli/auth.py` — Retry-Schleife (Fork) statt Doppellesung, aber deren
  `encoding="utf-8"` übernommen.
- `gateway/run.py` — Disconnect-Budget + Stale-Pending-Cleanup (Fork) neben
  erweiterter `_platform_connect_timeout_secs`-Signatur (Upstream).
- zwei Testdateien — beide Seiten; bei `test_system_prompt.py` werden beide
  Helfer tatsächlich aufgerufen.

**Bug im Vorbeigehen gefunden:** die fork-eigene `claim_owned_retry()` hing noch
am leckenden `_connect()`. Upstreams Leak-Fix konnte sie nicht sehen, weil die
Funktion bei ihnen nicht existiert — die Sorte Schaden, die ein Sync still
hinterlässt.

**Das Werkzeug hat seinen eigenen Sync validiert:** Risikofläche 61 Dateien,
8.976 Fork-Zeilen, **3 fehlend — alle drei die oben genannten bewussten
Auflösungen**. Null Unerklärtes.

## 4 — Autoland-Entkopplung (`8845b6acb`, Operator-Entscheid)

Die Engine-Rolle pro Phase ist nicht mehr Teil der Landungsautorität; sie floatet
wie das Modell. Vorher setzte ein `PHASE_*_ENGINE`-Override den manual-land-Marker
— `dashboard-experience` loggte deshalb **seit dem 22.07.** jede Nacht
`AUTOLAND übersprungen`, während seine night-overrides auf
`codex`/`alibaba-token-plan`/`codex` zeigten.

Entfernt an drei Stellen: Ladezeit-Vertrag (jetzt prompt-only),
Sicherheitsprojektion, Laufzeit-Autorisierung. `safety_sha256` neu berechnet.

**Weiterhin fail-closed gebunden:** Prompt-Zuordnung, Prompt-Inhalt (SHA-256),
kuratierter Pack-Pfad, gebundenes Live-Repo, `base_branch`, `land_remote`,
`land_push`, `land_gates`. Landung verlangt weiter Verify-PASS, Visual-Gate und
grüne land_gates.

**Bewusst aufgegeben:** die Garantie Bauer ≠ Prüfer.

**Nicht offensichtlich, hätte den Fix wirkungslos gemacht:** bei gestauter Arbeit
läuft der Resume-Pfad, und der prüft den Marker (`runner.py:1847`) **vor** dem
Aufräumen (`:1851`). Der Marker vom 22.07. musste separat entfernt werden.
Es lag echte gestaute Arbeit vor (`b3dd3a259`, 92 Zeilen netto).

## 5 — Nightly-Foundry (`43dfc232d`)

`hermes-autoresearch-v2-nightly` war seit 24.07. rot. **Es war kein
`RecursionError`** — der Journal-Frame war ein `faulthandler`-Watchdog-Dump nach
1805 s. Ursache: `generate_mutants` baute pro entdeckter Stelle einen
vollständigen Mutanten und wandte `max_mutants` erst auf das Ergebnis an. Ziel
Tag 205 ist `kanban_db.py`: 10.899 Stellen × ~1,3 s ≈ **4 Stunden**, um sechs
Mutanten zu behalten.

Fix: Stellen vorher sortieren, Schleife bei `max_mutants` abbrechen. Selbst
nachgemessen: **6 Mutanten in 9,05 s**. Ausgabe-Äquivalenz gegen die alte
Implementierung über 8 Fälle byte-identisch.

## Offen / Operator

1. **Der Gateway läuft seit 11:32 auf altem Code.** Der Sync hat `gateway/` +
   `tui_gateway/` um 1.886 Zeilen geändert (`tui_gateway/server.py` +672). Ein
   Neustart schießt laufende Sessions und Worker ab — bewusst nicht angefasst.
2. **Verwaister Desktop-Split** (~70 KB toter Code, siehe oben) — löschen oder
   neu anschließen.
3. **Axis B (`kanban_db.py`-Umbau) hat jetzt Zahlen gegen sich.** 0 von 187
   Upstream-Commits fassen die Datei an, letzte Berührung 29.06.; die 12
   Konflikte lagen in `gateway/`, `cron/`, `agent/`, `hermes_cli/`. Die Prämisse
   „jedes Upstream-Update kollidiert dort" ist für diesen Zyklus widerlegt.
4. **`scripts/run-affected.sh` ist zweimal als Gate ausgefallen** — einmal per
   Voll-Suite-Fallback mit `EXIT=0` bei abgebrochenem Lauf, einmal weil es mit
   explizitem Ref keine untracked Dateien sieht. Beide Male hätte es grün
   gemeldet, ohne das Richtige zu testen.

## Prozess-Regel aus dem Vorfall

Ein Receipt mit `status: blocked` darf ohne dokumentierte Operator-Freigabe nicht
gelandet werden. Am 24.07. lagen neun Minuten zwischen „kein Merge durchgeführt"
und dem gelandeten Merge; der Preis waren 42 rote Tests.
