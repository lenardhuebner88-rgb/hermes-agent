# NIGHT RUN 2 — mutation hardening of the operational tooling

**Read this file again whenever you have lost the thread — after a compaction, after
a long detour, or whenever you are unsure what comes next. It is the only source of
truth for this run. Do not improvise a different procedure.**

## Mission

The fork owns 87 Python modules under `scripts/`, `tools/`, `gateway/`, `agent/` and
`plugins/` that do not exist upstream. This is the operational layer: reapers,
watchdogs, gate stampers, dispatch guards, anchor checkers — the code whose failures
are *silent* by nature, because a broken watchdog reports "all clear".

You measure how much of it is actually pinned by tests, and you close the gap, one
module per iteration: generate single-edit mutants of a module, run its test file
against each mutant, and every mutant that **survives** (tests stay green) is a
behaviour nobody tests. You write the test that kills it.

A test that raises coverage but kills no mutant is worthless here. The probe proves
the difference mechanically, so there is no room for a test that only looks good.

## Model and delegation — a hard boundary

You are running on **qwen3.8-max-preview** inside the Claude Code harness, on Piet's
Alibaba Token-Plan seat. Everything you spawn must stay on that seat.

**Forbidden without exception — these leave the seat and cost real money on another
account:**
- subagents of another family: `council` (routes to OpenRouter, has burned ~20 EUR
  before), `codex:codex-rescue`, any `codex:*` agent, `minimax-auditor`
- the skills `spawn-foreign`, `orchestrate`, `subagent-routing`, any `codex:*` skill
- shelling out to another CLI: `foreign.sh`, `codex`, `kimi`, `grok`, `claude`,
  `hermes kanban spawn` or anything else that starts a worker
- the `Workflow` tool — it fans out dozens of agents and is not budgeted for tonight

**Allowed:** the ordinary tools (Read, Edit, Write, Bash, Grep, Glob), and — only if
a task genuinely needs it — plain subagents (`general-purpose`, `Explore`, `auditor`,
`builder`, `reviewer`). Those inherit `CLAUDE_CODE_SUBAGENT_MODEL`, which this session
pins to qwen3.8-max-preview, so they stay on the same seat. You do not need them for
this work; the loop below is single-threaded by design.

If you catch yourself reaching for a foreign lane because something is hard: that is
the moment to write a REJECTED line and take the next candidate instead.

## Where you are

- Worktree: `/home/piet/.hermes/hermes-agent/.claude/worktrees/qwen-tools-nightly`
- Branch: `qwen/mutation-hardening-tools-2026-07-29` (forked from `main`)
- Python: `.venv/bin/python` — **always this one**. Never bare `python3`, never bare
  `pytest`, never `npx`. `.venv` is a symlink to the live checkout's venv; leave it alone.
- Candidates: `NIGHT-RUN/CANDIDATES.md` (44 module/test pairs, work top-down)
- Your ledger: `NIGHT-RUN/LEDGER.md` — append one entry per finished module
- Suspected real defects: `NIGHT-RUN/FINDINGS.md`

**A second agent is working in a sibling worktree** (`qwen-mutation-nightly`, branch
`qwen/mutation-hardening-2026-07-28`) on `hermes_cli/` and `loops/`. Your candidate
list deliberately does not overlap. Never read, write or `cd` into that worktree, and
never touch a file outside your own.

## Hard rules — violating any of these ends the run badly

1. **Never leave this worktree.** No `cd` to `/home/piet/.hermes/hermes-agent`
   (the live checkout, edited by other sessions in parallel), no other repo, not the
   sibling worktree.
2. **Never push.** Not to `origin` (that is the upstream project), not to `piet-fork`,
   not anywhere. Commits stay local on this branch.
3. **Never merge, rebase, reset, stash, or switch branch.** Stay on
   `qwen/mutation-hardening-tools-2026-07-29` for the whole run.
4. **Never deploy and never restart a service.** No `systemctl`, no
   `scripts/deploy_dashboard.sh`, no gateway restart. A restart would kill your own
   session and the sibling run with it.
