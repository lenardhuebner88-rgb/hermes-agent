# Lane- & Review-Routing — operator-gesteuerte Build-/Prep-/Review-Achsen

> Design-Spec · 2026-06-19 · Status: signiert (Operator-OK)
> Betrifft: Hermes-Kanban-Dispatch + `/control` Flow-Tab + Canon
> Quelle der Live-Wahrheit bleibt `00-Canon/`; dieser Spec ist die Bauvorlage.
>
> **KLARSTELLUNG / FAKT (2026-06-20, Phase A implementiert):** Die kanonische Claude-Coder-Lane
> heißt **`premium`** — NICHT `opus-coder` (das war ein Spec-Irrtum: ein neues Profil ist nie nötig,
> `premium` existiert bereits als claude-cli/Opus-Lane auf der Claude Max-Subscription). Faktenstand:
> **`coder` = Codex/GPT (gpt-5.5)**, **`premium` = der Claude-Coder (claude-cli/Opus)**;
> **`coder-claude` ist ein deprecated, rückwärtskompatibler Alias → `premium`**. Wo unten noch
> `opus-coder` steht, lies `premium`.
>
> **PROGRAMM-STATUS (2026-06-20, am Code verifiziert — Live schlägt Doku):**
> - **Phase A — LIVE** (`e6ea7ce2b`): Lane-Konsolidierung, `coder-claude`→`premium`-Alias
>   (`_LANE_ALIASES`, `kanban_db.py`), Decompose-Routing-Tabelle migriert.
> - **Phase B — LIVE** (`499fe12a0`/`000ee405d`/`fee7672cd`): `review_tier`-Spalte
>   (`kanban_db.py:889`, Migration `:2217`, Werte `standard|review|critical`), gestufter Gate
>   `verifier→reviewer→critic` (`_review_stages_for_tier`, `_review_spawn_profile_for:6568`,
>   `_advance_review_chain:7549`), Auto-Retry mit strukturierten Findings
>   (`auto_retry_blocked_tasks:14287` + `_render_review_findings:3790`). Build-Receipt vorhanden.
>   Auto-Risk hinter Flag `kanban.review_gate.auto_tier` (default false = byte-identisch).
> - **Phase C — SHIPPED** (`f3ef7f54b` + `24b78f334`): Flow-Tab Operator-Hebel (Lane-Dropdown,
>   review_tier-Toggle, Scout-Checkbox, ChainCard Review-Pill), deployed, Gates grün.
> - **Offene Folge-Slices** (am Code lokalisiert, noch ungebaut): (a) Scout-Auto-Insertion bei
>   `review_tier:critical` — Flag `auto_scout_on_critical` default OFF, koppelt plan-ingest +
>   `set_task_review_tier→critical`; (b) Live-Stage-Pill (laufende Stufe statt konfiguriertem Tier
>   im `/board`-Payload); (c) Decompose-LLM darf Scout vorschlagen.
> - **Phase D — vertagt** (eigene Session): Canon-Entscheidungstabelle + „keine hardcodierten
>   Modellnamen" (Ziel 5 unten).
> - IST-Stand-Beleg: `vault/03-Agents/Claude-Code/receipts/2026-06-20-lane-review-routing-ist-stand.md`.

## Problem

Heute entscheidet der **Decompose-LLM** anhand der Profil-Beschreibung, welches Worker-Profil
eine Kanban-Task baut; Default-Fallback ist `coder` (gpt-5.5). Der Operator hat keinen bequemen,
sichtbaren Hebel, um pro Vorhaben die Build-Lane oder die Review-Tiefe zu bestimmen. Die Rollen
`critic`, `premium`, `reviewer` existieren als gültige Lanes, werden im automatischen Gate aber
**nie** getriggert — der Review-Gate spawnt ausschließlich `verifier`. `premium` und `coder-claude`
sind zudem fast identisch (beide opus/claude-cli). `research` gilt fälschlich als „nicht verfügbar".

### Belegte Ausgangslage (Audit 2026-06-19)

- Routing-Entscheider: `hermes_cli/kanban_decompose.py:63–120` (LLM matcht Task→Lane),
  Default `kanban.default_assignee: coder` (`config.yaml:731`).
- Spawn-Präzedenz: `task.model_override > aktive Lane > profil.config > subscription`
  (`hermes_cli/kanban_db.py:14994–15005`, Kommentar `:17650–17657`).
