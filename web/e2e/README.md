# FO Backlog E2E Gate

This Playwright suite verifies the Family Organizer backlog command UX against a running Hermes dashboard.

Default target:

```bash
npm run e2e
```

The default `baseURL` is `http://127.0.0.1:9119` and the config intentionally does not spawn a server. The production dashboard service is expected to be running already.

For source-branch verification without restarting the live dashboard service, run a Vite dev server against the live backend and point Playwright at it:

```bash
HERMES_DASHBOARD_URL=http://127.0.0.1:9119 npm run dev -- --host 127.0.0.1 --port 9120
PLAYWRIGHT_BASE_URL=http://127.0.0.1:9120 npm run e2e
```

The suite asserts:

- no `console.error`, failed requests, or `4xx/5xx` responses during tested flows;
- keyboard queue navigation, drawer semantics, clipboard actions, quick views, persisted filters, and top-candidate commissioning controls;
- 390x844 mobile layout without horizontal overflow;
- `/control/orchestrator` and `/control/autoresearch` load without auth/noise regressions.
