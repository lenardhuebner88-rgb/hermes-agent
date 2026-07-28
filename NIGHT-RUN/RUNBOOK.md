# NIGHT RUN — mutation hardening of fork-owned modules

**Read this file again at the start of every turn. It is the only source of truth
for this run. Do not improvise a different procedure.**

## Mission

This fork carries 128 Python modules that do not exist upstream. Upstream ships no
tests for them, so whatever is not pinned by a fork-owned test is not pinned at all.

You measure that gap with mutation testing and close it, one module per iteration:
generate single-edit mutants of a module, run its test file against each mutant, and
every mutant that **survives** (tests stay green) is a behaviour nobody tests. You
write the test that kills it.

This is not a coverage exercise. A test that raises coverage but kills no mutant is
worthless here and will be rejected — the probe proves the difference mechanically.

## Where you are

- Worktree: `/home/piet/.hermes/hermes-agent/.claude/worktrees/qwen-mutation-nightly`
- Branch: `qwen/mutation-hardening-2026-07-28` (forked from `main`)
- Python: `.venv/bin/python` — **always this one**. Never `python3`, never `pytest`
  bare, never `npx`. `.venv` is a symlink to the live checkout's venv; do not touch it.
- Candidates: `NIGHT-RUN/CANDIDATES.md` (98 module/test pairs, work top-down)
- Your ledger: `NIGHT-RUN/LEDGER.md` — append one entry per finished module
- Suspected real defects: `NIGHT-RUN/FINDINGS.md`

## Hard rules — violating any of these ends the run badly

1. **Never leave this worktree.** No `cd` to `/home/piet/.hermes/hermes-agent`
   (the live checkout, edited by other sessions in parallel), no other repo.
2. **Never push.** Not to `origin` (that is the upstream project), not to `piet-fork`,
   not anywhere. Commits stay local on this branch.
3. **Never merge, rebase, reset, stash, or switch branch.** Stay on
   `qwen/mutation-hardening-2026-07-28` for the whole run.
4. **Never deploy and never restart a service.** No `systemctl`, no
   `scripts/deploy_dashboard.sh`, no gateway restart. Restarting a service would kill
   your own session.
5. **Never touch runtime state**: nothing under `~/.hermes/` except this worktree —
   in particular not `config.yaml`, `auth.json`, `kanban.db`.
6. **Never modify** `hermes_cli/kanban_db.py`, `gateway/run.py`, `cli.py`, or anything
   under `web/`. They are out of scope for this run.
7. **Never delete, skip, xfail or weaken an existing test** to make something pass.
   The discovered test count of a file must never go down.
8. **No `git add -A` / `git commit -a`.** Stage the exact files you changed, by name.

## The loop — one module per iteration

### 1. Pick the next candidate

Take the first unchecked `- [ ]` entry in `NIGHT-RUN/CANDIDATES.md` that is not
already in `NIGHT-RUN/LEDGER.md`. Work Band A first, then B, then C.

### 2. Measure the baseline

```
.venv/bin/python scripts/mutation_probe.py <MODULE> <TESTFILE> --max 20
```

The probe generates deterministic mutants, applies them one at a time, runs the test
file against each, and restores the module afterwards. It prints per-mutant
`KILLED` / `SURVIVED`, a score, and the list of survivors with their index.

Reject the candidate and move to the next one when:
- the probe reports `baseline is FAIL` (a red baseline makes the numbers meaningless), or
- the printed baseline is **slower than 30 s** (20 mutants would cost >10 min), or
- the probe reports **0 survivors** (the module is already fully pinned — that is a
  good result, record it in the ledger as `no gap` and move on), or
- generating mutants fails.

Record every rejection as a one-line ledger entry with the reason. A rejected
candidate is normal progress, not a blocker.

### 3. Kill up to 3 survivors

For each survivor you take (max 3 per module, pick the ones that guard real logic,
not cosmetics):

a. **Understand what the mutation changes.** Read the module around the printed line.
   Ask: what observable behaviour differs between original and mutant?

b. **Write a test in the module's existing test file** that asserts exactly that
   observable behaviour, in the style of the tests already in that file (same
   fixtures, same naming, same imports). Test *behaviour through the public
   surface*, never `inspect.getsource`, never asserting on the source text — a test
   that reads the source would "pass" against any mutant and is cheating.

