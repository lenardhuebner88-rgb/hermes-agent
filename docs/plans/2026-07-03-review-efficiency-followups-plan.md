---
title: "Review efficiency follow-ups after live value audit"
type: planspec-draft
created: 2026-07-03
agent: Codex
status: draft-not-ingested
freigabe: operator
live_test_depth: smoke
taskgraph_hints:
  binding: true
  subtasks:
    - id: R1
      title: "Instrument verifier/critic review_findings before lane-skip decisions"
      lane: coder
      review_tier: review
      deps: []
      acceptance_criteria:
        - "Verifier and critic completions consistently emit metadata.review_findings with blocking and observations counts."
        - "Dashboard review_value shows finding_runs coverage per stage so NULL means uninstrumented and 0 means measured-no-findings."
        - "No critic/reviewer skip policy ships until finding coverage is high enough to distinguish no value from no telemetry."
    - id: R2
      title: "Expose complete-freigabe in the Strategist UI"
      lane: coder
      review_tier: standard
      deps: []
      acceptance_criteria:
        - "Held freigabe:operator proposals offer a third action: close as done elsewhere with a mandatory note."
        - "The action calls POST /api/plugins/kanban/strategist/proposals/{task_id}/complete and removes the proposal from the list."
        - "Static render tests cover approve, veto, and complete confirmation states without nested card layout."
---

# Review Efficiency Follow-Ups

Draft only. Do not ingest automatically.

Live audit on 2026-07-03 showed the largest raw review-value window at 2026-05-13 13:00:52 UTC through 2026-07-03 05:48:06 UTC. The useful signal is uneven: reviewer has the only measured findings; critic has high token spend but no review_findings coverage, so a critic skip is not yet evidence-safe.

Implemented now: CLI `complete-freigabe` plus API regression coverage make done-elsewhere closure available without releasing a held chain to build/review. Expected effect: proposals that were already handled outside Hermes can close at near-zero review tokens instead of entering a build/review chain.

Remaining levers:

1. Instrument before skipping critic. Critic consumed 14.76M total tokens in the raw window, but no run emitted `review_findings`; that is missing telemetry, not proof of no value. Add coverage first, then evaluate a reversible skip/downgrade policy for critical lanes that remain findingless.
2. Add the UI action for complete-freigabe. Backend and CLI are covered, but the Strategist view still only exposes approve/veto. A UI button prevents the operator from choosing release or veto when the correct state is "done elsewhere."
