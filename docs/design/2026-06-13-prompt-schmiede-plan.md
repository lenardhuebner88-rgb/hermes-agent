# Prompt-Schmiede Tab — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the read-only `/control/schmiede` tab — a copy-paste Prompt-Schmiede (Konfigurator + Kanon) that composes best-practice agent-control prompts from a backend-served catalog.

**Architecture:** Backend serves a static JSON catalog (`hermes_cli/data/promptforge_catalog.json`) via a new gated read-only endpoint `GET /api/promptforge/catalog` (registered through a `register_promptforge_routes(app)` factory, mirroring `library_view.py`). The frontend holds all logic: a one-shot hook loads the catalog, `composer.ts` (pure) assembles the final prompt text deterministically, `heuristic.ts` (pure) scores it client-side, and two view halves (`Konfigurator.tsx`, `Kanon.tsx`) render it. No dispatch, no DB, no persisted presets.

**Tech Stack:** FastAPI (Python), React 18 + TypeScript + Vite, vitest (frontend unit), pytest (backend). Gates run **only in the live checkout** `~/.hermes/hermes-agent/web` via `.bin/` (worktrees lack `node_modules`).

---

## Grounding facts (verified 2026-06-13, file:line)

- Endpoint factory pattern: `hermes_cli/library_view.py:750-784` (`register_*_routes(app)` + inner `@app.get`, `await asyncio.to_thread(...)` for FS reads), registered at bottom of `hermes_cli/web_server.py:12037-12054`.
- Auth: `auth_middleware` (`web_server.py:525-539`) gates every `/api/` path not in `PUBLIC_API_PATHS` (`hermes_cli/dashboard_auth/public_paths.py:33-48`). A read-only catalog endpoint needs **no** `_require_token` call and should **not** be made public (frontend `fetchJSON` injects the token automatically).
- JSON-from-disk pattern: `json.loads(path.read_text(encoding="utf-8"))`; package-relative path `Path(__file__).parent / "data" / "promptforge_catalog.json"`.
- Model reuse: import `loadLanes`, `FALLBACK_MODELS`, `LaneModelOption` from `web/src/control/views/lanes/api.ts` (`:50-58, 252-254, 316-320`). `data.models` else `FALLBACK_MODELS`.
- Fetch helper: `fetchJSON` from `@/lib/api` (`web/src/lib/api.ts:112-124`) — injects session token itself.
- One-shot hook model: `useDeepAudit` (`web/src/control/hooks/useControlData.ts:372-451`) — `useState` + `aliveRef` + single `useEffect([])`, **not** `pollingStore`.
- vitest convention: `*.test.ts` next to source, `import { describe, expect, it } from "vitest"` (`web/src/control/lib/slug.test.ts:1`). Worker cap `maxWorkers:4`.
- pytest endpoint convention: bare `FastAPI()` + `register_*_routes(app)` + `TestClient(app)` fixture (`tests/test_autoresearch_dashboard_routes.py:53-57`); runner `scripts/run_tests.sh tests/test_promptforge.py`.
- i18n: `de.tabs` flat object at `web/src/control/i18n/de.ts:8`. Density: `import type { Density } from "../hooks/useDensity"` (`Density = "airy" | "compact"`).
- UI primitives: `FleetPanel` + `FleetEmptyState` (`web/src/control/components/fleet/atoms.tsx:43-76`), `CopyButton` (`web/src/control/views/backlog/CopyButton.tsx:6-34`, props `{ text, label, copiedLabel }`).
- Tab wiring touch points: `ControlShell.tsx:11` (union), `:28-42` (`moreTabs`); `ControlPage.tsx` lazy block (`:20-63`), `activeFromPath` (`:65-86`), `viewImporters` (`:91-104`), `tabPath` (`:112-126`), `<Routes>` (`:209-229`).

---

## File Structure

**Backend (new + modify):**
- Create: `hermes_cli/data/promptforge_catalog.json` — the static catalog (all seed content).
- Create: `hermes_cli/promptforge_view.py` — `register_promptforge_routes(app)` + cached loader.
- Modify: `hermes_cli/web_server.py` — import + call `register_promptforge_routes(app)` near the other `register_*_routes(app)` calls (~`:12037-12054`).
- Create: `tests/test_promptforge.py` — endpoint + schema test.

**Frontend pure logic (new):**
- Create: `web/src/control/views/schmiede/catalog.ts` — TS types + `ForgeSelection`.
- Create: `web/src/control/views/schmiede/composer.ts` — `compose()` (pure).
- Create: `web/src/control/views/schmiede/composer.test.ts`.
- Create: `web/src/control/views/schmiede/heuristic.ts` — `score()` (pure).
- Create: `web/src/control/views/schmiede/heuristic.test.ts`.

**Frontend views (new):**
- Create: `web/src/control/views/schmiede/Konfigurator.tsx` — left half (selection + live preview + score).
- Create: `web/src/control/views/schmiede/Kanon.tsx` — right half (raw templates + teaching material).
- Create: `web/src/control/views/SchmiedeView.tsx` — composes the two halves, uses the hook.

**Frontend wiring (modify):**
- Modify: `web/src/control/hooks/useControlData.ts` — add `usePromptForgeCatalog()`.
- Modify: `web/src/control/components/ControlShell.tsx` — union + `moreTabs` entry.
- Modify: `web/src/control/ControlPage.tsx` — lazy import, `activeFromPath`, `viewImporters`, `tabPath`, `<Route>`.
- Modify: `web/src/control/i18n/de.ts` — `de.tabs.schmiede`.

---

## Task 1: Backend catalog JSON

**Files:**
- Create: `hermes_cli/data/promptforge_catalog.json`

- [ ] **Step 1: Create the catalog file** with the full verbatim seed content (Spec §7). This is the core value — no placeholders.

