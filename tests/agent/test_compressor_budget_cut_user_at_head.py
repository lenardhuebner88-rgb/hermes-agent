"""Regression coverage for the causal-coupling branch discarding the
token-budget cut (#22523 follow-up).

``_ensure_last_user_message_in_tail`` handles the case where the last
*real* user message sits exactly at ``head_end`` (the first compressible
index) — very common from the second compaction onward, because
``_effective_protect_first_n`` decays to 0 and a resumed autonomous
session often has one nudge right after the head followed by hundreds of
assistant/tool turns with no further user input.

Pre-fix, that branch returned ``max(pair_end, head_end + 1)`` — silently
throwing away the deep token-budget ``cut_idx`` (e.g. 290 → 4).  The
"protected tail" then ballooned to nearly the whole transcript, the
compression became a near-no-op, and the reactive context-overflow
recovery re-sent essentially the same oversized prompt until the turn
died with "max compression attempts reached".

Pinned here:

* the branch preserves a budget cut that already contains the head pair
  entirely in the summarised region;
* the #22523 semantics survive: a budget cut landing INSIDE the head
  pair is still pushed forward to ``pair_end`` (pair summarised as a
  unit, never split);
* integration through ``_find_tail_cut_by_tokens``: a long transcript
  whose only real user message sits right after the system prompt gets
  a budget-sized tail, not a tail of nearly the whole transcript.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture()
def compressor():
    from agent.context_compressor import ContextCompressor

    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100_000,
    ):
        c = ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=2,
            protect_last_n=3,
            quiet_mode=True,
        )
        c.tail_token_budget = 200
        return c


def _long_transcript(n_pairs: int) -> list[dict]:
    """system, user@1, then assistant/tool churn — no further user turns."""
    messages = [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "kick off the task"},
    ]
    for i in range(n_pairs):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"c{i}", "function": {"name": "work", "arguments": "{}"}}
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"c{i}",
                "content": "tool output " + ("x" * 400),
            }
        )
    messages.append({"role": "assistant", "content": "latest visible reply"})
    return messages


class TestEnsureLastUserMessageInTailKeepsBudgetCut:
    def test_deep_budget_cut_is_preserved_when_user_sits_at_head_end(
        self, compressor
    ):
        messages = _long_transcript(20)  # n = 2 + 40 + 1 = 43
        n = len(messages)
        head_end = 1  # system-only head; user sits at index 1 == head_end
        deep_cut = n - 6

        result = compressor._ensure_last_user_message_in_tail(
            messages, deep_cut, head_end
        )
        # Pre-fix this returned pair_end (== 4: user@1 + assistant@2 + tool@3),
        # ballooning the tail to nearly the whole transcript.
        assert result == deep_cut

    def test_budget_cut_inside_head_pair_still_pushed_to_pair_end(
        self, compressor
    ):
        """#22523 semantics preserved: never split the head turn-pair."""
        messages = _long_transcript(20)
        head_end = 1
        # user@1, assistant@2, tool@3 → pair_end == 4.  A cut at 3 would
        # split the pair (tool result summarised without its call).
        result = compressor._ensure_last_user_message_in_tail(
            messages, 3, head_end
        )
        assert result == 4

    def test_user_beyond_head_end_still_anchored_into_tail(self, compressor):
        """The original #10896 anchor behaviour is untouched: a real user
        message deeper in the middle region pulls the cut back to it."""
        messages = _long_transcript(20)
        user_idx = 11
        messages[user_idx] = {"role": "user", "content": "mid-run steer"}
        deep_cut = len(messages) - 6

        result = compressor._ensure_last_user_message_in_tail(
            messages, deep_cut, head_end=1
        )
        assert result == user_idx


class TestFindTailCutByTokensIntegration:
    def test_tail_stays_budget_sized_when_only_user_is_at_head(self, compressor):
        messages = _long_transcript(60)  # n = 123, each tool msg ~100+ tokens
        n = len(messages)

        cut = compressor._find_tail_cut_by_tokens(messages, head_end=1)

        # Pre-fix: cut collapsed to pair_end (4) → tail of n-4 messages.
        # Post-fix: the budget walk's deep cut survives.  Allow generous
        # slack for the min-tail floor and assistant anchor, but the tail
        # must remain a small fraction of the transcript, not nearly all
        # of it.
        tail_size = n - cut
        assert tail_size < n // 2, (
            f"tail ballooned: cut={cut}, tail={tail_size} of {n} messages"
        )

    def test_short_transcript_anchor_never_lands_inside_head_pair(self, compressor):
        """When the assistant anchor's pull-back is affordable (small
        transcript), it still must not drag the cut INSIDE the head
        turn-pair — the pair is summarised as a unit (#22523)."""
        messages = [
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "kick off"},
            {"role": "assistant", "content": "the visible reply"},
            {"role": "tool", "tool_call_id": "c0", "content": "small"},
        ]
        for i in range(8):
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": f"c{i}", "function": {"name": "work", "arguments": "{}"}}
                    ],
                }
            )
            messages.append(
                {"role": "tool", "tool_call_id": f"c{i}", "content": "out"}
            )

        cut = compressor._find_tail_cut_by_tokens(messages, head_end=1)

        # pair: user@1 + assistant@2 (+ tool@3 belongs to c0 shape) — the
        # cut must sit at/after the pair end, never at index 2 or 3.
        pair_end = compressor._find_turn_pair_end(messages, 1)
        assert cut >= pair_end, (
            f"cut {cut} landed inside the head pair (pair_end={pair_end})"
        )
