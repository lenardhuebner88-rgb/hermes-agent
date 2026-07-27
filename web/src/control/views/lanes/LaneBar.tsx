import { useState } from "react";
import { Plus } from "lucide-react";
import { cn } from "@/lib/utils";
import { DrawerShell } from "../../components/leitstand";
import { LANE_PRESETS, type LanePresetId } from "./fit";
import type { Lane } from "./api";
import { t } from "./strings";

// LaneBar — one card per lane preset. Two deliberately separate gestures
// (Editieren ≠ Aktivieren): tapping the card SELECTS it for viewing/editing
// (neutral „Ansicht" marker), the explicit „Aktivieren" button (with inline
// confirm) flips the live lane (bronze LED + inset bronze edge = currently
// live, DESIGN.md rule 1). Non-builtin lanes carry a manage affordance
// (rename / duplicate / delete in a DrawerShell). The trailing ghost card
// creates a new lane — empty or from a Kompass preset. Horizontal-scroll with
// scroll-snap on every width; from the 3-zone cut the rail stacks vertically
// (lanes.css).

/** Short display name of an override model id ("openai/gpt-5.6-sol" → "gpt-5.6-sol"). */
function shortModel(id: string): string {
  const tail = id.split("/").pop() ?? id;
  return tail.length > 24 ? `${tail.slice(0, 23)}…` : tail;
}

/** "gpt-5.6-sol · qwen3.8 · +2" — distinct override models wired in this lane. */
function laneModelSummary(lane: Lane): string | null {
  const models = Array.from(
    new Set(
      Object.values(lane.profiles)
        .map((entry) => entry.model)
        .filter((m): m is string => Boolean(m)),
    ),
  );
  if (models.length === 0) return null;
  const shown = models.slice(0, 2).map(shortModel);
  const rest = models.length - shown.length;
  return rest > 0 ? `${shown.join(" · ")} · +${rest}` : shown.join(" · ");
}

function savedTime(lane: Lane): string | null {
  if (!lane.updated_at) return null;
  return new Date(lane.updated_at * 1000).toLocaleTimeString("de-DE", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function ManageDrawer({
  lane,
  busy,
  onClose,
  onRename,
  onDuplicate,
  onDelete,
}: {
  lane: Lane;
  busy: boolean;
  onClose: () => void;
  onRename: (laneId: string, name: string) => void;
  onDuplicate: (laneId: string) => void;
  onDelete: (laneId: string) => void;
}) {
  const [name, setName] = useState(lane.name);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  return (
    <DrawerShell
      eyebrow={lane.name}
      title={t.manageTitle}
      onClose={onClose}
      ariaLabel={`${t.manageTitle}: ${lane.name}`}
    >
      <div className="space-y-4">
        <div>
          <label htmlFor="lp-rename" className="mb-1 block text-micro uppercase tracking-wide text-ink-3">
            {t.rename}
          </label>
          <div className="flex gap-2">
            <input
              id="lp-rename"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="min-h-11 min-w-0 flex-1 rounded-card border border-line bg-surface-2 px-2 text-sec text-ink focus:border-live focus:outline-none"
            />
            <button
              type="button"
              disabled={busy || name.trim() === "" || name.trim() === lane.name}
              onClick={() => {
                onRename(lane.id, name.trim());
                onClose();
              }}
              className="min-h-11 shrink-0 rounded-card border border-live bg-live/15 px-3 text-micro font-medium text-bronze-hi disabled:opacity-40"
            >
              {t.renameSave}
            </button>
          </div>
        </div>

        <button
          type="button"
          disabled={busy}
          onClick={() => {
            onDuplicate(lane.id);
            onClose();
          }}
          className="min-h-11 w-full rounded-card border border-line px-3 text-sec text-ink-2 transition-colors duration-150 hover:border-live hover:text-live disabled:opacity-40"
        >
          {t.duplicate}
        </button>

        <div className="border-t border-line-soft pt-3">
          {lane.builtin ? (
            <p className="text-micro text-ink-3">{t.builtinNoDelete}</p>
          ) : confirmingDelete ? (
            <div className="space-y-2">
              <p className="text-micro text-status-alert">{t.deleteConfirm(lane.name)}</p>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    onDelete(lane.id);
                    onClose();
                  }}
                  className="min-h-11 flex-1 rounded-card border border-status-alert bg-status-alert/15 px-2 text-micro font-medium text-status-alert disabled:opacity-40"
                >
                  {t.deleteYes}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setConfirmingDelete(false)}
                  className="min-h-11 flex-1 rounded-card border border-line px-2 text-micro text-ink-2 disabled:opacity-40"
                >
                  {t.confirmNo}
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() => setConfirmingDelete(true)}
              className="min-h-11 w-full rounded-card border border-line px-3 text-sec text-ink-2 transition-colors duration-150 hover:border-status-alert hover:text-status-alert disabled:opacity-40"
            >
              {t.deleteLane}
            </button>
          )}
        </div>
      </div>
    </DrawerShell>
  );
}

