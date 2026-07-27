# Unified usage-facts read contract

Contract version: `usage-facts.v1`
Normalization version: `origin-input.v1`

The Kanban dashboard plugin exposes:

```text
GET /api/plugins/kanban/stats/usage-facts
```

Optional repeatable/filter parameters are `origin`, `captured_from` (inclusive)
and `captured_to` (exclusive). `board` selects the Kanban database used by the
read-time provider projection. Both databases are opened read-only; the
endpoint never runs schema initialization or materialization.

The executable example for S7 is
`tests/fixtures/usage_facts_readmodel/s7_payload_example.json`.

## Group contract

`groups[]` is already aggregated. S7 must not sum native database rows.
The stable group key is:

```json
{
  "origin": "…",
  "profile": null,
  "lane": "…",
  "model": null,
  "model_label": "nicht_zuordenbar"
}
```

The required dimensions are therefore `origin × profile × lane × model`.
Provider and billing variations inside one key are already reconciled into
`billing`. Empty billing categories are omitted at group level; all three
categories are always present in `summary.billing`.

`fact_rows` counts selected token-bearing fact rows. Provider-only S5 rows do
not masquerade as zero usage; they remain visible under
`kanban.usage_coverage.provider_only_fact_runs`.

## Input normalization

The persisted `input_tokens` column is source-native and must never be summed
across origins directly.

| Origins | Persisted input meaning | `context_input` | `new_input` | `uncached_input` |
|---|---|---|---|---|
| `claude_code`, `hermes_agent`, `hermes_aux` | cache-exclusive | input + cache-read + cache-write | input + cache-write | input |
| `codex_cli`, `kimi_cli`, `grok_cli`, `qwen_cli` | cache-inclusive | input | input − cache-read | input − cache-read − cache-write |

The endpoint repeats this table in `normalization.origins`.

Each derived field carries a status:

- `exact`: every source bucket needed by that formula was observed.
- `lower_bound`: known components are returned, but an exclusive cache bucket
  was not observed.
- `unavailable`: the requested split cannot be derived. No zero is invented.

`new_input` means all prompt input except cache reads. It is deliberately not
called “billable”: on subscription routes the same tokens consume quota but
have zero marginal dollar cost.

## Workload attribution

`summary.workload` is a single aggregate view of normalized `context_input`:
`main`, `subagent`, and `unknown` each expose `fact_rows` and
`context_input_tokens`. It is not repeated in `groups[]`.

`main` is limited to the canonical `main`/`main_loop` protocol values.
`subagent` accepts the canonical `subagent` value plus agent type names learned
from selected facts where `call_kind == "subagent"` and `profile` records the
agent type. This data-derived registration deliberately avoids a stale,
hard-coded catalog of agent names. Any other (including blank) `call_kind`
remains `unknown`, rather than being attributed to main work.

`subagent_share.of_all_context` is the subagent normalized-context share over
all normalized context. `of_classified_context` excludes the separately
reported unknown bucket. `classification_status` is `partial` whenever unknown
facts are present, so consumers cannot mistake either ratio for a complete
attribution.

## Billing contract

Dollar and quota values are structurally separate:

- `billing.metered.metered_usd` contains only non-subscription billing modes.
- `billing.quota` contains subscription token consumption and
  `marginal_usd: "0"`.
- `billing.quota.list_equivalent_usd` is optional comparison information, not
  actual spend and must not be added to metered dollars.
- `billing.unclassified` contains rows whose `billing_mode` is absent or
  `unknown`; they are not guessed into either side.

All price/equivalent computations happen at read time through
`agent.usage_pricing`. Amounts are decimal strings. `known_amount_usd` may have
status `partial` or `unknown`; consult the priced/unpriced/lower-bound counts
before displaying it.

## Unattributed and Kanban contract

`unattributed` is the pre-aggregated `model IS NULL` bucket. It always has the
label `nicht_zuordenbar`, retains token totals, and includes an origin
breakdown.

`kanban.provider_classification` is the S5 read-time partition:
`nie_gelaufen`, `unbekannt`, `rekonstruiert`, `gestempelt`. It is a total
partition over all board runs; unknown runs are counted, never filtered.
`kanban.usage_coverage.state == "thin"` means the board has materially fewer
token-bearing facts than runs. This distinguishes historical hook absence from
zero consumption.
