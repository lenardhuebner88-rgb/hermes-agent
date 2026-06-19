# coder-Profil — Skill-Set Audit (Inventar · Herkunft · Nutzung · Keep/Archive/Slim)

**Profil:** `coder`  ·  **Pfad:** `~/.hermes/profiles/coder/skills/`  ·  **read-only Analyse, keine Mutation.**
**Token-Schätzung:** `bytes/4` (kalibriert: Summe SKILL.md = 313.729 tok ≈ die genannten *314k*; `hermes-agent` SKILL.md = 11.776 tok ≈ die genannten *11,8k*).

## Executive Summary
- **107 Skills** on-disk (1 SKILL.md je Skill-Dir; die 24 Top-Dirs sind *Kategorien*, Skills liegen verschachtelt). Voll geladen = **313.729 tok** (SKILL.md-Bodies).
- **Real genutzt (Transkript-`skill_view`-Calls, 95 Sessions): nur 3 nennenswert** — `test-driven-development` 48× · `hermes-agent` 37× · `systematic-debugging` 32× (bare-name 45/32/29 + kategorie-präfigierte Varianten). Long-Tail 1–6×: kanban-worker, kanban-orchestrator, kanban-execution-worker-readiness, native-mcp, github-pr-workflow, linear, openclaw-operator. **~95 Skills: 0 Nutzung.** (Historisch/umbenannt, kein SKILL.md mehr: `hermes-kanban-worker-scope-control` 6×, `openclaw-mc-hardening` 4×.)
- **`.usage.json`-`use_count` ist NICHT die Nutzung** (Inklusions-/Pin-State): zeigt z. B. kanban-worker=195, openclaw-operator=195 — Transkripte sagen 6 bzw. 1. Counts daher aus Transkripten.

### Klassifikation (Ergebnis)
| Klasse | # | md tok | Wirkung |
|---|--:|--:|---|
| **KEEP** | 47 | 158,743 | genutzt · gepinnt · coding/kanban/hermes/debug/test/research |
| **ARCHIVE** | 59 | 143,210 | coding-fremd & ungenutzt & nicht gepinnt — **~46% des Footprints entfernbar** |
| **SLIM** | 1 | 11.776 | `hermes-agent` (genutzt, aber 11,8k tok → Body/Refs trimmen) |

