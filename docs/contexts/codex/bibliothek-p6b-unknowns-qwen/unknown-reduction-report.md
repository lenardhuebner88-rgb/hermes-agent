# P6b — Arbeitsstrang B: belegbare `Unbekannt`-Lücken (Report)

Read-only-Inventur + genau **eine** belegte Unknown-Reduktions-Regel. Kein
Body-Mining, kein Raten, kein Korrektur-Overlay, kein künstliches Hermes-System
für unbekannte Menschenrollen. Originale unverändert; nur `library_view.py`
(Receipt-Adapter) + fokussierte Backend-Tests.

## Gemessene Basis (read-only, aggregiert, 1779 geladene Items)

Alle Items `partial`; Facetten-/Filterfläche (producer/path) unverändert.

| Rolle         | Vorher Unknown | Nachher Unknown | Delta |
|---------------|----------------|-----------------|-------|
| auftraggeber  | 1778/1779 (99%)| 1778/1779 (99%) | 0     |
| **delegation**| **1778/1779 (99%)** | **1389/1779 (78%)** | **−389** |
| autor         | 23/1779 (1%)   | 23/1779 (1%)    | 0     |
| review        | 1779/1779 (100%)| 1779/1779 (100%)| 0    |
| ablage        | 0/1779 (0%)    | 0/1779 (0%)     | 0     |

Status unverändert `partial` (alle 5 Rollen für `evidenced` nötig; review +
auftraggeber bleiben offen). Erzeuger-Facette unberührt.

## Umgesetzter Hebel (einziger)

**Receipt → Delegation aus Receipt-eigenem Frontmatter `assignee`.**
- Belegquelle: explizites strukturiertes Frontmatter, bereits von
  `_split_frontmatter` geparst (Kanban-Assigneeprofil des dokumentierten Tasks).
- Messung (9034 Receipts read-only): `assignee` in 6782 (75%), saubere
  Single-Token-Profilwerte (coder/worker/worker1/backend-engineer/…), 0 Leerwerte.
- Umsetzung: `_receipt_delegation_raw(meta)` → `delegation_raw` in
  `_parse_receipt_file`. List- UND Detailpfad laufen durch dieselbe Funktion →
  Parität strukturell garantiert. Abwesenheits-Sentinel (`""`, `none`, `null`,
  `unknown`, `unbekannt`, `-`) bleiben `Unbekannt`.
- Warum höchster + sicherster Hebel: höchste Abdeckung, schon geladen, keine
  DB/Body-Abhängigkeit, deterministisch, semantisch identisch zum bestehenden
  Deliverable-Adapter (dort `assignee` → delegation).

## Verbleibende `Unbekannt`-Klassen + fehlender Beleg

1. **Review (100% Unknown, alle Adapter):** keine Quelle führt
   Review-Attribution als strukturiertes Metadatum. Weder Cron-Meta, noch
   Receipt-Frontmatter (einzelne `reviewer`/`review_*`-Keys sind unkuratierte
   Freiformt-Splitter <10 Vorkommen, kein stabiler Vertrag), noch Kanban-Spalten
   liefern einen maschinenprüfbaren Reviewer. Beleg fehlt komplett → bleibt
   `Unbekannt` (ADR 0001: keine Heuristik).
2. **Auftraggeber (99% Unknown):**
   - *Cron (299 Items):* kein Auftraggeber-Metadatum im Cron-Store; das
     auslösende Subjekt steckt nur im Prompt/Script (verboten) → bleibt offen.
   - *Receipt (1456 Items):* Frontmatter-`task_id` (6870/9034) wäre ein stabiler
     ID-Ref → `tasks.created_by`, aber nur ein Bruchteil lädt (newest-200-Cap)
     und die Auflösung braucht einen DB-Join pro Quelle; **bewusst nicht
     umgesetzt** (Auftrag: nur der höchste sichere Hebel). Kandidat für eine
     spätere, separat zu testende Regel.
3. **Rest-Delegation (78% Unknown):** geladene Receipts ohne
   `assignee`-Frontmatter (ältere/andere Formate) — kein Beleg → bleibt offen.
4. **Menschenrollen:** kein künstliches `Hermes-System`-Mapping für unbekannte
   Menschen erzeugt; konkrete Durchreichen (z. B. `piet`) sind expliziter
   Metadatenbeleg, keine Erfindung.

## Tests / Exit-Codes (Repo-Wrapper, keine volle Suite)

- `scripts/run_tests.sh tests/hermes_cli/test_library_view.py` → **53 passed,
  0 failed**, Exit 0 (davon 4 neue: `_receipt_delegation_raw`-Unit, Delegation
  aus Frontmatter, Sentinel→Unbekannt, List/Detail-Parität).
- Gezielt `-k "delegation or absent_assignee or parity"` → **6 passed**, Exit 0.

## Geänderte Dateien

- `hermes_cli/library_view.py` — `_receipt_delegation_raw()` +
  `_ABSENT_ASSIGNEE`; Delegation-Beleg in `_parse_receipt_file`.
- `tests/hermes_cli/test_library_view.py` — 4 neue Backend-Tests.
