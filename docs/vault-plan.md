# Plan: Obsidian-Vault als geteilter Wissens- & Koordinationslayer — backlog-integriert, ausgerollt per `/goal`

> **Status: PROPOSAL — Review durch Hermes ausstehend.** Dieser Plan wurde in einem ephemeren
> Container (ohne Zugriff aufs Live-System) erstellt. Eine Hermes-Session auf dem Homeserver soll ihn
> gegen die reale Umgebung prüfen (Pfade, Env-Vars, Cron-CLI, Memory-Datei, Profile, Backlog-Parser)
> und bei Bedarf direkt korrigieren, **bevor** irgendetwas implementiert wird.

## Context

Auf dem Homeserver laufen **Hermes**, **Codex CLI** und **Claude Code** nebeneinander auf demselben Dateisystem,
koordinieren sich aber nicht — Wissen und Zwischenstände gehen zwischen Sessions/Tools verloren. Ein Obsidian-Vault
ist das tool-neutrale Medium dafür. Das **„dashboard backlog"-Konzept**, in das integriert werden soll, ist **kein
Kanban**, sondern ein **datei-basiertes Backlog**: `.md`-Dateien mit YAML-Frontmatter in einem Verzeichnis, die ein
Web-Dashboard **read-only** rendert (`hermes_cli/orchestration_backlog_view.py`, `hermes_cli/family_organizer_view.py`).
Es gibt **keine DB, keine Schreib-API, kein Toolset** — Tasks entstehen/ändern sich, indem man eine `.md`-Datei schreibt.
**Das ist dasselbe Medium wie der Vault → die Integration ist nativ, nicht verbogen.** (Eine frühere kanban-basierte
Fassung wurde verworfen und der Branch sauber zurückgerollt.)

**Arbeitsteilung:** Dieser Container ist ein **ephemerer Klon**, nicht der Homeserver. Hier wird nur **Repo-Material**
committet (Templates, Setup-Skript, Architektur-Doku, `/goal`-Brief). Das **Live-Bringen + E2E-Verifizieren** macht
eine **Hermes-Session auf dem Homeserver** autonom über den `/goal`-Brief.

**Ziele (Priorität laut User):**
1. **Strikt ergänzend / non-disruptiv** — ersetzt/verschiebt nichts Bestehendes (höchste Priorität).
2. **Parallele Session-Handoffs** zwischen Tools mit vollem Kontext.
3. **Kombinierte Wissensbasis** — Koordination (ADRs/Status/Handoffs) **und** PARA/Zettelkasten.
4. **Autonome Cron-Berichte** (datierte Digests zu AI-News/Best-in-Class-Harnesses, Upstream-Changelogs).
5. **Synergie mit den datei-basierten Backlogs** (beide: Orchestration **und** Family-Organizer) — kein Parallelsystem.
6. **Ruhiger, phasenweiser Rollout** — der `/goal` stoppt nach jeder Phase und berichtet.

### Verifizierte Code-Grundlage (read-only Agenten)
- **Hermes-Memory** = `~/.hermes/memories/MEMORY.md` (pro Profil geklont, `hermes_cli/profiles.py`) — *nicht* die Repo-`AGENTS.md` (Dev-Guide).
- **Obsidian-Skill existiert** (`skills/note-taking/obsidian/`), nutzt `OBSIDIAN_VAULT_PATH` (aus `~/.hermes/.env`, Fallback `~/Documents/Obsidian Vault`) + reine File-Tools (`read_file/write_file/patch/search_files`) → **kein neuer Code/kein Tool nötig**.
- **Cron** = `hermes cron create <schedule> <prompt> [--skill] [--workdir] [--profile]` (`hermes_cli/cron.py`); Jobs in `~/.hermes/cron/jobs.json`, Output `~/.hermes/cron/output/{job_id}/{ts}.md`. Im Cron-Kontext deaktiviert: `cronjob, messaging, clarify`.
- **Profiles** = isolierte `HERMES_HOME` unter `~/.hermes/profiles/<name>/` (`profiles.py`) → restriktives `vault-cron`-Profil für Jobs.
- **Backlogs** (`hermes_cli/orchestration_backlog_view.py`, `family_organizer_view.py`):
  | | Orchestration | Family-Organizer |
  |---|---|---|
  | Env-Var | `ORCHESTRATION_BACKLOG_DIR` | `FAMILY_ORGANIZER_BACKLOG_DIR` |
  | Quelle | **Working-Tree** (live) | **git `origin/main`** eines *separaten* Repos |
  | Datei | `{id}.md` (id = freeform) | `{id}-{slug}.md` (id = 4-stellig) |
  | Status | `backlog/todo/doing/review/done` | `now/next/later/in_progress/blocked/done` |
  | Felder | `priority, dependsOn[], planGate, gate, root` | `owner, risk, area, lane, result` |
  | Parser | erste-`:`-Split (kein YAML-Lib), **Custom-Keys werden durchgereicht**, `body` als Plaintext gerendert | dito |

