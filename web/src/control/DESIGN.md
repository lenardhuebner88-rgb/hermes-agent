# Leitstand design language — v2 "Bronze auf Graphit"

Binding pattern doc for `/control` UI. Canonical reference: the
operator-approved mockup at `docs/design/werkbank-mockup.html` (Direction A,
"Bronze auf Graphit" — see run-state `DIRECTIONS.md` for the full decision
record and the rejected "Hauptbuch" alternative). The v1 mockup
(`docs/design/leitstand-mockup-terminals.html`, cyan-on-navy) stays in git
history as a historical reference only — it is no longer the source of truth.

New UI in `web/src/control` copies the werkbank mockup's patterns; the tokens
below (in `theme.css`) are the mechanical binding of that mockup into
Tailwind v4 utilities. `theme.css` is the **one token source of truth**;
`styles/control-tokens.css` is a deprecated compat layer that re-binds the
legacy `--hc-*`/raw-utility vocabulary onto these tokens so existing call
sites keep compiling while views migrate per-route (Waves 3/4) — see the
header comment in that file.

## Tokens

A machined precision instrument on a warm graphite ground (**not** navy) —
one calibrated bronze accent replaces the old cyan glow.

| Token | Value | Meaning / when to use |
|---|---|---|
| `--color-surface-0` | `#0e100f` | App canvas (page background). Warm graphite, green-warm cast. |
| `--color-surface-1` | `#141715` | Column / panel background. |
| `--color-surface-2` | `#1a1e1b` | Cards, inset content inside a panel. |
| `--color-surface-3` | `#232824` | Hover / selected fill on interactive rows only. |
| `--color-line` | `#2a302b` | Hairline borders (panels, buttons, chips). |
| `--color-line-soft` | `#1e2420` | Softer hairlines (panel headers, panel body dividers). |
| `--color-live` | `#c9884a` | Interactive / live accent — bronze. **Only** for things that are actually interactive or currently live. |
| `--color-brand` | `#8a8577` | Quiet chrome accent (icons, unselected avatars, non-live branding). Warm grey, not the accent. |
| `--color-bronze` | `#c9884a` | Alias of `--color-live` for new code — the one calibrated accent channel. |
| `--color-bronze-hi` | `#dda05f` | Text-on-dark / hover variant of the bronze channel. |
| `--color-status-ok` | `#86b97e` | Status trio: healthy / done / green (moss). |
| `--color-status-warn` | `#d9b23a` | Status trio: needs attention / degraded (signal yellow). |
| `--color-status-alert` | `#e0604f` | Status trio: failed / tot / alert (warm red). |
| `--color-ink` | `#ebe7de` | Primary text (warm off-white). |
| `--color-ink-2` | `#a9a59b` | Secondary text — AA-contrast floor on `surface-1`/`surface-2`. Minimum for body text. |
| `--color-ink-3` | `#757166` | Tertiary text / eyebrows only (not body copy). |
| `--radius-panel` | `10px` | Panel-level rounding (columns, top-level containers). |
| `--radius-card` | `7px` | Card-level rounding (rows, buttons, chips-adjacent controls). |
| `--font-display` | `'Archivo Variable', 'Arial Narrow', sans-serif` | Mastheads, KPI headlines, eyebrows — semi-expanded caps, the machined-plate voice. |
| `--font-data` | `'IBM Plex Mono', ui-monospace, 'Courier New', monospace` | DATA only: ids, costs, timestamps, code, terminal. |

All fonts are self-hosted via `@fontsource`/`@fontsource-variable` — **never**
a remote `fonts.googleapis.com` (or any other remote) request. Zero-layout-
shift, works offline, no third-party request from the operator's browser.

## Accent doctrine

1. **Bronze (`live`/`bronze`) is reserved for interactive or currently-live
   elements** — selected-row indicator, live status chip, primary CTA, focus
   ring. Never used decoratively or for static chrome.
2. **Status trio (`ok`/`warn`/`alert`) carries semantic meaning only**,
   matching the chip vocabulary: `läuft`/`ok` = green, `frage`/degraded =
   warn, `tot`/failed = alert, `idle` = neutral `ink-3` (no color). Status
   never appears color-only — always LED/dot + label.
3. **Channel separation by FORM, not just color.** Bronze and the status
   trio must never trade places by shape:
   - Bronze never renders as a **chip** — chips communicate status, and a
     bronze chip would read as a status color, collapsing the two channels.
   - A status color never renders as a **button or link** — status is a
     read-only signal, not an affordance. If a status needs to be
     actionable, the control itself uses the bronze/neutral button
     vocabulary and the status rides alongside it as a separate chip/LED.
4. **Warn vs. bronze distinction is deliberate**, not incidental: warn
   (`#d9b23a`, HSL hue ≈45°, L\* ≈74) sits ~12 L\* lighter and ~16° further
   into yellow than bronze (`#c9884a`, HSL hue ≈29°, L\* ≈62), and warn never
   appears without an icon + label. Do not rely on hue alone to
   disambiguate the two in a new composition — check the rendered pair.
5. **Three surface depths, used consistently**: `surface-0` = page canvas,
   `surface-1` = panel body, `surface-2` = card / inset content, `surface-3`
   = hover/selected state only (never a resting background).
6. **Text hierarchy**: `ink` for primary content, `ink-2` as the floor for
   body text (AA on `surface-1`/`surface-2`), `ink-3` only for
   eyebrows/tertiary labels. Never use `white/45` or similar opacity hacks —
   they fall below AA.
7. **Chips communicate status only, never navigation.** A chip is not a
   button; clicking should not be the only way to reach a view.
