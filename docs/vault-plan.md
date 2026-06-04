# Plan: Obsidian-Vault als geteilter Wissens- & Koordinationslayer — backlog-integriert, ausgerollt per `/goal`

> **Status: PROPOSAL — korrigiert nach Homeserver-Review.** Dieser Plan bleibt absichtlich
> additiv: keine Kanban-Einführung, keine Backlog-Schema-Änderung, keine DB-/API-Schreibschicht,
> kein Verschieben bestehender SSoTs. Umsetzungsschritte mit Datei-/Profil-/Cron-Mutationen bleiben
> Go-/Gate-pflichtig.

## Context

Auf dem Homeserver laufen **Hermes**, **Codex CLI** und **Claude Code** nebeneinander auf demselben Dateisystem,
koordinieren sich aber nicht automatisch — Wissen und Zwischenstände gehen zwischen Sessions/Tools leicht verloren. Ein
Obsidian-Vault ist das tool-neutrale Medium dafür. Das **„dashboard backlog"-Konzept**, in das integriert werden soll,
ist **kein Kanban**, sondern ein **datei-basiertes Backlog**: `.md`-Dateien mit Frontmatter in einem Verzeichnis, die ein
Web-Dashboard **read-only** rendert (`hermes_cli/orchestration_backlog_view.py`, `hermes_cli/family_organizer_view.py`).
Es gibt **keine DB, keine Schreib-API, kein Toolset** — Tasks entstehen/ändern sich, indem man eine `.md`-Datei schreibt.

**Wichtig nach Live-Review:** Der Vault ist eine **dünne additive Wissens-/Koordinationsschicht über den bestehenden
Backlogs**, nicht deren Ersatz. Die Backlog-Verzeichnisse bleiben Source of Truth; der Vault verlinkt, spiegelt und
bewahrt Kontext.

**Branch-Hinweis:** Der Review-Branch liegt auf dem Homeserver nicht auf `origin`, sondern auf
`piet-fork/claude/obsidian-vault-optimization-1T1L0`. Lokale Arbeit sollte auf einem isolierten Worktree erfolgen,
damit der produktive Hermes-Agent-Checkout nicht gestört wird.

**Ziele (Priorität laut User):**
1. **Strikt ergänzend / non-disruptiv** — ersetzt/verschiebt nichts Bestehendes (höchste Priorität).
2. **Parallele Session-Handoffs** zwischen Tools mit vollem Kontext.
3. **Kombinierte Wissensbasis** — Koordination (ADRs/Status/Handoffs) **und** PARA/Zettelkasten.
4. **Autonome Cron-Berichte** (datierte Digests zu AI-News/Best-in-Class-Harnesses, Upstream-Changelogs).
5. **Synergie mit den datei-basierten Backlogs** (beide: Orchestration **und** Family-Organizer) — kein Parallelsystem.
6. **Ruhiger, phasenweiser Rollout** — der `/goal` stoppt nach jeder Phase und berichtet.

### Verifizierte Code- und Live-Grundlage
- **Vault-Pfad:** `OBSIDIAN_VAULT_PATH` ist live aktuell **nicht gesetzt**. Der Obsidian-Skill nennt als generischen
  Fallback `~/Documents/Obsidian Vault`; dieser Pfad existiert auf dem Homeserver nicht. Auf Piet's Homeserver existiert
  `/home/piet/vault` als kanonischer Vault/SSoT-Pfad. Phase 0 muss deshalb den konkreten Vault-Pfad explizit auflösen
  und darf nicht still einen anderen Fallback verwenden.
- **Hermes-Memory:** root/default-Memory liegt bei `~/.hermes/memories/MEMORY.md`. Profile haben eigene Memories unter
  `~/.hermes/profiles/<name>/memories/MEMORY.md`; `HERMES_HOME` bestimmt die Isolation. Cron-Agenten laufen mit
  `skip_memory=True`, daher darf Cron **nicht** von einem Memory-Pointer abhängen.
- **Obsidian-Skill existiert** (`skills/note-taking/obsidian/`), nutzt `OBSIDIAN_VAULT_PATH` bzw. den Skill-Fallback und
  reine File-Tools (`read_file/write_file/patch/search_files`) → **kein neuer Code/kein Tool nötig**. Der Skill löst aber
  keine Sync-, Lock- oder Indexierungsprobleme.
