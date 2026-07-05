/**
 * Fleet atoms — MOVED to the canonical Leitstand building-block layer
 * (components/leitstand/atoms.tsx, S1). This file stays as a thin re-export so
 * existing `components/fleet/atoms` imports keep working against the ONE
 * canonical source. New code should import from `components/leitstand`.
 */
export { FleetPod, FleetPanel, FleetEmptyState, RoleChip } from "../leitstand/atoms";
