import { boardDataColor, selectableFleetBoards } from "../../lib/multiBoard";

export function BoardBadge({ slug }: { slug?: string | null }) {
  if (!slug || slug === "default") return null;
  return (
    <span
      className="inline-flex min-w-0 items-center gap-1 rounded-card border border-line bg-surface-2 px-1.5 py-0.5 font-data text-micro text-ink-2"
      title={`Board: ${slug}`}
    >
      <span className="size-1.5 shrink-0 rounded-full" style={{ backgroundColor: boardDataColor(slug) }} aria-hidden="true" />
      <span className="max-w-28 truncate">{slug}</span>
    </span>
  );
}

export function BoardSwitcher({
  boards,
  current,
  selected,
  onSelect,
}: {
  boards: Array<{
    slug: string;
    name: string;
    archived: boolean;
    project_bound?: boolean;
    project_name?: string | null;
  }>;
  current: string;
  selected: string | null;
  onSelect: (board: string | null) => void;
}) {
  // projects.db is fail-soft in the API. If it is temporarily unavailable,
  // keep navigation usable without pretending that any board is bound.
  const available = selectableFleetBoards(boards);
  const currentProject = available.find((board) => board.slug === current);
  return (
    <label className="mb-3 flex min-h-12 items-center gap-2 rounded-panel border border-line bg-surface-1 px-3 text-sec text-ink-2">
      <span className="font-display text-micro uppercase tracking-[0.12em] text-ink-3">Board</span>
      <span className="size-2 shrink-0 rounded-full" style={{ backgroundColor: boardDataColor(selected ?? current) }} aria-hidden="true" />
      <select
        className="min-h-10 min-w-0 flex-1 rounded-card border border-line bg-surface-2 px-2 text-sec text-ink outline-none focus:border-live focus:ring-1 focus:ring-live"
        value={selected ?? ""}
        onChange={(event) => onSelect(event.target.value || null)}
        aria-label="Board auswählen"
      >
        {currentProject ? (
          <option value="">{currentProject.project_name ?? currentProject.name} · aktuell</option>
        ) : null}
        {available.filter((board) => board.slug !== current).map((board) => (
          <option key={board.slug} value={board.slug}>{board.project_name || board.name || board.slug}</option>
        ))}
      </select>
      {selected ? <span className="hidden text-micro text-ink-3 tab:inline">nur lesen</span> : null}
    </label>
  );
}