### ⚠ Korrektur am Task-Grounding (Herkunft → Mechanismus)
Das Grounding sagt „diese ARCHIVE-Skills sind ALLE gebündelt → `hermes skills uninstall`". **Quell-verifiziert falsch:**
- `hermes skills uninstall` läuft **nur auf hub-installierte (official) Skills** und **verweigert builtins** (`tools/skills_hub.py:3413-3415`: „is not a hub-installed skill (may be a builtin)").
- Der Curator (`archive`) **verweigert bundled & hub** (`tools/skill_usage.py:431-446`, ausser `curator.prune_builtins`).
- `godmode` ist **NICHT gebündelt**, sondern **local** (weder in `.bundled_manifest` noch `.usage.json`) → Mechanismus = `hermes curator archive`, nicht `uninstall`.

**Korrekte Mechanismus-Matrix (3 Herkünfte, je reversibel):**
| Herkunft | # | Entfernen / Deaktivieren | uninstall? | curator archive? |
|---|--:|---|:--:|:--:|
| **bundled** (`.bundled_manifest`, builtin) | 74 | `hermes skills config` → `skills.disabled` (non-destruktiv, reversibel) · ODER `hermes skills opt-out --remove` (bulk, löscht pristine bundled) | ❌ verweigert | ❌ verweigert |
| **hub/official** (`.hub/lock.json`) | 7 | `hermes skills uninstall <name>` (löscht Dir; reversibel via `install`) | ✅ | ❌ |
| **local** (agent-erstellt/lokal, weder noch) | 26 | `hermes curator archive <name>` (→ `.archive/`, reversibel via `restore`) · ODER `config` disable | ❌ verweigert | ✅ |

> `plan` ist der einzige `PROTECTED_BUILTIN` (nie kuratierbar). **`ARCHIVE` nach Herkunft:** 46 bundled (→ config/opt-out) · 7 local (→ curator archive) · 6 hub (→ uninstall).

### 🎯 Profil-Targeting (Pflicht — Befehle müssen `coder` treffen)
Globales Flag `-p coder` **vor** dem Subcommand (Resolver `hermes_cli/main.py:338`; setzt `HERMES_HOME=~/.hermes/profiles/coder`). Sonst trifft es das aktive/Default-Profil.
```
hermes -p coder skills config            # bundled deaktivieren (TUI)
hermes -p coder skills uninstall <hub>   # hub/official entfernen
hermes -p coder curator archive <local>  # local archivieren
```

---

## SLIM (1)
| Skill | Cat | md tok | all tok | Usage | Origin | Mechanism | Note |
|---|---|--:|--:|--:|---|---|---|
| `hermes-agent` | autonomous-ai-agents | 11776 | 23826 | 37 | bundled | hermes skills config disable · opt-out --remove (bulk) | used 37× but 11.8k md tok — trim refs/body |

## ARCHIVE (59) — ~143,210 md tok
| Skill | Cat | md tok | all tok | Usage | Origin | Mechanism | Note |
|---|---|--:|--:|--:|---|---|---|
| `humanizer` | creative | 7506 | 7773 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (creative), unused |
| `p5js` | creative | 6874 | 41392 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (creative), unused |
| `comfyui` | creative | 6072 | 71004 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (creative), unused |
| `godmode` | red-teaming | 5011 | 29866 | — | local | hermes curator archive <name>  (rev: restore) | coding-foreign (red-teaming), unused |
| `claude-design` | creative | 4965 | 4965 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | design-adjacent (could aid UI work) but unused — operator may keep |
| `audiocraft-audio-generation` | models | 4047 | 11138 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | ML-ops, unused, out-of-scope for web/dashboard coder (operator judgment) |
| `outlines` | inference | 3951 | 16592 | — | local | hermes curator archive <name>  (rev: restore) | ML-ops, unused, out-of-scope for web/dashboard coder (operator judgment) |
| `xurl` | social-media | 3880 | 3880 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (social-media), unused |
| `obliteratus` | inference | 3866 | 8053 | — | local | hermes curator archive <name>  (rev: restore) | ML-ops, unused, out-of-scope for web/dashboard coder (operator judgment) |
| `touchdesigner-mcp` | creative | 3857 | 63752 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (creative), unused |
| `ascii-video` | creative | 3716 | 80546 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (creative), unused |
| `notion` | productivity | 3657 | 4412 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (productivity), unused |
| `baoyu-comic` | creative | 3655 | 29862 | — | hub | hermes skills uninstall <name>  (rev: install) | coding-foreign (creative), unused |
| `pretext` | creative | 3542 | 20318 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (creative), unused |
| `segment-anything-model` | models | 3343 | 10272 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | ML-ops, unused, out-of-scope for web/dashboard coder (operator judgment) |
| `weights-and-biases` | evaluation | 3099 | 14928 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | ML-ops, unused, out-of-scope for web/dashboard coder (operator judgment) |
| `manim-video` | creative | 3006 | 25102 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (creative), unused |
| `evaluating-llms-harness` | evaluation | 3005 | 14614 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | ML-ops, unused, out-of-scope for web/dashboard coder (operator judgment) |
| `fine-tuning-with-trl` | training | 2993 | 11522 | — | local | hermes curator archive <name>  (rev: restore) | ML-ops, unused, out-of-scope for web/dashboard coder (operator judgment) |
| `airtable` | productivity | 2834 | 2834 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (productivity), unused |
| `google-workspace` | productivity | 2697 | 16426 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (productivity), unused |
| `ascii-art` | creative | 2639 | 2639 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (creative), unused |
| `baoyu-infographic` | creative | 2608 | 17783 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (creative), unused |
| `songwriting-and-ai-music` | creative | 2552 | 2552 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (creative), unused |
| `baoyu-article-illustrator` | creative | 2533 | 27326 | — | hub | hermes skills uninstall <name>  (rev: install) | coding-foreign (creative), unused |
| `popular-web-designs` | creative | 2430 | 218586 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | design-adjacent (could aid UI work) but unused — operator may keep |
| `sketch` | creative | 2326 | 2326 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | design-adjacent (could aid UI work) but unused — operator may keep |
| `powerpoint` | productivity | 2324 | 260037 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (productivity), unused |
| `serving-llms-vllm` | inference | 2268 | 8969 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | ML-ops, unused, out-of-scope for web/dashboard coder (operator judgment) |
| `llama-cpp` | inference | 2220 | 11054 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | ML-ops, unused, out-of-scope for web/dashboard coder (operator judgment) |
| `pokemon-player` | gaming | 2177 | 2177 | — | hub | hermes skills uninstall <name>  (rev: install) | coding-foreign (gaming), unused |
| `pixel-art` | creative | 1895 | 9221 | — | hub | hermes skills uninstall <name>  (rev: install) | coding-foreign (creative), unused |
| `macos-computer-use` | apple | 1827 | 1827 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (apple), unused |
| `excalidraw` | creative | 1825 | 7350 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | design-adjacent (could aid UI work) but unused — operator may keep |
| `himalaya` | email | 1791 | 4217 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (email), unused |
| `design-md` | creative | 1756 | 2397 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | design-adjacent (could aid UI work) but unused — operator may keep |
| `teams-meeting-pipeline` | productivity | 1721 | 1721 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (productivity), unused |
| `maps` | productivity | 1682 | 13351 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (productivity), unused |
| `minecraft-modpack-server` | gaming | 1640 | 1640 | — | hub | hermes skills uninstall <name>  (rev: install) | coding-foreign (gaming), unused |
| `heartmula` | media | 1603 | 1603 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (media), unused |
| `ideation` | creative | 1580 | 2633 | — | hub | hermes skills uninstall <name>  (rev: install) | coding-foreign (creative), unused |
| `spotify` | media | 1580 | 1580 | — | local | hermes curator archive <name>  (rev: restore) | coding-foreign (media), unused |
| `architecture-diagram` | creative | 1458 | 4578 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | design-adjacent (could aid UI work) but unused — operator may keep |
| `ocr-and-documents` | productivity | 1320 | 2898 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (productivity), unused |
| `blogwatcher` | research | 1278 | 1278 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | content out-of-scope (blog-watching) despite research/ cat |
| `axolotl` | training | 1224 | 78172 | — | local | hermes curator archive <name>  (rev: restore) | ML-ops, unused, out-of-scope for web/dashboard coder (operator judgment) |
| `yuanbao` | (root) | 949 | 949 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign ((root)), unused |
| `findmy` | apple | 927 | 927 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (apple), unused |
| `huggingface-hub` | mlops | 918 | 918 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | ML-ops, unused, out-of-scope for web/dashboard coder (operator judgment) |
| `apple-reminders` | apple | 902 | 902 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (apple), unused |
| `youtube-content` | media | 846 | 3933 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (media), unused |
| `obsidian` | note-taking | 819 | 819 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (note-taking), unused |
| `gif-search` | media | 680 | 680 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (media), unused |
| `openhue` | smart-home | 678 | 678 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (smart-home), unused |
| `imessage` | apple | 610 | 610 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (apple), unused |
| `songsee` | media | 584 | 584 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (media), unused |
| `unsloth` | training | 568 | 476297 | — | local | hermes curator archive <name>  (rev: restore) | ML-ops, unused, out-of-scope for web/dashboard coder (operator judgment) |
| `apple-notes` | apple | 542 | 542 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (apple), unused |
| `nano-pdf` | productivity | 354 | 354 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | coding-foreign (productivity), unused |

## KEEP (47) — 158,743 md tok
| Skill | Cat | md tok | all tok | Usage | Origin | Mechanism | Note |
|---|---|--:|--:|--:|---|---|---|
| `research-paper-writing` | research | 25844 | 352192 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | research/ KEEP-by-rule but 26k tok & unused → operator: archive/slim |
| `openclaw-operator` | devops | 21898 | 68711 | 1 | local | hermes curator archive <name>  (rev: restore) | pinned/piet-approved (protected); ⚠heavy 22k md |
| `claude-code` | autonomous-ai-agents | 8680 | 8680 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (autonomous-ai-agents), unused |
| `llm-wiki` | research | 5032 | 5032 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (research), unused |
| `kanban-orchestrator` | devops | 4330 | 4330 | 1 | bundled | hermes skills config disable · opt-out --remove (bulk) | used 1× |
| `kanban-worker` | devops | 4054 | 4054 | 6 | bundled | hermes skills config disable · opt-out --remove (bulk) | used 6× |
| `dspy` | research | 3805 | 15375 | — | hub | hermes skills uninstall <name>  (rev: install) | in-scope category (research), unused |
| `linear` | productivity | 3696 | 7596 | 1 | local | hermes curator archive <name>  (rev: restore) | used 1× |
| `github-repo-management` | github | 3536 | 5875 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (github), unused |
| `github-code-review` | github | 3404 | 4020 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (github), unused |
| `native-mcp` | mcp | 3305 | 3305 | 1 | local | hermes curator archive <name>  (rev: restore) | used 1× |
| `python-debugpy` | software-development | 3293 | 3293 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (software-development), unused |
| `kanban-codex-lane` | autonomous-ai-agents | 3194 | 3746 | — | local | hermes curator archive <name>  (rev: restore) | in-scope category (autonomous-ai-agents), unused |
| `github-pr-workflow` | github | 2943 | 5040 | 1 | bundled | hermes skills config disable · opt-out --remove (bulk) | used 1× |
| `openclaw-model-routing` | devops | 2882 | 9352 | — | local | hermes curator archive <name>  (rev: restore) | pinned/piet-approved (protected) |
| `hermes-s6-container-supervision` | software-development | 2868 | 2868 | — | local | hermes curator archive <name>  (rev: restore) | in-scope category (software-development), unused |
| `systematic-debugging` | software-development | 2811 | 2811 | 32 | bundled | hermes skills config disable · opt-out --remove (bulk) | used 32× |
| `node-inspect-debugger` | software-development | 2732 | 2732 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (software-development), unused |
| `subagent-driven-development` | software-development | 2686 | 4905 | — | local | hermes curator archive <name>  (rev: restore) | in-scope category (software-development), unused |
| `openclaw-config-change-safe` | devops | 2609 | 2609 | — | local | hermes curator archive <name>  (rev: restore) | pinned/piet-approved (protected) |
| `github-auth` | github | 2525 | 3168 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (github), unused |
| `arxiv` | research | 2521 | 3589 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (research), unused |
| `test-driven-development` | software-development | 2391 | 2391 | 48 | bundled | hermes skills config disable · opt-out --remove (bulk) | used 48× |
| `github-issues` | github | 2329 | 2596 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (github), unused |
| `openclaw-stability-hardening` | devops | 2278 | 3752 | — | local | hermes curator archive <name>  (rev: restore) | pinned/piet-approved (protected) |
| `plan` | software-development | 2244 | 2244 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (software-development), unused |
| `spike` | software-development | 2182 | 2182 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (software-development), unused |
| `simplify-code` | software-development | 2126 | 2126 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (software-development), unused |
| `requesting-code-review` | software-development | 2116 | 2116 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (software-development), unused |
| `debugging-hermes-tui-commands` | software-development | 1978 | 1978 | — | local | hermes curator archive <name>  (rev: restore) | in-scope category (software-development), unused |
| `hermes-agent-skill-authoring` | software-development | 1902 | 1902 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (software-development), unused |
| `opencode` | autonomous-ai-agents | 1815 | 1815 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (autonomous-ai-agents), unused |
| `writing-plans` | software-development | 1807 | 1807 | — | local | hermes curator archive <name>  (rev: restore) | in-scope category (software-development), unused |
| `codex` | autonomous-ai-agents | 1715 | 1715 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (autonomous-ai-agents), unused |
| `webhook-subscriptions` | devops | 1709 | 1709 | — | local | hermes curator archive <name>  (rev: restore) | in-scope category (devops), unused |
| `free-model-audit` | research | 1651 | 1651 | — | local | hermes curator archive <name>  (rev: restore) | in-scope category (research), unused |
| `minimax-openclaw-token-plan` | devops | 1628 | 1628 | — | local | hermes curator archive <name>  (rev: restore) | pinned/piet-approved (protected) |
| `dogfood` | (root) | 1568 | 2870 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category ((root)), unused |
| `jupyter-live-kernel` | data-science | 1321 | 1321 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (data-science), unused |
| `openclaw-discord-ops` | devops | 1267 | 2239 | — | local | hermes curator archive <name>  (rev: restore) | pinned/piet-approved (protected) |
| `kanban-execution-worker-readiness` | devops | 1132 | 1132 | 1 | local | hermes curator archive <name>  (rev: restore) | used 1× |
| `workflow-library` | software-development | 918 | 6943 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (software-development), unused |
| `codebase-inspection` | github | 907 | 907 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (github), unused |
| `brainstorming` | creative | 885 | 885 | — | local | hermes curator archive <name>  (rev: restore) | pinned/piet-approved (protected) |
| `grill-me` | software-development | 827 | 827 | — | local | hermes curator archive <name>  (rev: restore) | pinned/piet-approved (protected) |
| `polymarket` | research | 746 | 4435 | — | bundled | hermes skills config disable · opt-out --remove (bulk) | in-scope category (research), unused |
| `openclaw-incident-rca` | devops | 653 | 653 | — | local | hermes curator archive <name>  (rev: restore) | pinned/piet-approved (protected) |

---

## Anhang A — Read-only Evidenz (exakte Befehle + Roh-Ausgaben)

> Alle Kommandos sind **read-only** (find/grep/sed/wc/python-json-Parse), keine Profil-Mutation.
> Profil-Root: `SK=~/.hermes/profiles/coder/skills`. Reproduziert 2026-06-19 im Task-Worktree.

### A1 · Inventar — 107 Skills / 24 Kategorien
```
$ find "$SK" -name SKILL.md -type f | wc -l
107
$ ls -1 "$SK" | wc -l        # Top-Level-Kategorie-Dirs
24
$ ls -d "$SK"                # Pfad existiert
/home/piet/.hermes/profiles/coder/skills   (OK)
```

### A2 · Token-Schätzung — 313.7k gesamt · hermes-agent 11.776 tok (`bytes/4`)
```
$ find "$SK" -name SKILL.md -printf '%s\n' | awk '{s+=$1} END{print s, s/4}'
1254900 313725                      # gesamt bytes / est_tokens (Σ-then-/4)
$ find "$SK" -name SKILL.md -printf '%s\n' | awk '{s+=int($1/4)} END{print s}'
313688                              # per-file floor(bytes/4) summiert (Report-Methode ≈313.729)
$ find "$SK" -path '*hermes-agent/SKILL.md' -printf '%s\n'
47104                               # → 47104/4 = 11776 tok  ✓ (= genannte 11,8k)
$ find "$SK" -path '*/test-driven-development/SKILL.md' -printf '%s\n'   #  9564 →  2391 tok
$ find "$SK" -path '*/systematic-debugging/SKILL.md'   -printf '%s\n'    # 11244 →  2811 tok
```

### A3 · Reale Nutzung — `skill_view`-**Toolcalls** über 95 Transkripte (System-Prompt-Mentions ausgeschlossen)
Transkripte sind OpenAI-Schema: echte Aufrufe stehen in `messages[].tool_calls[].function`
(name=`skill_view`, `arguments`=JSON). System-Prompt-Erwähnungen (`skill_view(name='…') before answering`)
liegen NICHT in `tool_calls` und werden so automatisch ausgeschlossen.
```
$ python3  # iter messages[].tool_calls[], filter function.name=='skill_view', zähle arguments.name
files_scanned=95  total_skill_view_toolcalls=139  distinct_names=17
  45  test-driven-development        # +  3  software-development:test-driven-development  = 48 ✓
  32  hermes-agent                   # +  4  autonomous-ai-agents:hermes-agent  + 1 …/hermes-agent = 37 ✓
  29  systematic-debugging           # +  3  software-development:systematic-debugging  = 32 ✓
   6  kanban-worker
   5  hermes-kanban-worker-scope-control   #   + 1 devops/… = 6  (historisch, kein SKILL.md mehr)
   4  openclaw-mc-hardening                #   (historisch, kein SKILL.md mehr)
   1  openclaw-operator · kanban-execution-worker-readiness · kanban-orchestrator
   1  github-pr-workflow · linear · native-mcp
```
→ Nur 10 *on-disk* Skills mit ≥1 echtem Call; die übrigen ~97 = 0 Nutzung. (Gegenprobe: das naive
`grep -c '"skill_view"'` = 359 mischt System-Prompt-Text mit ein → daher der Parser, nicht grep.)

### A4 · Herkunft — bundled 74 / hub 7 / local 26 (Match auf **declared `name:`**, nicht Dir-Name)
```
$ wc -l < "$SK/.bundled_manifest"                              # Format: name:hash je Zeile
74
$ python3 -c '…json.load(.hub/lock.json)["installed"]…'       # hub/official
hub_installed_count= 7
  baoyu-article-illustrator · baoyu-comic · creative-ideation · pixel-art
  minecraft-modpack-server · pokemon-player · dspy
```
**Aliasing-Hinweis (Präzision):** ein naiver *Dir-Name*-Scan ergibt 70 bundled / 30 local, weil 4
gebündelte Skills unter abweichendem Verzeichnis liegen. Der `.bundled_manifest` keyed auf das
**Frontmatter-`name:`** — Beleg:
```
$ for d in audiocraft lm-evaluation-harness segment-anything vllm; do grep -m1 '^name:' "$SK"/*/$d/SKILL.md; done
audiocraft            → name: audiocraft-audio-generation   (∈ .bundled_manifest = bundled)
lm-evaluation-harness → name: evaluating-llms-harness       (∈ .bundled_manifest = bundled)
segment-anything      → name: segment-anything-model        (∈ .bundled_manifest = bundled)
vllm                  → name: serving-llms-vllm             (∈ .bundled_manifest = bundled)
```
→ 30 − 4 = **26 local** (z. B. `godmode`: `name: godmode`, NICHT im Manifest → local; bestätigt die
Grounding-Korrektur „godmode = local, nicht bundled"). 74 + 7 + 26 = **107** ✓

### A5 · Pin-State — `.usage.json` = 21 agent-erstellte Skills · 9 gepinnt · 0 archiviert; `use_count` ≠ Nutzung
```
$ python3 -c '…json.load(.usage.json)…'
usage_entries=21  pinned=9  archived=0
PINNED: brainstorming · grill-me · minimax-openclaw-token-plan · openclaw-config-change-safe
        · openclaw-discord-ops · openclaw-incident-rca · openclaw-model-routing
        · openclaw-operator · openclaw-stability-hardening
archived_at set: none
$ # Pin-Marker (Beispiel brainstorming.curator_inclusion_reason):
  "piet-approved existing local skill inclusion; pinned to block auto archive/delete"
$ # use_count ≠ reale Nutzung — Beweis:
  kanban-worker:     use_count=196   (Transkripte: 6 echte Calls)
  openclaw-operator: use_count=195   (Transkripte: 1 echter Call)
```
→ Die 9 gepinnten Skills sind **protected** (KEIN ARCHIVE), genau wie im KEEP-Block markiert.

### A6 · Mechanismus — quell-verifiziert (exakte Zeilen)
```
$ sed -n '3409,3415p' tools/skills_hub.py        # uninstall verweigert Nicht-Hub (builtins)
def uninstall_skill(skill_name: str) -> Tuple[bool, str]:
    """Remove a hub-installed skill. Refuses to remove builtins."""
    ...
    if not entry:
        return False, f"'{skill_name}' is not a hub-installed skill (may be a builtin)"   # :3415

$ sed -n '672,690p' tools/skill_usage.py          # curator archive verweigert hub & bundled
def archive_skill(skill_name: str) -> Tuple[bool, str]:                                    # :672
    if not is_curation_eligible(skill_name):
        if is_protected_builtin(skill_name): return False, "...protected built-in...never archived"
        if is_hub_installed(skill_name):     return False, "...is hub-installed; never archive"   # :687
        return False, "...is a bundled built-in; enable curator.prune_builtins to allow pruning"  # :689

$ sed -n '437,446p' tools/skill_usage.py          # is_curation_eligible: protected & bundled → False
$ sed -n '66,68p'  tools/skill_usage.py           # PROTECTED_BUILTIN_SKILLS = { "plan" }  (einziger)
PROTECTED_BUILTIN_SKILLS: Set[str] = { "plan", }

$ grep -n '_apply_profile_override\|profiles" / name' hermes_cli/main.py
338: def _apply_profile_override():   # -p/--profile → setzt HERMES_HOME=~/.hermes/profiles/<name>
382:     candidate = home / ".hermes" / "profiles" / name
```
→ Bestätigt die Mechanismus-Matrix: **bundled → `skills config`/`opt-out`** (uninstall verweigert,
curator verweigert) · **hub → `skills uninstall`** · **local → `curator archive`** · `plan` = einziger
PROTECTED_BUILTIN. Profil-Targeting via `-p coder` ist Pflicht (Resolver `main.py:338`).

---
*read-only Audit; keine Profil-Mutation ausgeführt. Mechanismen quell-verifiziert (skills_hub.py:3415 / skill_usage.py:672-689,437-446,66 / hermes_cli/main.py:338,382). Nutzungs-Counts aus `profiles/coder/sessions/session_*.json` `tool_calls[].function`-`skill_view`-Aufrufen (System-Prompt-Erwähnungen liegen nicht in tool_calls → automatisch ausgeschlossen). Evidenz-Anhang A reproduziert alle Kernzahlen 1:1.*
