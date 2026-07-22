# Bibliothek

The Bibliothek presents human-readable Hermes documents through read-only
adapters over existing sources.

## Language

**Regal**:
A named Bibliothek section with a distinct document purpose, such as Lesesaal,
Ergebnisse, Nachschlagewerk, Briefings, or Modelle.
_Avoid_: Tab, bucket

**Lesesaal**:
The Regal for chronological working material such as digests, research answers,
deliverables, and receipts.
_Avoid_: Ergebnisse, archive

**Ergebnisse**:
The Regal for final verdicts and decisions rather than raw worker output.
_Avoid_: Lesesaal, reports

**Dokumenttyp**:
The type assigned explicitly by the source adapter; it is never inferred from
document text.
_Avoid_: guessed category, body classification

**Herkunft**:
The read-only provenance view that answers who created a document and by which
Weg it entered the Bibliothek.
_Avoid_: provenance in operator-facing UI, ownership

**Erzeuger**:
The actual author or responsible automation profile, never the commissioner or
reviewer; missing evidence is shown as Unbekannt.
_Avoid_: Auftraggeber, assignee when actual authorship is known

**Weg**:
One of Cron, Task, Receipt, Manuell, or Unbekannt; technical subtypes remain in
the details.
_Avoid_: source path, transport

**Herkunftskette**:
The five evidence slots Auftraggeber, Delegation, Autor, Review, and Ablage.
Every missing slot remains visibly Unbekannt.
_Avoid_: guessed workflow, audit log

**Belegstatus**:
The chain summary: vollständig belegt only when all five slots are evidenced,
teilweise belegt when at least one slot is evidenced, otherwise unbekannt.
_Avoid_: confidence score, completeness guess

**Facette**:
A multi-select filter over Erzeuger or Weg, using OR within one Facette and AND
between Facetten; counts reflect the other active filters before pagination.
_Avoid_: tag, time filter

**Korrektur-Overlay**:
The operator-confirmed, versioned override over the derived Herkunft; only the
Weg and the five Herkunftskette slots of one item can be set, source documents
stay byte-unchanged, and the effective value drives list, detail, and Facetten
through one shared apply step.
_Avoid_: editing receipts, ownership transfer, agent-applied fix

**Originalsnapshot**:
The immutable record of the derived Herkunft at the moment of first Korrektur;
shown next to the effective value and never overwritten by later edits.

**Aktuelle Ableitung**:
The current deterministic P6a Herkunft before active overrides. It is returned
separately from the immutable Originalsnapshot and is the base for previews and
for removing an override, because later source metadata may improve the
automatic result.
_Avoid_: previous value, diff base

**Operator-Bestätigung**:
The explicit confirm step plus mandatory reason required by every write on the
Korrektur path; without both, the path fails closed and nothing is persisted.
_Avoid_: auto-apply, tool finalization

**Änderungsverlauf**:
The append-only history of set and revert operations with actor, reason, and
timestamp; reverted records remain on file for audit.
_Avoid_: deletable changelog, undo stack
