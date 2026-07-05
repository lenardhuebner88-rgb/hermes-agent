# <Plan title>
**Goal:** <one sentence>

## Slice: <short verbatim title>
- lane: coder
- done-when: <verifiable done signal>
- files: path/a, path/b
- risk: <free text>
- deps: <other slice titles>

## Slice: <next slice title>
- done-when: <verifiable done signal>

---

A session turns its intended work into this prose Plan before board creation: write one `## Slice:` section per independently reviewable work slice, keep `done-when` observable, name explicit `deps` only when document order is not enough, then run `hermes plan compile <plan.md>` or the Fleet Plan composer to preview deterministic children, repairs, and warnings before ingest.
