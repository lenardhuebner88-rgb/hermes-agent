---
name: codex-exec-direct
description: Use when Codex must BUILD (edit/branch/commit) in a repo and the Claude Code codex plugin path fails or doesn't fit — symptoms: rescue task returns ".git is read-only in this managed session" / "cannot lock ref", you need to resume an existing Codex session with its context, you need Codex's final report as a file, or a Codex session refuses because of a coordination-claim overlap with YOUR OWN session.
---

# Codex exec direct (build path)

The codex **plugin** rescue path drives an app-server "managed session" whose `.git` is mounted read-only — it can diagnose but structurally CANNOT branch/commit. For builds, call the CLI directly. Empirically verified form (shipped real commits 2026-07-11):

```bash
codex exec -s workspace-write -C /abs/path/to/repo \
  -o /path/scratchpad/codex-last-message.txt \
  resume <SESSION_ID> \
  "<continuation prompt: scope, gates, one commit per stage, KEIN push, KEIN merge>"
```

- Fresh run: drop `resume <SESSION_ID>`, pass the prompt as the positional arg. `resume --last` picks the most recent session.
- **Flag placement trap:** `-s/-C/-o` belong to `exec` and go BEFORE the `resume` subcommand. After it → `error: unexpected argument '-s' found`.
- `-o` writes Codex's final message to a file — read that, not the noisy stdout. Add `--json > run.jsonl 2>&1` only if you want the full event stream (digest via log-analyst, never cat); `--json` is also an `exec`-level flag and goes BEFORE `resume`.
- Session IDs appear in every plugin/status report ("Codex session ID: …") and via `codex resume` listing.

## Harness integration

- Run via Bash `run_in_background: true`, `timeout: 600000`. Codex build turns run 5–15+ min; if the 10-min cap kills it, **commits made so far persist** — just `resume` the same session with "continue".
- Started via the plugin instead? The rescue subagent is a one-shot forwarder (it refuses status polling by contract). Poll from the MAIN session: `node /home/piet/.claude/plugins/cache/openai-codex/codex/<ver>/scripts/codex-companion.mjs status|result <task-id>`.

## Coordination self-block (expected, not an error)

Codex sessions honor `_agents/_coordination/` claims and will refuse surfaces claimed by an open note — **including your own**. Resolution (never falsify/close the note):
1. Verify the open note really belongs to YOUR session (read its task/touching — `agent: claude-code` alone can be a sibling session; never stamp AUTORISIERT on someone else's claim), then append to its log: "AUTORISIERT: codex exec-Session auf Branch <x> arbeitet UNTER diesem Claim im Auftrag von <me>."
2. In the prompt, state: the open note <filename> is the dispatching session's own claim; this run is delegated execution under it; write your own codex check-in note citing the delegation; do not touch the original note.

## Standing rules for the prompt

Always include: feature branch name, one commit per stage, gates to run (exit codes verbatim, no `| tail`), receipt path, **no push / no merge** (Claude reviews and merges). Check `git status --short` expectations — the live checkout is shared. On usage-limit failures (memory: Codex caps), fall back to the `builder` subagent and keep Codex for review.
