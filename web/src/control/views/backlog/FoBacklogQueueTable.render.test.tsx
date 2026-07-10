import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { BacklogItem } from "../../lib/schemas";
import { FoBacklogQueueSkeleton, FoBacklogQueueTable } from "./FoBacklogQueueTable";

const LEGACY_CLASSES = [
  "cyan-",
  "emerald-",
  "sky-",
  "teal-",
  "zinc-",
  "slate-",
  "red-",
  "amber-",
  "hc-mono",
  "hc-dim",
  "hc-soft",
  "--hc-border",
  "--hc-accent",
  "text-white",
  "bg-white",
  "border-white",
] as const;

function item(id: string, overrides: Partial<BacklogItem> = {}): BacklogItem {
  return {
    id,
    title: `Task ${id}`,
    status: "next",
    owner: "claude",
    risk: "low",
    area: "lists",
    updated: "2026-07-10",
    lane: null,
    result: null,
    stale: false,
    excerpt: "Kurze Beschreibung",
    source_path: `backlog/items/${id}-task.md`,
    ...overrides,
  };
}

describe("FoBacklogQueueTable sheet-A render branches", () => {
  it("keeps loaded, empty, loading, and action-error output free of removed legacy classes", () => {
    const props = { nowSec: 1_783_700_000, nextTaskId: "0001", onOpen: () => undefined };
    const loaded = renderToStaticMarkup(
      <FoBacklogQueueTable items={[item("0001", { risk: "high", missing_acceptance: true })]} {...props} onCommission={() => undefined} />,
    );
    const empty = renderToStaticMarkup(<FoBacklogQueueTable items={[]} {...props} />);
    const error = renderToStaticMarkup(
      <FoBacklogQueueTable items={[item("0002")]} {...props} onCommission={() => undefined} commissionState={{ "0002": "error" }} />,
    );
    const loading = renderToStaticMarkup(<FoBacklogQueueSkeleton />);
    const html = `${loaded}${empty}${error}${loading}`;

    for (const legacy of LEGACY_CLASSES) expect(html).not.toContain(legacy);
    expect(loaded).toContain("border-status-alert/30");
    expect(loaded).toContain("bg-status-alert");
    expect(error).toContain("border-live/40");
    expect(loaded).toContain("h-12");
    expect(error).toContain("min-h-12");
    expect(error).toContain("nochmal");
  });
});
