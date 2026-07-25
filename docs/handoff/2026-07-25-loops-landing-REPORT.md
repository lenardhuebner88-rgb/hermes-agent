# Loops Landing Report

**Date:** 2026-07-25
**Worked in:** the live checkout `~/.hermes/hermes-agent` (branch `main`), per brief.
**Pushed:** `piet-fork` only, fast-forward. `origin` (NousResearch) untouched — verified.

| Item | Outcome |
|---|---|
| L1 — dashboard-experience `b3dd3a259` | **Nothing to land** — already in main via `c5ff58866` |
| L2 — error-sweep `039eb3811` | **Landed** `e4fc30ce3` |
| L3a — git-probe degradation `01cab9b8f` | **Landed** `2b5ec6650` (+ blocker fix) |
| L3b — status-hue tokens `31557e05e` | **Landed** `c0d75337f` (+ blocker fix) |
| L3c — question-pill affordance `509fbb5b8` | **Landed** `18f1930c8` |
| L4 — phase-contract drift | **Diagnosed — operator decision, deliberately not changed** |

**Push proof**

```
piet-fork refs/heads/main  18f1930c87bdd6cad65b53e912a8be416b2c94b5
local main                 18f1930c87bdd6cad65b53e912a8be416b2c94b5
origin    refs/heads/main  760112adb6458417da8614d2269e5325f0739ed5   (unchanged)
```

Two foreign uncommitted files (`docs/kanban/LIFECYCLE.md`,
`tests/scripts/test_check_kanban_lifecycle_anchors.py`) were present throughout and were
left untouched — verified after the push.

---

## L1 — already landed, brief premise stale

The brief expected a rebase + frontend gate + landing. In fact `git cherry-pick` produced
an **empty** commit, and both files are byte-identical to main:

```
web/src/control/views/fleet/fleet.css        IDENTICAL to main
web/src/control/views/fleet/HeuteTab.test.tsx IDENTICAL to main
```

Main received the same change through `c5ff58866 kanban(t_d2f92645): port planspec title
touch target` — i.e. the work was ported by hand through the kanban route, almost certainly
*because* autoland was dead (see L4). Nothing was landed, no gate was needed.

## L2 — langfuse root-trace lifecycle → `e4fc30ce3`

Confirmed first that the commit is only the langfuse cleanup and does not re-land any of
the three already-in-main error-sweep changes: the already-landed `5e3b9d282` touches
`gateway/run.py`, `hermes_cli/web_server.py`, `tools/registry.py` and their tests — **zero
file overlap** with this commit.

The change collapses three duplicated
`start_as_current_observation(..., end_on_exit=False)` + manual `__enter__()` branches into
one `start_observation(**root_kwargs)` call, with `TraceState.root_ctx = None`. Since
`end_on_exit=False` meant the context manager never ended the span anyway, nothing is lost.

**Codex review — BLOCK, then reclassified.** Codex raised two lifecycle gaps: a terminal
`api_request_error` with no registered handler, and `root_span.end()` being skipped if the
root output update raises. I checked both against main rather than accepting them:

- `_finish_trace` extracted from `git show main:…` and from the staged tree is
  **byte-identical** — the second finding lives entirely in code this commit never touches.
- `git diff --cached -U0 … | grep -c api_request_error` → **0**.

Codex confirmed on re-check: `B1_PREEXISTING: yes`, `B2_PREEXISTING: yes`,
`BLOCKS_THIS_COMMIT: no` — "Land this commit as-is". Both remain open as **follow-ups**
(see below).

**Gates:** ruff clean; `tests/plugins/test_langfuse_plugin.py` 69 passed.

## L3a — git-probe degradation → `2b5ec6650`

`_git()` let `TimeoutExpired`/`OSError` escape, breaking the whole Loops response instead of
the single probe. It now returns a synthetic `CompletedProcess` with a sentinel stderr
prefix, and `_commits_ahead_with_error()` separates a real infrastructure failure from the
ordinary "branch does not exist yet".

