# coder-Profil — Skill-Hygiene: Entfern-Befehlsliste (DRY-RUN, herkunfts-richtig, reversibel)

> **Status:** DRY-RUN / Apply-Vorlage. **Hier wird NICHTS ausgeführt** — keine Live-Profil-Mutation.
> Quelle: `coder-skill-audit.md` (commit `abab5226a`) + Operator-Entscheidungen 2026-06-19.
> **Profil:** `coder` (`~/.hermes/profiles/coder`). **Mechanismen quell-verifiziert** gegen den
> Live-Stand des Profils (siehe *Verifikations-Basis* unten).

## 0. Was diese Liste tut (und was nicht)
- **66 Skills** werden zur Archivierung/Deaktivierung vorgemerkt: die **59 ARCHIVE** aus dem Audit
  **+ `research-paper-writing`** (Operator: archivieren trotz research-Regel — 25,8k tok, 0× genutzt)
  **+ die 6 `openclaw-*`** (Operator: unpin + archive).
- Jeder Skill bekommt den **herkunfts-richtigen, reversiblen** Befehl. **KEIN `rm`/`mv`.**
- KEEP-Skills (echte coding/kanban/hermes/debug/test + research-ML + gepinnte ausser `openclaw-*`)
  stehen **nicht** auf der Liste — siehe *Bewusst behalten*.
- Geschätzter Footprint-Gewinn: **≈ 200.641 md tok** (143.210 ARCHIVE-59 + 25.844 research-paper-writing
  + 31.587 openclaw-6) ≈ **64 % des geladenen SKILL.md-Volumens** (313.729 md tok). Schätzung = `bytes/4`.

---

## ⚠ Mechanismus-Korrektur (quell-verifiziert — wichtig vor dem Apply)
Das Task-Grounding nennt für **bundled** zwei Optionen: `hermes -p coder skills config` *disable*
**bzw.** `hermes -p coder skills opt-out --remove`. Beide sind so **nicht anwendbar** für eine
*selektive, nicht-destruktive* Liste:

1. **`skills config` existiert nicht als Subcommand.** Die `skills`-Subcommands sind
   `browse/search/install/inspect/list/check/update/audit/uninstall/reset/opt-out/opt-in/repair-official/publish/snapshot/tap`
   (`hermes_cli/subcommands/skills.py:21-265`). Es gibt **keinen** `skills config`.
2. **`opt-out --remove` ist BULK + löschend.** Es schreibt den `.no-bundled-skills`-Marker und
   **löscht ALLE unmodifizierten bundled Skills** (`skills.py:167-188`) — also auch die KEEP-bundled
   (z. B. `test-driven-development`, `systematic-debugging`, `plan`, alle `github-*`). Das ist weder
   per-Skill noch „KEIN rm". (Reversibel nur via `opt-in --sync` Re-Seed.)
