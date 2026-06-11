# Hermes Loop-Prompt-Generator

Use this when the user asks for iterative work. Hermes does not currently ship a separate `/loop` command; generate a `/goal` prompt that encodes the loop behavior.

## 1. Loop goal
What state should improve over repeated turns?

## 2. One iteration step
Each iteration should do at most:
- Re-check context/evidence:
- Perform one small analysis/change:
- Run one narrow check:
- Decide next state:

## 3. Progress metric
How does Hermes know progress happened?
- Tests fixed:
- Warnings reduced:
- Files documented:
- Hypotheses resolved:
- Planned items completed:

## 4. Stop or abort rules
Stop when:
- Done criteria are met.
- Verification is blocked.
- Scope expansion or approval is needed.
- The same failure repeats.
- Cost/time/turn budget is reached.

## 5. Final loop-compatible prompt

```text
/goal Work iteratively toward <goal>. In each iteration: (1) choose the next smallest useful step, (2) inspect only the context needed for that step, (3) execute that one step, (4) run the narrowest verification, and (5) decide done, continue, or block. Track progress with <metric>. Do not exceed <scope boundaries>. Stop only when <done criteria> or a clear blocker is reached.
```