- Review-Gate spawnt nur `verifier` (`config.yaml:741–747`, `_review_gate_should_apply`
  `kanban_db.py:6178`). Feuert für JEDE coder/coder-claude/premium-Completion gleich —
  **keine „kritisch"-Schwelle**.
- Block→coder-Loop **existiert bereits**: REQUEST_CHANGES → `block_task` (`kanban_db.py:7918`),
  assignee bleibt Original-coder, `auto_retry_blocked` (`config.yaml:726`, Limit 2,
  `kanban_db.py:13307–13326`) → `ready`; danach `needs_operator`. Anti-Loop via
  `source_status='review'` (`kanban_db.py:5943`).
- Strukturiertes Feedback-Werkzeug `ensure_needs_revision_fix_task()` existiert
  (`kanban_db.py:3792`), ist aber **nicht** in den Auto-Retry-Pfad verdrahtet — der coder
  bekommt heute nur ein Plaintext-Snippet des Blockgrunds.
- Risk-Heuristik `reviewer_gate_required(plan_spec)` existiert
  (`control_plane_gate.py:126`, prüft risk_class/code/database/deploy), wird im Kanban-Gate
  **nicht** aufgerufen.
- `research` ist voll funktionsfähiger Worker (gemini-3.5-flash, kanban-Toolset, vom
  Default-Gateway dispatchbar). `dispatch_in_gateway: false` ist korrektes Multi-Gateway-Design
  (`docs/kanban/multi-gateway.md`), **kein Bug** — research wird nur selten auto-gewählt.
- SOUL.md ist statisch/handgepflegt (`agent/prompt_builder.py:1695`), kein Generator;
  hardcodierte Modellnamen in SOUL.md Z. 45–55 + diversen `profiles/*/profile.yaml`-Descriptions
  driften gegen die echte config.yaml.

## Ziele

1. Der Operator kann pro Vorhaben deterministisch **Build-Lane** und **Review-Tiefe** wählen —
   über PlanSpec-Felder und einen Dashboard-Hebel; der LLM bleibt nur Vorschlag/Fallback.
2. `critic`/`reviewer` werden **nur bei wirklich kritischen Tasks** aktiv, mit garantiert sauberem
   Block→coder-Rückweg.
3. Die redundanten Opus-Lanes werden konsolidiert; das Roster wird klarer.
4. Optionaler **Scout**-Vorlauf groundet den Coder bei schweren Tasks (opt-in).
5. Keine hardcodierten Modellnamen mehr in SOUL.md/Doku.

### Nicht-Ziele (YAGNI)

- Kein Umbau von `research` (kein Bug). Nur Rollen-Doku in Canon.
- Kein automatischer Scout-Zwang für alle schweren Tasks (erst Wert messen, dann ggf. auto).
- Kein zweiter Dispatcher (`dispatch_in_gateway` bleibt wie es ist).
- Keine Änderung der bestehenden Auto-Retry-Loop-Begrenzung (Limit 2 ist korrekt).

## Modell: drei orthogonale Achsen

### Achse 1 — Build-Lane (WER baut)

| Lane | Modell/Runtime | Rolle |
|---|---|---|
| `coder` | gpt-5.5 / hermes | Default für normalen Code (Codex/GPT) |
| `premium` | opus / claude-cli (Claude Max-Sub) | reasoning-heavy / schwer; **der Claude-Coder; `coder-claude` geht als Alias darin auf** |

- **Default-Policy:** Decompose-LLM schlägt vor, `coder` bleibt Fallback. PlanSpec/Dashboard
  übersteuern den LLM.
- **Konsolidierung (FAKT 2026-06-20):** kanonischer Name **`premium`** (bestehende claude-cli/Opus-Lane).
  `coder-claude` wird **rückwärtskompatibler Alias** → auf `premium` gemappt (`_LANE_ALIASES` in
  `_canonical_assignee`), damit in-flight-Tasks und bestehende PlanSpecs nicht brechen. Das
  `coder-claude`-Profilverzeichnis bleibt als Alias bestehen, wird nicht hart gelöscht. **Kein neues
  Profil, kein `opus-coder`.**
- Die bisherige „Hochrisiko"-Semantik von `premium` wandert in die Review-Achse
  (`review_tier: critical`); als Build-Lane ist `premium` jetzt schlicht „der Claude-Coder".

### Achse 2 — Prep-Lane (Vorarbeit, opt-in)

| Lane | Modell | Rolle |
|---|---|---|
| `scout` | billig/schnell (z.B. gpt-5.4-mini) · read-only · Repo-Read-Tools | Code-Recon-Vorlauf: relevante Dateien+Zeilen, Caller, Risiken, Lösungs-Skizze als Brief |
| `research` | gemini-3.5-flash | externe Web-Recherche (Lib-Vergleich, API-Doku, Standards) — unverändert |

