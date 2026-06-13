"""Static Prompt-Schmiede catalog (curated agent-control prompt building blocks).

Single source of truth for GET /api/promptforge/catalog. Kept as a Python
constant (not a shipped JSON file) to match the existing in-code catalog
convention in this package — e.g. the Lanes model catalog `_LANE_MODEL_CATALOG`
in plugins/kanban/dashboard/plugin_api.py — and because repo `.gitignore` ignores
every `data/` directory. Edit this dict to maintain the catalog; the endpoint
serves it verbatim. Seed content + sources: docs design spec 2026-06-13.
"""
from __future__ import annotations

from typing import Any

PROMPTFORGE_CATALOG: dict[str, Any] = {
    "version": 1,
    "blocks": [
        {
            "id": "role",
            "letter": "A",
            "label": "Role",
            "description": "Setzt die Fachrolle — fokussiert Stil und Prioritäten.",
            "body": "You are a [security audit engineer / senior backend dev …].",
            "source": "Anthropic Prompting Best Practices",
            "category": "core"
        },
        {
            "id": "goal",
            "letter": "B",
            "label": "Goal",
            "description": "Das spezifische Ziel: Datei + Symptom + Outcome, nicht \"fix the bug\".",
            "body": "State the goal specifically: the file, the symptom, and the desired outcome — not just \"fix the bug\".",
            "source": "Anthropic (Claude Code Best Practices)",
            "category": "core"
        },
        {
            "id": "grounding",
            "letter": "C",
            "label": "Grounding",
            "description": "Erzwingt Lesen vor Antworten — kritisch bei langen Runs.",
            "body": "Never speculate about code you have not opened. Read relevant files BEFORE answering.",
            "source": "Anthropic (<investigate_before_answering>)",
            "category": "long-run"
        },
        {
            "id": "tools",
            "letter": "D",
            "label": "Tools",
            "description": "Macht Tool-Präferenzen und Parallelität explizit.",
            "body": "State tool preferences explicitly. Issue independent tool calls in parallel. Prefer `rg` over `grep`; use `apply_patch` for single-file edits.",
            "source": "Anthropic <use_parallel_tool_calls> · OpenAI Codex Guide",
            "category": "long-run"
        },
        {
            "id": "persistence",
            "letter": "E",
            "label": "Persistence / Keep-Going",
            "description": "Hält den Agenten am Arbeiten, bis wirklich fertig.",
            "body": "Keep going until the query is completely resolved before yielding. Always be as persistent and autonomous as possible and complete tasks fully.",
            "source": "OpenAI PE Guide · Anthropic Prompting BP",
            "category": "core"
        },
        {
            "id": "done-when",
            "letter": "F",
            "label": "Done-When",
            "description": "Maschinenlesbares Fertig-Kriterium = externe Evidenz.",
            "body": "Define a machine-checkable finish criterion = external evidence (tests green, build exit 0, empty queue) — not \"looks done\".",
            "source": "Anthropic Harnesses · OpenAI Codex",
            "category": "core"
        },
        {
            "id": "scope-constraints",
            "letter": "G",
            "label": "Scope-Constraints",
            "description": "Verhindert Scope-Creep und ungewollte Umbauten.",
            "body": "Only make changes directly requested. Don't refactor, add docstrings/comments to code you didn't change, or add defensive code for impossible cases.",
            "source": "Anthropic Prompting BP",
            "category": "core"
        },
        {
            "id": "reversibility-gate",
            "letter": "H",
            "label": "Reversibility-Gate",
            "description": "Bremst vor schwer umkehrbaren Aktionen.",
            "body": "For actions that are hard to reverse, affect shared systems, or are destructive (rm -rf, force-push, DB drop, PR comments), ask before proceeding.",
            "source": "Anthropic Prompting BP",
            "category": "core"
        },
        {
            "id": "verification",
            "letter": "I",
            "label": "Verification",
            "description": "Erzwingt Prüfung vor dem Fertig-Melden.",
            "body": "Before finishing, verify against [test/build/screenshot]. Only mark a feature passing after careful testing.",
            "source": "Anthropic Harnesses · OpenAI",
            "category": "core"
        },
        {
            "id": "escalation",
            "letter": "J",
            "label": "Escalation",
            "description": "Strukturierte Blockade statt stillem Workaround.",
            "body": "If blocked, report it as: Blocked (Reason + Question). Inform me rather than working around incorrect tests.",
            "source": "OpenAI Codex · Anthropic",
            "category": "optional"
        },
        {
            "id": "state-handoff",
            "letter": "K",
            "label": "State-Handoff",
            "description": "Persistenter Zustand über Sessions hinweg.",
            "body": "Persist progress in `progress.txt` (freeform) + `tests.json` (structured); use Git as a state log. Read these at session start.",
            "source": "Anthropic Harnesses",
            "category": "long-run"
        },
        {
            "id": "output-format",
            "letter": "L",
            "label": "Output-Format",
            "description": "Legt die Ausgabeform für den Verbraucher fest.",
            "body": "Specify the output shape for the consumer: XML tags / semantic Markdown / JSON.",
            "source": "Anthropic · OpenAI",
            "category": "optional"
        }
    ],
    "taskTypes": [
        {
            "id": "audit",
            "label": "Audit / Security-Review (read-only)",
            "blockIds": [
                "role",
                "goal",
                "scope-constraints",
                "grounding",
                "output-format",
                "verification"
            ],
            "typeBody": "Focus: OWASP Top 10 (injection, broken auth, secrets in source, SSRF/XSS/CSRF, insecure deserialization).\nOutput: Numbered list. Each item: [file:line] [Critical|High|Medium|Low] [Issue] [Fix suggestion].\nConstraints: Only report actual, exploitable issues — not theoretical risks or style. Do not modify any files.",
            "defaultDoneWhen": "A prioritized report covering all in-scope files is delivered.",
            "checklist": [
                "Read-only pledge",
                "Severity labels",
                "Scope bounded to recent changes",
                "Exploitable-only filter",
                "Numbered, actionable output"
            ],
            "rawTemplate": "Role: You are a security audit engineer.\nScope: Review all files recently modified in src/ — do NOT touch anything outside.\nFocus: OWASP Top 10 (injection, broken auth, secrets in source, SSRF/XSS/CSRF, insecure deserialization).\nOutput: Numbered list. Each item: [file:line] [Critical|High|Medium|Low] [Issue] [Fix suggestion]\nConstraints:\n  - Only report actual, exploitable issues — not theoretical risks or style.\n  - Do not modify any files.\nDone-when: A prioritized report covering all in-scope files is delivered.\nStop: If exploitability is uncertain, mark [Uncertain] and explain why.",
            "source": "Crash Override (Prompting LLMs for Security Reviews) · Anthropic claude-code code-review plugin"
        },
        {
            "id": "feature",
            "label": "Neues Feature",
            "blockIds": [
                "role",
                "goal",
                "grounding",
                "scope-constraints",
                "persistence",
                "verification"
            ],
            "typeBody": "Before code: ask up to 3 clarifying questions if requirements are ambiguous.\nDesign across layers: data model (tables/keys/migrations) · API (endpoints/shapes/auth/errors) · UI (screens/loading-empty-error states).\nPropose a written plan. Wait for approval before editing files.\nConstraints: keep each diff ≤~300 lines & reviewable; add unit tests for logic + one integration happy-path; update docs.",
            "defaultDoneWhen": "all layers implemented, tests pass, docs updated.",
            "checklist": [
                "Clarifying questions first",
                "Cross-layer design",
                "Written plan + approval gate",
                "Diffs ≤300 lines",
                "Unit + happy-path tests",
                "Docs updated"
            ],
            "rawTemplate": "Implement: [feature].\nBefore code: ask up to 3 clarifying questions if requirements are ambiguous.\nDesign across layers: data model (tables/keys/migrations) · API (endpoints/shapes/auth/errors) · UI (screens/loading-empty-error states).\nPropose a written plan. Wait for approval before editing files.\nConstraints: keep each diff ≤~300 lines & reviewable; add unit tests for logic + one integration happy-path; update docs.\nDone-when: all layers implemented, tests pass, docs updated.\nStop: if a design choice blocks >1 layer, surface it before proceeding.",
            "source": "QuantumByte · Four Modalities (DEV)"
        },
        {
            "id": "bugfix",
            "label": "Bugfix / Debugging",
            "blockIds": [
                "role",
                "goal",
                "grounding",
                "persistence",
                "verification"
            ],
            "typeBody": "Step 1 — Reason first (no code): list 5–7 possible root causes + a diagnostic for each.\nStep 2 — Diagnose: add minimal logging/assertions; show output before proposing a fix.\nStep 3 — Fix: smallest safe change; explain why it fixes the root cause, not the symptom.\nStep 4 — Verify: exact test command + expected output; add a regression test; prefer fixing over disabling tests.",
            "defaultDoneWhen": "previously failing test passes; regression test added; no new failures.",
            "checklist": [
                "Reason-first before code",
                "Diagnose with evidence",
                "Smallest safe fix",
                "Regression test added",
                "No disabled tests"
            ],
            "rawTemplate": "Bug: [symptom / error / stack trace]   Reproduce: [steps]   Expected/Actual: […]\nStep 1 — Reason first (no code): list 5–7 possible root causes + a diagnostic for each.\nStep 2 — Diagnose: add minimal logging/assertions; show output before proposing a fix.\nStep 3 — Fix: smallest safe change; explain why it fixes the root cause, not the symptom.\nStep 4 — Verify: exact test command + expected output; add a regression test; prefer fixing over disabling tests.\nDone-when: previously failing test passes; regression test added; no new failures.\nStop: if diagnosis is inconclusive after Step 2, report and ask before fixing.",
            "source": "Agentic Coding Handbook (Debug Workflow) · QuantumByte"
        },
        {
            "id": "refactor",
            "label": "Refactor (verhaltenserhaltend)",
            "blockIds": [
                "role",
                "goal",
                "scope-constraints",
                "verification"
            ],
            "typeBody": "Goal: improve [readability/structure/perf — pick one] without changing behavior.\nStep 1 — Characterization tests: lock in current observable behavior; must pass before & after.\nStep 2 — Refactor in small steps; explain each; call out any suspected behavior change.\nConstraints: no bug fixes / no features / no API changes this pass; keep commits individually reviewable.",
            "defaultDoneWhen": "all characterization tests pass; diff reviewable; no suspected behavior changes.",
            "checklist": [
                "Characterization tests first",
                "Small reviewable steps",
                "No behavior change",
                "No features/bugfixes this pass"
            ],
            "rawTemplate": "Refactor: [target file/function]. Goal: improve [readability/structure/perf — pick one] without changing behavior.\nStep 1 — Characterization tests: lock in current observable behavior; must pass before & after.\nStep 2 — Refactor in small steps; explain each; call out any suspected behavior change.\nConstraints: no bug fixes / no features / no API changes this pass; keep commits individually reviewable.\nDone-when: all characterization tests pass; diff reviewable; no suspected behavior changes.\nStop: if a step would change observable behavior, stop and ask.",
            "source": "QuantumByte · Azure DEV (Prompt-Driven Refactor)"
        },
        {
            "id": "research",
            "label": "Research / Investigation (kein Code)",
            "blockIds": [
                "role",
                "goal",
                "grounding",
                "scope-constraints",
                "output-format"
            ],
            "typeBody": "Constraint: your ONLY job is to document & explain as it exists today — no improvements, no critique, no file changes (except the research doc).\nProcedure: decompose into sub-questions → per sub-question find evidence (file:line + commit SHA, or URL) → synthesize → every claim cited.\nOutput: YAML frontmatter (date, question, git_commit) + Findings (section per sub-question) + Summary (3–5 bullets).",
            "defaultDoneWhen": "all sub-questions answered with citations; summary delivered.",
            "checklist": [
                "Document-only constraint",
                "Decompose into sub-questions",
                "Every claim cited",
                "YAML frontmatter output",
                "No speculation on gaps"
            ],
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
        {
            "id": "claude-goal",
            "label": "Claude Code · /goal",
            "mechanicNote": "Stop-Hook + Haiku-Evaluator; der Evaluator sieht NUR den Transcript-Output, kein Filesystem. Bedingung: messbarer End-State + stated check (npm test exits 0, git status clean) + Constraint + optional 'or stop after N turns'. Max 4000 Zeichen.",
            "wrapMode": "completion-condition",
            "source": "Anthropic /goal Docs · Linas Substack (Community, Pitfalls)"
        },
        {
            "id": "claude-loop",
            "label": "Claude Code · /loop",
            "mechanicNote": "Intervall (/loop 5m …) oder self-paced (/loop …) oder bare (Maintenance-Prompt). Self-paced: Claude beendet selbst, wenn 'provably complete'.",
            "wrapMode": "interval-loop",
            "source": "Anthropic Scheduled-Tasks Docs · Piebald-AI (Skill-Source, Community)"
        },
        {
            "id": "codex-goal",
            "label": "Codex · /goal",
            "mechanicNote": "experimentell (v0.128.0+), --approval-mode full-auto; kein externer Evaluator (self-assessed); pause/resume/clear; AGENTS.md als Betriebshandbuch.",
            "wrapMode": "full-auto",
            "source": "OpenAI Cookbook (Using Goals in Codex) · GitHub openai/codex"
        },
        {
            "id": "generic",
            "label": "Generischer System-Prompt",
            "mechanicNote": "Volle Block-Montage als System-Prompt, XML-getaggt.",
            "wrapMode": "system-prompt",
            "source": "Anthropic · OpenAI"
        }
    ],
    "heuristic": [
        {
            "id": "done-when",
            "label": "Hat Done-When",
            "appliesTo": [
                "*"
            ],
            "weight": 1,
            "rationale": "größter Einzel-Hebel"
        },
        {
            "id": "stop-condition",
            "label": "Hat Stop-Bedingung",
            "appliesTo": [
                "*"
            ],
            "weight": 1,
            "rationale": "verhindert Drift/stilles Falsch-Handeln"
        },
        {
            "id": "scope-limited",
            "label": "Scope begrenzt (Datei/Verzeichnis)",
            "appliesTo": [
                "*"
            ],
            "weight": 1,
            "rationale": "verhindert Scope-Creep"
        },
        {
            "id": "plan-first",
            "label": "Plan-First vor Code",
            "appliesTo": [
                "feature",
                "bugfix"
            ],
            "weight": 1,
            "rationale": "Reasoning-first hebt Patch-Qualität"
        },
        {
            "id": "output-format",
            "label": "Output-Format spezifiziert",
            "appliesTo": [
                "*"
            ],
            "weight": 1,
            "rationale": "maschinen-verarbeitbar"
        },
        {
            "id": "read-only",
            "label": "Read-Only-Pledge",
            "appliesTo": [
                "audit"
            ],
            "weight": 1,
            "rationale": "sonst wird Audit zum ungewollten Fix"
        },
        {
            "id": "behavior-preservation",
            "label": "Behavior-Preservation-Pledge",
            "appliesTo": [
                "refactor"
            ],
            "weight": 1,
            "rationale": "sonst stilles Verhaltens-Drift"
        },
        {
            "id": "regression-test",
            "label": "Regression-Test verlangt",
            "appliesTo": [
                "bugfix"
            ],
            "weight": 1,
            "rationale": "sonst kehrt der Bug zurück"
        },
        {
            "id": "clarification-gate",
            "label": "Clarification-Gate",
            "appliesTo": [
                "feature",
                "research"
            ],
            "weight": 1,
            "rationale": "gegen stille Fehlinterpretation"
        },
        {
            "id": "severity-label",
            "label": "Severity-Label",
            "appliesTo": [
                "audit"
            ],
            "weight": 1,
            "rationale": "sonst unpriorisierte Findings"
        }
    ],
    "evalEvidence": [
        {
            "name": "SWE-bench Verified",
            "measures": "echte GitHub-Issues, Patch muss Tests grün machen (500 menschl. annotierte)",
            "keyNumber": "GPT-4o 16% → 33,2% nur durch besseres Scaffold (Agentless)",
            "lesson": "Scaffold ≫ Modell",
            "source": "OpenAI (Introducing SWE-bench Verified) · Epoch AI · CodeAnt"
        },
        {
            "name": "SWE-bench Pro",
            "measures": "frische, kontaminationsarme Codebases",
            "keyNumber": "Opus 4.5 80,9% → 45,9% (−35 pp)",
            "lesson": "Verified-Scores tragen Training-Overlap",
            "source": "arxiv 2509.16941 (SWE-bench Pro)"
        },
        {
            "name": "Aider Polyglot",
            "measures": "225 Exercism-Tasks, 6 Sprachen, 2. Versuch",
            "keyNumber": "Refact.ai+Claude 3.7: 92,9% vs. bare 60,4% (+32,5 pp)",
            "lesson": "Scaffold/Prompt hebt identisches Modell massiv",
            "source": "Aider Leaderboard · Refact.ai Blog"
        },
        {
            "name": "terminal-bench 2.0",
            "measures": "End-to-End-Terminal-Workflows (Docker)",
            "keyNumber": "Codex (gpt-5-codex) 42,8%; Frontier <65%",
            "lesson": "misst Scaffold-Robustheit & Fehler-Recovery",
            "source": "Snorkel AI (terminal-bench 2.0) · Artificial Analysis"
        }
    ]
}
