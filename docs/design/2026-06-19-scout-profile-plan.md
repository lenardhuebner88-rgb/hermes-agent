# Scout-Profil (opt-in Code-Recon-Vorlauf) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eine neue read-only Hermes-Worker-Lane `scout`, die als opt-in Vorgänger-Subtask einer schweren Task die Code-Recon erledigt (relevante Dateien+Zeilen, Caller, Risiken, Lösungs-Skizze) und das Ergebnis als Task-Kommentar für den nachfolgenden Coder ablegt.

**Architecture:** Scout ist KEIN neuer Dispatch-Mechanismus — er nutzt die bestehenden `taskgraph_hints.deps` (Subtask-Abhängigkeit) und Task-Kommentare. Nötig sind nur: (1) `scout` als gültige PlanSpec-Lane whitelisten, (2) ein Runtime-Profil `~/.hermes/profiles/scout/` (billig/schnell, read-only), (3) die Konvention dokumentieren, (4) ein Live-Smoke. Der dirty `kanban_db.py` (parallele Lane-Kosten-Session) wird NICHT angefasst.

**Tech Stack:** Python (hermes_cli), pytest, YAML-Profile, Hermes-Kanban.

## Global Constraints

- **Live-Checkout** `~/.hermes/hermes-agent`: vor jeder git-Aktion `git status --short`; fremde uncommittete Arbeit unangetastet; NIE `git add -A` — nur gezielte Pfade. Push nur `piet-fork`, fast-forward, nie `origin`/force. (Aktuell läuft eine parallele Session auf `kanban_db.py`/`strategist.py` — diese Dateien NICHT berühren.)
- **Profile liegen in `~/.hermes/profiles/`** (Runtime-Home, **nicht git-versioniert**) — Profil-Anlage ist eine Live-Config-Änderung, kein Repo-Commit. Reversibel durch Verzeichnis-Löschen.
- **Lane-Name `scout`** ist distinkt vom Claude-Code-Instrument `dep-scout` (in `_CC_INSTRUMENT_LANES`) — kein Token-Konflikt, aber in Doku klarstellen.
- **Testumfang:** targeted (`scripts/run-affected.sh` / gezielte Datei); keine Vollsuite.
- **Modell:** billig/schnell — `gpt-5.4-mini` (provider `openai-codex`), wie `admin`.
- **Read-only:** scout darf lesen/grepen, aber nicht editieren/committen/deployen. **Wichtig:** das `file`-Toolset koppelt `read_file`/`write_file`/`patch`/`search_files` (toolsets.py:191-193) — Lesen/Schreiben sind NICHT per Toolset trennbar. Enforcement daher via (a) `terminal`-Toolset deaktiviert (keine Shell-Mutation), (b) explizite SOUL/Description-Anweisung „nur Lesen, kein write/patch/commit", (c) Worker-Cage-Marker (`HERMES_KANBAN_TASK`, blockt push/deploy/destruktiv) und (d) Smoke-Verifikation (git status nach Lauf sauber). Restrisiko (scout schreibt eine Arbeitsdatei via write_file) ist reversibel + vom Smoke gefangen.

---

### Task 1: `scout` als gültige PlanSpec-Lane whitelisten

**Files:**
- Modify: `hermes_cli/planspecs.py:37-48` (VALID_PLANSPEC_LANES)
- Test: `tests/hermes_cli/test_planspec_rubric.py`

**Interfaces:**
- Consumes: `planspecs.parse_binding_planspec(path, plans_root=...)`, `planspecs.validate_spec_rubric(spec) -> Optional[list[str]]` (None = clean).
- Produces: `"scout"` ∈ `planspecs.VALID_PLANSPEC_LANES`; eine PlanSpec mit `lane: scout` erzeugt kein `"unknown lane"`-Finding.

- [ ] **Step 1: Failing test schreiben**

In `tests/hermes_cli/test_planspec_rubric.py` ans Ende anfügen:

```python
# A scout-led chain: read-only recon predecessor, coder dependent.
SCOUT_CHAIN = """---
status: freigegeben-komplett
owner: Hermes
slice: R2
topic: "Scout lane"
freigabe: complete
live_test_depth: smoke
taskgraph_hints:
  binding: true
  subtasks:
    - id: R2-S1
      title: "Recon: map files and callers for the slice"
      lane: scout
      deps: []
      acceptance_criteria:
        - "Grounding brief posted as a task comment: files, callers, risks"
      body: "Read-only recon, no mutations"
    - id: R2-S2
      title: "Implement the slice from the scout brief"
      lane: coder
      deps: [R2-S1]
      acceptance_criteria:
        - "Slice implemented with tests, using the scout brief"
---
# R2
"""


def test_scout_lane_passes_rubric(tmp_path: Path):
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, SCOUT_CHAIN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)

    assert planspecs.validate_spec_rubric(spec) is None


def test_scout_is_a_valid_lane():
    assert "scout" in planspecs.VALID_PLANSPEC_LANES
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag verifizieren**

Run: `cd /home/piet/.hermes/hermes-agent && python -m pytest tests/hermes_cli/test_planspec_rubric.py::test_scout_lane_passes_rubric tests/hermes_cli/test_planspec_rubric.py::test_scout_is_a_valid_lane -v`
Expected: FAIL — `test_scout_is_a_valid_lane` AssertionError; `test_scout_lane_passes_rubric` liefert `["unknown lane: scout"]` statt `None`.

- [ ] **Step 3: Minimal-Implementierung**

In `hermes_cli/planspecs.py`, im `VALID_PLANSPEC_LANES`-Set (Zeile 37-48) nach `"coder-claude",` einfügen:

```python
    "scout",
```

Das Set lautet danach (Kontext):
```python
VALID_PLANSPEC_LANES = {
    "coder",
    "coder-claude",
    "scout",
    "premium",
    "reviewer",
    "critic",
    "verifier",
    "research",
    "admin",
    "family-ui",
    "fo-brain",
}
```

- [ ] **Step 4: Test laufen lassen, Erfolg verifizieren**

Run: `cd /home/piet/.hermes/hermes-agent && python -m pytest tests/hermes_cli/test_planspec_rubric.py::test_scout_lane_passes_rubric tests/hermes_cli/test_planspec_rubric.py::test_scout_is_a_valid_lane -v`
Expected: 2 passed.

- [ ] **Step 5: Affected-Tests + commit**

Run: `cd /home/piet/.hermes/hermes-agent && python -m pytest tests/hermes_cli/test_planspec_rubric.py tests/hermes_cli/test_planspecs.py -q && ruff check hermes_cli/planspecs.py`
Expected: alle grün, ruff clean.

```bash
cd /home/piet/.hermes/hermes-agent
git status --short   # NUR planspecs.py + test_planspec_rubric.py dürfen von UNS stammen; kanban_db.py/strategist.py = fremde Session, NICHT stagen
git add hermes_cli/planspecs.py tests/hermes_cli/test_planspec_rubric.py
git commit -m "feat(planspecs): scout als gültige read-only Recon-Lane whitelisten

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Runtime-Profil `scout` anlegen (read-only, billig)

**Files:**
- Create: `~/.hermes/profiles/scout/config.yaml` (kopiert aus `research`, dann Overrides)
- Create: `~/.hermes/profiles/scout/profile.yaml`

**Interfaces:**
- Consumes: bestehende Profil-Discovery (`profiles_mod.list_profiles()` scannt `~/.hermes/profiles/`).
- Produces: ein dispatchbares Worker-Profil `scout`; `hermes -p scout` existiert; Default-Gateway kann Tasks mit `assignee: scout` spawnen.

- [ ] **Step 1: Basis-Profil kopieren**

`research` ist die nächste read-only-orientierte Vorlage (kein Code-Write-Default, `dispatch_in_gateway: false`).

```bash
cp -r /home/piet/.hermes/profiles/research /home/piet/.hermes/profiles/scout
```

- [ ] **Step 2: Modell + Provider auf billig/schnell setzen**

In `~/.hermes/profiles/scout/config.yaml` den `model:`-Block (Zeile 1-4) ersetzen:

```yaml
model:
  default: gpt-5.4-mini
  provider: openai-codex
  max_tokens: 16384
```

(Den `providers:`/`fallback_providers:`-Block von research darunter belassen oder auf einen codex-Fallback reduzieren — die Recon ist anspruchslos.)

- [ ] **Step 3: Toolsets auf read-only-Recon zuschneiden**

Hintergrund (verifiziert in `toolsets.py`): das `file`-Toolset bündelt `read_file, write_file, patch, search_files` (Z. 191-193) — Lesen/Schreiben sind NICHT per Toolset trennbar. scout BEHÄLT daher `file` (für `read_file`/`search_files`) + `kanban` (für `kanban_comment`), und Read-only wird über deaktiviertes `terminal` + SOUL-Anweisung + Smoke durchgesetzt (siehe Global Constraints).