### Recherche-Basis (5 Quellen-Streams, zitiert)
- **Hybrid statt Single-Store:** jeder Agent behält seine Memory-Datei; *darüber* eine **dünne** gemeinsame Markdown-Schicht, klein halten (<~200 Zeilen) gegen „context rot" (Anthropic multi-agent system; Chroma *context-rot*; Cognition *Don't build multi-agents*).
- **AGENTS.md = De-facto-Standard** (60k+ Repos, Codex/Cursor/Copilot lesen es); Claude bindet es per `@AGENTS.md`-Import oder Symlink ein → *eine Quelle, viele Leser, „point don't duplicate"* (agents.md; Claude `code.claude.com/docs/en/memory`).
- **Anti-Korruption:** dedizierter Agenten-Ordner, **create-only/append**, section-gezielter `patch` statt Full-File-Overwrite, datierte/eindeutige Dateinamen, Frontmatter-Ownership (basic-memory; cyanheads/obsidian-mcp-server).
- **Concurrency:** datei-pro-Item + UUID/Datums-Namen reduzieren Konflikte; *„one task per agent"*; **committed git-ref als Source of Truth** fürs Dashboard, Working-Tree nur als „in-flight"-Overlay (backlog.md; dstask; git-bug).
- **Storage-Location:** Symlinks **offiziell von Obsidian abgeraten** (data-loss/corruption/crash; extern geschriebene Dateien werden unzuverlässig indexiert — genau der Agenten-Fall). „Backlog *im* Vault" = natives Idiom & echte Single-Source-of-Truth; „separat + Cross-Link" = sicherer additiver Fallback (help.obsidian.md/Symbolic+links; pjeby/obsidian-symlinks).
- **Links:** `[[wikilinks]]` brechen in Nicht-Obsidian-Renderern → **Vault-interne Notizen = wikilinks** (Graph/Backlinks-Wert für Menschen in Obsidian); **alles, was das Dashboard rendert (Backlog-Bodies/Cross-Refs) = klartext-Pfade/URLs** (Dashboard zeigt `body` als Plaintext, löst keine wikilinks auf).

---

## A. Vault-Struktur & Konventionen (Inhalt der mitgelieferten Templates)

Root = `OBSIDIAN_VAULT_PATH`. Kombiniert Koordination + PARA + Zettelkasten, Obsidian-nativ.

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
- **Frontmatter (jede Note):** `title, type, status, agent (hermes|codex|claude|human), created, updated, tags[], backlog_ref (opt.), session_ref (opt.), lock: null`. Flach & portabel, damit Custom-Parser *und* Obsidian es lesen.
- **Links:** vault-intern wikilinks; in Backlog-Item-Bodies & dashboard-sichtbaren Cross-Refs **klartext-Pfade/URLs**.
- **Concurrency (sync-agnostisch):** (1) datierte create-only Dateien für High-Traffic (Handoffs/Reports/Logs) → nie Konflikt; (2) append-only Logs via anchored `patch` nach `## Log`; (3) advisory `lock: "<agent>:<ISO-ts>"` (>30 min = stale) nur für die wenigen mutable Notes (Dashboards/MOCs), Eigentum des gardening-Cron; (4) Vault als git-Repo (`obsidian-git`) als spätere Stufe → Konflikte werden Merge-Konflikte statt stiller Overwrites.