- **Cron-CLI:** `hermes cron create <schedule> <prompt> [--skill] [--workdir] [--profile]` ist vorhanden. Es gibt kein
  `hermes cron create --toolsets ...`; `obsidian` ist ein Skill und muss über `--skill obsidian` geladen werden. Named
  Profiles müssen bereits existieren. Cron deaktiviert intern mindestens `cronjob`, `messaging`, `clarify` und läuft mit
  `skip_memory=True`.
- **Profile:** isolierte `HERMES_HOME` unter `~/.hermes/profiles/<name>/`. Ein Profil `vault-cron` existiert live aktuell
  nicht; dessen Erstellung wäre eine separate, Go-pflichtige Profil-Mutation.
- **Backlog-Pfade:**
  - `ORCHESTRATION_BACKLOG_DIR` ist live nicht gesetzt; Default/realer Pfad: `/home/piet/orchestration/backlog`.
  - `FAMILY_ORGANIZER_BACKLOG_DIR` ist live nicht gesetzt; Default/realer Pfad:
    `/home/piet/projects/family-organizer/backlog/items`.
- **Backlogs** (`hermes_cli/orchestration_backlog_view.py`, `family_organizer_view.py`):

  | | Orchestration | Family-Organizer |
  |---|---|---|
  | Env-Var | `ORCHESTRATION_BACKLOG_DIR` | `FAMILY_ORGANIZER_BACKLOG_DIR` |
  | Quelle | Working-Tree-Dateien, falls `ORCHESTRATION_BACKLOG_REF` leer ist | bevorzugt git `origin/main` eines separaten Repos; Fallback FS |
  | Datei | `{id}.md` / freie Markdown-Dateien | `{id}-{slug}.md` mit 4-stelliger id |
  | Status | `backlog/todo/doing/review/done` | `now/next/later/in_progress/blocked/done` |
  | Frontmatter | `id`, `title`, `status`, `priority`, `dependsOn`, `root`, `gate`, `planGate`, `created` sind praktisch relevant | kanonisch: `id`, `title`, `status`, `owner`, `risk`, `area`, `updated`; `result` bei `done`; `lane` optional |
  | Parser | erste-`:`-Split (kein YAML-Lib); List-API gibt nur ausgewählte Felder aus, Detail startet aus Frontmatter | erste-`:`-Split; API gibt explizite Felder aus, keine freien Custom-Keys |

### Recherche-Basis (5 Quellen-Streams, zitiert)
- **Hybrid statt Single-Store:** jeder Agent behält seine eigene Memory-/Session-Mechanik; *darüber* eine **dünne**
  gemeinsame Markdown-Schicht, klein halten (<~200 Zeilen) gegen „context rot" (Anthropic multi-agent system; Chroma
  *context-rot*; Cognition *Don't build multi-agents*).
- **AGENTS.md = De-facto-Standard**; Claude kann über `CLAUDE.md`/Import-Pointer angebunden werden → *eine Quelle,
  viele Leser, „point don't duplicate"*.
- **Anti-Korruption:** dedizierter Agenten-Ordner, **create-only/append**, section-gezielter `patch` statt
  Full-File-Overwrite, datierte/eindeutige Dateinamen, Frontmatter-Ownership.
- **Concurrency:** Datei-pro-Item + UUID/Datums-Namen reduzieren Konflikte; *„one task per agent"*; committed git-ref
  für SSoT-kritische Dashboards, Working-Tree nur dort, wo er bewusst living planning scratch ist.
- **Storage-Location:** Symlinks sind für Obsidian riskant; **separat + Cross-Link** ist der sichere additive Default.
  „Backlog im Vault" ist eine spätere SSoT-Änderung und deshalb nicht Teil des additiven Rollouts.
- **Links:** `[[wikilinks]]` nur vault-intern; alles, was das Dashboard rendert, nutzt Klartext-Pfade/URLs, weil die
  Dashboard-Bodies keine Wikilinks auflösen.

---

