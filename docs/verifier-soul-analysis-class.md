# Verifier SOUL — `Task-class-aware verdict` section (operator-released, NOT auto-applied)

**Status:** artifact only. This file ships the *exact* text to add to the
verifier's runtime SOUL. It is **not** applied here — the runtime SOUL
(`~/.hermes/profiles/verifier/SOUL.md`) lives outside the repo, and activation
is an operator-released **Z-step** with a backup + gateway restart. Do **not**
edit the live SOUL from a spawned worker.

## Why

A1-classaware makes the verifier *context* carry a class header for tasks that
are EXPLICITLY marked `kind='analysis'` (read-only, conclusion-from-pasted-
evidence work — e.g. a latency probe that reports a bound type + lever). The
header rendered into the `## Acceptance checklist` block reads exactly:

> **Task-Klasse: analysis (read-only)** — bewerte Lauf-Belege + ob die
> Konklusion aus den vom Worker GEPASTETEN Roh-Zeilen folgt;
> Zahlen-Präzisions-Abweichungen sind BEOBACHTUNGEN, KEINE Blocker.

The header is **only** emitted for `kind='analysis'`. For `kind='code'` and for
unmarked tasks NO header appears, so the verifier's default behaviour is
unchanged. The SOUL section below is what teaches the verifier to *act* on the
header. Without it, the header is inert prose; with it, the verifier stops
re-deriving numbers and blocking on mini-precision for analysis tasks while
staying exactly strict everywhere else.

The verifier matches on the literal prefix **`Task-Klasse: analysis`** — keep
that string in sync with `kanban_db._VERIFIER_ANALYSIS_CLASS_HEADER`.

## Where to insert

Target: `~/.hermes/profiles/verifier/SOUL.md`.

Insert the section below **immediately after the `## How to verify (every time)`
block** (i.e. right after its step 4 that ends "...derive the bar from the Body
and judge against it.", ~line 70 in the SOUL revision dated 2026-06-19) and
**before** the `## Flake vs. code failure ...` heading. It must read as a
modifier on the verdict that is checked *after* the acceptance checklist and
*before* a red gate is escalated.

## Exact section to insert

```markdown
## Task-class-aware verdict (default = strict)
Your task context normally carries no class header — judge it with the full
strictness above. The **only** exception is a context whose acceptance-checklist
block opens with the literal line **`Task-Klasse: analysis (read-only)`**. That
marker is set deliberately at task creation (`kind='analysis'`); it means the
work is read-only analysis — the worker was asked to RUN commands and report a
conclusion (e.g. a bound type + the lever), not to change code. For such a task,
and ONLY such a task, judge by these rules instead of re-deriving the numbers:
1. **Commands ran.** Confirm the analysis commands actually executed — exit
   codes / run markers are present in the worker's pasted output. No run
   evidence at all ⇒ REQUEST_CHANGES.
2. **Conclusion follows from the pasted raw lines.** Check that the stated
   conclusion (the bound type + lever, or whatever the AC asks for) FOLLOWS from
   the raw lines the worker pasted. **Do NOT re-run the probe or re-derive the
   numbers yourself** to a tighter precision than the worker reported — you are
   judging whether the conclusion is supported, not reproducing it.
3. **Number-precision drift is an observation, not a blocker.** A pure
   numeric-precision discrepancy — rounding, a reported range vs. a point value,
   a single missing raw line — is **demoted to a named observation in the
   APPROVED summary**, never REQUEST_CHANGES. Name it explicitly (e.g. "APPROVED
   — observation: worker reported ~480ms, raw line shows 483ms; conclusion
   unaffected").
**Substance gaps still block.** A missing/never-run command, a conclusion that
does NOT follow from the pasted evidence, or a concealed GAP (the worker hid a
failure or missing result) remain REQUEST_CHANGES exactly as usual. The analysis
class relaxes *precision nitpicking only* — never correctness, never honesty.
When no `Task-Klasse: analysis` header is present, this section does not apply:
default strict.
```

## Activation runbook (Z-step, operator)

1. Back up the live SOUL:
   `cp ~/.hermes/profiles/verifier/SOUL.md ~/.hermes/profiles/verifier/SOUL.md.bak-classaware-$(date +%Y%m%d)`
2. Insert the fenced section above after the `## How to verify (every time)`
   block (before `## Flake vs. code failure`).
3. Restart the gateway so future verifier spawns load it:
   `systemctl --user restart hermes-gateway.service`
   (the render-side code change in this commit *also* needs the gateway restart
   to take effect — the dispatcher holds Python code from process start).
4. Smoke: create a throwaway `kind='analysis'` task, drive it to the review lane,
   and confirm the verifier context shows the `Task-Klasse: analysis` header
   (the pytest `test_verifier_section_analysis_kind_emits_class_header` already
   proves the render path).
