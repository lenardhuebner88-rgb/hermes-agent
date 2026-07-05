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
