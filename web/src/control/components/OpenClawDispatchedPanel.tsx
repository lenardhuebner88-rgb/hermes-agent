import { ListChecks } from "lucide-react";
import { fmtAge, nowSec } from "../lib/derive";
import { de } from "../i18n/de";
import type { OpenClawDispatchedResponse, OpenClawDispatchedTask } from "../lib/schemas";
import type { ToneName } from "../lib/types";
import { StatusPill } from "./atoms";

interface Props {
  data: OpenClawDispatchedResponse | null;
  error?: string | null;
  now?: number;
}

function statusTone(status: string): ToneName {
  switch (status) {
    case "done":
      return "emerald";
    case "running":
      return "cyan";
    case "blocked":
      return "red";
    case "ready":
    case "todo":
    case "triage":
      return "amber";
    default:
      return "zinc";
  }
}

export function OpenClawDispatchedPanel({ data, error = null, now = nowSec() }: Props) {
  // Stale-retain: hold the last good list when a poll fails (data stays from the
  // previous successful render); only surface the notice, never blank the panel.
  const tasks: OpenClawDispatchedTask[] = data?.tasks ?? [];
  const notice = error || data?.stale;

  return (
    <section className="hc-card p-4">
      <div className="flex items-center gap-3">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-lg border border-[var(--hc-border)] bg-white/[.03] text-[var(--hc-accent-text)]">
          <ListChecks className="h-5 w-5" />
        </div>
        <div>
          <p className="hc-eyebrow">{de.openclaw.dispatchedEyebrow}</p>
          <h3 className="mt-1 text-base font-semibold text-white">{de.openclaw.dispatchedTitle}</h3>
        </div>
      </div>

      {notice ? <p className="mt-3 break-words text-sm text-amber-100">{de.openclaw.dispatchedStale}: {notice}</p> : null}

      {tasks.length === 0 ? (
        <p className="mt-3 text-sm hc-soft">{de.openclaw.dispatchedEmpty}</p>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="text-left hc-soft">
                <th className="px-2 py-1 font-medium">{de.openclaw.dispatchedColTask}</th>
                <th className="px-2 py-1 font-medium">{de.openclaw.dispatchedColAgent}</th>
                <th className="px-2 py-1 font-medium">{de.openclaw.dispatchedColStatus}</th>
                <th className="px-2 py-1 font-medium">{de.openclaw.dispatchedColMc}</th>
                <th className="px-2 py-1 font-medium">{de.openclaw.dispatchedColResult}</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <tr key={task.id} data-dispatched-row className="border-t border-[var(--hc-border)] align-top">
                  <td className="px-2 py-2">
                    <div className="font-medium text-white">{task.title}</div>
                    <div className="text-xs hc-soft">{task.updated_at > 0 ? `vor ${fmtAge(task.updated_at, now)}` : task.id}</div>
                  </td>
                  <td className="px-2 py-2 text-zinc-200">{task.agent ?? "—"}</td>
                  <td className="px-2 py-2">
                    <StatusPill tone={statusTone(task.status)} label={task.poll_state ?? task.status} />
                  </td>
                  <td className="px-2 py-2 font-mono text-xs text-zinc-300">{task.mc_task_id ?? "—"}</td>
                  <td className="px-2 py-2 text-zinc-200">
                    <span className="block max-w-[28rem] whitespace-pre-wrap break-words">
                      {task.result_summary ?? de.openclaw.dispatchedNoResult}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
