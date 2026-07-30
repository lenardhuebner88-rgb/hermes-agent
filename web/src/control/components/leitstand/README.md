# Leitstand building blocks

The **one canonical shared-component layer** for `/control` views. Before you
build a KPI tile, a section label, a subtab strip, a detail drawer or a result
row in a view, import it from here — do **not** re-derive the idiom locally
(that duplication across Fleet / System / Statistik is exactly what S1 removed).

Import from the barrel:

```ts
import { KpiTile, SectionHeader, SubtabChips, DrawerShell, ListRow, StatusChip } from "@/control/components/leitstand";
// or relative: "../components/leitstand"
```

All primitives are token-only (DESIGN.md rule 8 — no raw hex; `bg-surface-2`,
`border-line`, `text-ink-*`, `text-status-*`, `text-live`, or the `hc-*`
utility classes). Keep it that way; the ratchet in `scripts/gate-frontend.sh`
enforces it.

## Primitives

| Component | Purpose | Props sketch |
|---|---|---|
| **`ViewHeader`** | The ONE page header for data routes: display eyebrow + h1 title + optional calm description + right-aligned actions slot. Replaces the per-view hand-rolled headers (hc-type-display-tight, Eyebrow+h2, SectionHeader-as-header). | `{ eyebrow, title, description?, actions?, className? }` |
| **`FreshnessStrip`** | The quiet data-honesty one-liner: muted "aktualisiert vor X" (font-data micro ink-3, "—" until the first successful load) + refresh icon-button wired to the hook's `reload()`; spins only while a refetch is actually in flight. Not a banner. | `{ lastUpdated, onRefresh?, className? }` |
| **`KpiTile`** | Value+label tile with optional delta. Generalises `FleetPod` and the inline StatsMasthead KPIs. Rank-2 elevation: `shadow-raised` lifts the tile off its panel. | `{ label, value, suffix?, delta?, deltaTone?: "up"\|"down"\|"neutral", dot?: DotKind, icon?, className? }` |
| **`SectionHeader`** | Uppercase-mono section eyebrow left + quiet meta right, on a top hairline. Replaces the per-view `GroupLabel` pattern. | `{ label, meta?, rule?=true, className? }` |
| **`SubtabChips`** | Scrollable horizontal subtab / segment strip; count superscript + warn dot; one active. WAI-ARIA tabs with manual activation: `role="tablist"`/`role="tab"` + `aria-selected`, roving tabindex, Arrow/Home/End keys (chips also mirror `aria-pressed` for legacy consumers). Extracted from FleetView's inline `fleet-chip` row. | `{ items: SubtabItem[], active, onSelect, ariaLabelPrefix?, warnSuffix?, warnDotLabel?, className?, classes? }` |
| **`DrawerShell`** | Shared detail-drawer frame: body-portalled bottom-sheet / right drawer, backdrop, Escape + scroll-lock, header (icon/eyebrow/title/close) + scroll body + footer. Derived from `PlanSpecDetailDrawer` + `NodeDetailDrawer`. | `{ eyebrow?, title, icon?, onClose, ariaLabel, closeLabel?, headerExtra?, footer?, children, widthClassName? }` |
| **`ListRow`** | Compact result/list row: leading badge cluster, clamped title, mono meta, trailing control, optional expanded body. Generalised from `FleetResultCard`. | `{ leading?, title, meta?, trailing?, children?, onClick?, className? }` |
| **`StatusChip`** | Tinted icon KPI chip (icon + label + value + hint). Already the single shared version; re-exported here as the canonical import surface. | `{ icon, label, value, hint?, tone? }` |
| **`ErrorNote`** | The one fetch-error banner: calm message line (+ optional technical detail), always `role="alert"`, optional "Erneut laden" retry button wired to the hook's `reload()`. | `{ message, detail?, onRetry?, retryLabel?, tone?: "alert"\|"warn", className? }` |
| **`FleetPod` / `FleetPanel` / `FleetEmptyState` / `RoleChip`** | The original Fleet atoms, moved here as the canonical source. `components/fleet/atoms.tsx` re-exports them for back-compat. `FleetEmptyState` carries an optional `action?: ReactNode` — the doctrine's third element (nächste Aktion). | see `atoms.tsx` |

## Catalogue — what to reach for, and when not to

Three lines per block: **what it is for · when *not* to use it · the known
exception.** The middle line is the one that matters — most forking happens
because nobody wrote down where a primitive stops.