c. **Red proof — the mutant must die:**
   ```
   .venv/bin/python scripts/mutation_probe.py <MODULE> <TESTFILE> --max 20 --check <K>
   ```
   Exit code **0 = KILLED** → your test does its job. Exit code **1 = SURVIVED** →
   your test does not actually pin the behaviour; fix the test, do not move on.
   (`--check` applies mutant K, runs the tests, restores the module.)

d. **Green proof — the untouched module must still pass:**
   ```
   .venv/bin/python -m pytest -q <TESTFILE>
   ```

### 4. Re-measure

```
.venv/bin/python scripts/mutation_probe.py <MODULE> <TESTFILE> --max 20
```
The score must be strictly higher than the baseline. If it is not, your tests did
not land — go back to step 3.

### 5. Gate

```
git add <TESTFILE>                 # the selector is blind to untracked files
scripts/affected-tests.sh HEAD     # PRINTS the selection — look before you run
scripts/run-affected.sh HEAD       # only if the line above listed <= 25 files
.venv/bin/python -m ruff check <TESTFILE> <MODULE>
```

The `HEAD` argument is load-bearing: without it the selector diffs against `main`,
which includes every commit you made earlier tonight and drags in dozens of unrelated
files (measured: 31). With `HEAD` it selects only your uncommitted change — measured 1
file for a single test-file edit.

If the printed selection is larger than ~25 files, do **not** run it. Run
`.venv/bin/python -m pytest -q <TESTFILE>` instead and note in the ledger that you
ran the narrow gate and why. A narrow honest gate beats a broad one that eats an hour.

### 6. Commit

```
git commit -m "test(<module>): kill <N> mutants — <before>% -> <after>%"
```
One commit per module. Body: the survivor indices, operators and lines you killed.
Never mix two modules in one commit — Piet reviews and cherry-picks these in the
morning.

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

## When a survivor looks like a real bug

Sometimes the mutant is *more* correct than the original, or the survivor exposes a
branch that can never be reached, or a guard that does nothing. That is a finding.

- **Default: do not fix it.** Append it to `NIGHT-RUN/FINDINGS.md` with file, line,
  the mutant that exposed it, a concrete failure scenario (inputs → wrong output),
  and move on. A written finding is worth more than a risky 03:00 fix.
- **Only fix it** when *all* of these hold: the fix is **5 lines or fewer**, it stays
  inside a fork-owned module from `CANDIDATES.md`, and you have a test that is **RED
  on the current code and GREEN after the fix** — prove it by writing the test first,
  running it (must fail), then applying the fix (must pass). Commit it **separately**
  as `fix(<module>): <what> — test <name> red before, green after`, and record both
  proofs in the ledger. Anything larger goes to `FINDINGS.md` instead.

## Never report yourself blocked

A hard candidate is not a blocker. If a module resists — confusing fixtures, a test
you cannot make red, an import that will not load — then:
1. `git checkout -- <files you touched>` to revert your attempt,
2. write one ledger line `REJECTED <module> — <reason in one sentence>`,
3. take the next candidate.

The only conditions under which you stop and ask Piet: the worktree is gone, git
refuses to commit, or `.venv/bin/python -m pytest` cannot run at all — and only after
two genuine repair attempts.

## Every response ends with this line — no exceptions

```
STATUS ledger=<entries> killed_total=<mutants killed across run> started=<ISO8601Z> now=<ISO8601Z> elapsed=<h.h>h next=<module>
```

Get `now` from `date -u +%Y-%m-%dT%H:%M:%SZ`. `started` is the timestamp of your very
first ledger entry — carry it forward unchanged every turn. This line is how the goal
judge sees your progress; a response without it will be treated as no progress.

## Working style

- One module per turn is the target. Do not try to hold three modules in your head.
- Small, surgical test additions. Match the surrounding file exactly.
- Do not refactor the module you are testing. You are pinning behaviour, not changing it.
- Do not write new helper scripts. `scripts/mutation_probe.py` is the only tool you need.
- No commentary about how well the run is going. Report numbers.
