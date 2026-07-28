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
| **`KpiTile`** | Value+label tile with optional delta. Generalises `FleetPod` and the inline StatsMasthead KPIs. | `{ label, value, suffix?, delta?, deltaTone?: "up"\|"down"\|"neutral", dot?: DotKind, icon?, className? }` |
| **`SectionHeader`** | Uppercase-mono section eyebrow left + quiet meta right, on a top hairline. Replaces the per-view `GroupLabel` pattern. | `{ label, meta?, rule?=true, className? }` |
| **`SubtabChips`** | Scrollable horizontal subtab / segment strip; count superscript + warn dot; one active. Extracted from FleetView's inline `fleet-chip` row. | `{ items: SubtabItem[], active, onSelect, ariaLabelPrefix?, warnSuffix?, warnDotLabel?, className?, classes? }` |
| **`DrawerShell`** | Shared detail-drawer frame: body-portalled bottom-sheet / right drawer, backdrop, Escape + scroll-lock, header (icon/eyebrow/title/close) + scroll body + footer. Derived from `PlanSpecDetailDrawer` + `NodeDetailDrawer`. | `{ eyebrow?, title, icon?, onClose, ariaLabel, closeLabel?, headerExtra?, footer?, children, widthClassName? }` |
| **`ListRow`** | Compact result/list row: leading badge cluster, clamped title, mono meta, trailing control, optional expanded body. Generalised from `FleetResultCard`. | `{ leading?, title, meta?, trailing?, children?, onClick?, className? }` |
| **`StatusChip`** | Tinted icon KPI chip (icon + label + value + hint). Already the single shared version; re-exported here as the canonical import surface. | `{ icon, label, value, hint?, tone? }` |
| **`FleetPod` / `FleetPanel` / `FleetEmptyState` / `RoleChip`** | The original Fleet atoms, moved here as the canonical source. `components/fleet/atoms.tsx` re-exports them for back-compat. | see `atoms.tsx` |

## Catalogue — what to reach for, and when not to

Three lines per block: **what it is for · when *not* to use it · the known
exception.** The middle line is the one that matters — most forking happens
because nobody wrote down where a primitive stops.

| Block | Use it for | Do **not** use it for | Known exception |
|---|---|---|---|
| **`KpiTile`** | A single number with a label, optionally a delta. | Anything that needs a body, a list, or an action inside. That is a panel, not a tile. | Colour rides on the delta only — the tile surface stays neutral. |
| **`SectionHeader`** | Labelling a section inside a panel: eyebrow left, quiet meta right. | A route masthead — that is `PulsLeiste`, and there is exactly one per route. | `rule={false}` when the header already sits on a hairline. |
| **`SubtabChips`** | One mutually-exclusive choice inside a view (Heute / Worker / Ketten …). | Navigation between routes — chips communicate state, never destination (DESIGN.md rule 7). | Themed views pass `classes` to keep their skin; structure and behaviour stay shared. |
| **`ListRow`** | A compact result row: badges, clamped title, mono meta, trailing control. | Anything wider than a row — if it wants two columns, it wants `TwoPane`. | `children` renders an expanded body in place, so a row need not open a drawer. |
| **`StatusChip`** | A tinted icon + label + value chip in a KPI cluster. | A status *word* in a card meta line — that is `SignalLabel`, which carries no chip body. | — |
| **`SignalLabel`** | LED dot + word, inline, no frame (card meta lines, column headers). | Anywhere a frame is needed to separate it from surrounding text — use `SignalChip`. | `signalToneFromLegacy` maps older tone strings; new call sites pass `SignalTone`. |
| **`SignalChip`** | The same status word with a chip body, where it must stand apart. | As a button. A status is a read-only signal, never an affordance (DESIGN.md accent doctrine 3). | — |
| **`DrawerShell`** | Every detail drawer: portalled bottom-sheet under 600px, right side-sheet from `tab`. | A permanent second pane on wide screens — that is `TwoPane`. | Below 1024px `TwoPane` deliberately hands over to `DrawerShell`. |
| **`TwoPane`** | List/detail side by side from Expanded upward. | Compact and Medium. The caller owns the viewport fork and passes no detail there. | Pair with `useTwoPaneExpanded` so the fork is decided in one place. |
| **`PulsLeiste`** | The one instrument band every route carries — masthead + Worker · Inbox · Kosten · Gateway. | A second copy of any of those four states elsewhere on the same screen (see "one state, one display"). | Purely presentational: it fetches nothing, all values come from the caller's hooks. |
| **`FleetPod` / `FleetPanel` / `FleetEmptyState` / `RoleChip`** | The Fleet idiom, canonical here. | New generic tiles — `KpiTile` generalises `FleetPod`; reach for the general one first. | `components/fleet/atoms.tsx` re-exports these for back-compat only. |
| **`DigestCard`** | Weekly scorecard digest against `/api/plugins/kanban/scores/digest`. | Anything else — it is shaped to that one payload. | ⚠ **Not exported from the barrel and currently has no call site** (measured 2026-07-28). Either wire it up or retire it; a shared layer with an orphan in it teaches the wrong lesson. |

### `SubtabItem`

```ts
interface SubtabItem { id: string; label: string; count?: number; warn?: boolean }
```

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