```json
{
  "version": 1,
  "blocks": [
    { "id": "role", "letter": "A", "label": "Role", "description": "Setzt die Fachrolle — fokussiert Stil und Prioritäten.", "body": "You are a [security audit engineer / senior backend dev …].", "source": "Anthropic Prompting Best Practices", "category": "core" },
    { "id": "goal", "letter": "B", "label": "Goal", "description": "Das spezifische Ziel: Datei + Symptom + Outcome, nicht \"fix the bug\".", "body": "State the goal specifically: the file, the symptom, and the desired outcome — not just \"fix the bug\".", "source": "Anthropic (Claude Code Best Practices)", "category": "core" },
    { "id": "grounding", "letter": "C", "label": "Grounding", "description": "Erzwingt Lesen vor Antworten — kritisch bei langen Runs.", "body": "Never speculate about code you have not opened. Read relevant files BEFORE answering.", "source": "Anthropic (<investigate_before_answering>)", "category": "long-run" },
    { "id": "tools", "letter": "D", "label": "Tools", "description": "Macht Tool-Präferenzen und Parallelität explizit.", "body": "State tool preferences explicitly. Issue independent tool calls in parallel. Prefer `rg` over `grep`; use `apply_patch` for single-file edits.", "source": "Anthropic <use_parallel_tool_calls> · OpenAI Codex Guide", "category": "long-run" },
    { "id": "persistence", "letter": "E", "label": "Persistence / Keep-Going", "description": "Hält den Agenten am Arbeiten, bis wirklich fertig.", "body": "Keep going until the query is completely resolved before yielding. Always be as persistent and autonomous as possible and complete tasks fully.", "source": "OpenAI PE Guide · Anthropic Prompting BP", "category": "core" },
    { "id": "done-when", "letter": "F", "label": "Done-When", "description": "Maschinenlesbares Fertig-Kriterium = externe Evidenz.", "body": "Define a machine-checkable finish criterion = external evidence (tests green, build exit 0, empty queue) — not \"looks done\".", "source": "Anthropic Harnesses · OpenAI Codex", "category": "core" },
    { "id": "scope-constraints", "letter": "G", "label": "Scope-Constraints", "description": "Verhindert Scope-Creep und ungewollte Umbauten.", "body": "Only make changes directly requested. Don't refactor, add docstrings/comments to code you didn't change, or add defensive code for impossible cases.", "source": "Anthropic Prompting BP", "category": "core" },
    { "id": "reversibility-gate", "letter": "H", "label": "Reversibility-Gate", "description": "Bremst vor schwer umkehrbaren Aktionen.", "body": "For actions that are hard to reverse, affect shared systems, or are destructive (rm -rf, force-push, DB drop, PR comments), ask before proceeding.", "source": "Anthropic Prompting BP", "category": "core" },
    { "id": "verification", "letter": "I", "label": "Verification", "description": "Erzwingt Prüfung vor dem Fertig-Melden.", "body": "Before finishing, verify against [test/build/screenshot]. Only mark a feature passing after careful testing.", "source": "Anthropic Harnesses · OpenAI", "category": "core" },
    { "id": "escalation", "letter": "J", "label": "Escalation", "description": "Strukturierte Blockade statt stillem Workaround.", "body": "If blocked, report it as: Blocked (Reason + Question). Inform me rather than working around incorrect tests.", "source": "OpenAI Codex · Anthropic", "category": "optional" },
    { "id": "state-handoff", "letter": "K", "label": "State-Handoff", "description": "Persistenter Zustand über Sessions hinweg.", "body": "Persist progress in `progress.txt` (freeform) + `tests.json` (structured); use Git as a state log. Read these at session start.", "source": "Anthropic Harnesses", "category": "long-run" },
    { "id": "output-format", "letter": "L", "label": "Output-Format", "description": "Legt die Ausgabeform für den Verbraucher fest.", "body": "Specify the output shape for the consumer: XML tags / semantic Markdown / JSON.", "source": "Anthropic · OpenAI", "category": "optional" }
  ],
  "taskTypes": [
    {
      "id": "audit",
      "label": "Audit / Security-Review (read-only)",
      "blockIds": ["role", "goal", "scope-constraints", "grounding", "output-format", "verification"],
      "typeBody": "Focus: OWASP Top 10 (injection, broken auth, secrets in source, SSRF/XSS/CSRF, insecure deserialization).\nOutput: Numbered list. Each item: [file:line] [Critical|High|Medium|Low] [Issue] [Fix suggestion].\nConstraints: Only report actual, exploitable issues — not theoretical risks or style. Do not modify any files.",
      "defaultDoneWhen": "A prioritized report covering all in-scope files is delivered.",
      "checklist": ["Read-only pledge", "Severity labels", "Scope bounded to recent changes", "Exploitable-only filter", "Numbered, actionable output"],
      "rawTemplate": "Role: You are a security audit engineer.\nScope: Review all files recently modified in src/ — do NOT touch anything outside.\nFocus: OWASP Top 10 (injection, broken auth, secrets in source, SSRF/XSS/CSRF, insecure deserialization).\nOutput: Numbered list. Each item: [file:line] [Critical|High|Medium|Low] [Issue] [Fix suggestion]\nConstraints:\n  - Only report actual, exploitable issues — not theoretical risks or style.\n  - Do not modify any files.\nDone-when: A prioritized report covering all in-scope files is delivered.\nStop: If exploitability is uncertain, mark [Uncertain] and explain why.",
      "source": "Crash Override (Prompting LLMs for Security Reviews) · Anthropic claude-code code-review plugin"
    },
    {
      "id": "feature",
      "label": "Neues Feature",
      "blockIds": ["role", "goal", "grounding", "scope-constraints", "persistence", "verification"],
      "typeBody": "Before code: ask up to 3 clarifying questions if requirements are ambiguous.\nDesign across layers: data model (tables/keys/migrations) · API (endpoints/shapes/auth/errors) · UI (screens/loading-empty-error states).\nPropose a written plan. Wait for approval before editing files.\nConstraints: keep each diff ≤~300 lines & reviewable; add unit tests for logic + one integration happy-path; update docs.",
      "defaultDoneWhen": "all layers implemented, tests pass, docs updated.",
      "checklist": ["Clarifying questions first", "Cross-layer design", "Written plan + approval gate", "Diffs ≤300 lines", "Unit + happy-path tests", "Docs updated"],
      "rawTemplate": "Implement: [feature].\nBefore code: ask up to 3 clarifying questions if requirements are ambiguous.\nDesign across layers: data model (tables/keys/migrations) · API (endpoints/shapes/auth/errors) · UI (screens/loading-empty-error states).\nPropose a written plan. Wait for approval before editing files.\nConstraints: keep each diff ≤~300 lines & reviewable; add unit tests for logic + one integration happy-path; update docs.\nDone-when: all layers implemented, tests pass, docs updated.\nStop: if a design choice blocks >1 layer, surface it before proceeding.",
      "source": "QuantumByte · Four Modalities (DEV)"
    },
    {
      "id": "bugfix",
      "label": "Bugfix / Debugging",
      "blockIds": ["role", "goal", "grounding", "persistence", "verification"],
      "typeBody": "Step 1 — Reason first (no code): list 5–7 possible root causes + a diagnostic for each.\nStep 2 — Diagnose: add minimal logging/assertions; show output before proposing a fix.\nStep 3 — Fix: smallest safe change; explain why it fixes the root cause, not the symptom.\nStep 4 — Verify: exact test command + expected output; add a regression test; prefer fixing over disabling tests.",
      "defaultDoneWhen": "previously failing test passes; regression test added; no new failures.",
      "checklist": ["Reason-first before code", "Diagnose with evidence", "Smallest safe fix", "Regression test added", "No disabled tests"],
      "rawTemplate": "Bug: [symptom / error / stack trace]   Reproduce: [steps]   Expected/Actual: […]\nStep 1 — Reason first (no code): list 5–7 possible root causes + a diagnostic for each.\nStep 2 — Diagnose: add minimal logging/assertions; show output before proposing a fix.\nStep 3 — Fix: smallest safe change; explain why it fixes the root cause, not the symptom.\nStep 4 — Verify: exact test command + expected output; add a regression test; prefer fixing over disabling tests.\nDone-when: previously failing test passes; regression test added; no new failures.\nStop: if diagnosis is inconclusive after Step 2, report and ask before fixing.",
      "source": "Agentic Coding Handbook (Debug Workflow) · QuantumByte"
    },
    {
      "id": "refactor",
      "label": "Refactor (verhaltenserhaltend)",
      "blockIds": ["role", "goal", "scope-constraints", "verification"],
      "typeBody": "Goal: improve [readability/structure/perf — pick one] without changing behavior.\nStep 1 — Characterization tests: lock in current observable behavior; must pass before & after.\nStep 2 — Refactor in small steps; explain each; call out any suspected behavior change.\nConstraints: no bug fixes / no features / no API changes this pass; keep commits individually reviewable.",
      "defaultDoneWhen": "all characterization tests pass; diff reviewable; no suspected behavior changes.",
      "checklist": ["Characterization tests first", "Small reviewable steps", "No behavior change", "No features/bugfixes this pass"],
      "rawTemplate": "Refactor: [target file/function]. Goal: improve [readability/structure/perf — pick one] without changing behavior.\nStep 1 — Characterization tests: lock in current observable behavior; must pass before & after.\nStep 2 — Refactor in small steps; explain each; call out any suspected behavior change.\nConstraints: no bug fixes / no features / no API changes this pass; keep commits individually reviewable.\nDone-when: all characterization tests pass; diff reviewable; no suspected behavior changes.\nStop: if a step would change observable behavior, stop and ask.",
      "source": "QuantumByte · Azure DEV (Prompt-Driven Refactor)"
    },
    {
      "id": "research",
      "label": "Research / Investigation (kein Code)",
      "blockIds": ["role", "goal", "grounding", "scope-constraints", "output-format"],
      "typeBody": "Constraint: your ONLY job is to document & explain as it exists today — no improvements, no critique, no file changes (except the research doc).\nProcedure: decompose into sub-questions → per sub-question find evidence (file:line + commit SHA, or URL) → synthesize → every claim cited.\nOutput: YAML frontmatter (date, question, git_commit) + Findings (section per sub-question) + Summary (3–5 bullets).",
      "defaultDoneWhen": "all sub-questions answered with citations; summary delivered.",
      "checklist": ["Document-only constraint", "Decompose into sub-questions", "Every claim cited", "YAML frontmatter output", "No speculation on gaps"],
      "rawTemplate": "Research task: [question].\nConstraint: your ONLY job is to document & explain as it exists today — no improvements, no critique, no file changes (except the research doc).\nProcedure: decompose into sub-questions → per sub-question find evidence (file:line + commit SHA, or URL) → synthesize → every claim cited.\nOutput: YAML frontmatter (date, question, git_commit) + Findings (section per sub-question) + Summary (3–5 bullets).\nDone-when: all sub-questions answered with citations; summary delivered.\nStop: if evidence is contradictory or missing, report the gap — do not speculate.",
      "source": "HumanLayer research_codebase.md"
    }
  ],
  "modes": [
    {
      "id": "stop-on-doubt",
      "label": "Stop-on-Doubt",
      "description": "Hält vor jeder irreversiblen Aktion an und wartet auf Bestätigung; stoppt sofort bei Fehlern.",
      "overrides": {
        "reversibilityGate": "Before any irreversible action (delete, push, migrate, overwrite): state exactly what you will do, identify what cannot be undone, and wait for explicit confirmation.",
        "escalation": "If a tool output indicates failure or unexpected results, halt immediately — do not continue past a failed step. If a previous step violated a constraint: halt, state which rule, propose a fix, wait for confirmation."
      },
      "rawPreset": "Before any irreversible action (delete, push, migrate, overwrite): state exactly what you will do, identify what cannot be undone, and wait for explicit confirmation. If a tool output indicates failure or unexpected results, halt immediately — do not continue past a failed step. If a previous step violated a constraint: halt, state which rule, propose a fix, wait for confirmation.",
      "source": "Anthropic Claude Code Auto Mode · Cline Rules (Community)"
    },
    {
      "id": "act-and-report",
      "label": "Act-and-Report",
      "description": "Arbeitet autonom im Projektverzeichnis; externe Aktionen werden ausgeführt und sofort gemeldet.",
      "overrides": {
        "persistence": "Proceed autonomously for all reads and edits within the project directory. For shell commands, external API calls, and filesystem ops outside the project: execute, then immediately report what was done and the result.",
        "escalation": "If 3 consecutive unexpected results accumulate, stop and present a status summary before continuing."
      },
      "rawPreset": "Proceed autonomously for all reads and edits within the project directory. For shell commands, external API calls, and filesystem ops outside the project: execute, then immediately report what was done and the result. If 3 consecutive unexpected results accumulate, stop and present a status summary before continuing.",
      "source": "Anthropic Claude Code Auto Mode · floydous production prompt (Community)"
    },
    {
      "id": "fully-autonomous",
      "label": "Fully-Autonomous",
      "description": "Keine Bestätigungsschleifen; harte Deny-Liste; nur bei abgesicherter Umgebung einsetzen.",
      "overrides": {
        "persistence": "No confirmation loops. Proceed with sensible defaults. Don't finish until tests pass and the linter is green. If something goes sideways, stop and re-plan.",
        "reversibilityGate": "Allow exceptions: installing packages declared in the manifest, standard credential flows, pushing to the working branch. Deny unconditionally: force-push over history, mass-delete, writing credentials to unrelated files, exfiltration.",
        "escalation": "Precondition (operator-set): network egress locked down, no secrets loaded, file system scoped to project dir, disposable env, git checkpoints in place."
      },
      "rawPreset": "No confirmation loops. Proceed with sensible defaults. Allow exceptions: installing packages declared in the manifest, standard credential flows, pushing to the working branch. Deny unconditionally: force-push over history, mass-delete, writing credentials to unrelated files, exfiltration. Don't finish until tests pass and the linter is green. If something goes sideways, stop and re-plan.\nPrecondition (operator-set): network egress locked down, no secrets loaded, file system scoped to project dir, disposable env, git checkpoints in place.",
      "source": "Anthropic „Measuring Agent Autonomy\" · OWASP LLM06:2025 · TrueFoundry (Safe-Boundary-Precondition)"
    }
  ],
  "targets": [
    { "id": "claude-goal", "label": "Claude Code · /goal", "mechanicNote": "Stop-Hook + Haiku-Evaluator; der Evaluator sieht NUR den Transcript-Output, kein Filesystem. Bedingung: messbarer End-State + stated check (npm test exits 0, git status clean) + Constraint + optional 'or stop after N turns'. Max 4000 Zeichen.", "wrapMode": "completion-condition", "source": "Anthropic /goal Docs · Linas Substack (Community, Pitfalls)" },
    { "id": "claude-loop", "label": "Claude Code · /loop", "mechanicNote": "Intervall (/loop 5m …) oder self-paced (/loop …) oder bare (Maintenance-Prompt). Self-paced: Claude beendet selbst, wenn 'provably complete'.", "wrapMode": "interval-loop", "source": "Anthropic Scheduled-Tasks Docs · Piebald-AI (Skill-Source, Community)" },
    { "id": "codex-goal", "label": "Codex · /goal", "mechanicNote": "experimentell (v0.128.0+), --approval-mode full-auto; kein externer Evaluator (self-assessed); pause/resume/clear; AGENTS.md als Betriebshandbuch.", "wrapMode": "full-auto", "source": "OpenAI Cookbook (Using Goals in Codex) · GitHub openai/codex" },
    { "id": "generic", "label": "Generischer System-Prompt", "mechanicNote": "Volle Block-Montage als System-Prompt, XML-getaggt.", "wrapMode": "system-prompt", "source": "Anthropic · OpenAI" }
  ],
  "heuristic": [
    { "id": "done-when", "label": "Hat Done-When", "appliesTo": ["*"], "weight": 1, "rationale": "größter Einzel-Hebel" },
    { "id": "stop-condition", "label": "Hat Stop-Bedingung", "appliesTo": ["*"], "weight": 1, "rationale": "verhindert Drift/stilles Falsch-Handeln" },
    { "id": "scope-limited", "label": "Scope begrenzt (Datei/Verzeichnis)", "appliesTo": ["*"], "weight": 1, "rationale": "verhindert Scope-Creep" },
    { "id": "plan-first", "label": "Plan-First vor Code", "appliesTo": ["feature", "bugfix"], "weight": 1, "rationale": "Reasoning-first hebt Patch-Qualität" },
    { "id": "output-format", "label": "Output-Format spezifiziert", "appliesTo": ["*"], "weight": 1, "rationale": "maschinen-verarbeitbar" },
    { "id": "read-only", "label": "Read-Only-Pledge", "appliesTo": ["audit"], "weight": 1, "rationale": "sonst wird Audit zum ungewollten Fix" },
    { "id": "behavior-preservation", "label": "Behavior-Preservation-Pledge", "appliesTo": ["refactor"], "weight": 1, "rationale": "sonst stilles Verhaltens-Drift" },
    { "id": "regression-test", "label": "Regression-Test verlangt", "appliesTo": ["bugfix"], "weight": 1, "rationale": "sonst kehrt der Bug zurück" },
    { "id": "clarification-gate", "label": "Clarification-Gate", "appliesTo": ["feature", "research"], "weight": 1, "rationale": "gegen stille Fehlinterpretation" },
    { "id": "severity-label", "label": "Severity-Label", "appliesTo": ["audit"], "weight": 1, "rationale": "sonst unpriorisierte Findings" }
  ],
  "evalEvidence": [
    { "name": "SWE-bench Verified", "measures": "echte GitHub-Issues, Patch muss Tests grün machen (500 menschl. annotierte)", "keyNumber": "GPT-4o 16% → 33,2% nur durch besseres Scaffold (Agentless)", "lesson": "Scaffold ≫ Modell", "source": "OpenAI (Introducing SWE-bench Verified) · Epoch AI · CodeAnt" },
    { "name": "SWE-bench Pro", "measures": "frische, kontaminationsarme Codebases", "keyNumber": "Opus 4.5 80,9% → 45,9% (−35 pp)", "lesson": "Verified-Scores tragen Training-Overlap", "source": "arxiv 2509.16941 (SWE-bench Pro)" },
    { "name": "Aider Polyglot", "measures": "225 Exercism-Tasks, 6 Sprachen, 2. Versuch", "keyNumber": "Refact.ai+Claude 3.7: 92,9% vs. bare 60,4% (+32,5 pp)", "lesson": "Scaffold/Prompt hebt identisches Modell massiv", "source": "Aider Leaderboard · Refact.ai Blog" },
    { "name": "terminal-bench 2.0", "measures": "End-to-End-Terminal-Workflows (Docker)", "keyNumber": "Codex (gpt-5-codex) 42,8%; Frontier <65%", "lesson": "misst Scaffold-Robustheit & Fehler-Recovery", "source": "Snorkel AI (terminal-bench 2.0) · Artificial Analysis" }
  ]
}
```

