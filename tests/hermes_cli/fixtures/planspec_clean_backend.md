---
status: Entwurf
freigabe: nicht signiert
live_test_depth: contract
taskgraph_hints:
  binding: true
  subtasks:
    - id: S1
      title: Clean backend slice
      lane: coder
      scope_files:
        - hermes_cli/planspecs.py
      max_iterations: 180
---
# Clean backend example

This frozen PlanSpec is the deterministic negative counter for the author rubric.