**Codex review — BLOCK, and it was right about something that mattered.** The backend adds
`summary["error"]`, but `LoopPackSummarySchema` is a plain `z.object` with no
`.passthrough()`, so zod **silently stripped the key**. A git timeout would have rendered as
a confident "0 offene Commits" — quieter than the loud failure it replaced. Landing the
backend alone would have traded a visible error for a silent one.

I verified the strip claim in the schema, then completed the contract: optional `error` on
the schema and on `LoopPackSummary`, rendered as a warn callout on the card. Writing the
test for that exposed a **second defect I would otherwise have shipped**:
`isLoopPackError()` discriminated on `"error" in pack`, so any summary carrying a probe
error rendered as a *Manifest-Fehler* card. The test caught the misrender; the guard now
also requires `type` to be absent, which only a genuine `{name, error}` pack satisfies.

Codex's other blocker — a plain non-zero git exit (inaccessible repo, missing `main`) still
reads as "branch does not exist" — is **pre-existing**: `if res.returncode != 0: return []`
predates this commit, verified via `git show main:hermes_cli/control_loops.py`. Recorded as
a follow-up, not widened here.

**Gates:** ruff clean; `tests/hermes_cli/test_control_loops.py` 51 passed;
`gate-frontend.sh --skip-build` exit 0 (2793 vitest tests).

## L3b — status colours onto semantic tokens → `c0d75337f`

Eight production files moved from raw Tailwind palette classes to
`status-ok`/`status-warn`/`status-alert`/`ink-2`/`surface-2`/`line`, plus a new ratchet test
scanning every non-test source under `control/views` and `control/components`.

**Codex review — BLOCK on the ratchet, not the migration.** It confirmed the substitutions
are purely presentational (all tokens resolve via `theme.css`; `SystemHealthStrip` shows no
drift because the compatibility CSS already mapped the old light shades onto exactly these
tokens; no logic change). But the guard could not keep its promise: the regex covered only
`text|bg|border|ring|shadow`, so `fill-`, `stroke-`, `outline-`, `divide-`, `decoration-`,
`accent-`, `caret-` and the gradient stops `from-/via-/to-` could reintroduce a default
palette unnoticed — and those escape the existing hex-only gate too. It also scanned
`.spec` files and `__tests__` directories, which would flag fixtures as violations.

Both fixed: the regex now covers every colour-taking utility family, the walker skips
`.spec` plus `__tests__`/`__mocks__`, and the guard self-tests against nine violating and
three token-based samples so the widened coverage is itself protected. **No existing file
violates the wider rule** — the ratchet tightens with no migration debt.

**Gates:** `gate-frontend.sh --skip-build` exit 0, vitest 2984 passed.

## L3c — question-pill affordance → `18f1930c8`

The warn-coloured chip becomes a neutral button carrying a warn LED; the 44px touch target
moves into the component and the arbitrary `text-[11px]`/`text-[10px]` become `text-micro`.

**Codex review — PASS, no blockers.** It checked both production call sites
(`AgentTerminalsView.tsx:2281,2885`) — neither passes `className` any more, the first stays
equivalent, the second gains the 44px height and lines up with its 48px sibling. Accessible
name, focus outline, click and hidden-at-zero behaviour unchanged; the state still reads
without colour via the label; `text-ink` on `surface-2` measures ~13.7:1.

**Gates:** `gate-frontend.sh --skip-build` exit 0.

### Note on the skipped build step

Every frontend gate ran as `--skip-build` — lint:control, `tsc -b --noEmit` and the full
vitest suite all executed and are the correctness signal. The build step was deliberately
not run: `npm run build` writes `hermes_cli/web_dist`, which per `scripts/gate-frontend.sh`
doubles as the de-facto asset publish of this shared, parallel-edited live checkout, and no
such step was mandated by the brief. **`hermes_cli/web_dist` therefore does not yet contain
these UI changes** — an operator publish is required for them to be served.

