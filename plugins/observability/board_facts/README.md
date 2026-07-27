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
