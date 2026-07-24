import { useState } from "react";
import { Plus } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Lane } from "./api";
import { t } from "./strings";

// LaneBar — one card per lane preset. The active lane carries the bronze
// identity treatment (surface-2 + inset bronze edge via `.lp-active`, bronze
// LED + „Aktiv" eyebrow — bronze = currently-live, DESIGN.md rule 1). Clicking
// an inactive lane asks for inline confirmation before activating (no
// window.confirm). Trailing ghost card creates a new lane from the current
// matrix staging. Horizontal-scroll on every width (pills on mobile, Phase D).

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

export function LaneBar({
  lanes,
  activeId,
  profileCount,
  busy,
  onActivate,
  onCreate,
}: {
  lanes: Lane[];
  activeId: string | null;
  /** Catalog profile count (the matrix rows) — NOT the lane's override count. */
  profileCount: number;
  busy: boolean;
  onActivate: (laneId: string) => void;
  onCreate: (name: string) => void;
}) {
  const [pendingActivate, setPendingActivate] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");

  return (
    <div className="lane-scroll flex gap-2 overflow-x-auto pb-1">
      {lanes.map((lane) => {
        const active = lane.active || lane.id === activeId;
        const overrideCount = Object.keys(lane.profiles).length;
        const confirming = pendingActivate === lane.id;
        const summary = laneModelSummary(lane);
        const saved = savedTime(lane);
        return (
          <div
            key={lane.id}
            className={cn(
              "lane-card min-w-[11rem] shrink-0 rounded-card border border-line bg-surface-1 p-3",
              active && "lp-active border-live/40",
            )}
          >
            <div className="flex items-center gap-2">
              {active ? (
                <span aria-hidden className="size-2 shrink-0 rounded-full bg-live" />
              ) : (
                <span aria-hidden className="size-2 shrink-0 rounded-full bg-ink-3/50" />
              )}
              <span className="min-w-0 flex-1 truncate text-sec font-semibold text-ink" title={lane.name}>
                {lane.name}
              </span>
            </div>
            {active ? <div className="mt-0.5 text-micro uppercase tracking-wide text-bronze-hi">{t.aktiv}</div> : null}

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

      {/* ghost card: neue Lane */}
      <div className="min-w-[11rem] shrink-0 rounded-card border border-dashed border-line p-3">
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
            <div className="flex gap-1.5">
              <button
                type="button"
                disabled={busy || newName.trim() === ""}
                onClick={() => {
                  onCreate(newName.trim());
                  setNewName("");
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
    </div>
  );
}