5. **Never touch runtime state**: nothing under `~/.hermes/` except this worktree —
   in particular not `config.yaml`, `auth.json`, `kanban.db`, `.env`.
6. **Never modify** `hermes_cli/kanban_db.py`, `gateway/run.py`, `cli.py`,
   `hermes_cli/web_server.py`, `tui_gateway/server.py`, or anything under `web/`.
7. **Never delete, skip, xfail or weaken an existing test.** The discovered test count
   of a file must never go down.
8. **No `git add -A`, no `git commit -a`.** Stage the exact files you changed, by name.
9. **Never run the full test suite.** `scripts/run_tests.sh` without arguments takes
   over 15 minutes and would eat the night.

## The loop — one module per iteration

### 1. Pick the next candidate

Take the first unchecked `- [ ]` entry in `NIGHT-RUN/CANDIDATES.md` that is not
already in `NIGHT-RUN/LEDGER.md`. Work Band A first, then B, then C.

### 2. Measure the baseline

```
.venv/bin/python scripts/mutation_probe.py <MODULE> <TESTFILE> --max 30
```

The probe generates deterministic mutants, applies them one at a time, runs the test
file against each, and restores the module afterwards. It prints per-mutant
`KILLED` / `SURVIVED`, a score, and the survivors with their index.

Reject the candidate and move on when:
- the probe reports `baseline is FAIL` — a red baseline makes the numbers meaningless, or
- the printed baseline is **slower than 45 s** — 30 mutants would cost over 20 minutes, or
- the probe reports **0 survivors** — the module is already fully pinned. That is a
  good result; record it as `no gap` with the score and move on.

**If the probe exits with "no mutants" it prints the reason. That reason is a
finding, not a shrug** — write it to `FINDINGS.md`. One such cause is already known
and fixed in your copy of the probe: `hermes_cli/autoresearch_reconcile.py` carries a
UTF-8 BOM, which makes `ast.parse` on a `read_text(encoding="utf-8")` string raise
while the normal import path works fine. Any repo tool that parses sources this way
is silently blind to that file. If you meet a *new* cause, it belongs in FINDINGS
with the same level of detail.

Record every rejection as a one-line ledger entry with the reason. A rejected
candidate is normal progress, not a blocker.

### 3. Kill up to 5 survivors

For each survivor you take (up to 5 per module — prefer the ones guarding real logic
over cosmetic ones):

a. **Understand what the mutation changes.** Read the module around the printed line.
   What observable behaviour differs between original and mutant?

b. **Write a test in the module's existing test file** that asserts exactly that
   observable behaviour, in the style of the tests already in that file (same
   fixtures, same naming, same imports). Test *behaviour through the public surface*.
   Never `inspect.getsource`, never assert on source text — a test that reads the
   source would "pass" against any mutant and is cheating.

c. **Red proof — the mutant must die:**
   ```
   .venv/bin/python scripts/mutation_probe.py <MODULE> <TESTFILE> --max 30 --check <K>
   ```
   Exit **0 = KILLED** → your test does its job. Exit **1 = SURVIVED** → your test
   does not actually pin the behaviour; fix the test, do not move on.

d. **Green proof — the untouched module must still pass:**
   ```
   .venv/bin/python -m pytest -q <TESTFILE>
   ```

### 4. Re-measure

```
.venv/bin/python scripts/mutation_probe.py <MODULE> <TESTFILE> --max 30
```
The score must be strictly higher than the baseline. If it is not, your tests did not
land — back to step 3.

### 5. Gate

```
git add <TESTFILE>                 # the selector is blind to untracked files
scripts/affected-tests.sh HEAD     # PRINTS the selection — look before you run
scripts/run-affected.sh HEAD       # only if the line above listed <= 25 files
.venv/bin/python -m ruff check <TESTFILE> <MODULE>
```

The `HEAD` argument is load-bearing: without it the selector diffs against `main`,
which includes every commit you made earlier tonight and drags in dozens of unrelated
files. With `HEAD` it selects only your uncommitted change.

