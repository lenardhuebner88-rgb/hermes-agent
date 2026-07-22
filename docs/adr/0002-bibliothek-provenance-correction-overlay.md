---
status: accepted
date: 2026-07-22
---

# Correct Bibliothek provenance through an operator-confirmed overlay

The deterministic P6a derivation (ADR 0001) leaves legitimate gaps — review
attribution and cron commissioners are often absent, and the derivation must
never guess. Rather than mine bodies or mutate source documents, P6b layers a
versioned, profile-local JSON overlay OVER the derivation: an operator may
override only `producer`/`path` and the five Herkunftskette slots of one item,
keyed by its stable item identifier. Originals (Cron/Task/Receipt/Deliverable)
stay byte-unchanged; the overlay lives separately under `HERMES_HOME/control/`.

The single write path is fail-closed: the mutation requires an explicit
`confirm is true` step plus a mandatory reason, and it is reachable only via
session-gated `/api/` routes (never `PUBLIC_API_PATHS`); no agent or tool
automatism finalizes a correction. An immutable Originalsnapshot (taken from
the derived contract at first correction) and an append-only history keep every
change auditable, and reverted records remain on file. The effective contract
drives list, detail, facets, and badges through one shared apply step, so both
views always agree. Responses expose both the immutable `original` snapshot and
the current automatic `derived` contract: previews and removed overrides use
`derived`, so later evidence improvements are not mistaken for the old snapshot.

Trade-offs: loopback dashboard auth provides no hard user identity (one shared
operator session), so the confirm step documents deliberate operator action
rather than attributing it — `actor` is fixed to `operator`. The store is
profile-local, not cross-profile shared. A corrupt or future-version store
degrades reads fail-soft to the pure derivation, while every mutation refuses
to overwrite that unreadable state; recovery is a separate operator action so
existing records and append-only history cannot be silently destroyed.
