# Context Map

Hermes has several bounded contexts. This map grows lazily and lists only
contexts whose language has been resolved explicitly.

## Contexts

- [Bibliothek](./docs/contexts/bibliothek/CONTEXT.md) — presents human-readable
  documents from existing Hermes sources without owning their content.

## Relationships

- **Cron, Kanban, Vault Receipts, llm-wiki → Bibliothek**: the Bibliothek reads
  their existing metadata and documents through adapters; it never mutates
  those upstream sources.
- **Bibliothek → Operator**: it presents source type and Herkunft in plain
  language while keeping raw IDs and paths in technical details.
- **Operator → Bibliothek overlay**: an explicitly confirmed correction writes
  only to the profile-local overlay; source documents remain unchanged.