8. **Radius**: panels/top-level containers use `radius-panel` (10px);
   cards, rows, and buttons use `radius-card` (7px) — tighter than v1's
   14px/10px, deliberately machined rather than "app-store card".
9. **No raw hex in components.** Every color in `web/src/control` components
   comes from a token (Tailwind utility like `bg-surface-1`, `text-ink-2`,
   `border-line`) — never a literal `#hex` or arbitrary `[#...]`/`[rgb(...)]`
   class. Enforced by the ratchet in `scripts/gate-frontend.sh` for
   `.tsx`/`.ts`. The ratchet does **not** scan CSS files (`theme.css`,
   `control-tokens.css`, per-view `.css` files) — this convention covers
   those by rule, not by grep: new CSS still draws from the token sheet
   above (`var(--color-*)` or `color-mix()` on top of it), it just isn't
   mechanically ratcheted the way `.tsx` literals are.
10. **Extend the mockup first.** If a new pattern isn't covered here, add it
    to `docs/design/werkbank-mockup.html`, get it approved, then port the
    tokens/rules here — don't invent ad hoc colors in components.

## Mono = data only

Mono type (`--font-data` / `IBM Plex Mono`) is reserved for **data**: ids,
counts, money, timestamps, code, terminal output — anywhere the operator
needs to compare digits/characters column-on-column (tabular numerals).
It is not chrome. Section labels, eyebrows, and mastheads are **Archivo**
(`--font-display`) in expanded small caps with tracking, not mono-wallpaper
— mono regains its signal value only if it isn't used everywhere.

## Type scale

Six named steps (Tailwind v4 `--text-*` tokens in `theme.css`, each paired
with a `--text-*--line-height`). Root stays at the app default; scale is
independent of viewport (no per-component responsive font-size).

| Step | Size | Line-height | Typical use |
|---|---|---|---|
| `micro` | `0.73rem` | `1.3` | Eyebrows, micro-instrument labels, badges. |
| `sec` | `0.87rem` | `1.45` | Secondary/meta text, table cells. |
| `body` | `1rem` | `1.5` | Default body copy. |
| `emph` | `1.2rem` | `1.35` | Emphasized inline content, subtitle. |
| `h2` | `1.47rem` | `1.25` | Section headings. |
| `h1` | `1.87rem` | `1.15` | Page/route titles. |
| `hero` | `2.67rem` | `1.1` | Hero numbers, masthead statements. |

## Puls-Leiste contract

One persistent instrument strip at the top of **every** route:

- **Left:** the route masthead in Archivo expanded caps.
- **Right:** the same four live micro-instruments on every route, in this
  order — **Worker · Fragen · Kosten heute · Gateway** — giving every route
  identical muscle-memory geometry. Kosten heute renders in **USD** (`$`,
  de-comma — `fleetHub.fmtUsd`), not `€` (the werkbank mockup's `€4,12` is
  illustrative only); currency follows backend field `actual_cost_usd`
  (falls back to the marked-equivalent `cost_usd_equivalent` when the actual
  cost is absent — see `fleetHub.costDisplayValue`).
- Status is never color-only: every LED/dot ships with a label (and a count
  where applicable). No exceptions — this is the one place an operator must
  be able to scan without hovering.
- Replaces the old per-view triple-stacked header bands (e.g. Fleet's own
  header) with one shared strip; it is the "instrument, not website"
  identity anchor.

## UX contract — feature visibility and window classes

The dashboard is an operator cockpit, not a gallery of routes. Responsive work
preserves the same capabilities while changing hierarchy and pane structure.

- **Compact (<600 CSS px):** one primary pane, bottom navigation, one obvious
  next action in the first viewport. Supporting information may collapse into a
  drawer, but critical state is not removed.
- **Medium (600–839 CSS px):** a deliberate tablet layout. Do not merely stretch
  the compact bottom-bar layout; use the extra width for labelled navigation or
  a supporting pane where that improves the active job.
- **Expanded (≥840 CSS px):** persistent navigation and multi-pane/list-detail
  layouts where related context materially helps. Large desktop widths may widen
  panes, but must not turn into sparse empty chrome.

For every non-deprecated Control capability:

1. It is reachable through labelled navigation or the command surface in at most
   two interactions from `/control`.
2. If it needs operator attention, its signal appears on Start or Fleet without
   requiring the operator to know the destination route.
3. Mobile/tablet adaptations do not hide content solely because it does not fit;
   they change order, grouping, disclosure, or pane structure.
4. Objective autonomous fixes may address overflow, accessible names, touch
   targets, broken hierarchy, and existing-rule inconsistencies. New visual
   direction, route demotion/removal, or taste-led density changes require two
   Design-Board variants and operator choice.
5. Primary touch controls aim for 44–48 CSS px. No authored button/form/tab target
   may be smaller than 24×24 CSS px without a documented WCAG 2.5.8 exception.

Every UX PlanSpec names the affected user journey, route, Compact/Medium/Expanded
expectation, real/edge data state, and a visible done-when. Screenshot diffs are
regression evidence, not a beauty score; ARIA structure and behavioral assertions
must prove the same contract.

## Motion

- State changes animate 120–160ms `ease-out` — quick, mechanical, not
  bouncy.
- **No ambient animation.** The old `hc-drift` mist keyframe (a slow
  looping cyan blob) is removed entirely; any residual glow in
  `control-tokens.css` is static.
- LED pulse is reserved for genuinely-live elements (a worker actually
  running, a gateway actually up) — not decorative.
- `prefers-reduced-motion: reduce` is a central kill switch: every
  animation in `/control` (LED pulse, staggered list entrance, skeleton
  shimmer) must have a `@media (prefers-reduced-motion: reduce)` (or
  `no-preference`-gated) counterpart that removes motion, not just slows it.

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
