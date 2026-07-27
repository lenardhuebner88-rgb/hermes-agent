# Board Facts observability

`board_facts` captures nullable run facts, individual LLM-call usage, and
redacted prompt/response/tool traces in an independent SQLite database.
It never writes to the Kanban database or the legacy `scores` table.

Enable or disable it with:

```bash
hermes plugins enable observability/board_facts
hermes plugins disable observability/board_facts
```

The database defaults to:

```text
/mnt/data/hermes-observability/usage_facts.db
```

For tests or an intentional alternate mounted data volume, set
`HERMES_USAGE_FACTS_DB`. Trace rows alone expire after 180 days. Override that
window with `HERMES_USAGE_TRACE_RETENTION_DAYS`; usage facts and LLM-call rows
are not removed by retention.

All fields are nullable observations. Missing provider data remains `NULL`;
the plugin does not manufacture zeros. Hook failures are fail-soft and cannot
break an agent run. Trace content is serialized and secret-redacted before the
SQLite connection is opened, with fail-closed omission if redaction itself
fails.

`tool_output_chars` is the Unicode character count derived from the real
`post_tool_call.result` payload. String results are counted directly;
structured results are serialized with deterministic JSON first. It is not a
token count or tokenizer estimate. Trace retention is purged at most once per
plugin process, while the SQLite schema is initialized once per database file
identity and every short-lived connection is explicitly closed.

Reasoning effort sources
------------------------

The request hook carries the actual transport body. The plugin reads the
top-level `reasoning` form used by the Responses transport,
`extra_body.reasoning` from `agent/transports/chat_completions.py`, and
Anthropic's `thinking`/`output_config.effort` shapes from
`agent/anthropic_adapter.py`. Legacy Anthropic `thinking.budget_tokens` values
are reversed only for the four exact budgets used by that adapter; other
budgets remain unknown.

Structurally leere Felder
-------------------------

These fields intentionally remain `NULL` until a real producer exists. The
row-level `source` column describes the strongest observation in the row; it
does not turn these unknown individual fields into measured facts.

- `fallback_depth`: the authoritative state is
  `AIAgent._fallback_chain` plus `AIAgent._fallback_index`, used by
  `_has_pending_fallback` in `run_agent.py`. None of the existing plugin hooks
  receives the agent or the chain position. Comparing requested and response
  model names was rejected because providers commonly append a version suffix.
  Supplying the position would require a later, fork-owned measurement point;
  the upstream hook emitters are deliberately unchanged.
- `first_token_ms`: no real hook emission exists. It would require a timestamp
  in the `on_first_delta` callback of
  `interruptible_streaming_api_call`.
- `context_window_limit`: no real hook emission exists. A later slice can look
  up the static model limit from the `hermes_cli/models.py` catalogue.
- `response_id`: `_api_response_payload_for_hook` does not include
  `response.id`; a later upstream-compatible slice would need to add it there.
- `top_p`: the main conversation loop has no call site that sets it. Only
  explicit request overrides can currently populate it, so it is normally
  `NULL`.

`temperature` is not structurally dead, but is expected to be sparse. The main
path sets it only through `profile.fixed_temperature` and through Anthropic's
legacy-thinking requirement of `temperature=1`.

`lane` also has two documented meanings: Kanban hook context supplies the real
lane, while non-Kanban runs fall back to the `HERMES_PROFILE` name. Consumers
must not silently aggregate those two populations as one semantic dimension.