scout erbt von research `toolsets: - hermes-cli`. Sicherstellen, dass `file` + `kanban` aktiv sind (Teil von hermes-cli) und `terminal` deaktiviert ist. In `~/.hermes/profiles/scout/config.yaml` unter `agent.disabled_toolsets:` (Zeile ~30) setzen:

```yaml
  disabled_toolsets:
    - terminal
    - code_execution
    - image_gen
    - video
    - video_gen
    - tts
    - todo
    - messaging
    - web
    - x_search
    - vision
    - computer_use
    - browser
    - cronjob
    - skills
    - moa
```

(D.h. NUR `file` + `kanban` bleiben effektiv übrig — Lesen/Suchen + Kommentar. `terminal` ist deaktiviert, damit scout keine Shell-Mutation ausführen kann.)

`dispatch_in_gateway: false` (von research geerbt) belassen — scout braucht keinen eigenen Gateway-Service, der Default-Gateway dispatcht ihn.

Verifizieren, dass die Namen exakt stimmen (kein Tippfehler lässt ein Mutations-Toolset durch):

Run: `cd /home/piet/.hermes/hermes-agent && python3 -c "import toolsets, yaml; valid=set(toolsets.TOOLSETS) if hasattr(toolsets,'TOOLSETS') else None; d=yaml.safe_load(open('/home/piet/.hermes/profiles/scout/config.yaml'))['agent']['disabled_toolsets']; print('disabled', d); print('unknown', [x for x in d if valid and x not in valid])"`
Expected: `unknown []` (alle disabled-Namen sind echte Toolsets); `terminal` ist enthalten.

- [ ] **Step 4: profile.yaml-Description (Routing-Signal) schreiben**

`~/.hermes/profiles/scout/profile.yaml` ersetzen durch (keine hardcodierten Modellnamen — siehe Phase-D-Konvention):

```yaml
description: "Read-only Code-Recon-Vorlauf: liest das Repo (Dateien, Caller, Risiken)
  und legt einen Grounding-Brief als Task-Kommentar ab, BEVOR ein coder/opus-coder
  die schwere Slice baut. Implementiert NICHT, editiert NICHT, committet NICHT —
  nur Lesen + Brief. Opt-in via PlanSpec-Vorgänger-Subtask (lane: scout, deps []).
  NICHT 'dep-scout' (das ist ein Claude-Code-Instrument). Profilname exakt: scout."
description_auto: false
```

- [ ] **Step 5: Profil-Discovery verifizieren**

Run: `cd /home/piet/.hermes/hermes-agent && hermes profile list 2>/dev/null | grep -i scout || python -c "from hermes_cli import profiles as p; print('scout' in [x['name'] if isinstance(x,dict) else x for x in p.list_profiles()])"`
Expected: `scout` taucht in der Profil-Liste auf.

- [ ] **Step 6: YAML-Validität + Read-only-Description prüfen**

Run: `python3 -c "import yaml; c=yaml.safe_load(open('/home/piet/.hermes/profiles/scout/config.yaml')); pf=yaml.safe_load(open('/home/piet/.hermes/profiles/scout/profile.yaml')); print('model', c['model']['default'], '| dispatch', c.get('dispatch_in_gateway'), '| disabled', c['agent']['disabled_toolsets']); print('desc_ok', 'read-only' in pf['description'].lower())"`
Expected: model `gpt-5.4-mini`, dispatch `False`, disabled-Liste enthält `terminal` (und NICHT `file`/`kanban`), `desc_ok True`.

(Kein git-Commit — Profile sind Runtime, nicht versioniert.)

---

### Task 3: Scout-Subtask-Konvention in Canon dokumentieren

**Files:**
- Modify: `/home/piet/vault/00-Canon/planspec-taskgraph.md`

**Interfaces:**
- Consumes: nichts (Doku).
- Produces: dokumentierte Konvention, wie ein scout-Vorlauf in einer PlanSpec autoriert wird.

- [ ] **Step 1: Vault-Coordination Check-IN**

Run: `python3 /home/piet/vault/_agents/_shared/scripts/coordination-open-sessions.py 2>/dev/null | grep -i "planspec-taskgraph\|00-Canon" || echo "kein Overlap"`
Expected: kein offenes `touching:` auf `planspec-taskgraph.md`. Falls Overlap → stop, warten.
Dann Check-IN-Eintrag in `vault/_agents/_coordination/` mit `touching: [00-Canon/planspec-taskgraph.md]`.