## A. Vault-Struktur & Konventionen (Inhalt der mitgelieferten Templates)

Root = explizit aufgelöster Vault-Pfad. Auf Piet's Homeserver ist der erwartete Pfad `/home/piet/vault`; `OBSIDIAN_VAULT_PATH`
muss in Phase 0 geprüft/gesetzt werden, bevor Dateitools auf den Vault zeigen. Kombiniert Koordination + PARA + Zettelkasten,
Obsidian-nativ.

```
<vault>/
├── _meta/   CONVENTIONS.md (kanonisch) · Frontmatter Schema.md · Vault MOC.md
│            templates/{handoff,adr,report,zettel,project,backlog-orchestration,backlog-fo}.md
├── _inbox/
├── Coordination/  Handoffs/ · Decisions/ (ADRs) · Status/ (Backlog Mirror.md, Project Dashboard.md) · Logs/ (append-only, monatlich)
├── Reports/   ai-news/ · upstream-watch/ · vault-gardening/  (+ Reports MOC.md)
├── Projects/ (hermes-agent/ · family-organizer/ …)   Areas/   Resources/ (zettel/ · literature/)   Archive/   Daily/
```

- **Naming:** `YYYY-MM-DD-<slug>.md`; Zettel `YYYYMMDDHHmm-<slug>.md`. Jeder Ordner hat `* MOC.md`. Keine Orphans.
- **Frontmatter (jede Vault-Note):** `title, type, status, agent (hermes|codex|claude|human), created, updated, tags[], backlog_ref (opt.), session_ref (opt.), lock: null`. Flach & portabel; Werte dürfen `:` enthalten, weil lokale Parser nur first-colon splitten.
- **Links:** vault-intern wikilinks; in Backlog-Item-Bodies & dashboard-sichtbaren Cross-Refs **klartext-Pfade/URLs**.
- **Concurrency (sync-agnostisch):** (1) datierte create-only Dateien für High-Traffic (Handoffs/Reports/Logs) → nie Konflikt; (2) append-only Logs via anchored `patch` nach `## Log`; (3) advisory `lock: "<agent>:<ISO-ts>"` (>30 min = stale) nur für wenige mutable Notes (Dashboards/MOCs), Eigentum des gardening-Cron; (4) Vault als git-Repo/Obsidian-Git erst als spätere Stufe → Konflikte werden Merge-Konflikte statt stiller Overwrites.

**Single Source of Truth + Pointer (nicht kopieren):** kanonisch `_meta/CONVENTIONS.md` (<~200 Zeilen); dünne Pointer in
`<vault>/AGENTS.md` (Codex) und `<vault>/CLAUDE.md` (Claude). Hermes bekommt den Vault-Kontext primär über
`--skill obsidian`, `--workdir <vault>` und explizite Prompts. Ein Memory-Pointer darf nur profilgenau und als Komfort-
Hinweis genutzt werden; er ist **keine** Cron-Abhängigkeit, weil Cron mit `skip_memory=True` läuft. Pointer-Abschnitte in
Repo-`AGENTS.md`/`CLAUDE.md` sind separate Kontext-Mutationen und nicht Teil des Minimal-Scaffolds.

## B. Backlog-Synergie (Kern) — beide Backlogs, ohne Schema-Änderung

**Rollentrennung — kein Feld gehört beiden:**
- **Backlog-Verzeichnisse + Dashboard = der LIVE-Task-State** (was todo/doing/blocked/done ist). Operativ, strukturiert.
- **Vault = das DURABLE Wissen/Narrativ** (Warum = ADRs, Wie-weiter = Handoffs, Was-passiert = Reports, Referenz = Zettel).
- **Regel:** State im Backlog, Wissen im Vault. Sie **referenzieren** sich, **kopieren** nie.

