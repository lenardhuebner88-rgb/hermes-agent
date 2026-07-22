---
status: accepted
date: 2026-07-22
---

# Derive Bibliothek provenance as a read-only overlay

The Bibliothek derives Herkunft on each read from existing Cron, Task,
Deliverable, and Receipt metadata. It does not persist the projection or mutate
source documents, because provenance corrections and grouping belong to a
later overlay rather than to the canonical receipts or tasks themselves.

Only explicit metadata may populate Erzeuger, Weg, or a Herkunftskette slot;
missing evidence stays Unbekannt. This preserves source integrity and keeps
legacy mapping deterministic, at the cost of partial chains until upstream
systems expose additional evidence such as review attribution.
