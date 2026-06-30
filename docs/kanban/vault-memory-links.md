# Kanban Vault / Memory Links

Hermes Kanban derives Vault and memory links from existing task text. No separate table is authoritative: the source remains the task body, result, run summary, comment, event payload, or PlanSpec provenance field.

Recognized forms:

- Obsidian wikilinks under the configured vault root: `[[00-Canon/vision]]`, `[[03-Agents/Hermes/receipts/example|Receipt]]`
- Markdown links under the vault root: `[Vision](00-Canon/vision.md)`
- Absolute vault paths: `/home/piet/vault/00-Canon/vision.md`
- Hermes/Codex/MemSearch memory files: `${HERMES_HOME}/memories/MEMORY.md`, `$HERMES_HOME/memories/MEMORY.md`, `/home/piet/.memsearch/shared/memory/YYYY-MM-DD.md`
- Episodic anchors: `memsearch:<chunk-hash>`

Dashboard behavior:

- `/api/plugins/kanban/board` includes a compact `vault_memory_links` list per card for the `/control/flow` board.
- `/api/plugins/kanban/tasks/:id` includes the richer selected-task link list, including links found in comments and event payloads.
- Existing text-like files get an authenticated `url` served by `/api/plugins/kanban/vault-memory-links/file`.
- Vault links also carry an `obsidian_url` so an operator can jump into Obsidian.
- Other Vault file types still surface as links with `obsidian_url`, but without a browser preview URL.
- Missing files stay visible with `exists: false`; the Flow detail rail shows them as missing targets instead of hiding them.
- The authenticated browser preview is intentionally limited to text-like files: `.md`, `.txt`, and `.jsonl`.
- Board cards use a bounded compact scan; the task detail drawer performs the richer scan including comments and events.

Operational convention:

Use folder-qualified wikilinks or relative Markdown paths in tasks and receipts. Basename-only wikilinks are intentionally not resolved by a whole-vault scan during board polling, so `[[00-Canon/vision]]` is preferable to `[[vision]]`.