**Speicherort-Empfehlung:**
> **Default = „getrennt + Cross-Link", strikt additiv** — beide Backlogs bleiben **exakt wo sie sind**. Der Vault verweist
> auf Items; Backlog-Items verweisen im Body auf Vault-Notizen. Das ehrt Priorität #1 und funktioniert für **beide**
> Backlogs trotz unterschiedlicher Quell-Semantik.
>
> **Nicht Phase 1–5 / nicht additiv:** `ORCHESTRATION_BACKLOG_DIR=$OBSIDIAN_VAULT_PATH/...` würde die operative Quelle
> des Orchestration-Dashboards umbiegen. Das ist eine spätere SSoT-/Routing-Änderung und braucht eigenen Review + Go.
> Für Family-Organizer ist diese Option ausgeschlossen, weil FO ein separates Repo mit `origin/main`-Semantik ist.
> **Symlink (Option C) ausgeschlossen** wegen Obsidian-/Indexierungs-/Korruptionsrisiko.

**Drei Brücken — keine Backlog-Schema-Änderung:**
1. **Handoff ⇄ Backlog-Item:** Handoff-Note bekommt `backlog_ref: orchestration:<id>` bzw. `fo:<id>`. Das Backlog-Item
   bekommt eine Klartext-Pfadzeile im `body`, z. B. `Vault: /home/piet/vault/Coordination/Handoffs/<file>.md`. Ein
   zusätzlicher Custom-Key `vault_ref` ist erlaubt, aber **nicht verlässlich dashboard-sichtbar**: FO-API reicht freie
   Custom-Keys nicht aus; Orchestration listet ebenfalls nur ausgewählte Felder. Sichtbarkeit läuft deshalb immer über
   den Body.
2. **Report → Backlog:** Findet ein Cron-Report etwas Aktionierbares, schreibt er zuerst die reiche Report-Note in
   `Reports/…`. Ein Backlog-Item wird nur als separater, gegateter Schritt erzeugt:
   - Orchestration: Datei ins bestehende Working-Tree-Dir, mit dem realen Frontmatter-Vertrag (`status: backlog`,
     `priority`, `dependsOn`, `root`, `gate`, `planGate`, `created` soweit passend) + Body-Link zurück.
   - Family-Organizer: **kein autonomer Cron-Commit/Push**. FO-Backlog ist Repo-/Governance-SSoT; Report-Cron erzeugt
     höchstens einen Vorschlag/Handoff. Ein echtes FO-Item braucht explizites Go, sauberen Repo-Status, Gate und erst
     dann Commit/Push nach dem FO-Protokoll.
3. **Backlog → Vault-Spiegel:** Der gardening-Cron liest beide Backlogs (Dateien bzw. `/api/orchestration/backlog` +
   `/api/family-organizer/backlog`, falls verfügbar) und schreibt einen **read-only**, datierten Snapshot des offenen/
   blockierten Stands nach `Coordination/Status/Backlog Mirror.md` (+ `Backlog MOC`). Backlog-Verzeichnisse bleiben
   Source of Truth; der Vault spiegelt nur.

## C. Session-Handoff-Protokoll

Template `_meta/templates/handoff.md`, **eine neue Datei pro Handoff** unter
`Coordination/Handoffs/YYYY-MM-DD-<slug>-handoff.md`. Abschnitte: Goal · Current state · What's done · Next steps · Open
questions/blockers · Relevant links · How to resume. Frontmatter `status: open`, optional `backlog_ref`, `session_ref`.
- **Offenen Handoff finden:** `search_files` content `status: open` glob `*-handoff.md` → neueste; `Handoffs MOC.md` führt die kuratierte Open-Liste (gardening hält sie ehrlich).
- **Hermes-Verzahnung:** `session_ref` → Hermes-Session-Export oder resumierbarer Hinweis, sodass Hermes exakt `--resume`
  kann, während jedes Tool die tool-neutrale Handoff-Note liest (komplementär, additiv).

## D. Cron-Report-Jobs (optional; `vault-cron`-Profil nur nach Setup-Go)

Cron-Jobs müssen mit realen CLI-Flags angelegt werden, z. B. `hermes cron create '0 9 * * *' '<prompt>' --skill obsidian --workdir /home/piet/vault --profile vault-cron`.
Es gibt kein `--toolsets`-Flag für `cron create`. Das Profil `vault-cron` muss vorher existieren; andernfalls läuft der Job im Scheduler-Profil oder `--profile` schlägt fehl.