**Single Source of Truth + Pointer (nicht kopieren):** kanonisch `_meta/CONVENTIONS.md` (<~200 Zeilen); dünne Pointer in `<vault>/AGENTS.md` (Codex), `<vault>/CLAUDE.md` (Claude, via `@AGENTS.md`-Import bzw. Symlink), **eine Zeile** in `~/.hermes/memories/MEMORY.md` (Hermes) — plus angehängte Pointer-Abschnitte in der Repo-`AGENTS.md`/`CLAUDE.md`.

## B. Backlog-Synergie (Kern) — beide Backlogs, ohne Schema-Änderung

**Rollentrennung — kein Feld gehört beiden:**
- **Backlog-Verzeichnisse + Dashboard = der LIVE-Task-State** (was todo/doing/blocked/done ist). Operativ, strukturiert.
- **Vault = das DURABLE Wissen/Narrativ** (Warum = ADRs, Wie-weiter = Handoffs, Was-passiert = Reports, Referenz = Zettel).
- **Regel:** State im Backlog, Wissen im Vault. Sie **referenzieren** sich, **kopieren** nie.

**Speicherort-Empfehlung (delegiert an mich; recherche- + constraint-gestützt):**
> **Default = Option B „getrennt + Cross-Link", strikt additiv** — beide Backlogs bleiben **exakt wo sie sind**; der Vault verweist auf Items und Reports schreiben Items in die *bestehenden* Verzeichnisse (jeweils mit dem korrekten Git-Verhalten). Das ehrt Priorität #1 und funktioniert für **beide** Backlogs trotz unterschiedlicher Quell-Semantik (Working-Tree vs. separates origin/main-Repo).
> **Opt-in-Upgrade (später) = Option A nur fürs Orchestration-Backlog:** dessen Working-Tree-Dir lässt sich sauber in einen Vault-Unterordner zeigen (`ORCHESTRATION_BACKLOG_DIR=$OBSIDIAN_VAULT_PATH/Coordination/Backlog`) → echte Single-Source-of-Truth + Obsidian-Graph „for free". **Nicht** für Family-Organizer (separates Repo, origin/main-Semantik — würde verflechten). **Symlink (Option C) ausgeschlossen** (Obsidian warnt offiziell vor data-loss/corruption; extern geschriebene Dateien werden unzuverlässig indexiert).
>
> *Begründung:* Recherche nennt A „best-in-class" und C gefährlich; B den sicheren additiven Fallback. Da der User „additiv/non-disruptiv" als #1 setzt **und** zwei Backlogs mit unterschiedlicher Git-Semantik (eines in fremdem Repo) existieren, ist **B die kontextuell richtige Best-in-Class-Wahl**; A bleibt dokumentierter Opt-in-Pfad für den einen Backlog, wo es sauber ist.

**Drei Brücken — alle über bestehende Felder (Custom-Keys werden vom Parser durchgereicht, keine Schema-Änderung):**
1. **Handoff ⇄ Backlog-Item:** Handoff-Note bekommt `backlog_ref: orchestration:<id>` (bzw. `fo:<id>`); das Backlog-Item bekommt Custom-Key `vault_ref: Coordination/Handoffs/<file>.md` **plus** eine Klartext-Pfadzeile im `body` (im Dashboard sichtbar). Bidirektional, übersteht Archivierung.
2. **Report → Backlog:** Findet ein Cron-Report etwas Aktionierbares, schreibt er (a) die reiche Report-Note in `Reports/…` **und** (b) ein **neues Backlog-Item `.md`** ins passende Verzeichnis — mit dem **exakten Frontmatter-Vertrag** des Ziel-Backlogs (Orchestration: `status: backlog`, `priority`; FO: `status: next`, `owner/risk/area`) + `vault_ref` + Body-Link zurück. **Git-Quelle respektieren:** Orchestration → Datei ins Working-Tree-Dir; FO → ins family-organizer-Repo-Checkout schreiben und nach `origin/main` committen (das liest das Dashboard). *So treiben Reports Arbeit, statt sich zu stapeln.*
3. **Backlog → Vault-Spiegel:** Der gardening-Cron liest beide Backlogs (Dateien bzw. `/api/orchestration/backlog` + `/api/family-organizer/backlog`) und schreibt einen **read-only**, datierten Snapshot des offenen/blockierten Stands nach `Coordination/Status/Backlog Mirror.md` (+ `Backlog MOC`). Backlog-Verzeichnisse bleiben Source of Truth; der Vault spiegelt nur.