- [ ] **Step 2: Validate the JSON parses.**

Run: `python -c "import json; d=json.load(open('hermes_cli/data/promptforge_catalog.json')); print(len(d['blocks']), len(d['taskTypes']), len(d['modes']), len(d['targets']), len(d['heuristic']), len(d['evalEvidence']))"`
Expected: `12 5 3 4 10 4`

- [ ] **Step 3: Commit** (do not push).

```bash
git add hermes_cli/data/promptforge_catalog.json
git commit -m "feat(promptforge): seed catalog JSON (12 blocks, 5 task-types, 3 modes, 4 targets)"
```

---

## Task 2: Backend endpoint + pytest (TDD)

**Files:**
- Create: `tests/test_promptforge.py`
- Create: `hermes_cli/promptforge_view.py`
- Modify: `hermes_cli/web_server.py` (register call)

- [ ] **Step 1: Write the failing test.**

```python
# tests/test_promptforge.py
"""GET /api/promptforge/catalog — read-only static catalog endpoint."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import promptforge_view


def _client() -> TestClient:
    app = FastAPI()
    promptforge_view.register_promptforge_routes(app)
    return TestClient(app)


def test_catalog_returns_200_and_full_schema():
    resp = _client().get("/api/promptforge/catalog")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == 1
    for key in ("blocks", "taskTypes", "modes", "targets", "heuristic", "evalEvidence"):
        assert isinstance(data[key], list) and data[key], f"{key} must be a non-empty list"
    assert len(data["blocks"]) == 12
    assert len(data["taskTypes"]) == 5
    assert len(data["modes"]) == 3
    assert len(data["targets"]) == 4
    assert len(data["heuristic"]) == 10
    assert len(data["evalEvidence"]) == 4


def test_blocks_have_required_fields():
    data = _client().get("/api/promptforge/catalog").json()
    for block in data["blocks"]:
        for field in ("id", "letter", "label", "description", "body", "source", "category"):
            assert block.get(field), f"block {block.get('id')} missing {field}"
        assert block["category"] in ("core", "long-run", "optional")


def test_task_types_reference_known_blocks():
    data = _client().get("/api/promptforge/catalog").json()
    known = {b["id"] for b in data["blocks"]}
    for tt in data["taskTypes"]:
        for field in ("id", "label", "blockIds", "typeBody", "defaultDoneWhen", "checklist", "rawTemplate", "source"):
            assert tt.get(field), f"taskType {tt.get('id')} missing {field}"
        for bid in tt["blockIds"]:
            assert bid in known, f"taskType {tt['id']} references unknown block {bid}"
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `scripts/run_tests.sh tests/test_promptforge.py`
Expected: FAIL — `ModuleNotFoundError: hermes_cli.promptforge_view` (module not yet created).

- [ ] **Step 3: Implement the endpoint module.**

```python
# hermes_cli/promptforge_view.py
"""Read-only Prompt-Schmiede catalog endpoint.

Serves the static curated prompt catalog (hermes_cli/data/promptforge_catalog.json)
under GET /api/promptforge/catalog. No state, no mutation, no auth call needed —
the blanket auth_middleware already gates /api/ paths not in PUBLIC_API_PATHS,
and this catalog is intentionally NOT public.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

_CATALOG_PATH = Path(__file__).parent / "data" / "promptforge_catalog.json"

_cache: dict[str, Any] | None = None


def _load_catalog() -> dict[str, Any]:
    """Load + cache the catalog JSON from disk (blocking; call via to_thread)."""
    global _cache
    if _cache is None:
        _cache = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    return _cache


def register_promptforge_routes(app: Any) -> None:
    @app.get("/api/promptforge/catalog")
    async def get_promptforge_catalog() -> dict[str, Any]:
        return await asyncio.to_thread(_load_catalog)
```

- [ ] **Step 4: Run the test to verify it passes.**

Run: `scripts/run_tests.sh tests/test_promptforge.py`
Expected: PASS (3 tests).

- [ ] **Step 5: Wire the registration into web_server.py.**

Find the block where other `register_*_routes(app)` are called (grounding: `web_server.py:12037-12054`). Add the import with the other `hermes_cli` view imports and the call alongside the others:

```python
from hermes_cli.promptforge_view import register_promptforge_routes
# ... near the other register_*_routes(app) calls:
register_promptforge_routes(app)
```

- [ ] **Step 6: ruff + commit.**

Run: `ruff check hermes_cli/promptforge_view.py tests/test_promptforge.py`
Expected: no errors.

```bash
git add hermes_cli/promptforge_view.py tests/test_promptforge.py hermes_cli/web_server.py
git commit -m "feat(promptforge): GET /api/promptforge/catalog endpoint + tests"
```

---

## Task 3: Frontend catalog types

**Files:**
- Create: `web/src/control/views/schmiede/catalog.ts`

- [ ] **Step 1: Create the types file** (mirrors Spec §4 + the selection model the composer consumes).

```ts
// web/src/control/views/schmiede/catalog.ts
export type BlockCategory = "core" | "long-run" | "optional";
export type WrapMode = "completion-condition" | "interval-loop" | "full-auto" | "system-prompt";

export interface Block {
  id: string;
  letter: string;
  label: string;
  description: string;
  body: string;
  source: string;
  category: BlockCategory;
}

export interface TaskType {
  id: string;
  label: string;
  blockIds: string[];
  typeBody: string;
  defaultDoneWhen: string;
  checklist: string[];
  rawTemplate: string;
  source: string;
}

export interface ModeOverrides {
  reversibilityGate?: string;
  escalation?: string;
  persistence?: string;
}

export interface Mode {
  id: string;
  label: string;
  description: string;
  overrides: ModeOverrides;
  rawPreset: string;
  source: string;
}

export interface Target {
  id: string;
  label: string;
  mechanicNote: string;
  wrapMode: WrapMode;
  source: string;
}

export interface HeuristicCheck {
  id: string;
  label: string;
  appliesTo: string[];
  weight: number;
  rationale: string;
}

export interface EvalEvidence {
  name: string;
  measures: string;
  keyNumber: string;
  lesson: string;
  source: string;
}

export interface PromptForgeCatalog {
  version: number;
  blocks: Block[];
  taskTypes: TaskType[];
  modes: Mode[];
  targets: Target[];
  heuristic: HeuristicCheck[];
  evalEvidence: EvalEvidence[];
}

/** The Konfigurator's current selection — drives compose(). */
export interface ForgeSelection {
  targetId: string;
  taskTypeId: string;
  modeId: string;
  modelId: string;
  slots: {
    task: string;
    scope: string;
    /** /loop only: suggested interval in minutes (undefined = self-paced). */
    intervalMinutes?: number;
    /** completion-condition / interval-loop: max turns/rounds before stop. */
    maxTurns?: number;
  };
}
```

- [ ] **Step 2: Commit.**

```bash
git add web/src/control/views/schmiede/catalog.ts
git commit -m "feat(promptforge): frontend catalog types"
```

---

## Task 4: composer.ts (TDD)

**Files:**
- Test: `web/src/control/views/schmiede/composer.test.ts`
- Create: `web/src/control/views/schmiede/composer.ts`

Composer contract: `compose(selection, catalog)` assembles deterministically in Spec §5 order — (1) Role + Goal + Scope (slot-filled), (2) task-type body, (3) persistence (mode override wins, else block E), (4) verification (block I), (5) Done-when (type default), (6) reversibility-gate + escalation from mode overrides, then (7) the target adapter wrap. References blocks by known id; `blockIds`/`checklist` drive the Kanon display, not the composer.

- [ ] **Step 1: Write the failing test.** Uses a small inline fixture catalog so the test is hermetic.

```ts
// web/src/control/views/schmiede/composer.test.ts
import { describe, expect, it } from "vitest";
import { compose } from "./composer";
import type { ForgeSelection, PromptForgeCatalog } from "./catalog";

const CATALOG: PromptForgeCatalog = {
  version: 1,
  blocks: [
    { id: "role", letter: "A", label: "Role", description: "", body: "You are a [role].", source: "", category: "core" },
    { id: "goal", letter: "B", label: "Goal", description: "", body: "goal-block", source: "", category: "core" },
    { id: "scope-constraints", letter: "G", label: "Scope", description: "", body: "scope-block", source: "", category: "core" },
    { id: "persistence", letter: "E", label: "Persistence", description: "", body: "KEEP-GOING-BASE", source: "", category: "core" },
    { id: "verification", letter: "I", label: "Verification", description: "", body: "VERIFY-BLOCK", source: "", category: "core" },
  ],
  taskTypes: [
    { id: "audit", label: "Audit", blockIds: ["role", "goal"], typeBody: "AUDIT-BODY do not modify any files", defaultDoneWhen: "report delivered", checklist: [], rawTemplate: "", source: "" },
    { id: "feature", label: "Feature", blockIds: ["role", "goal"], typeBody: "FEATURE-BODY", defaultDoneWhen: "tests pass", checklist: [], rawTemplate: "", source: "" },
  ],
  modes: [
    { id: "stop-on-doubt", label: "Stop", description: "", overrides: { reversibilityGate: "REV-GATE wait for explicit confirmation", escalation: "ESC-HALT" }, rawPreset: "", source: "" },
    { id: "fully-autonomous", label: "Auto", description: "", overrides: { persistence: "NO-CONFIRM-LOOPS", reversibilityGate: "DENY force-push mass-delete", escalation: "PRECONDITION egress locked" }, rawPreset: "", source: "" },
  ],
  targets: [
    { id: "generic", label: "Generic", mechanicNote: "", wrapMode: "system-prompt", source: "" },
    { id: "claude-goal", label: "claude /goal", mechanicNote: "", wrapMode: "completion-condition", source: "" },
    { id: "claude-loop", label: "claude /loop", mechanicNote: "", wrapMode: "interval-loop", source: "" },
    { id: "codex-goal", label: "codex /goal", mechanicNote: "", wrapMode: "full-auto", source: "" },
  ],
  heuristic: [],
  evalEvidence: [],
};

function sel(over: Partial<ForgeSelection> = {}): ForgeSelection {
  return {
    targetId: "generic",
    taskTypeId: "audit",
    modeId: "stop-on-doubt",
    modelId: "claude-opus-4-8",
    slots: { task: "Fix the login race in auth.ts", scope: "src/auth", ...(over.slots ?? {}) },
    ...over,
  };
}

describe("compose", () => {
  it("fills role, goal and scope slots", () => {
    const out = compose(sel(), CATALOG);
    expect(out).toContain("You are a [role].");
    expect(out).toContain("Goal: Fix the login race in auth.ts");
    expect(out).toContain("Scope: src/auth");
  });

  it("includes the task-type body, verification block and type done-when", () => {
    const out = compose(sel(), CATALOG);
    expect(out).toContain("AUDIT-BODY");
    expect(out).toContain("VERIFY-BLOCK");
    expect(out).toContain("Done-when: report delivered");
  });

  it("uses base persistence unless the mode overrides it", () => {
    expect(compose(sel({ modeId: "stop-on-doubt" }), CATALOG)).toContain("KEEP-GOING-BASE");
    const auto = compose(sel({ modeId: "fully-autonomous" }), CATALOG);
    expect(auto).toContain("NO-CONFIRM-LOOPS");
    expect(auto).not.toContain("KEEP-GOING-BASE");
  });

  it("injects mode reversibility-gate and escalation overrides", () => {
    const out = compose(sel({ modeId: "stop-on-doubt" }), CATALOG);
    expect(out).toContain("REV-GATE");
    expect(out).toContain("ESC-HALT");
  });

  it("wraps generic target as an XML system prompt", () => {
    const out = compose(sel({ targetId: "generic" }), CATALOG);
    expect(out.startsWith("<system_prompt>")).toBe(true);
    expect(out.trimEnd().endsWith("</system_prompt>")).toBe(true);
  });

  it("wraps claude /goal with a transcript-provable completion condition + turn cap", () => {
    const out = compose(sel({ targetId: "claude-goal", slots: { task: "t", scope: "s", maxTurns: 12 } }), CATALOG);
    expect(out).toContain("/goal");
    expect(out).toContain("stop after 12 turns");
    expect(out.toLowerCase()).toContain("transcript");
  });

  it("wraps claude /loop with interval and round protocol", () => {
    const withIv = compose(sel({ targetId: "claude-loop", slots: { task: "t", scope: "s", intervalMinutes: 5 } }), CATALOG);
    expect(withIv).toContain("/loop 5m");
    expect(withIv).toContain("[DONE]");
    const selfPaced = compose(sel({ targetId: "claude-loop", slots: { task: "t", scope: "s" } }), CATALOG);
    expect(selfPaced).toContain("self-paced");
  });

  it("wraps codex /goal full-auto with a hard deny list", () => {
    const out = compose(sel({ targetId: "codex-goal" }), CATALOG);
    expect(out.toLowerCase()).toContain("full-auto");
    expect(out.toLowerCase()).toContain("force-push");
  });

  it("surfaces the chosen model as a hint line", () => {
    expect(compose(sel({ modelId: "gpt-5.5" }), CATALOG)).toContain("gpt-5.5");
  });

  it("falls back to placeholders for empty slots and returns '' on unknown ids", () => {
    expect(compose(sel({ slots: { task: "", scope: "" } }), CATALOG)).toContain("[describe the task");
    expect(compose(sel({ taskTypeId: "nope" }), CATALOG)).toBe("");
  });
});
```

- [ ] **Step 2: Run it to verify it fails.**

Run (from live checkout): `cd ~/.hermes/hermes-agent/web && .bin/vitest run src/control/views/schmiede/composer.test.ts`
Expected: FAIL — `compose` is not exported / file missing.

- [ ] **Step 3: Implement composer.ts.**

```ts
// web/src/control/views/schmiede/composer.ts
import type { ForgeSelection, PromptForgeCatalog, Target, TaskType } from "./catalog";

function blockBody(catalog: PromptForgeCatalog, id: string): string {
  return catalog.blocks.find((b) => b.id === id)?.body ?? "";
}

/** Deterministic best-practice assembly (Spec §5), then target-adapter wrap. */
export function compose(selection: ForgeSelection, catalog: PromptForgeCatalog): string {
  const taskType = catalog.taskTypes.find((t) => t.id === selection.taskTypeId);
  const mode = catalog.modes.find((m) => m.id === selection.modeId);
  const target = catalog.targets.find((t) => t.id === selection.targetId);
  if (!taskType || !mode || !target) return "";

  const task = selection.slots.task.trim() || "[describe the task: file + symptom + outcome]";
  const scope = selection.slots.scope.trim() || "[scope: file / directory boundary]";

  const parts: string[] = [];
  parts.push(blockBody(catalog, "role"));            // A
  parts.push(`Goal: ${task}`);                       // B (slot)
  parts.push(`Scope: ${scope}`);                     // G (slot)
  parts.push(taskType.typeBody);                     // type-specific core
  parts.push(mode.overrides.persistence ?? blockBody(catalog, "persistence")); // E (mode wins)
  parts.push(blockBody(catalog, "verification"));    // I
  parts.push(`Done-when: ${taskType.defaultDoneWhen}`); // F
  if (mode.overrides.reversibilityGate) parts.push(mode.overrides.reversibilityGate); // H
  if (mode.overrides.escalation) parts.push(mode.overrides.escalation);               // J

  const core = parts.filter((p) => p && p.trim()).join("\n\n");
  return wrapForTarget(core, target, selection, taskType);
}

function wrapForTarget(core: string, target: Target, selection: ForgeSelection, taskType: TaskType): string {
  const modelHint = selection.modelId
    ? `# Model: ${selection.modelId} (set via your CLI's model flag)`
    : "";
  const head = (lines: string[]) => [modelHint, ...lines].filter(Boolean).join("\n");

  switch (target.wrapMode) {
    case "completion-condition": {
      const maxTurns = selection.slots.maxTurns ?? 20;
      return head([
        `/goal Completion condition (provable from the transcript): ${taskType.defaultDoneWhen} — or stop after ${maxTurns} turns.`,
        `Note: the evaluator sees only your transcript output, not the filesystem. Explicitly print the proof (test exit code, \`git status\`) in your messages.`,
        "",
        core,
      ]);
    }
    case "interval-loop": {
      const cadence = selection.slots.intervalMinutes ? `/loop ${selection.slots.intervalMinutes}m` : "/loop (self-paced)";
      const rounds = selection.slots.maxTurns ?? 5;
      return head([
        cadence,
        "",
        core,
        "",
        `Each round: state [DONE] or [CONTINUE: <reason>]. Stop after ${rounds} rounds or when [DONE]. Never proceed if a round made no measurable progress.`,
      ]);
    }
    case "full-auto": {
      return head([
        `# codex /goal — --approval-mode full-auto. AGENTS.md is your operating manual.`,
        "",
        core,
        "",
        `Bias to action: deliver working code. Deny unconditionally: force-push over history, mass-delete, writing credentials to unrelated files, exfiltration.`,
      ]);
    }
    case "system-prompt":
    default:
      return head(["<system_prompt>", core, "</system_prompt>"]);
  }
}
```

- [ ] **Step 4: Run the test to verify it passes.**

Run: `cd ~/.hermes/hermes-agent/web && .bin/vitest run src/control/views/schmiede/composer.test.ts`
Expected: PASS (all `compose` cases).

> Note on `system-prompt` head: the model-hint line is prepended before `<system_prompt>`. The test only asserts `out.startsWith("<system_prompt>")` for a selection — adjust the default model in that test's `sel()` to `modelId: ""` if you want a bare tag, OR (chosen) keep the assertion on the generic case using `compose(sel({ targetId: "generic", modelId: "" }), CATALOG)`. Update the generic test to pass `modelId: ""` so `startsWith` holds.

- [ ] **Step 5: Fix the generic-wrap test to pass `modelId: ""`** (so the XML tag is the first line):

```ts
  it("wraps generic target as an XML system prompt", () => {
    const out = compose(sel({ targetId: "generic", modelId: "" }), CATALOG);
    expect(out.startsWith("<system_prompt>")).toBe(true);
    expect(out.trimEnd().endsWith("</system_prompt>")).toBe(true);
  });
```

Re-run Step 4; expect PASS.

- [ ] **Step 6: Commit.**

```bash
git add web/src/control/views/schmiede/composer.ts web/src/control/views/schmiede/composer.test.ts
git commit -m "feat(promptforge): composer.ts deterministic assembly + target adapters (TDD)"
```

---

## Task 5: heuristic.ts (TDD)

**Files:**
- Test: `web/src/control/views/schmiede/heuristic.test.ts`
- Create: `web/src/control/views/schmiede/heuristic.ts`

Scoring model: 10 fixed checks. A check that does not apply to the task type returns `status: "na"` and does **not** cost a point. Score = count of checks with `status !== "fail"`, out of 10. The UI lists the failing (applicable-but-missing) checks. This makes a strong, in-scope prompt score 8–10 (Spec §10).

- [ ] **Step 1: Write the failing test.**

```ts
// web/src/control/views/schmiede/heuristic.test.ts
import { describe, expect, it } from "vitest";
import { score } from "./heuristic";

const STRONG_AUDIT = `You are a security audit engineer.
Scope: Review files recently modified in src/ — do NOT touch anything outside.
Output: Numbered list. Each item: [file:line] [Critical|High|Medium|Low] [Issue] [Fix].
Do not modify any files.
Done-when: a prioritized report is delivered.
Stop: if exploitability is uncertain, mark [Uncertain].`;

describe("score", () => {
  it("scores a strong audit prompt >= 8 with max 10", () => {
    const r = score(STRONG_AUDIT, "audit");
    expect(r.max).toBe(10);
    expect(r.score).toBeGreaterThanOrEqual(8);
  });

  it("marks non-applicable checks as na (e.g. plan-first on audit)", () => {
    const r = score(STRONG_AUDIT, "audit");
    expect(r.checks.find((c) => c.id === "plan-first")?.status).toBe("na");
  });

  it("fails the done-when check when the prompt lacks it", () => {
    const r = score("You are a dev. Just do something useful.", "feature");
    expect(r.checks.find((c) => c.id === "done-when")?.status).toBe("fail");
  });

  it("fails read-only for an audit prompt without a read-only pledge", () => {
    const noPledge = `You are an auditor. Output: numbered list with [Critical] severity. Done-when: report done. Stop if unsure.`;
    const r = score(noPledge, "audit");
    expect(r.checks.find((c) => c.id === "read-only")?.status).toBe("fail");
  });

  it("read-only is na for a feature prompt", () => {
    const r = score("Implement X. Done-when: tests pass. Stop if ambiguous.", "feature");
    expect(r.checks.find((c) => c.id === "read-only")?.status).toBe("na");
  });
});
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `cd ~/.hermes/hermes-agent/web && .bin/vitest run src/control/views/schmiede/heuristic.test.ts`
Expected: FAIL — `score` not exported.

- [ ] **Step 3: Implement heuristic.ts.**

```ts
// web/src/control/views/schmiede/heuristic.ts
export type CheckStatus = "pass" | "fail" | "na";

export interface CheckResult {
  id: string;
  label: string;
  status: CheckStatus;
  rationale: string;
}

export interface HeuristicResult {
  score: number;
  max: number;
  checks: CheckResult[];
}

interface Detector {
  id: string;
  label: string;
  appliesTo: string[];
  rationale: string;
  test: (p: string) => boolean;
}

// ids + labels MUST stay in sync with the catalog `heuristic[]` rows (the
// catalog provides documentation; the predicates below cannot live in JSON).
const DETECTORS: Detector[] = [
  { id: "done-when", label: "Hat Done-When", appliesTo: ["*"], rationale: "größter Einzel-Hebel", test: (p) => /done[- ]when|completion condition|finished when|done:/i.test(p) },
  { id: "stop-condition", label: "Hat Stop-Bedingung", appliesTo: ["*"], rationale: "verhindert Drift/stilles Falsch-Handeln", test: (p) => /\bstop\b|\bhalt\b|ask before|wait for (explicit )?confirmation|stop after/i.test(p) },
  { id: "scope-limited", label: "Scope begrenzt (Datei/Verzeichnis)", appliesTo: ["*"], rationale: "verhindert Scope-Creep", test: (p) => /scope:|only (make|touch|modify|change)|do not touch|outside|within .*(dir|directory|src)|files? (in|recently)/i.test(p) },
  { id: "plan-first", label: "Plan-First vor Code", appliesTo: ["feature", "bugfix"], rationale: "Reasoning-first hebt Patch-Qualität", test: (p) => /reason first|before (writing )?code|propose a (written )?plan|wait for approval|\(no code\)/i.test(p) },
  { id: "output-format", label: "Output-Format spezifiziert", appliesTo: ["*"], rationale: "maschinen-verarbeitbar", test: (p) => /output:|output format|numbered list|\bjson\b|\byaml\b|\bxml\b|frontmatter|format:/i.test(p) },
  { id: "read-only", label: "Read-Only-Pledge", appliesTo: ["audit"], rationale: "sonst wird Audit zum ungewollten Fix", test: (p) => /do not modify|read[- ]only|don'?t (change|edit|modify)|no file changes/i.test(p) },
  { id: "behavior-preservation", label: "Behavior-Preservation-Pledge", appliesTo: ["refactor"], rationale: "sonst stilles Verhaltens-Drift", test: (p) => /without changing behavior|behavior[- ]preserv|characterization test|same observable behavior/i.test(p) },
  { id: "regression-test", label: "Regression-Test verlangt", appliesTo: ["bugfix"], rationale: "sonst kehrt der Bug zurück", test: (p) => /regression test|add a .{0,15}test|previously failing test/i.test(p) },
  { id: "clarification-gate", label: "Clarification-Gate", appliesTo: ["feature", "research"], rationale: "gegen stille Fehlinterpretation", test: (p) => /clarifying question|ask .{0,20}question|if .{0,30}ambiguous|surface it|surface the/i.test(p) },
  { id: "severity-label", label: "Severity-Label", appliesTo: ["audit"], rationale: "sonst unpriorisierte Findings", test: (p) => /severity|\[critical|critical\s*\|\s*high|priorit/i.test(p) },
];

export function score(promptText: string, taskTypeId: string): HeuristicResult {
  const checks: CheckResult[] = DETECTORS.map((d) => {
    const applies = d.appliesTo.includes("*") || d.appliesTo.includes(taskTypeId);
    if (!applies) return { id: d.id, label: d.label, status: "na" as const, rationale: d.rationale };
    return { id: d.id, label: d.label, status: d.test(promptText) ? "pass" : ("fail" as const), rationale: d.rationale };
  });
  const passed = checks.filter((c) => c.status !== "fail").length;
  return { score: passed, max: DETECTORS.length, checks };
}
```

- [ ] **Step 4: Run the test to verify it passes.**

Run: `cd ~/.hermes/hermes-agent/web && .bin/vitest run src/control/views/schmiede/heuristic.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add web/src/control/views/schmiede/heuristic.ts web/src/control/views/schmiede/heuristic.test.ts
git commit -m "feat(promptforge): heuristic.ts 10-point client-side score (TDD)"
```

---

## Task 6: usePromptForgeCatalog() hook

**Files:**
- Modify: `web/src/control/hooks/useControlData.ts`

- [ ] **Step 1: Add the one-shot hook** (model: `useDeepAudit`, `useControlData.ts:372-451`). Place it near the other resource hooks. Ensure `fetchJSON` is already imported (it is, line 2).

```ts
// add to web/src/control/hooks/useControlData.ts
import type { PromptForgeCatalog } from "../views/schmiede/catalog";

export interface PromptForgeCatalogState {
  data: PromptForgeCatalog | null;
  error: string | null;
  loading: boolean;
  lastUpdated: number | null;
}

/** One-shot load of the static Prompt-Schmiede catalog. No polling. */
export function usePromptForgeCatalog(): PromptForgeCatalogState {
  const [data, setData] = useState<PromptForgeCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const payload = await fetchJSON<PromptForgeCatalog>("/api/promptforge/catalog");
        if (!aliveRef.current) return;
        setData(payload);
        setError(null);
        setLastUpdated(Math.floor(Date.now() / 1000));
      } catch (err) {
        if (!aliveRef.current) return;
        setError(err instanceof Error ? err.message : "Katalog konnte nicht geladen werden");
      } finally {
        if (aliveRef.current) setLoading(false);
      }
    })();
  }, []);

  return { data, error, loading, lastUpdated };
}
```

> Verify `useState`, `useEffect`, `useRef` are already imported at the top of `useControlData.ts` (they are — used by every hook). If `fetchJSON` is generic-typed, the `<PromptForgeCatalog>` call works; if not, cast: `(await fetchJSON("/api/promptforge/catalog")) as PromptForgeCatalog`.

- [ ] **Step 2: Type-check.**

Run: `cd ~/.hermes/hermes-agent/web && .bin/tsc --noEmit`
Expected: no new errors (other in-flight errors from parallel sessions are not ours — verify diff-relative).

- [ ] **Step 3: Commit.**

```bash
git add web/src/control/hooks/useControlData.ts
git commit -m "feat(promptforge): usePromptForgeCatalog one-shot hook"
```

---

## Task 7: Konfigurator.tsx (left half)

**Files:**
- Create: `web/src/control/views/schmiede/Konfigurator.tsx`

- [ ] **Step 1: Implement the Konfigurator.** Selection state → live `compose()` preview → `score()` panel → copy. Reuses `FleetPanel`, `CopyButton`, and `LaneModelOption`/`FALLBACK_MODELS` from lanes.

```tsx
// web/src/control/views/schmiede/Konfigurator.tsx
import { useMemo, useState } from "react";
import { FleetPanel } from "../../components/fleet/atoms";
import { CopyButton } from "../backlog/CopyButton";
import { FALLBACK_MODELS, type LaneModelOption } from "../lanes/api";
import type { ForgeSelection, PromptForgeCatalog } from "./catalog";
import { compose } from "./composer";
import { score } from "./heuristic";

export function Konfigurator({ catalog, models }: { catalog: PromptForgeCatalog; models?: LaneModelOption[] }) {
  const modelList = models && models.length > 0 ? models : FALLBACK_MODELS;
  const [selection, setSelection] = useState<ForgeSelection>(() => ({
    targetId: catalog.targets[0]?.id ?? "generic",
    taskTypeId: catalog.taskTypes[0]?.id ?? "audit",
    modeId: catalog.modes[0]?.id ?? "stop-on-doubt",
    modelId: modelList[0]?.id ?? "",
    slots: { task: "", scope: "", maxTurns: 20 },
  }));

  const preview = useMemo(() => compose(selection, catalog), [selection, catalog]);
  const rating = useMemo(() => score(preview, selection.taskTypeId), [preview, selection.taskTypeId]);
  const target = catalog.targets.find((t) => t.id === selection.targetId);
  const isLoop = target?.wrapMode === "interval-loop";
  const isGoal = target?.wrapMode === "completion-condition" || isLoop;

  const set = (patch: Partial<ForgeSelection>) => setSelection((s) => ({ ...s, ...patch }));
  const setSlot = (patch: Partial<ForgeSelection["slots"]>) => setSelection((s) => ({ ...s, slots: { ...s.slots, ...patch } }));

  return (
    <div className="grid gap-4">
      <FleetPanel eyebrow="Konfigurator">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Ziel-CLI">
            <Select value={selection.targetId} onChange={(v) => set({ targetId: v })} options={catalog.targets.map((t) => ({ value: t.id, label: t.label }))} />
          </Field>
          <Field label="Task-Typ">
            <Select value={selection.taskTypeId} onChange={(v) => set({ taskTypeId: v })} options={catalog.taskTypes.map((t) => ({ value: t.id, label: t.label }))} />
          </Field>
          <Field label="Modus">
            <Select value={selection.modeId} onChange={(v) => set({ modeId: v })} options={catalog.modes.map((m) => ({ value: m.id, label: m.label }))} />
          </Field>
          <Field label="Modell">
            <Select value={selection.modelId} onChange={(v) => set({ modelId: v })} options={modelList.map((m) => ({ value: m.id, label: m.label }))} />
          </Field>
        </div>
        <div className="mt-3 grid gap-3">
          <Field label="Aufgabe (Datei + Symptom + Outcome)">
            <textarea className="hc-input min-h-[64px] w-full" value={selection.slots.task} onChange={(e) => setSlot({ task: e.target.value })} placeholder="z.B. auth.ts: Login-Race → deterministische Session-Erstellung" />
          </Field>
          <Field label="Scope (Datei / Verzeichnis)">
            <input className="hc-input w-full" value={selection.slots.scope} onChange={(e) => setSlot({ scope: e.target.value })} placeholder="src/auth" />
          </Field>
          {isGoal ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {isLoop ? (
                <Field label="Intervall (Minuten, leer = self-paced)">
                  <input type="number" min={1} className="hc-input w-full" value={selection.slots.intervalMinutes ?? ""} onChange={(e) => setSlot({ intervalMinutes: e.target.value ? Number(e.target.value) : undefined })} />
                </Field>
              ) : null}
              <Field label={isLoop ? "Max Runden" : "Max Turns"}>
                <input type="number" min={1} className="hc-input w-full" value={selection.slots.maxTurns ?? ""} onChange={(e) => setSlot({ maxTurns: e.target.value ? Number(e.target.value) : undefined })} />
              </Field>
            </div>
          ) : null}
        </div>
        {target ? <p className="mt-3 text-xs hc-dim">{target.mechanicNote}</p> : null}
      </FleetPanel>

      <FleetPanel eyebrow="Live-Vorschau" meta={<CopyButton text={preview} label="Kopieren" copiedLabel="Kopiert" />}>
        <pre className="hc-mono max-h-[420px] overflow-auto whitespace-pre-wrap rounded-md bg-black/30 p-3 text-xs leading-relaxed text-white/90">{preview}</pre>
      </FleetPanel>

      <FleetPanel eyebrow="Qualitäts-Score" meta={<span className="hc-mono text-sm">{rating.score} / {rating.max}</span>}>
        <ul className="grid gap-1 text-sm">
          {rating.checks.map((c) => (
            <li key={c.id} className="flex items-center gap-2">
              <span className={c.status === "pass" ? "text-emerald-400" : c.status === "fail" ? "text-rose-400" : "hc-dim"}>
                {c.status === "pass" ? "✓" : c.status === "fail" ? "✗" : "–"}
              </span>
              <span className={c.status === "fail" ? "text-white" : "hc-soft"}>{c.label}</span>
              {c.status === "fail" ? <span className="hc-dim text-xs">— {c.rationale}</span> : null}
            </li>
          ))}
        </ul>
        <p className="mt-2 text-xs hc-dim">8–10 = gut · 5–7 = akzeptabel · &lt;5 = Drift-Risiko. „–" = für diesen Task-Typ nicht relevant.</p>
      </FleetPanel>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="grid gap-1 text-sm">
      <span className="hc-eyebrow">{label}</span>
      {children}
    </label>
  );
}

function Select({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: Array<{ value: string; label: string }> }) {
  return (
    <select className="hc-input w-full" value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
}
```

> If `hc-input` is not an existing token class, fall back to inline Tailwind (`rounded-md border border-white/10 bg-black/20 px-2 py-1.5 text-white`). Verify by grepping `hc-input` in `web/src/control/styles/`; if absent, replace all `className="hc-input ..."` with the Tailwind equivalent in this file before committing.

- [ ] **Step 2: Commit.**

```bash
git add web/src/control/views/schmiede/Konfigurator.tsx
git commit -m "feat(promptforge): Konfigurator — selection, live preview, score, copy"
```

---

## Task 8: Kanon.tsx (right half)

**Files:**
- Create: `web/src/control/views/schmiede/Kanon.tsx`

- [ ] **Step 1: Implement the Kanon** — renders the 12-block taxonomy, 5 raw templates, 3 mode presets, and the eval-evidence table; each prompt text copyable.

```tsx
// web/src/control/views/schmiede/Kanon.tsx
import { FleetPanel } from "../../components/fleet/atoms";
import { CopyButton } from "../backlog/CopyButton";
import type { PromptForgeCatalog } from "./catalog";

export function Kanon({ catalog }: { catalog: PromptForgeCatalog }) {
  return (
    <div className="grid gap-4">
      <FleetPanel eyebrow="12-Block-Taxonomie">
        <ul className="grid gap-2 text-sm">
          {catalog.blocks.map((b) => (
            <li key={b.id} className="rounded-md border border-white/5 bg-black/20 p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-white">{b.letter} · {b.label}</span>
                <span className="hc-dim text-xs">{b.category}</span>
              </div>
              <p className="mt-1 hc-soft text-xs">{b.description}</p>
              <div className="mt-1.5 flex items-start justify-between gap-2">
                <code className="hc-mono whitespace-pre-wrap text-xs text-white/80">{b.body}</code>
                <CopyButton text={b.body} label="Kopieren" copiedLabel="Kopiert" />
              </div>
              <p className="mt-1 hc-dim text-[10px]">{b.source}</p>
            </li>
          ))}
        </ul>
      </FleetPanel>

      <FleetPanel eyebrow="Rohe Vorlagen (Kanon)">
        <div className="grid gap-3">
          {catalog.taskTypes.map((t) => (
            <div key={t.id} className="rounded-md border border-white/5 bg-black/20 p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-white">{t.label}</span>
                <CopyButton text={t.rawTemplate} label="Kopieren" copiedLabel="Kopiert" />
              </div>
              <pre className="hc-mono mt-1.5 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-black/30 p-2 text-xs text-white/85">{t.rawTemplate}</pre>
              <p className="mt-1 hc-dim text-[10px]">{t.source}</p>
            </div>
          ))}
        </div>
      </FleetPanel>

      <FleetPanel eyebrow="Modus-Presets">
        <div className="grid gap-3">
          {catalog.modes.map((m) => (
            <div key={m.id} className="rounded-md border border-white/5 bg-black/20 p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-white">{m.label}</span>
                <CopyButton text={m.rawPreset} label="Kopieren" copiedLabel="Kopiert" />
              </div>
              <p className="mt-1 hc-soft text-xs">{m.description}</p>
              <pre className="hc-mono mt-1.5 whitespace-pre-wrap rounded bg-black/30 p-2 text-xs text-white/85">{m.rawPreset}</pre>
              <p className="mt-1 hc-dim text-[10px]">{m.source}</p>
            </div>
          ))}
        </div>
      </FleetPanel>

      <FleetPanel eyebrow="Eval-Evidenz" meta={<span className="hc-dim text-xs">Scaffold ≫ Modell</span>}>
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="hc-dim text-left">
                <th className="py-1 pr-2">Eval</th>
                <th className="py-1 pr-2">misst</th>
                <th className="py-1 pr-2">belegte Zahl</th>
                <th className="py-1">Lehre</th>
              </tr>
            </thead>
            <tbody>
              {catalog.evalEvidence.map((e) => (
                <tr key={e.name} className="border-t border-white/5 align-top">
                  <td className="py-1.5 pr-2 font-medium text-white">{e.name}</td>
                  <td className="py-1.5 pr-2 hc-soft">{e.measures}</td>
                  <td className="py-1.5 pr-2 hc-mono text-white/85">{e.keyNumber}</td>
                  <td className="py-1.5 hc-soft">{e.lesson}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </FleetPanel>
    </div>
  );
}
```

- [ ] **Step 2: Commit.**

```bash
git add web/src/control/views/schmiede/Kanon.tsx
git commit -m "feat(promptforge): Kanon — taxonomy, raw templates, presets, eval table"
```

---

## Task 9: SchmiedeView.tsx (the tab body)

**Files:**
- Create: `web/src/control/views/SchmiedeView.tsx`

- [ ] **Step 1: Implement the view** — loads the catalog via the hook, renders loading/error/empty, and lays out Konfigurator + Kanon side by side (stacked on mobile).

```tsx
// web/src/control/views/SchmiedeView.tsx
import { usePromptForgeCatalog } from "../hooks/useControlData";
import type { Density } from "../hooks/useDensity";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import { Konfigurator } from "./schmiede/Konfigurator";
import { Kanon } from "./schmiede/Kanon";

export function SchmiedeView(_props: { density?: Density }) {
  const { data, error, loading } = usePromptForgeCatalog();

  return (
    <div className="grid gap-4">
      <header>
        <p className="hc-eyebrow">Prompt-Schmiede</p>
        <h2 className="mt-1 text-xl font-semibold text-white">Best-Practice-Prompts für Agent-Steuerbefehle</h2>
        <p className="mt-1 hc-soft text-sm">Konfigurieren → kopieren → in Claude Code / Codex einfügen. Kein Dispatch, nur Text.</p>
      </header>

      {loading && !data ? (
        <FleetPanel eyebrow="Lädt"><p className="hc-soft text-sm">Katalog wird geladen …</p></FleetPanel>
      ) : error && !data ? (
        <FleetPanel eyebrow="Fehler"><FleetEmptyState title="Katalog nicht erreichbar" desc={error} /></FleetPanel>
      ) : data ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <Konfigurator catalog={data} models={data.targets ? undefined : undefined} />
          <Kanon catalog={data} />
        </div>
      ) : (
        <FleetPanel eyebrow="Leer"><FleetEmptyState title="Kein Katalog" desc="Die Antwort enthielt keine Daten." /></FleetPanel>
      )}
    </div>
  );
}
```

> The `models` prop on `<Konfigurator>` is left `undefined` so it falls back to `FALLBACK_MODELS` (the lanes catalog is the authoritative live list, but a one-shot fetch of lanes here is out of MVP scope — the spec permits `FALLBACK_MODELS` as the reuse path). Remove the dead `models={...}` and just render `<Konfigurator catalog={data} />`.

- [ ] **Step 2: Simplify the Konfigurator call** to `<Konfigurator catalog={data} />` (drop the confusing `models` ternary).

- [ ] **Step 3: Type-check.**

Run: `cd ~/.hermes/hermes-agent/web && .bin/tsc --noEmit`
Expected: no new errors.

- [ ] **Step 4: Commit.**

```bash
git add web/src/control/views/SchmiedeView.tsx
git commit -m "feat(promptforge): SchmiedeView — catalog load + two-column layout"
```

---

## Task 10: Tab wiring

**Files:**
- Modify: `web/src/control/components/ControlShell.tsx` (union + moreTabs)
- Modify: `web/src/control/i18n/de.ts` (tab label)
- Modify: `web/src/control/ControlPage.tsx` (lazy, activeFromPath, viewImporters, tabPath, Route)

- [ ] **Step 1: Extend the `ControlTab` union** at `ControlShell.tsx:11` — append `| "schmiede"`:

```ts
export type ControlTab = "overview" | "inbox" | "pulse" | "workstreams" | "flow" | "statistik" | "autoresearch" | "backlog" | "orchestrator" | "crons" | "lanes" | "research" | "bibliothek" | "schmiede";
```

- [ ] **Step 2: Add the `moreTabs` entry** (`ControlShell.tsx:28-42`). Import a fitting icon (`Hammer`) into the lucide import on line 2, then add to `moreTabs`:

```ts
// in the lucide-react import on line 2, add: Hammer
  { label: de.tabs.schmiede, path: "/control/schmiede", icon: Hammer },
```

- [ ] **Step 3: Add the i18n label** at `de.ts:8` — append `schmiede: "Prompt-Schmiede"` to the `tabs` object.

- [ ] **Step 4: Add the lazy import** in `ControlPage.tsx` (after `BibliothekView`, ~`:61-63`):

```ts
const SchmiedeView = lazy(() =>
  import("./views/SchmiedeView").then((m) => ({ default: m.SchmiedeView })),
);
```

- [ ] **Step 5: Add `activeFromPath` branch** (`ControlPage.tsx`, with the other `if` lines ~`:78`):

```ts
  if (pathname.includes("/control/schmiede")) return "schmiede";
```

- [ ] **Step 6: Add `viewImporters` entry** (`:103`):

```ts
  schmiede: () => import("./views/SchmiedeView"),
```

- [ ] **Step 7: Add `tabPath` entry** (`:125`):

```ts
  schmiede: "/control/schmiede",
```

- [ ] **Step 8: Add the `<Route>`** (with the other routes, after `bibliothek` ~`:227`):

```tsx
            <Route path="schmiede" element={<SchmiedeView density={density.density} />} />
```

- [ ] **Step 9: Type-check + lint.**

Run: `cd ~/.hermes/hermes-agent/web && .bin/tsc --noEmit && npm run lint:control`
Expected: no new errors (diff-relative; do not touch `App.tsx`).

- [ ] **Step 10: Commit.**

```bash
git add web/src/control/components/ControlShell.tsx web/src/control/i18n/de.ts web/src/control/ControlPage.tsx
git commit -m "feat(promptforge): wire /control/schmiede tab (route, nav, i18n)"
```

---

## Task 11: Full gates (live checkout)

> Worktree has no `node_modules` — run all frontend gates in `~/.hermes/hermes-agent/web` via `.bin/`. First merge or rsync this branch's changes into the live checkout, or run gates from the live checkout against the same files (the live checkout is the same repo; the worktree changes must be present there). Per CLAUDE.md: merge finished worktree work back to the live branch — no direct edit of the live checkout.

- [ ] **Step 1: Frontend gates** (live checkout):

Run: `cd ~/.hermes/hermes-agent/web && npm run lint:control && npx tsc --noEmit && npx vitest run && npm run build`
Expected: all green. New tests `composer.test.ts` + `heuristic.test.ts` pass. `lint:control` clean for `src/control`.

- [ ] **Step 2: Python gates:**

Run: `cd ~/.hermes/hermes-agent && scripts/run_tests.sh tests/test_promptforge.py && ruff check hermes_cli/promptforge_view.py`
Expected: green. Then full suite if time: `scripts/run_tests.sh`.

- [ ] **Step 3: reviewer + verifier** on the combined diff (independent lens). Block on any real finding.

---

## Task 12: UI acceptance (ui-verifier)

- [ ] **Step 1: Deploy to the live dashboard** only on truly-green gates:

Run: `cd ~/.hermes/hermes-agent && CONFIRMED=1 scripts/deploy_dashboard.sh && systemctl --user restart hermes-dashboard.service`

- [ ] **Step 2: Verify the API payload** (truth = payload, not screenshot):

Run: `curl -s http://127.0.0.1:9119/api/promptforge/catalog -H "X-Hermes-Session-Token: $(cat ~/.hermes/<session-token-source>)" | python -c "import sys,json; d=json.load(sys.stdin); print(d['version'], len(d['blocks']), len(d['taskTypes']))"`
Expected: `1 12 5`. (A bare loopback curl without token returns 401 — that is correct, the SPA injects the token.)

- [ ] **Step 3: Dispatch `ui-verifier`** — Desktop + Tablet viewport on `/control/schmiede`:
  - Tab appears in the "Mehr" dropdown; route loads.
  - Konfigurator: changing Ziel-CLI / Task-Typ / Modus / Modell / slots updates the Live-Vorschau; Kopieren-Button works; score panel shows missing checks.
  - Kanon: 12 blocks + 5 templates + 3 presets + eval table render; copy buttons work.
  - Browser console clean.

---

## Self-Review (completed)

**Spec coverage:** §1 purpose → Tasks 7–9. §2 architecture (backend data / frontend logic) → Tasks 1–2 (backend), 4–5 (logic). §3 wiring (all 12 points) → Task 10 + hook Task 6 + endpoint Task 2. §4 schema → Task 3 types + Task 1 JSON + Task 2 schema test. §5 composer + adapters → Task 4. §6 heuristic + eval evidence → Task 5 + Kanon Task 8. §7 seed content verbatim → Task 1 JSON. §9 YAGNI (no dispatch/DB/persist) → respected (read-only endpoint, no mutation). §10 test strategy → Tasks 2,4,5,11,12. §11 catalog format = JSON → Task 1.

**Placeholder scan:** the only `[bracketed]` text is inside verbatim prompt-template content (intentional, from the spec) and composer slot fallbacks. No TODO/TBD steps.

**Type consistency:** `PromptForgeCatalog`, `ForgeSelection`, `Block`, `Mode`, `Target`, `TaskType`, `HeuristicCheck`, `EvalEvidence` defined in Task 3 and used identically in Tasks 4,5,6,7,8,9. `compose(selection, catalog)` and `score(promptText, taskTypeId)` signatures consistent across tests and callers. Catalog `heuristic[]` ids match `heuristic.ts` DETECTOR ids (done-when, stop-condition, scope-limited, plan-first, output-format, read-only, behavior-preservation, regression-test, clarification-gate, severity-label).