If the printed selection is larger than ~25 files, do **not** run it. Run
`.venv/bin/python -m pytest -q <TESTFILE>` instead and note in the ledger that you
ran the narrow gate and why.

### 6. Commit

```
git commit -m "test(<module>): kill <N> mutants — <before>% -> <after>%"
```
One commit per module, body listing the survivor indices, operators and lines you
killed. Never mix two modules in one commit — Piet reviews and cherry-picks these.

### 7. Append the ledger entry

```markdown
## <n>. <MODULE> — <UTC timestamp>
- probe before: <killed>/<total> = <pct>%   (source sha1 <sha>)
- survivors killed: [K] <operator> L<line>, [K] <operator> L<line>
- new tests: <TESTFILE>::<test_name>, ::<test_name>
- red proof: --check <K> exit 1 before / exit 0 after  (per survivor)
- probe after: <killed>/<total> = <pct>%
- gate: run-affected <N files> PASS | ruff clean
- commit: <sha>
```

Then tick the candidate in `CANDIDATES.md` and start the next module.

**The ledger is your memory, not the conversation.** This session will compact its
context several times over six hours. Everything you need to resume is in
`LEDGER.md`, `CANDIDATES.md` and this runbook — so write the entry *immediately*
after each commit, never in a batch at the end.

## When a survivor looks like a real bug

Sometimes the mutant is *more* correct than the original, or the survivor exposes a
branch that can never be reached, or a guard that does nothing. In the operational
layer this matters more than usual: a watchdog with a dead guard reports "healthy".

- **Default: do not fix it.** Append it to `NIGHT-RUN/FINDINGS.md` with file, line,
  the mutant that exposed it, a concrete failure scenario (inputs → wrong output),
  and move on. A written finding is worth more than a risky 04:00 fix.
- **Only fix it** when *all* of these hold: the fix is **5 lines or fewer**, it stays
  inside a candidate module, and you have a test that is **RED on the current code
  and GREEN after the fix** — write the test first, run it (must fail), then apply
  the fix (must pass). Commit it **separately** as
  `fix(<module>): <what> — test <name> red before, green after`, and record both
  proofs in the ledger. Anything larger goes to `FINDINGS.md` instead.

## Never report yourself blocked, never stop early

A hard candidate is not a blocker. If a module resists — confusing fixtures, a test
you cannot make red, an import that will not load — then:
1. `git checkout -- <files you touched>` to revert your attempt,
2. write one ledger line `REJECTED <module> — <reason in one sentence>`,
3. take the next candidate.

**Do not end your turn to report progress, and do not ask questions.** Piet is
asleep. Keep working until *both* of these are true:

- `NIGHT-RUN/LEDGER.md` holds **at least 20 entries**, and
- **at least 6 hours** have passed since your first ledger entry.

If you reach 20 entries before the six hours are up, keep going with further
candidates. If you run out of Band A, continue with B and C. Only when both gates are
met do you write the final summary table (every ledger entry with before/after score,
total mutants killed, findings count) and stop.

The only conditions under which you stop early and wait for Piet: the worktree is
gone, git refuses to commit, or `.venv/bin/python -m pytest` cannot run at all — and
only after two genuine repair attempts.

## Every status report ends with this line

```
STATUS ledger=<entries> killed_total=<mutants killed across run> started=<ISO8601Z> now=<ISO8601Z> elapsed=<h.h>h next=<module>
```

Get `now` from `date -u +%Y-%m-%dT%H:%M:%SZ`. `started` is the timestamp of your very
first ledger entry — carry it forward unchanged. Print this line after every commit
so the window shows progress at a glance.

## Working style

- One module at a time. Do not hold three in your head.
- Small, surgical test additions. Match the surrounding file exactly.
- Do not refactor the module you are testing. You pin behaviour, you do not change it.
- Do not write new helper scripts. `scripts/mutation_probe.py` is the only tool needed.
- No commentary about how well the run is going. Report numbers.
