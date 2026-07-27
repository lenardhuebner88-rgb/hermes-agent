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

Request messages are content-fingerprinted per run. Replayed history is
discarded before repeated redaction and SQLite writes; a bounded in-process
cache handles the hot path and a nullable unique database fingerprint keeps a
process restart from duplicating already persisted request messages.

`response_id` and `first_token_ms` are measured provider/stream observations.
The first-token field stays `NULL` when the request produces no streaming
delta. `context_window_limit` is different: it is resolved only from an exact
key in `agent.model_metadata.DEFAULT_CONTEXT_LENGTHS`, never via live probes,
configuration lookups, or generic family fallbacks. Its field-specific
`context_window_limit_source` is therefore always `derived`.

Reasoning effort sources
------------------------

The request hook carries the actual transport body. The plugin reads the
top-level `reasoning` form used by the Responses transport,
`extra_body.reasoning` from `agent/transports/chat_completions.py`, and
Anthropic's `thinking`/`output_config.effort` shapes from
`agent/anthropic_adapter.py`. Legacy Anthropic `thinking.budget_tokens` values
are reversed only for the four exact budgets used by that adapter; other
budgets remain unknown.

Expected sparse provider fields
-------------------------------

`openai-codex` worker requests do not set `service_tier` by default; the
Responses transport receives it only through an explicit request override.
`NULL` is therefore expected for ordinary workers.

The same transport does set `reasoning.effort` (default `medium`). Tool-heavy
worker requests can exceed the 50,000-character hook-payload limit, however.
`AIAgent._sanitize_hook_payload` then replaces the structured request with a
truncated preview object before plugins run, so `board_facts` has no reliable
effort field to read. It deliberately leaves `reasoning_effort` as `NULL`
instead of inferring the transport default, because reasoning can be disabled
or overridden per request.

`cache_write_tokens` is also expected to be `NULL` for current
`openai-codex` Responses usage. The normalizer already accepts Anthropic's
`cache_creation_input_tokens`, OpenAI-compatible
`prompt_tokens_details.cache_write_tokens`, and Codex
`input_tokens_details.cache_creation_tokens`. Live Codex usage supplies
`input_tokens_details.cached_tokens` (including explicit zero) but omits the
cache-creation field, so there is no measured write-token value to record.

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
- `top_p`: the main conversation loop has no call site that sets it. Only
  explicit request overrides can currently populate it, so it is normally
  `NULL`.

`temperature` is not structurally dead, but is expected to be sparse. The main
path sets it only through `profile.fixed_temperature` and through Anthropic's
legacy-thinking requirement of `temperature=1`.

`lane` also has two documented meanings: Kanban hook context supplies the real
lane, while non-Kanban runs fall back to the `HERMES_PROFILE` name. Consumers
must not silently aggregate those two populations as one semantic dimension.