- **Scout-Aktivierung: opt-in.** Läuft nur, wenn Operator/PlanSpec/Dashboard ihn anfordern oder
  der Decompose-LLM ihn als Vorgänger-Subtask einplant. Mechanik: Scout ist ein
  **Vorgänger-Subtask**, von dem die Coder-Subtask per `deps` abhängt; der Scout-Brief landet als
  Task-Kommentar, den der Coder beim Start liest. `deps` + Kommentare existieren bereits — es
  braucht nur das Profil + die Konvention.
- **Messen vor Automatisieren:** Wenn der Scout-Brief nachweislich Coder-Retries senkt, kann er
  später für `review_tier: critical` automatisch vorgeschaltet werden (Folge-Spec, nicht jetzt).

### Achse 3 — Review-Tier (WER prüft)

`review_tier ∈ {standard, review, critical}`, bestimmt als **Maximum dreier Quellen**:

1. **Auto-Risk** (Default): `reviewer_gate_required()` / `risk_class` aus `control_plane_gate.py`
   im Kanban-Gate aufrufen. DB-Migration/Deploy/Security → `critical`; sonstiger Code →
   `review`/`standard`.
2. **PlanSpec-Feld** `taskgraph_hints.subtasks[].review_tier` — deterministischer Hebel.
3. **Dashboard-Toggle** pro Kette — schreibt dasselbe Feld.

„Höchste gewinnt": Auto kann hochstufen, Operator kann per PlanSpec/Dashboard hoch- **oder**
runterstufen.

**Gestufte, sequenzielle Review-Kette (fail-fast):**

```
coder fertig → review_tier bestimmen
  standard:  verifier              → (APPROVED→done | BLOCK→auto_retry→coder)
  review:    verifier ✓ → reviewer → (APPROVED→done | BLOCK→coder)
  critical:  verifier ✓ → reviewer ✓ → critic → (APPROVED→done | BLOCK→coder)
```

- **Fail-Fast:** Jede Stufe, die blockt, beendet die Kette sofort und gibt via vorhandenem
  `auto_retry_blocked` an den Original-coder zurück (kein Sinn, opus auf kaputten Code zu werfen).
- **Strukturiertes Feedback:** Bei BLOCK wird `ensure_needs_revision_fix_task()` verdrahtet statt
  Plaintext-Snippet — der coder bekommt die vollständigen `blocking_findings`.
- **Anti-Loop:** Jede Review-Stufe nutzt `source_status='review'`, damit Review-Runs nie selbst
  wieder ins Gate fallen. Loop-Begrenzung (Limit 2 → `needs_operator`) bleibt.

## Operator-Hebel (alle Achsen)

| Hebel | Build-Lane | review_tier | Scout |
|---|---|---|---|
| **PlanSpec** `taskgraph_hints.subtasks[]` | `lane` (existiert) | `review_tier` (neu) | Vorgänger-Subtask mit `lane: scout` (neu) |
| **Dashboard** Flow-Tab | Lane-Dropdown (coder/opus-coder) | review_tier-Toggle (auto/review/critical) | „Scout vorschalten"-Checkbox |
| **Decompose-LLM** | Vorschlag, coder=Fallback | — (Auto-Risk-Heuristik) | darf Vorgänger-Subtask einplanen |
| **Kanban-CLI** | `kanban create --assignee` (existiert) | per Task-Feld | manueller Vorgänger-Subtask |

Dashboard-Umfang: Hebel **pro Kette** beim Start (Lane-Dropdown + Tier-Toggle + Scout-Checkbox),
plus Sichtbarkeit der laufenden Review-Stufe als Pill in der `ChainCard` (`FlowView.tsx`).

## Rückwärtskompatibilität

- `assignee ∈ {premium, coder-claude}` → transparent auf `opus-coder` gemappt (Alias-Tabelle),
  geprüft an allen Stellen, die heute `code_roles = (coder, coder-claude, premium)` referenzieren
  (`kanban_db.py:5947`, `config.yaml:743`, Lane-Seeds `kanban_db.py:17668`, Decompose-Prompt).
- Bestehende PlanSpecs mit `lane: premium`/`coder-claude` bleiben gültig (Alias).
- `VALID_PLANSPEC_LANES` (`planspecs.py:37`) wird um `opus-coder`, `scout` erweitert; alte Namen
  bleiben als akzeptierte Aliase.