3. **Die operator-gemeinte „config disable" (= per-Skill, nicht-destruktiv, → Zustand *disabled*)**
   wird real durch **`curator archive`** umgesetzt: das verschiebt das Skill-Dir nach `.archive/`
   (kein Löschen), setzt `state=archived` (genau der Zustand, den `skills list --enabled-only`
   als „disabled" ausblendet) und ist via `curator restore` rückholbar
   (`tools/skill_usage.py:672-724` / `727-759`).
   **`curator archive` funktioniert hier auch auf bundled built-ins**, weil im Live-`coder`-Profil
   **`curator.prune_builtins: true`** gilt (global `~/.hermes/config.yaml:528`; Profil überschreibt es
   nicht; Default ohnehin `True`, `skill_usage.py:257`). Damit ist `is_curation_eligible()==True` für
   alle 47 bundled-Kandidaten (empirisch bestätigt, s. u.).

**Konsequenz:** Diese Liste benutzt **`curator archive` für bundled UND local**, **`skills uninstall`
für hub**. Das ist die per-Skill-, reversible-, nicht-löschende Realisierung der Operator-Absicht.
(`opt-out --remove` bleibt nur eine *bulk*-Alternative, falls man wirklich **alle** bundled inkl. KEEP
entfernen wollte — hier **nicht empfohlen**.)

> `plan` ist der einzige `PROTECTED_BUILTIN` (`skill_usage.py:66`) und steht auf **keiner** Liste.

---

## 1. BACKUP zuerst (PFLICHT — vor jedem Entfern-Befehl)
```bash
# (a) Vollständiger tar.gz-Snapshot des coder-Skill-Baums (inkl. .archive/) → rollback-fähig.
#     Schreibt nach ~/.hermes/profiles/coder/skills/.curator_backups/<utc>/  (HERMES_HOME-relativ).
hermes -p coder curator backup --reason "skill-hygiene-2026-06-19"

# (b) Maschinenlesbarer Snapshot der installierten Skills (inkl. hub-IDs für sauberen Re-Install).
hermes -p coder skills snapshot export ~/.hermes/reports/coder-skills-snapshot-pre-hygiene.json
```
> `curator backup` ist derselbe Mechanismus, den der Curator vor jedem echten Run automatisch fährt
> (`agent/curator_backup.py:70-75,212-230`). `snapshot export` braucht das `export <file>`-Subcommand
> (`skills.py:240-247`) — bloßes `skills snapshot` genügt nicht.

---

## 2. BUNDLED (47) — `curator archive` (reversibel: `curator restore <name>`)
> Alle `pinned=False`, `curation_eligible=True`, `state=active` (live verifiziert). Archivieren
> verschiebt nach `.archive/` **und** trägt den Namen in die Suppression-Liste ein, damit der
> Update-Re-Seeder ihn nicht zurückholt (`skill_usage.py:719-721`).

```bash
hermes -p coder curator archive humanizer
hermes -p coder curator archive p5js
hermes -p coder curator archive comfyui
hermes -p coder curator archive claude-design
hermes -p coder curator archive audiocraft-audio-generation
hermes -p coder curator archive xurl
hermes -p coder curator archive touchdesigner-mcp
hermes -p coder curator archive ascii-video
hermes -p coder curator archive notion
hermes -p coder curator archive pretext
hermes -p coder curator archive segment-anything-model
hermes -p coder curator archive weights-and-biases
hermes -p coder curator archive manim-video
hermes -p coder curator archive evaluating-llms-harness
hermes -p coder curator archive airtable
hermes -p coder curator archive google-workspace
hermes -p coder curator archive ascii-art
hermes -p coder curator archive baoyu-infographic
hermes -p coder curator archive songwriting-and-ai-music
hermes -p coder curator archive popular-web-designs
hermes -p coder curator archive sketch
hermes -p coder curator archive powerpoint
hermes -p coder curator archive serving-llms-vllm
hermes -p coder curator archive llama-cpp
hermes -p coder curator archive macos-computer-use
hermes -p coder curator archive excalidraw
hermes -p coder curator archive himalaya
hermes -p coder curator archive design-md
hermes -p coder curator archive teams-meeting-pipeline
hermes -p coder curator archive maps
hermes -p coder curator archive heartmula
hermes -p coder curator archive architecture-diagram
hermes -p coder curator archive ocr-and-documents
hermes -p coder curator archive blogwatcher
hermes -p coder curator archive yuanbao
hermes -p coder curator archive findmy
hermes -p coder curator archive huggingface-hub
hermes -p coder curator archive apple-reminders
hermes -p coder curator archive youtube-content
hermes -p coder curator archive obsidian
hermes -p coder curator archive gif-search
hermes -p coder curator archive openhue
hermes -p coder curator archive imessage
hermes -p coder curator archive songsee
hermes -p coder curator archive apple-notes
hermes -p coder curator archive nano-pdf
# Operator-Zusatz (research/-Regel-Ausnahme): größter Skill, 25,8k tok, 0× genutzt
hermes -p coder curator archive research-paper-writing
```

## 3. HUB/OFFICIAL (6) — `skills uninstall` (reversibel: `skills install <hub-id>`)
> `curation_eligible=False` (hub hat externen Upstream-Owner → `curator` verweigert,
> `skill_usage.py:442-443`/`686-687`). Korrekter Pfad = `uninstall` (`skills.py:138-141`;
> hub-IDs für den Re-Install stehen in `.hub/lock.json` bzw. im Snapshot aus Schritt 1b).

```bash
hermes -p coder skills uninstall baoyu-comic
hermes -p coder skills uninstall baoyu-article-illustrator
hermes -p coder skills uninstall pokemon-player
hermes -p coder skills uninstall pixel-art
hermes -p coder skills uninstall minecraft-modpack-server
hermes -p coder skills uninstall ideation
```

## 4. LOCAL / agent-erstellt (7) — `curator archive` (reversibel: `curator restore <name>`)
> Weder bundled noch hub → `is_agent_created==True`, `curation_eligible=True`, `pinned=False`.

```bash
hermes -p coder curator archive godmode
hermes -p coder curator archive outlines
hermes -p coder curator archive obliteratus
hermes -p coder curator archive fine-tuning-with-trl
hermes -p coder curator archive spotify
hermes -p coder curator archive axolotl
hermes -p coder curator archive unsloth
```

## 5. LOCAL + GEPINNT — die 6 `openclaw-*` (ZUERST unpin, DANN archive)
> Operator 2026-06-19: unpin + archive. Alle sind `pinned=True` (local). `curator archive` verweigert
> gepinnte Skills mit „unpin first" (`hermes_cli/curator.py:274-279`); `curator unpin` greift nur auf
> agent-created Skills (`curator.py:249-255`) — passt, da local. Reversibel: `curator restore <name>`
> (und bei Bedarf `curator pin <name>`).

```bash
hermes -p coder curator unpin openclaw-operator        && hermes -p coder curator archive openclaw-operator
hermes -p coder curator unpin openclaw-model-routing   && hermes -p coder curator archive openclaw-model-routing
hermes -p coder curator unpin openclaw-config-change-safe && hermes -p coder curator archive openclaw-config-change-safe
hermes -p coder curator unpin openclaw-stability-hardening && hermes -p coder curator archive openclaw-stability-hardening
hermes -p coder curator unpin openclaw-discord-ops     && hermes -p coder curator archive openclaw-discord-ops
hermes -p coder curator unpin openclaw-incident-rca    && hermes -p coder curator archive openclaw-incident-rca
```

---

## 6. Reversal / Rollback (alles rückholbar)
```bash
# Einzelner bundled/local Skill zurück (löscht Suppression-Eintrag, holt aus .archive/):
hermes -p coder curator restore <name>
# Einzelner hub-Skill zurück:
hermes -p coder skills install <hub-id>          # hub-ID aus dem Snapshot / .hub/lock.json
# Pin wiederherstellen (optional, nur openclaw-*):
hermes -p coder curator pin <name>
# Komplett-Rollback auf den Pre-Hygiene-Snapshot (alle Skills auf einen Schlag):
hermes -p coder curator rollback                 # neuester .curator_backups-Snapshot
hermes -p coder curator rollback --list          # verfügbare Snapshots zeigen
```

## 7. Verifikation nach Apply (read-only)
```bash
hermes -p coder skills list --enabled-only       # die 66 dürfen NICHT mehr erscheinen
hermes -p coder curator list-archived            # bundled+local Archivierte sollten gelistet sein
# Gegencheck der KEEP-Kerne (müssen weiter aktiv sein):
hermes -p coder skills list --enabled-only | grep -E 'test-driven-development|systematic-debugging|kanban-worker|kanban-orchestrator|hermes-agent|plan'
```

## 8. Bewusst BEHALTEN (NICHT auf der Liste)
- **Genutzt:** `test-driven-development`, `systematic-debugging`, `hermes-agent` (→ SLIM, separat),
  `kanban-worker`, `kanban-orchestrator`, `kanban-execution-worker-readiness`, `native-mcp`,
  `github-pr-workflow`, `linear`.
- **In-scope ungenutzt, aber kategorisch coding/debug/test/hermes/research:** alle `github-*`,
  `python-debugpy`, `node-inspect-debugger`, `spike`, `simplify-code`, `requesting-code-review`,
  `writing-plans`, `subagent-driven-development`, `workflow-library`, `claude-code`, `codex`,
  `opencode`, `arxiv`, `dspy`, `llm-wiki`, `polymarket`, `jupyter-live-kernel`, `dogfood`,
  `grill-me`, `brainstorming`, `plan` (PROTECTED).
- **Gepinnt & behalten:** `minimax-openclaw-token-plan` (gehört NICHT zu den 6 `openclaw-*`).
- **Unangetastet (TDD/Debug-Disziplin):** `test-driven-development`, `systematic-debugging`.

---

## 9. Apply-Plan für Z1 (kurz)
1. **Review:** Diese Liste + Mechanismus-Korrektur abnehmen (besonders bundled→`curator archive`
   statt `opt-out`, weil `prune_builtins:true` live).
2. **Backup (Schritt 1a+1b)** ausführen — Pflicht-Vorbedingung; Pfade in ein Receipt notieren.
3. **Schritte 2→5 der Reihe nach** am **Live-`coder`-Profil** ausführen (nicht im Worktree;
   `-p coder` setzt HERMES_HOME korrekt). Bei jedem Befehl auf `0`-Exit / „archived"/„uninstalled"
   achten; Fehler einzeln behandeln (nicht weiterlaufen).
4. **Verifikation (Schritt 7).** Erwartung: 66 weg aus `--enabled-only`, KEEP-Kerne intakt.
5. **Wirkung sichtbar:** Skills werden bei der **nächsten neuen Session** (`/reset`) aus dem
   System-Prompt ausgeblendet — kein laufender Prozess wird mid-session verändert (Prompt-Cache).
6. **Receipt** → `vault/03-Agents/Claude-Code/receipts/` (Backup-Pfade, Befehls-Exits, vorher/nachher
   `skills list`-Counts, Footprint-Delta).
7. **Rollback-Pfad** (Schritt 6) ins Receipt kopieren, falls etwas fehlt.

---

### Verifikations-Basis (wie diese Liste belegt wurde)
- Herkunft/Pin/Eligibility **empirisch** über `tools.skill_usage` mit `HERMES_HOME=…/profiles/coder`:
  47 bundled (`curation_eligible=True`), 6 hub (`False`), 13 local (7 `pinned=False` + 6 `openclaw-*`
  `pinned=True`); `prune_builtins_enabled=True`; `PROTECTED_BUILTIN_SKILLS={plan}`.
- Befehls-Signaturen quell-verifiziert: `hermes_cli/subcommands/skills.py`, `hermes_cli/curator.py`,
  `tools/skill_usage.py`, `agent/curator_backup.py`.
- **Keine** dieser Befehle wurde ausgeführt; Live-Profil unverändert.
