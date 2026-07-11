UI-WIP blieb außerhalb des Reviews.

- **BLOCKER — `hermes_cli/voice_live_session.py:68-76, 300-344, 1778-1784`:** Die Persona verspricht weiterhin ein automatisch geliefertes Standbild beim Ansprechen. Default-`on_demand` verhindert genau dieses Bild; `look_closely` wird nur für Details empfohlen. Normale visuelle Fragen können daher blind beantwortet werden. Runde‑1-Blocker 1 ist nicht vollständig behoben.
- **BLOCKER — `hermes_cli/voice_live_session.py:1076-1101`:** `watch_view` überspringt im Default das Änderungsbild, sendet trotzdem „Prüfe …“ und zählt die Notification als gesendet. Der lokal...


## Session 23:42

### 23:42
<!-- session:46e72a41-a9f8-466e-8639-69afc99f5c03 turn:87a5da0a-ea39-4a95-9423-6fba91c76c3b transcript:/home/piet/.claude/projects/-home-piet-projects-family-organizer/46e72a41-a9f8-466e-8639-69afc99f5c03.jsonl -->
- User received task completion notification that agent K finished WeekBoard round-4 fix work (hint relocation and MAX_CELL_EVENTS cap adjustment).
- Agent K had moved the "+N weitere" hint out of the overflow-hidden clip container to a flex sibling, changed MAX_CELL_EVENTS from 4 to 1, and rewrote tests to match; all gates passed (lint, vitest, build, playwright visual with no snapshot regeneration needed).
- Claude Code identified a product problem: MAX_CELL_EVENTS=1 results in zero appointments visible on birthday days, making it "product-broken."
- Claude Code made a design decision to replace the strict cap with `line-clamp-2` on board chips instead (bounded wrapping with ellipsis, consistent with prior hero card treatment), which makes chip height deterministic while restoring a sane appointment cap.
- Claude Code dispatched the line-clamp round (implying a new subtask execution).
- Claude Code outlined the next workflow sequence: visual re-acceptance by Claude Code, independent review via a Claude `reviewer` subagent (per Piet's directive; Codex retired for this session), then K commit and S rebuild.