export function LaneBar({
  lanes,
  activeId,
  selectedId,
  profileCount,
  busy,
  onSelect,
  onActivate,
  onCreate,
  onRename,
  onDuplicate,
  onDelete,
}: {
  lanes: Lane[];
  activeId: string | null;
  /** Lane currently opened in the matrix (view/edit target — not necessarily live). */
  selectedId: string | null;
  /** Catalog profile count (the matrix rows) — NOT the lane's override count. */
  profileCount: number;
  busy: boolean;
  onSelect: (laneId: string) => void;
  onActivate: (laneId: string) => void;
  onCreate: (name: string, preset: LanePresetId | null) => void;
  onRename: (laneId: string, name: string) => void;
  onDuplicate: (laneId: string) => void;
  onDelete: (laneId: string) => void;
}) {
  const [pendingActivate, setPendingActivate] = useState<string | null>(null);
  const [managing, setManaging] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [preset, setPreset] = useState<LanePresetId | null>(null);

  const manageLane = managing ? lanes.find((l) => l.id === managing) ?? null : null;

  return (
    <div className="lane-scroll flex gap-2 overflow-x-auto pb-1">
      {lanes.map((lane) => {
        const active = lane.active || lane.id === activeId;
        const selected = lane.id === selectedId;
        const overrideCount = Object.keys(lane.profiles).length;
        const confirming = pendingActivate === lane.id;
        const summary = laneModelSummary(lane);
        const saved = savedTime(lane);
        return (
          <div
            key={lane.id}
            className={cn(
              "lane-card min-w-[11rem] shrink-0 snap-start rounded-card border border-line bg-surface-1 p-3",
              active && "lp-active border-live/40",
              selected && !active && "border-ink-3",
            )}
          >
            <button
              type="button"
              onClick={() => onSelect(lane.id)}
              aria-label={`${lane.name} ansehen`}
              className="flex w-full items-center gap-2 text-left"
            >
              {active ? (
                <span aria-hidden className="size-2 shrink-0 rounded-full bg-live" />
              ) : (
                <span aria-hidden className="size-2 shrink-0 rounded-full bg-ink-3/50" />
              )}
              <span className="min-w-0 flex-1 truncate text-sec font-semibold text-ink" title={lane.name}>
                {lane.name}
              </span>
            </button>
            <div className="mt-0.5 flex items-center gap-2 text-micro uppercase tracking-wide">
              {active ? <span className="text-bronze-hi">{t.aktiv}</span> : null}
              {selected && !active ? <span className="text-ink-2">{t.ansicht}</span> : null}
              {!lane.builtin ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setManaging(lane.id)}
                  aria-label={`${t.manage}: ${lane.name}`}
                  className="text-ink-3 underline-offset-2 transition-colors duration-150 hover:text-live hover:underline disabled:opacity-40"
                >
                  {t.manage}
                </button>
              ) : null}
            </div>

            {confirming ? (
              <div className="mt-2 space-y-2">
                <p className="text-micro text-ink-2">{t.activateConfirm}</p>
                <div className="flex gap-1.5">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      onActivate(lane.id);
                      setPendingActivate(null);
                    }}
                    className="min-h-11 flex-1 rounded-card border border-live bg-live/15 px-2 text-micro font-medium text-bronze-hi disabled:opacity-40"
                  >
                    {t.confirmYes}
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setPendingActivate(null)}
                    className="min-h-11 flex-1 rounded-card border border-line px-2 text-micro text-ink-2 disabled:opacity-40"
                  >
                    {t.confirmNo}
                  </button>
                </div>
              </div>
            ) : (
              <>
                {summary ? (
                  <div className="mt-1 truncate font-data text-micro text-ink-2" title={summary}>
                    {summary}
                  </div>
                ) : null}
                <div className="mt-1 font-data text-micro tabular-nums text-ink-3">
                  {lane.builtin ? t.builtin : t.eigeneLane} · {t.overrides(overrideCount)} ·{" "}
                  {t.profileCount(profileCount)}
                </div>
                {saved ? (
                  <div className="mt-0.5 font-data text-micro tabular-nums text-ink-3">{t.zuletzt(saved)}</div>
                ) : null}
                {!active ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setPendingActivate(lane.id)}
                    className="mt-2 min-h-11 w-full rounded-card border border-line px-2 text-micro text-ink-2 transition-colors duration-150 hover:border-live hover:text-live disabled:opacity-40"
                  >
                    {t.activate}
                  </button>
                ) : null}
              </>
            )}
          </div>
        );
      })}

      {/* ghost card: neue Lane (leer oder aus Kompass-Preset) */}
      <div className="min-w-[11rem] shrink-0 snap-start rounded-card border border-dashed border-line p-3">
        {creating ? (
          <div className="space-y-2">
            <input
              type="text"
              value={newName}
              aria-label={t.neueLanePlaceholder}
              placeholder={t.neueLanePlaceholder}
              autoFocus
              onChange={(e) => setNewName(e.target.value)}
              className="min-h-11 w-full rounded-card border border-line bg-surface-2 px-2 text-sec text-ink placeholder:text-ink-3 focus:border-live focus:outline-none"
            />
            <div>
              <span className="mb-1 block text-micro uppercase tracking-wide text-ink-3">{t.presetLabel}</span>
              <div className="flex flex-wrap gap-1" role="radiogroup" aria-label={t.presetLabel} title={t.presetHint}>
                <button
                  type="button"
                  role="radio"
                  aria-checked={preset === null}
                  onClick={() => setPreset(null)}
                  className={cn(
                    "min-h-11 rounded-card border px-2 text-micro",
                    preset === null ? "border-live bg-live/15 text-bronze-hi" : "border-line text-ink-2 hover:border-live hover:text-live",
                  )}
                >
                  {t.presetLeer}
                </button>
                {LANE_PRESETS.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    role="radio"
                    aria-checked={preset === p.id}
                    title={p.blurb}
                    onClick={() => setPreset(p.id)}
                    className={cn(
                      "min-h-11 rounded-card border px-2 text-micro",
                      preset === p.id ? "border-live bg-live/15 text-bronze-hi" : "border-line text-ink-2 hover:border-live hover:text-live",
                    )}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex gap-1.5">
              <button
                type="button"
                disabled={busy || newName.trim() === ""}
                onClick={() => {
                  onCreate(newName.trim(), preset);
                  setNewName("");
                  setPreset(null);
                  setCreating(false);
                }}
                className="min-h-11 flex-1 rounded-card border border-live bg-live/15 px-2 text-micro font-medium text-bronze-hi disabled:opacity-40"
              >
                {t.create}
              </button>
              <button
                type="button"
                onClick={() => {
                  setCreating(false);
                  setNewName("");
                  setPreset(null);
                }}
                className="min-h-11 rounded-card border border-line px-2 text-micro text-ink-2"
              >
                {t.confirmNo}
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => setCreating(true)}
            className="flex min-h-16 w-full flex-col items-center justify-center gap-1 text-micro text-ink-3 transition-colors duration-150 hover:text-live disabled:opacity-40"
          >
            <Plus className="h-4 w-4" />
            {t.neueLane}
          </button>
        )}
      </div>

      {manageLane ? (
        <ManageDrawer
          lane={manageLane}
          busy={busy}
          onClose={() => setManaging(null)}
          onRename={onRename}
          onDuplicate={onDuplicate}
          onDelete={onDelete}
        />
      ) : null}
    </div>
  );
}