| Block | Use it for | Do **not** use it for | Known exception |
|---|---|---|---|
| **`ViewHeader`** | The page header of a data route: eyebrow, h1 title, optional description, right-aligned `actions` (status chip or the one page action). | Routes whose PulsLeiste masthead label already suffices (a title would double it — e.g. Crons); the Jarvis zone (own A4 token source); Hero routes (Research/Strategist — the Hero IS the header); skin routes whose palette is not composed from the sheet tokens (Loops night shift). | The header carries no card/banner surface of its own — it sits directly on the page canvas. |
| **`FreshnessStrip`** | One muted freshness line per data view (or per section when a view has several independent polls): "aktualisiert vor X" + manual refetch, wired to the primary polling hook's `lastUpdated`/`reload()`. | Action feedback (a POST result is not "aktualisiert"), and as a replacement for `StaleBadge` — the badge flags the degraded state, the strip reports the normal age; both may sit side by side. | Views with a hand-rolled loader (Issues/DesignBoard/Research/Strategist) track `lastUpdated` in local state on each successful load. |
| **`KpiTile`** | A single number with a label, optionally a delta. | Anything that needs a body, a list, or an action inside. That is a panel, not a tile. | Colour rides on the delta only — the tile surface stays neutral. |
| **`SectionHeader`** | Labelling a section inside a panel: eyebrow left, quiet meta right. | A route masthead — that is `PulsLeiste`, and there is exactly one per route. | `rule={false}` when the header already sits on a hairline. |
| **`SubtabChips`** | One mutually-exclusive choice inside a view (Heute / Worker / Ketten …). | Navigation between routes — chips communicate state, never destination (DESIGN.md rule 7). | Themed views pass `classes` to keep their skin; structure and behaviour stay shared. |
| **`ListRow`** | A compact result row: badges, clamped title, mono meta, trailing control. | Anything wider than a row — if it wants two columns, it wants `TwoPane`. | `children` renders an expanded body in place, so a row need not open a drawer. |
| **`StatusChip`** | A tinted icon + label + value chip in a KPI cluster. | A status *word* in a card meta line — that is `SignalLabel`, which carries no chip body. | — |
| **`SignalLabel`** | LED dot + word, inline, no frame (card meta lines, column headers). | Anywhere a frame is needed to separate it from surrounding text — use `SignalChip`. | `signalToneFromLegacy` maps older tone strings; new call sites pass `SignalTone`. |
| **`SignalChip`** | The same status word with a chip body, where it must stand apart. | As a button. A status is a read-only signal, never an affordance (DESIGN.md accent doctrine 3). | — |
| **`ErrorNote`** | A failed **fetch**: message + optional detail + retry wired to the hook's `reload()`. | Action errors (a failed POST — nothing to "erneut laden"), data-level notices (job `last_error`, gateway down, thin denominators) and empty states (that is `FleetEmptyState`). | `tone="warn"` when the read error degrades instead of killing (stale-while-error with retained data). |
| **`DrawerShell`** | Every detail drawer: portalled bottom-sheet under 600px, right side-sheet from `tab`. | A permanent second pane on wide screens — that is `TwoPane`. | Below 840px `TwoPane` deliberately hands over to `DrawerShell`. |
| **`TwoPane`** | List/detail side by side from Expanded (≥840px) upward. | Compact. Medium keeps the drawer; the caller owns the viewport fork and passes no detail there. | Pair with `useTwoPaneExpanded` so the fork is decided in one place. |
| **`PulsLeiste`** | The one instrument band every route carries — masthead + Worker · Inbox · Kosten · Gateway as hairline-separated machined cells (divide-line-soft), same cell grid on the compact scroll row. | A second copy of any of those four states elsewhere on the same screen (see "one state, one display"). | Purely presentational: it fetches nothing, all values come from the caller's hooks. No `hover:bg-surface-3` on instruments — none of them is a link/button (accent doctrine); hover fill lands only on interactive rows. |
| **`FleetPod` / `FleetPanel` / `FleetEmptyState` / `RoleChip`** | The Fleet idiom, canonical here. | New generic tiles — `KpiTile` generalises `FleetPod`; reach for the general one first. | `components/fleet/atoms.tsx` re-exports these for back-compat only. |
| **`DigestCard`** | Weekly scorecard digest against `/api/plugins/kanban/scores/digest`. | Anything else — it is shaped to that one payload. | ⚠ **Not exported from the barrel and currently has no call site** (measured 2026-07-28). Either wire it up or retire it; a shared layer with an orphan in it teaches the wrong lesson. |

### `SubtabItem`

```ts
interface SubtabItem { id: string; label: string; count?: number; warn?: boolean }
```

### `FleetEmptyState` — the doctrine's third element

Empty states follow Situation → Bewertung → nächste Aktion (DESIGN.md W4-7).
`title`/`desc` carry the first two; the optional `action` prop carries the
third — ONE calm link or button row, rendered under the description:

```tsx
<FleetEmptyState
  title="Noch keine Design-Karten"
  desc="Der Arbeitsbereich ist noch unbestückt."
  action={<button type="button" onClick={openForm}>Neue Karte anlegen</button>}
/>
```

Omit `action` when there is no obvious next step — the row simply disappears.
Never pair `action` with `ok` on a routine empty list: a neutral Leere is not
an Erfolgszustand (kein ok-Grün auf Neutral).

### Skinning `SubtabChips`

The default skin is a neutral, token-based Leitstand chip. A themed view passes
its own `classes` so structure/behaviour is shared but the look is preserved —
e.g. FleetView keeps its dark `[data-fleet-theme]` look:

```tsx
<SubtabChips
  items={subtabDefs}
  active={subtab}
  onSelect={setSubtab}
  ariaLabelPrefix="Subtab"
  className="py-2.5"
  classes={{ chip: "fleet-chip", chipActive: "fleet-chip-on", warnDot: "fleet-warn-dot" }}
/>
```

## Proof of shareability (S1)

- `FleetView` consumes `SubtabChips` for its Heute/Worker/Ketten/… strip.
- `SystemView` consumes `SectionHeader` (its former local `GroupLabel`) and the
  shared `StatusChip` row.

See `leitstand.test.tsx` for the render/behaviour guards.