1. **Tägliches AI-News/Harness-Digest** (`0 9 * * *`) → `Reports/ai-news/<TODAY>-…md` + Wikilink in `Reports MOC.md`; aktionierbar → Orchestration-Vorschlag oder gegatetes Orchestration-Backlog-Item (`status: backlog`).
2. **Wöchentlicher Upstream-Watch** (`0 8 * * 1`, `--workdir <repo-or-vault>`) → `Reports/upstream-watch/<ISO-WEEK>-…md`; konventionsrelevant → Verweis auf `CONVENTIONS` + Handoff/Backlog-Vorschlag.
3. **Wöchentliches Vault-Gardening** (`0 20 * * 0`) → MOCs neu bauen, Handoffs>7d & Notes>30d als `stale` markieren, optionale Archivierung nur nach klarer Regel, **Backlog-Spiegel** (B.3), **Link-Check** (meldet/öffnet Vorschlag, **löscht nie**), Bericht nach `Reports/vault-gardening/`.

## E. Liefergegenstand — in DIESEM Branch committen & pushen (Branch `claude/obsidian-vault-optimization-1T1L0`)

1. **`docs/vault-rollout-goal.md`** — **der `/goal`-Brief** (Herzstück): einfügbarer Standing-Goal-Text, den eine Hermes-Session auf dem Homeserver autonom abarbeitet, **phasenweise mit „STOPP & berichte, warte auf ‚weiter'"** nach jeder Phase. Enthält die E2E-Akzeptanzkriterien.
2. **`docs/vault-templates/`** — kanonische Quelldateien, die das Skript ins Vault kopiert: `_meta/CONVENTIONS.md`, `Frontmatter Schema.md`, `Vault MOC.md`, `templates/*.md` **inkl. neuer `backlog-orchestration.md` & `backlog-fo.md`**. Die Backlog-Templates müssen exakt den realen Ziel-Verträgen folgen und dürfen Custom-Keys nur optional/nicht-sichtbar verwenden.
3. **`scripts/setup-vault.sh`** — idempotent/additiv: resolved `OBSIDIAN_VAULT_PATH` oder explizit `/home/piet/vault`; legt Baum (§A) an, kopiert Templates nur falls **nicht vorhanden**, überschreibt nichts, trockenlauf-fähig. Kein stiller Fallback auf einen nicht existenten Pfad.
4. **`docs/vault-architecture.md`** — Referenz: Architektur, §B-Synergie, Speicherort-Empfehlung, exakte `hermes cron create`-Befehle mit `--skill obsidian`, Profil-Setup-Gate, kurze Quellen-Bibliografie.
5. **Pointer-Dateien:** `<vault>/AGENTS.md` und `<vault>/CLAUDE.md` sind Teil des Vault-Scaffolds. Repo-`AGENTS.md`/Repo-`CLAUDE.md` Pointer sind separate, optional gegatete Kontext-Mutationen und dürfen nicht still mit dem Minimal-Rollout vermischt werden.

## F. `/goal`-Brief — Phasenstruktur (Inhalt von `docs/vault-rollout-goal.md`)

Standing Goal, stoppt nach **jeder** Phase mit kurzem Statusbericht:
- **Phase 0 – Preflight (read-only):** `OBSIDIAN_VAULT_PATH` auflösen; wenn unset, `/home/piet/vault` als Kandidat prüfen; beide Backlog-Dirs/Env-Vars vorhanden?; Cron-CLI-Flags prüfen; Profile prüfen (`vault-cron` existiert?); Repo-Status für FO sauber? → Bericht, STOPP.
- **Phase 1 – Scaffold:** `bash scripts/setup-vault.sh` nur nach Go → Baum + `_meta` + Templates + MOC-Seeds + ADR-0001 + Beispiel-Handoff; zweiter Lauf idempotent. → Bericht, STOPP.
- **Phase 2 – Pointer:** Vault-`AGENTS.md`/`CLAUDE.md`; optional `OBSIDIAN_VAULT_PATH` in `~/.hermes/.env` falls fehlt. Memory-Pointer nur profilgenau und nicht als Cron-Abhängigkeit. Repo-Pointer nur mit separatem Go. → Bericht, STOPP.
- **Phase 3 – Backlog-Brücke:** Beispiel-Handoff mit einem **echten Orchestration-Backlog-Item** verknüpfen — `.md` ins bestehende Orchestration-Dir schreiben (`body`-Link auf Vault; `vault_ref` optional) und Handoff-Frontmatter `backlog_ref` setzen; im Dashboard prüfen. FO nur als Vorschlag/Handoff, kein autonomer Repo-Push. → Bericht, STOPP.
- **Phase 4 – Crons:** nur falls `vault-cron` existiert oder nach separatem Profil-Go angelegt wurde; Jobs mit `--skill obsidian --workdir /home/piet/vault --profile vault-cron` registrieren; gardening einmal `hermes cron run <id>`. → Bericht, STOPP.
- **Phase 5 – E2E-Verifikation:** Akzeptanztests, PASS/FAIL je Check.

