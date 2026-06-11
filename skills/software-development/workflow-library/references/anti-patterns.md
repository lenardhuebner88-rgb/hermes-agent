# Workflow Prompt Anti-patterns

## Vague Done

Bad: `/goal Improve this repo.`

Fix: name the repo, target outcome, allowed actions, verification command, and Stop criteria.

## Hidden approval

Bad: `/goal Upgrade everything and restart whatever is needed.`

Fix: require approval before major upgrades, restarts, DB migrations, production mutations, or long-running commands.

## Verification-free summary

Bad: `Stop when you think it looks good.`

Fix: require a narrow check with real output, or a blocker if no safe check exists.

## Unsafe read-only task

Bad: `Analyze prod and try a quick smoke if needed.`

Fix: explicitly forbid writes, restarts, traffic smokes, config edits, secrets, and PII.

## `/loop` confusion

Bad: telling the user to run `/loop` as if it exists.

Fix: describe loop-style work as a `/goal` prompt with per-iteration step, progress metric, and stop/abort rules.
