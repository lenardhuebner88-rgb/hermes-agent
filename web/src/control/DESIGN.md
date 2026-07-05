# Leitstand design language

Binding pattern doc for `/control` UI. Canonical reference: the
operator-approved mockup at `docs/design/leitstand-mockup-terminals.html`.
New UI in `web/src/control` copies its patterns; the tokens below (in
`theme.css`) are the mechanical binding of that mockup into Tailwind v4
utilities.

## Tokens

| Token | Value | Meaning / when to use |
|---|---|---|
| `--color-surface-0` | `#050b14` | App canvas (page background). |
| `--color-surface-1` | `#081322` | Column / panel background. |
| `--color-surface-2` | `#0c1b2e` | Cards, inset content inside a panel. |
| `--color-surface-3` | `#102438` | Hover / selected fill on interactive rows. |
| `--color-line` | `#1b3049` | Hairline borders (panels, buttons, chips). |
| `--color-line-soft` | `#132338` | Softer hairlines (panel headers, panel body dividers). |
| `--color-live` | `#4fd8eb` | Interactive / live accent. **Only** for things that are actually interactive or currently live. |
| `--color-brand` | `#6f8fb8` | Quiet chrome accent (icons, unselected avatars, non-live branding). |
| `--color-status-ok` | `#3ddc97` | Status trio: healthy / done / green. |
| `--color-status-warn` | `#f2b84b` | Status trio: needs attention / degraded. |
| `--color-status-alert` | `#ff6b6b` | Status trio: failed / tot / alert. |
| `--color-ink` | `#e9f2f7` | Primary text. |
| `--color-ink-2` | `#9db4c4` | Secondary text — AA-contrast floor on `surface-1`. Minimum for body text. |
| `--color-ink-3` | `#64809a` | Tertiary text / eyebrows only (not body copy). |
| `--radius-panel` | `14px` | Panel-level rounding (columns, top-level containers). |
| `--radius-card` | `10px` | Card-level rounding (rows, buttons, chips-adjacent controls). |

## Rules

1. **Cyan (`live`) is reserved for interactive or currently-live elements** — selected-row indicator, live status chip, primary CTA, focus ring. Never used decoratively or for static chrome.
2. **Status trio (`ok` / `warn` / `alert`) carries semantic meaning only**, matching the chip vocabulary: `läuft`/`ok` = green, `frage`/degraded = warn, `tot`/failed = alert, `idle` = neutral `ink-3` (no color).
3. **Three surface depths, used consistently**: `surface-0` = page canvas, `surface-1` = panel body, `surface-2` = card / inset content, `surface-3` = hover/selected state only (never a resting background).
4. **Text hierarchy**: `ink` for primary content, `ink-2` as the floor for body text (AA on `surface-1`), `ink-3` only for eyebrows/tertiary labels. Never use `white/45` or similar opacity hacks — they fall below AA.
5. **Section labels are uppercase mono micro-eyebrows** (`ink-3`, small size, wide letter-spacing) — not bold headings.
6. **Chips communicate status only, never navigation.** A chip is not a button; clicking should not be the only way to reach a view.
7. **Radius**: panels/top-level containers use `radius-panel`; cards, rows, and buttons use `radius-card`.
8. **No raw hex in components.** Every color in `web/src/control` components comes from a token (Tailwind utility like `bg-surface-1`, `text-ink-2`, `border-line`) — never a literal `#hex` or arbitrary `[#...]`/`[rgb(...)]` class. Enforced by the ratchet in `scripts/gate-frontend.sh`.
9. **Mobile**: no desktop tables. A table collapses to a card list; a card expands into a drawer for details. The active chain/session stays visible at all times (no dead-end views that hide current state).
10. **Extend the mockup first.** If a new pattern isn't covered here, add it to the mockup, get it approved, then port the tokens/rules here — don't invent ad hoc colors in components.
11. **One design language.** The Leitstand surfaces above are the only page canvas. A parallel visual language (own token set, light "broadsheet" surface, serif display type, …) is a design decision the operator makes explicitly per view and that gets recorded here — token-cleanliness and shared-component usage do NOT make an alternative skin compliant. Incident: Phase 3 S2 shipped CommandHome as a light broadsheet surface that passed every mechanical gate (2026-07-05).
12. **Redesign means visible change, proven visually.** Any task whose goal is design work must ship a side-by-side screenshot against the reference (Fleet tab or the approved mockup) as completion evidence, and the review verdict must answer explicitly: "would a viewer attribute this view and Fleet to the same app?" Console-0-errors and a green ratchet are necessary, never sufficient.

## Building blocks (shared components)

The rules above are realised as one canonical component layer at
`web/src/control/components/leitstand/` — `KpiTile`, `SectionHeader`,
`SubtabChips`, `DrawerShell`, `ListRow`, `StatusChip`, and the Fleet atoms
(`FleetPod` / `FleetPanel` / `FleetEmptyState` / `RoleChip`). Import these from
`components/leitstand` instead of re-deriving the idiom per view. Props and
usage: `components/leitstand/README.md`.

## VISUAL-SELF-VERIFY

VISUAL-SELF-VERIFY runs through `scripts/visual-verify.sh`, never against the
live `:9119` dashboard. The script creates a disposable `HERMES_HOME`, unsets
live Kanban board environment, enables `HERMES_SANDBOX_MODE=1`, starts `hermes
serve` on an ephemeral loopback port without an auth provider, and tears the
instance down via `trap`.

PlanSpec AC example:

```bash
scripts/visual-verify.sh --output-dir /tmp/hermes-visual-ac /control /control/agents
```

Use `--skip-build` only when `web/dist` already reflects the branch under test.
Evidence is written as PNGs for 390px, 820px, and desktop plus `summary.json`;
any console error or horizontal overflow makes the script exit non-zero.

Optional seeds use a conservative writer schema inside the isolated home:

```json
{
  "files": [
    {
      "path": "fixtures/example.json",
      "json": { "records": [{ "id": "demo", "status": "ok" }] }
    }
  ]
}
```

Every `path` is relative to the disposable `HERMES_HOME`; absolute paths and
`..` are rejected. The whole seeded home is removed after the run, so seed data
cannot touch the operator's live config or `kanban.db`.