## C. Session-Handoff-Protokoll
Template `_meta/templates/handoff.md`, **eine neue Datei pro Handoff** unter `Coordination/Handoffs/YYYY-MM-DD-<slug>-handoff.md`. Abschnitte: Goal · Current state · What's done · Next steps · Open questions/blockers · Relevant links · How to resume. Frontmatter `status: open`, optional `backlog_ref`, `session_ref`.
- **Offenen Handoff finden:** `search_files` content `status: open` glob `*-handoff.md` → neueste; `Handoffs MOC.md` führt die kuratierte Open-Liste (gardening hält sie ehrlich).
- **Hermes-Verzahnung:** `session_ref` → Hermes-Session-Export, sodass Hermes exakt `--resume` kann, während jedes Tool die tool-neutrale Handoff-Note liest (komplementär, additiv).

## D. Cron-Report-Jobs (dediziertes `vault-cron`-Profil, Toolsets `obsidian, web`; FO-Job zusätzlich Repo-`--workdir`)
1. **Tägliches AI-News/Harness-Digest** (`0 9 * * *`) → `Reports/ai-news/<TODAY>-…md` + Wikilink in `Reports MOC.md`; aktionierbar → Orchestration-Backlog-Item (`status: backlog`).
2. **Wöchentlicher Upstream-Watch** (`0 8 * * 1`, `--workdir <repo>`) → `Reports/upstream-watch/<ISO-WEEK>-…md`; konventionsrelevant → Verweis auf `CONVENTIONS` + Backlog-Item.
3. **Wöchentliches Vault-Gardening** (`0 20 * * 0`) → MOCs neu bauen, Handoffs>7d & Notes>30d als `stale` flaggen, `done/stale`→`Archive/`, **Backlog-Spiegel** (B.3), **Link-Check** (lychee-Stil: meldet/öffnet Backlog-Item, **löscht nie**), Bericht nach `Reports/vault-gardening/`.

## E. Liefergegenstand — in DIESEM Container committen & pushen (Branch `claude/obsidian-vault-optimization-1T1L0`)
1. **`docs/vault-rollout-goal.md`** — **der `/goal`-Brief** (Herzstück): einfügbarer Standing-Goal-Text, den eine Hermes-Session auf dem Homeserver autonom abarbeitet, **phasenweise mit „STOPP & berichte, warte auf ‚weiter'"** nach jeder Phase. Enthält die E2E-Akzeptanzkriterien.
2. **`docs/vault-templates/`** — kanonische Quelldateien, die das Skript ins Vault kopiert: `_meta/CONVENTIONS.md`, `Frontmatter Schema.md`, `Vault MOC.md`, `templates/*.md` **inkl. neuer `backlog-orchestration.md` & `backlog-fo.md`** (exakt dem jeweiligen Frontmatter-Vertrag folgend), MOC-Seeds, `ADR-0001-adopt-shared-vault.md`, Beispiel-`…-bootstrap-handoff.md`, Pointer-`AGENTS.md`/`CLAUDE.md`.
3. **`scripts/setup-vault.sh`** — idempotent/additiv: resolved `OBSIDIAN_VAULT_PATH` (Fallback), legt Baum (§A) an, kopiert Templates nur falls **nicht vorhanden**, überschreibt nichts, trockenlauf-fähig.
4. **`docs/vault-architecture.md`** — Referenz: Architektur, §B-Synergie, **Speicherort-Empfehlung mit Recherche-Zitaten**, exakte `hermes cron create`-Befehle, `vault-cron`-Profil-Setup, kurze Quellen-Bibliografie.
5. **Repo-`AGENTS.md`** Pointer-Abschnitt **anhängen** (nicht umschreiben); **Repo-`CLAUDE.md`** neu mit Pointer (`@AGENTS.md`).

