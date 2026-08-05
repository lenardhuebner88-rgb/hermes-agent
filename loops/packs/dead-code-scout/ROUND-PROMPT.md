# Runde: Dead-Code-Kandidaten mit zwei Belegen melden

Du bist ein **Scout**, kein Entferner. In dieser Runde darfst du **NICHTS loeschen**, keine Kandidaten instrumentieren und keine Produktdatei veraendern. Erzeuge ausschliesslich den maschinenlesbaren Bericht `{{STATE_DIR}}/dead-code-candidates.json` im pack-eigenen State-Verzeichnis und fasse ihn im Ledger zusammen.

## Verbindlicher Messvertrag

1. Verwende ausschliesslich die eingecheckte Revier-Allowlist `scripts/dead_code_revier_allowlist.txt`. Bestimme oder erweitere das Revier nicht aus Prosa. Insbesondere sind `web/src/App.tsx`, `web/src/main.tsx`, `web/index.html` und `web/vite.config.ts` upstream-eigen und tabu.
2. Fuehre mit der gesperrten Projektumgebung aus:
   `/home/piet/.hermes/hermes-agent/.venv/bin/python scripts/dead_code_candidates.py --repo-root "$PWD" --allowlist scripts/dead_code_revier_allowlist.txt --output "{{STATE_DIR}}/dead-code-candidates.json"`
3. Ein berichteter Kandidat braucht **zwei Belege**:
   - `vulture==2.14` meldet das Symbol unbenutzt.
   - Die repo-weite Textsuche des Scanners findet weder den Symbolnamen noch den relativen Quellpfad oder den Python-Modulnamen ausserhalb seiner Deklaration.
4. Der zweite Beleg ist zwingend, weil dynamische Aufrufe der statischen Analyse entgehen. Eine Zeichenkette wie `python -m package.module` in `pack.yaml`, Cron-Konfiguration, systemd-Units oder Shell-Skripten verwirft den Fund. Dasselbe gilt fuer einen direkten Aufruf ueber den relativen Pfad einer Scriptdatei.
5. Nenne keine Anzahl dynamischer Einstiege ohne einen vorher festgelegten und im Ledger notierten Zaehlvertrag. Fuer diesen Scout ist keine solche Bestandszahl erforderlich.

## Bericht und Ledger

- Der Scannerbericht ist die Kandidaten-SSoT. Uebernimm keine verworfenen Funde in eine zweite handgeschriebene Kandidatenliste.
- Pruefe nach dem Lauf, dass jeder Eintrag unter `candidates` durch die Allowlist gedeckt ist und `repo_search_evidence.matches` leer ist.
- Melde Kandidatenzahl, Berichtspfad, `vulture==2.14`, Allowlist-Pfad und den wortwoertlichen Suchvertrag aus dem Bericht.
- Wenn der Scanner oder die Pruefung scheitert, melde den Fehler und parke die Runde. Veraendere nicht die Allowlist, um einen Fund passend zu machen.
- Keine Loeschung, kein Grabstein und kein Autoland in dieser Stufe. Die spaetere Grabstein-Stufe entscheidet separat ueber weitere Behandlung.
- Schreibe als einzige Zeile nach `{{STATE_DIR}}/last-status`: `REPORTED <anzahl>` bei mindestens einem belegten Kandidaten, `DRY no-candidates` bei null Kandidaten oder `BLOCKED <konkreter Grund>` bei einem fehlgeschlagenen Vertrag.

## Harte Verbote

Kein Commit, kein `git push`, kein Merge, kein Branch-Wechsel und keine Aenderung ausserhalb des pack-eigenen State-Verzeichnisses. Der Bericht ist Beobachtung, keine Landung.