## G. Reihenfolge

1. **Jetzt (Branch):** nur Proposal-/Artefaktkorrekturen committen; keine Live-Mutationen.
2. **Homeserver (Hermes-Session, autonom):** Repo/Branch bewusst auschecken, `/goal` mit dem Brief → Phasen 0–5 mit Stopps und Go-Gates.
3. **Nach 1 Woche echter Handoffs:** Sync (`obsidian-git`) und mögliche Orchestration-Backlog-SSoT-Änderung separat revisiten; nicht als Teil des additiven Rollouts.

---

## Verifikation

**Repo-Artefakte (vor Push):**
- `bash -n scripts/setup-vault.sh` + Trockenlauf mit explizitem Testpfad → Baum/Dateien == §A; zweiter Lauf idempotent; keine bestehenden Dateien überschrieben.
- `backlog-orchestration.md`/`backlog-fo.md`-Frontmatter **gegen die echten Parser/Checker** prüfen:
  - Orchestration: Status-Enum, `priority`, `dependsOn`, `root`, `gate`, `planGate`, `created`; Body-Link sichtbar.
  - FO: `id`, `title`, `status`, `owner`, `risk`, `area`, `updated`; `result` bei `done`; `lane` optional; Body-Link sichtbar.
- `docs/vault-architecture.md`-Cron-Befehle gegen `hermes_cli/main.py`/Cron-CLI-Flags prüfen: `--skill`, `--workdir`, `--profile`; kein `--toolsets`.

**E2E auf dem Homeserver (Phase 5, autonom; PASS/FAIL):**
1. **Vault-Resolution:** Hermes liest `_meta/CONVENTIONS.md` aus dem explizit aufgelösten Vault-Pfad und nennt die Lock-Regel.
2. **Handoff-Flow:** Beispiel-Handoff via `search_files status:open` auffindbar; Frontmatter trägt `backlog_ref`.
3. **Backlog-Brücke:** das verknüpfte Orchestration-Item erscheint im Dashboard (`/api/orchestration/backlog`); `body` zeigt den Vault-Pfad. `vault_ref` ist optional und nicht als sichtbares API-Feld gefordert.
4. **Cron-Berichte:** `hermes cron run <gardening-id>` → MOCs gebaut, `vault-gardening`-Report datiert da, `Backlog Mirror.md` enthält beide Backlogs.
5. **Report→Backlog:** Test-Lauf mit fingiertem aktionierbarem Punkt erzeugt zuerst Report + Handoff/Vorschlag; ein echtes Backlog-Item nur nach dem jeweiligen Gate.
6. **Cross-Tool:** Codex (`--workdir <vault>`) und Claude Code lesen je `CONVENTIONS.md` via Vault-Pointer.

### Kritische Dateien
- vorhanden (lesen/anhängen): `skills/note-taking/obsidian/SKILL.md`, `hermes_cli/main.py`, `cron/scheduler.py`, `hermes_cli/orchestration_backlog_view.py`, `hermes_cli/family_organizer_view.py`, `tools/memory_tool.py`, `AGENTS.md`
- **neu/zu liefern (dieser Branch):** `docs/vault-rollout-goal.md`, `docs/vault-architecture.md`, `docs/vault-templates/*`, `scripts/setup-vault.sh`, optionale Pointer-Dateien