- [ ] **Step 2: Konventions-Abschnitt einfügen**

In `planspec-taskgraph.md` im Lane-/Subtask-Abschnitt ergänzen (verbatim):

```markdown
### Optionaler Scout-Vorlauf (opt-in)

Für schwere Slices kann dem Coder ein read-only Recon-Vorlauf vorgeschaltet werden:
ein Subtask mit `lane: scout` und `deps: []`, von dem der Coder-Subtask per `deps`
abhängt. Der scout-Worker liest das Repo und legt den Grounding-Brief (relevante
Dateien+Zeilen, Caller, Risiken, Lösungs-Skizze) als Task-Kommentar ab; der Coder
liest ihn beim Start. Beispiel:

    subtasks:
      - id: S1
        title: "Recon: Dateien/Caller/Risiken für die Slice kartieren"
        lane: scout
        deps: []
        acceptance_criteria:
          - "Grounding-Brief als Kommentar: Dateien, Caller, Risiken, Skizze"
      - id: S2
        title: "Slice umsetzen (nutzt den scout-Brief)"
        lane: coder
        deps: [S1]
        acceptance_criteria:
          - "Slice implementiert + Tests"

`scout` ist read-only (kein Edit/Commit/Deploy) und NICHT identisch mit dem
Claude-Code-Instrument `dep-scout`. Default ist KEIN scout-Vorlauf — er wird
bewusst pro Slice angefordert (Wert zuerst messen, dann ggf. für review_tier:critical
automatisieren).
```

- [ ] **Step 3: Receipt + Check-OUT**

Receipt nach `vault/03-Agents/Claude-Code/receipts/scout-convention-receipt.md` (was, Beleg, status); Check-IN-Eintrag `ended:` setzen.

---

### Task 4: Live-Smoke — scout-Vorlauf dispatchen und Brief verifizieren

**Files:** keine (Dogfood-Verifikation gegen die laufende Kanban).

**Interfaces:**
- Consumes: Task 1+2 (Lane + Profil live).
- Produces: Beleg, dass ein scout-Worker spawnt, liest, einen Brief-Kommentar schreibt und NICHTS editiert.

- [ ] **Step 1: Test-PlanSpec mit scout-Vorlauf anlegen**

Eine kleine PlanSpec (`/tmp/scout-smoke.md`) mit der Shape aus Task 3 schreiben — eine harmlose Recon-Aufgabe, z.B. "kartiere die Aufrufer von `validate_spec_rubric`". `deps`-Kette scout→coder, aber für den Smoke reicht der scout-Subtask.

- [ ] **Step 2: Ingest + Dispatch beobachten**

Run: `cd /home/piet/.hermes/hermes-agent && hermes plan ingest /tmp/scout-smoke.md`
Dann den scout-Worker beobachten: `hermes kanban show <child-id>` bis Status `running`→`done`.
Expected: der scout-Subtask bekommt `assignee: scout`, spawnt, läuft.

- [ ] **Step 3: Brief + Read-only verifizieren**

Run: `hermes kanban show <scout-child-id>` — der Brief muss als Kommentar vorliegen (Dateien/Caller/Risiken).
Run: `git -C /home/piet/.hermes/hermes-agent status --short` — der scout-Lauf darf KEINE Datei-Mutationen hinterlassen haben (read-only bewiesen).
Expected: Brief-Kommentar vorhanden; git status durch scout unverändert.

- [ ] **Step 4: Ergebnis festhalten**

Falls grün: scout ist live + read-only bewiesen → Memory-Pointer (MEMORY.md) ergänzen. Falls scout doch schreiben konnte → disabled_toolsets aus Task 2 Step 3 nachschärfen und Smoke wiederholen.

---

## Offene Punkte / Folge-Arbeit

- **Auto-Insertion bei `review_tier:critical`** ist bewusst NICHT Teil dieses Plans (opt-in zuerst, Wert messen). Folge-Spec, nachdem Phase B (review_tier) gebaut ist.
- **Decompose-LLM darf scout vorschlagen:** optional `kanban_decompose.py`-Prompt (clean file) um eine scout-Zeile ergänzen, sodass der LLM bei schweren Tasks selbst einen scout-Vorlauf einplant. Eigener kleiner Folge-Slice, nicht hier.
