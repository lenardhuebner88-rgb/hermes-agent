/**
 * B2: Pure label builder for ChainSelector options.
 * Isolated to its own module so the component file exports only React
 * components (react-refresh/only-export-components rule).
 */
import { taskStatusLabel } from "../../lib/tones";
import type { ChainModel } from "../../lib/fleet";
import type { BoardTask } from "../../lib/types";

/**
 * Builds a single ` · `-separated option string for a chain.
 * Format: `<title> · <mid> · <N> Tasks`
 * where `mid` is "N läuft" | "N blockiert" | localised status label.
 */
export function buildChainOptionLabel(chain: ChainModel<BoardTask>): string {
  const title = chain.root?.title ?? chain.rootId;
  const status = chain.root?.status ?? "todo";

  let mid: string;
  if (chain.runningCount > 0) {
    mid = `${chain.runningCount} läuft`;
  } else if (chain.blockedCount > 0) {
    mid = `${chain.blockedCount} blockiert`;
  } else {
    // B3: translate raw status via taskStatusLabel
    mid = taskStatusLabel[status] ?? status;
  }

  return `${title} · ${mid} · ${chain.total} Tasks`;
}