One gate run showed a single failure in `BibliothekCorrectionEditor.test.tsx` (a focus
assertion). It passed 11/11 in isolation and the full gate passed on re-run: a flake under
parallel load, unrelated to any changed file.

---

## L4 — phase-contract drift: diagnosed, operator decision

**The brief's hypothesis was wrong.** `loops/packs/dashboard-experience/pack.yaml` matches
the curated `AUTOLAND_PHASE_CONTRACT` exactly, all three prompt filenames included, and
`python -m loops.runner --pack dashboard-experience --cmd status` runs **exit 0 with no
ManifestError**. The manifest is not drifted, so there was nothing to "fix back".

The real cause is `_runtime_autoland_authorized()` in `loops/runner.py`: if **any**
`PHASE_*_ENGINE`/`PHASE_*_MODEL` override exists, it compares the *effective engine* per
phase against the contract. The persistent override file
`~/.hermes/loops/dashboard-experience/night-overrides.env` — written from the /control start
dialog, hence "UI-Phasenvertrag" — reroutes all three phases:

| Phase | SOLL (contract) | IST (`night-overrides.env`) |
|---|---|---|
| plan | `claude` | **`codex`** |
| build | `codex` | **`alibaba-token-plan`** |
| verify | `claude` | **`codex`** |

All three mismatch, so authorization fails, `AUTOLAND_MANUAL` is written, and every run logs
the skip. The ledger shows it firing nightly:

```
2026-07-22 21:00 R1 ✅ P1-fleet-planspec-title-touch-target.md verified (b3dd3a259)
2026-07-22 21:00 AUTOLAND übersprungen (night):  abweichender UI-Phasenvertrag
2026-07-23 04:52 AUTOLAND übersprungen (resume): abweichender UI-Phasenvertrag
2026-07-24 04:52 AUTOLAND übersprungen (resume): abweichender UI-Phasenvertrag
2026-07-25 04:52 AUTOLAND übersprungen (resume): abweichender UI-Phasenvertrag
```

**This is the fail-closed design working as intended, not a bug.** Someone deliberately
rerouted the pack to a cross-family lineup via the dashboard; the runner correctly refuses
to auto-land work produced by an unbound routing. Per the brief ("falls die Änderung gewollt
war, ist das eine Operator-Entscheidung — dann NUR im Report dokumentieren, nicht raten"),
**I changed nothing.**

**For the operator — one precise detail that widens the options.** The check triggers on any
`PHASE_*` override but only compares **engines**; models are explicitly allowed to float.
So autoland can be restored *without* giving up model choice:

- Restore the three engines in `night-overrides.env` to `claude` / `codex` / `claude`,
  keeping `PHASE_*_MODEL` overrides as desired — the `AUTOLAND_MANUAL` marker is unlinked
  automatically on the next authorized run (`_prepare_runtime_land_mode`).
- Or keep the current cross-family routing and accept manual landing as the standing mode.

This also closes the loop on L1: the one commit this pack verified (`b3dd3a259`) never
auto-landed, and was ultimately ported to main by hand as `c5ff58866`.

---

## Open follow-ups (not fixed here)

1. `plugins/observability/langfuse/__init__.py` — terminal `api_request_error` has no
   registered handler; spans are retained until LRU eviction. Pre-existing.
2. `plugins/observability/langfuse/__init__.py` (`_finish_trace`) — if the root output
   update raises, `root_span.end()` is skipped and only `client.flush()` runs. Pre-existing.
3. `hermes_cli/control_loops.py` — a plain non-zero git exit (inaccessible repo, missing
   `main`) is still classified as "branch does not exist yet". Pre-existing; a fix would
   need to distinguish git's exit codes rather than only the timeout/OSError sentinel.
4. `hermes_cli/web_dist` does not contain the three landed UI changes — operator publish
   required.
