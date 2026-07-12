# retention_reap.py — operator wiring

`scripts/retention_reap.py` plans cleanup for three bounded roots:

- regular files strictly older than 14×24 hours below `~/.hermes/playwright-mcp-output`;
- browser revision directories in `~/.cache/ms-playwright` that are not referenced by dynamically discovered Playwright `package.json` + `browsers.json` metadata;
- `~/.hermes/kanban.db.bak*` sets, grouping each primary backup with `-wal`/`-shm` sidecars and retaining the three newest sets.

The default is dry-run. Every candidate is journalled as `WOULD-DELETE` with path and byte size. `--apply` is the only deletion mode. Browser metadata is discovered dynamically below the user's home in every
`node_modules`, `site-packages`, and `dist-packages` tree, including Python's
bundled Playwright driver. Browser cleanup fails closed when installed metadata
is missing or invalid, or when `PLAYWRIGHT_BROWSERS_PATH` targets a different
cache. A non-blocking process lock makes overlapping runs exit successfully
without acting.

## Repo-durable timer templates

- `scripts/systemd/agent-retention-reap.service` runs the default dry-run.
- `scripts/systemd/agent-retention-reap.timer` runs daily at 04:45 with `Persistent=true`.

They are templates only. This task does not copy, enable, start, or reload them, and does not alter the existing `nightly-audit.timer`.

Operator installation after review:

```bash
install -m644 scripts/systemd/agent-retention-reap.service ~/.config/systemd/user/
install -m644 scripts/systemd/agent-retention-reap.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-retention-reap.timer
```

Inspect dry-run decisions with:

```bash
journalctl --user -u agent-retention-reap.service
```

Only after the journal has been reviewed, add `--apply` to the installed service's `ExecStart` and reload the user manager. The repo template intentionally remains dry-run-first.