## F. `/goal`-Brief — Phasenstruktur (Inhalt von `docs/vault-rollout-goal.md`)
Standing Goal, stoppt nach **jeder** Phase mit kurzem Statusbericht:
- **Phase 0 – Preflight (read-only):** `OBSIDIAN_VAULT_PATH` auflösen; Repo gepullt; beide Backlog-Dirs/Env-Vars vorhanden?; Cron-Scheduler aktiv? → Bericht, STOPP.
- **Phase 1 – Scaffold:** `bash scripts/setup-vault.sh` → Baum + `_meta` + Templates + MOC-Seeds + ADR-0001 + Beispiel-Handoff; zweiter Lauf idempotent. → Bericht, STOPP.
- **Phase 2 – Pointer & Memory:** Vault-`AGENTS.md`/`CLAUDE.md`; eine Zeile an `~/.hermes/memories/MEMORY.md`; `OBSIDIAN_VAULT_PATH` in `~/.hermes/.env` falls fehlt. → Bericht, STOPP.
- **Phase 3 – Backlog-Brücke:** Beispiel-Handoff mit einem **echten Backlog-Item** verknüpfen — ein `.md` ins Orchestration-Dir schreiben (`vault_ref` + Body-Link) und Handoff-Frontmatter `backlog_ref` setzen; im Dashboard prüfen. → Bericht, STOPP.
- **Phase 4 – Crons:** `vault-cron`-Profil (`obsidian,web`); die 3 Jobs (§D) registrieren; gardening einmal `hermes cron run <id>`. → Bericht, STOPP.
- **Phase 5 – E2E-Verifikation:** Akzeptanztests, PASS/FAIL je Check.

## G. Reihenfolge
1. **Jetzt (Container):** Artefakte §E schreiben → commit & push auf den Branch.
2. **Homeserver (Hermes-Session, autonom):** Repo pullen, `hermes`, `/goal` mit dem Brief → Phasen 0–5 mit Stopps.
3. **Nach 1 Woche echter Handoffs:** Sync (`obsidian-git`) und Option-A-Upgrade fürs Orchestration-Backlog revisiten.

---

## Verifikation
**Repo-Artefakte (in DIESEM Container, vor Push):**
- `bash -n scripts/setup-vault.sh` + Trockenlauf `OBSIDIAN_VAULT_PATH=/tmp/vault-test bash scripts/setup-vault.sh` → Baum/Dateien == §A; zweiter Lauf idempotent.
- `backlog-orchestration.md`/`backlog-fo.md`-Frontmatter **gegen die Parser** (`orchestration_backlog_view.py`, `family_organizer_view.py`) gegenprüfen (Status-Enums, Pflichtfelder, `:`-Split-Verträglichkeit).
- `docs/vault-architecture.md`-Cron-Befehle gegen `hermes_cli/cron.py`-Flags gegenprüfen.

**E2E auf dem Homeserver (Phase 5, autonom; PASS/FAIL):**
1. **Vault-Resolution:** Hermes liest `_meta/CONVENTIONS.md` und nennt die Lock-Regel.
2. **Handoff-Flow:** Beispiel-Handoff via `search_files status:open` auffindbar; Frontmatter trägt `backlog_ref`.
3. **Backlog-Brücke:** das verknüpfte Item erscheint im Dashboard (`/api/orchestration/backlog`), `body` zeigt den Vault-Pfad, `vault_ref` durchgereicht.
4. **Cron-Berichte:** `hermes cron run <gardening-id>` → MOCs gebaut, `vault-gardening`-Report datiert da, `Backlog Mirror.md` enthält beide Backlogs.
5. **Report→Backlog:** Test-Lauf mit fingiertem aktionierbarem Punkt legt ein Backlog-Item an, das auf die Report-Note linkt (korrekter Frontmatter-Vertrag).
6. **Cross-Tool:** Codex (`--workdir <vault>`) und Claude Code lesen je `CONVENTIONS.md` via ihrer Pointer.

### Kritische Dateien
- vorhanden (lesen/anhängen): `skills/note-taking/obsidian/SKILL.md`, `hermes_cli/cron.py`, `hermes_cli/orchestration_backlog_view.py`, `hermes_cli/family_organizer_view.py`, `hermes_cli/profiles.py`, `AGENTS.md`
- **neu (dieser Branch):** `docs/vault-rollout-goal.md`, `docs/vault-architecture.md`, `docs/vault-templates/*`, `scripts/setup-vault.sh`, Repo-`CLAUDE.md`