- Tasks ohne `review_tier` verhalten sich wie heute (Auto-Risk bestimmt; Default-Pfad = `verifier`).

## Phasen

### Phase D — sofort & standalone (risikolos, reine Doku)
- SOUL.md (`~/.hermes/SOUL.md` Z. 45–55) + `profiles/*/profile.yaml`-Descriptions: Modellnamen
  durch Rollen-/Tier-Beschreibungen ersetzen („das im Profil konfigurierte Modell",
  „OpenAI-Codex-Lane", „Claude-Max-Lane").
- Canon: `research`-Rolle dokumentieren („externe Web-Recherche, dispatchbar, selten auto-gewählt")
  + Notiz, dass `dispatch_in_gateway: false` Absicht ist.
- Canon `planspec-taskgraph.md`: Entscheidungstabelle (wann coder/opus-coder/critical/scout) +
  neue Felder `review_tier` dokumentieren.

### Phase A — Build-Lane-Konsolidierung (Backend + Config) — ✅ IMPLEMENTIERT 2026-06-20
- **Kanonische Claude-Lane = `premium`** (existierte schon); `coder-claude` → Alias auf `premium`
  (`_LANE_ALIASES`). **Kein neues Profil, kein `opus-coder`.**
- `premium` war bereits in `code_roles`, Lane-Seeds, `VALID_PLANSPEC_LANES`, `_WORKER_SCOPE_LANES`
  und `AUTO_RETRY_ESCALATION_PROFILE` verdrahtet → nur verifiziert. Geändert: `_canonical_assignee`
  (Alias), Decompose-Prompt-Lane-Tabelle, `_CODE_LANE_REASONS[premium]`; Follow-up: Conflict-Park-/
  Funnel-Literale auf `premium` migriert.
- Umgesetzt in-session (commits auf `worktree-bridge-cse`), Cross-Family-reviewer APPROVED, Gates grün.

### Phase B — Gestufter Review-Gate (Kern, `kanban_db.py`)
- `review_tier`-Signal (3 Quellen, max-merge) inkl. `reviewer_gate_required()`-Einbindung in
  `_review_gate_should_apply()`.
- Ketten-Mechanik verifier→reviewer→critic (fail-fast) auf dem bestehenden `review`-Status +
  `auto_retry`-Loop.
- `ensure_needs_revision_fix_task()` in den Block-Pfad verdrahten (strukturiertes Feedback).
- Tief in Bestands-Code → **Codex baut, Claude reviewt cross-family.**

### Phase C — Dashboard-Hebel (`/control` Flow-Tab)
- Lane-Dropdown + review_tier-Toggle + Scout-Checkbox beim Kette-Start; schreibt
  `tasks.assignee`/`tasks.review_tier` deterministisch via neuen/erweiterten Endpoint.
- Stage-Pill (laufende Review-Stufe) in `ChainCard`.

### Scout — opt-in Profil + Konvention
- `scout`-Profil (billig/schnell, read-only, Repo-Read-Tools) anlegen.
- Decompose-/PlanSpec-Konvention: Scout als Vorgänger-Subtask via `deps`, Brief als Kommentar.

Jede Phase mit eigenen Gates + Review, bevor die nächste startet.

## Risiken & offene Punkte

- **Live-Checkout:** `~/.hermes/hermes-agent` wird parallel editiert — vor jeder git-Aktion
  `git status`; nur selektiv committen, fremde WIP in Ruhe lassen.
- **Alias-Vollständigkeit:** Alle Referenzen auf `premium`/`coder-claude` müssen gefunden werden
  (Caller-Grep), sonst brechen in-flight-Tasks. Adversarialer Test mit einer offenen `premium`-Task.
- **Scout-Wert unbelegt:** bewusst opt-in; Wirksamkeit (Retry-Senkung) später messen.
- **Kosten kritischer Tier:** `critical` = bis zu 3 Review-Läufe (verifier+reviewer+critic).
  Fail-fast begrenzt das; Auto-Risk muss konservativ kalibriert sein, damit nicht zu viel
  hochstuft.
- **review_tier-Speicherort:** neue Spalte `tasks.review_tier` (Migration additiv/expand-contract,
  mit Backup) — in Phase B zu klären.

## Nächster Schritt

Phase D kann sofort als kleiner standalone Doku-Change laufen. A/B/C + Scout gehen über
`writing-plans` in einen Implementierungsplan; A/B baut Codex (cross-family Review durch Claude).
